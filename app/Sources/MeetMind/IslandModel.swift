// IslandModel.swift — чистая логика островка у чёлки (дизайн:
// docs/superpowers/specs/2026-10-09-notch-island-design.md).
//
// UpcomingMeetingPicker — какую встречу из Календаря напомнить сейчас.
// IslandLayout — рамка окна островка для экрана с чёлкой и без неё.
// Без AppKit/EventKit — тесты в app/Tests.

import CoreGraphics
import Foundation

/// Событие Календаря в том виде, который нужен островку.
struct CalendarEventInfo: Equatable {
    let id: String
    let title: String
    let start: Date
    let end: Date
    /// Код Google Meet из ссылки в событии — «Записать» тогда остановится сам.
    let meetCode: String?
}

struct UpcomingMeetingPicker {
    /// За сколько до начала показать пилюлю.
    var leadTime: TimeInterval = 60
    /// Сколько ещё держать её после начала, если ничего не нажали.
    var lingerAfterStart: TimeInterval = 5 * 60

    /// Встречи, которые больше не показываем: нажали ✕ / «Записать» или во
    /// время их окна шла запись.
    private var handled = Set<String>()

    mutating func dismiss(_ id: String) { handled.insert(id) }

    mutating func pick(_ events: [CalendarEventInfo], now: Date, isRecording: Bool) -> CalendarEventInfo? {
        let inWindow = events.filter { e in
            let untilStart = e.start.timeIntervalSince(now)
            return untilStart <= leadTime && -untilStart <= lingerAfterStart
        }
        if isRecording {
            // Пишем уже — напоминать не о чем, и после стопа не всплываем.
            inWindow.forEach { handled.insert($0.id) }
            return nil
        }
        return inWindow.filter { !handled.contains($0.id) }.min { $0.start < $1.start }
    }
}

/// Ширина «ушек» — частей островка в строке меню слева и справа от чёлки
/// (правка 2026-10-12: компакт записи и напоминание — вбок, а не вниз).
enum IslandEars {
    /// Запись: точка слева, таймер «24:13» справа.
    static let compactShort: CGFloat = 56
    /// Таймер с часами «1:02:13» не влезает в короткое ушко.
    static let compactLong: CGFloat = 72
    /// Напоминание: слева «📅 название», справа «1 мин ● Записать ✕». Ушки по
    /// длине названия, но не шире потолка — длинное название обрежется.
    static let upcomingMax: CGFloat = 220

    static func compact(elapsed: String) -> CGFloat {
        elapsed.count > 5 ? compactLong : compactShort
    }

    static func upcoming(title: String, startsText: String) -> CGFloat {
        // Оценка ширины текста по числу символов (12,5 pt semibold ≈ 7,2 pt на
        // символ): точность не нужна — лишнее обрежется многоточием.
        let leading = 12 + 14 + 6 + CGFloat(title.count) * 7.2 + 8
        return min(upcomingMax, max(leading, upcomingTrailing(startsText: startsText)))
    }

    /// Правое ушко напоминания: время, «● Записать», ✕ и отступы.
    static func upcomingTrailing(startsText: String) -> CGFloat {
        8 + CGFloat(startsText.count) * 6.8 + 6 + 92 + 6 + 20 + 10
    }
}

/// Геометрия островка на конкретном экране (координаты AppKit, y вверх).
///
/// Компакт записи и напоминание — «ушки» в строке меню по бокам от чёлки.
/// Раскрытая запись — «шея» ровно ширины чёлки и карточка под строкой меню.
/// Окно — фиксированного размера (вмещает самые широкие ушки и самую большую
/// карточку); форма внутри анимируется, поэтому окно не дёргается.
struct IslandLayout {
    /// Ширина «чёлки» на экране без выреза — как у MacBook Air/Pro 14".
    static let fakeNotchWidth: CGFloat = 185
    static let minBarHeight: CGFloat = 28
    /// Самая большая карточка (раскрытая запись) — под неё размер окна.
    static let maxBodyWidth: CGFloat = 420
    static let maxBodyHeight: CGFloat = 84
    /// Запас окна по краям под вогнутые уголки у края экрана.
    static let margin: CGFloat = 12

    let screen: CGRect
    let notchWidth: CGFloat
    let barHeight: CGFloat
    let hasNotch: Bool

    /// notchLeftMaxX / notchRightMinX — края выреза в координатах экрана.
    init(screen: CGRect, notchLeftMaxX: CGFloat?, notchRightMinX: CGFloat?,
         topInset: CGFloat, menuBarHeight: CGFloat) {
        self.screen = screen
        if let l = notchLeftMaxX, let r = notchRightMinX, r > l, topInset > 0 {
            hasNotch = true
            notchWidth = r - l
            barHeight = topInset
        } else {
            hasNotch = false
            notchWidth = Self.fakeNotchWidth
            barHeight = max(menuBarHeight, Self.minBarHeight)
        }
    }

    /// Рамка окна: по центру экрана, прижата к верхнему краю.
    func panelFrame() -> CGRect {
        let width = max(Self.maxBodyWidth, notchWidth + 2 * IslandEars.upcomingMax) + 2 * Self.margin
        let height = barHeight + Self.maxBodyHeight + Self.margin
        return CGRect(x: screen.midX - width / 2, y: screen.maxY - height,
                      width: width, height: height)
    }

    /// Видимая часть островка (полоса с ушками / шея + карточка) — зона
    /// наведения мыши и кликов.
    func hitRect(bodyWidth: CGFloat, bodyHeight: CGFloat, earWidth: CGFloat = 0) -> CGRect {
        let width = max(bodyWidth, notchWidth + 2 * earWidth)
        let height = barHeight + bodyHeight
        return CGRect(x: screen.midX - width / 2, y: screen.maxY - height,
                      width: width, height: height)
    }
}

/// Наведение мыши с задержкой: раскрываем, если мышь задержалась внутри
/// ≥ openDelay, сворачиваем, если её нет внутри ≥ closeDelay. Короткие выходы и
/// мимолётные касания ничего не дёргают.
struct HoverTracker {
    var openDelay: TimeInterval = 0.15
    var closeDelay: TimeInterval = 0.35

    private(set) var expanded = false
    private var insideSince: Date?
    private var outsideSince: Date?

    mutating func step(inside: Bool, now: Date) -> Bool {
        if inside {
            outsideSince = nil
            insideSince = insideSince ?? now
            if !expanded, now.timeIntervalSince(insideSince!) >= openDelay { expanded = true }
        } else {
            insideSince = nil
            outsideSince = outsideSince ?? now
            if expanded, now.timeIntervalSince(outsideSince!) >= closeDelay { expanded = false }
        }
        return expanded
    }

    mutating func reset() {
        expanded = false
        insideSince = nil
        outsideSince = nil
    }
}
