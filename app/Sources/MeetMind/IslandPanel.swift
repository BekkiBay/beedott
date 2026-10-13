// IslandPanel.swift — окно островка у чёлки и его контроллер (дизайн:
// docs/superpowers/specs/2026-10-09-notch-island-design.md).
//
// Окно фиксированного размера (IslandLayout.panelFrame), прозрачное: видна
// только чёрная форма, которая анимируется внутри. Мышь проходит сквозь окно
// везде, кроме видимой формы (ignoresMouseEvents переключается по положению
// курсора). Окно не активирует MeetMind — клик не уводит фокус из созвона.
//
// Контроллер:
//  • раз в полсекунды (AppModel.uiTick) решает, что показать: запись,
//    напоминание о встрече (UpcomingMeetingPicker) или ничего;
//  • 20 раз в секунду следит за мышью: раскрытие/сворачивание с задержкой
//    (HoverTracker) — без дёрганья;
//  • экран выбирается в момент появления (тот, где курсор) и дальше не
//    меняется — островок не прыгает между мониторами;
//  • во время демонстрации экрана прячется: на macOS 15+ флаг «не показывать
//    окно в захвате» (sharingType) захват экрана игнорирует.

import AppKit
import SwiftUI

final class IslandPanel: NSPanel {
    init() {
        super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        level = NSWindow.Level(rawValue: NSWindow.Level.mainMenu.rawValue + 3)
        collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        backgroundColor = .clear
        isOpaque = false
        hasShadow = false
        hidesOnDeactivate = false
        isMovable = false
        ignoresMouseEvents = true
        sharingType = .none  // старые способы захвата его честно пропускают
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// Хостинг, который принимает первый клик без активации окна — иначе кнопки
/// «Записать» / «Стоп» срабатывали бы только со второго нажатия.
final class IslandHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// Идёт ли сейчас захват экрана (демонстрация в Meet/Zoom, запись экрана).
/// Публичного API нет — приватная функция WindowServer, через dlsym: если её
/// не окажется, считаем, что захвата нет.
enum ScreenCaptureDetector {
    private typealias Fn = @convention(c) () -> Bool
    private static let fn: Fn? = {
        for path in ["/System/Library/PrivateFrameworks/SkyLight.framework/SkyLight",
                     "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"] {
            guard let handle = dlopen(path, RTLD_NOW) else { continue }
            for name in ["SLSIsScreenWatcherPresent", "CGSIsScreenWatcherPresent"] {
                if let sym = dlsym(handle, name) { return unsafeBitCast(sym, to: Fn.self) }
            }
        }
        return nil
    }()

    static var isCapturing: Bool { fn?() ?? false }
}

@MainActor
final class IslandController {
    private let panel = IslandPanel()
    private let store: IslandStore
    private var picker = UpcomingMeetingPicker()
    private var hover = HoverTracker()
    private var hoverTimer: Timer?
    private var shownEvent: CalendarEventInfo?
    private var layout: IslandLayout?
    private var wasCapturing = false
    private weak var model: AppModel?
    /// Демо для показа без настоящей встречи:
    /// `defaults write com.jvmshid.meetmind islandDemo -bool YES` → напоминание
    /// о «Демо-встрече» (флаг сбрасывается сам).
    private var demoEvent: CalendarEventInfo?

    init(model: AppModel) {
        self.model = model
        store = IslandStore(state: IslandViewState(mode: .hidden, notchWidth: 185, barHeight: 32))
        let hosting = IslandHostingView(rootView: IslandContainer(
            store: store,
            onRecord: { [weak self] in self?.record() },
            onDismiss: { [weak self] in self?.dismissUpcoming() },
            onStop: { [weak self] in self?.model?.stopRecording() },
            onToggleMute: { [weak self] in self?.model?.toggleMute() }))
        panel.contentView = hosting
        let timer = Timer(timeInterval: 0.05, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.trackMouse() }
        }
        RunLoop.main.add(timer, forMode: .common)
        hoverTimer = timer
    }

    /// Вызывается из AppModel.uiTick (0,5 с): что показать.
    func tick() {
        guard let model else { return }
        if UserDefaults.standard.bool(forKey: "islandDemo") {
            UserDefaults.standard.removeObject(forKey: "islandDemo")
            demoEvent = CalendarEventInfo(
                id: "demo-\(UUID().uuidString)", title: "Демо-встреча",
                start: Date().addingTimeInterval(55), end: Date().addingTimeInterval(30 * 60),
                meetCode: nil)
        }

        let capturing = ScreenCaptureDetector.isCapturing
        if capturing != wasCapturing {
            NSLog("MeetMind: захват экрана %@ — островок %@",
                  capturing ? "начался" : "закончился", capturing ? "спрятан" : "снова виден")
            wasCapturing = capturing
        }

        let recording = model.recordingSessionId != nil
        let events = model.upcomingEvents + (demoEvent.map { [$0] } ?? [])
        let upcoming = picker.pick(events, now: Date(), isRecording: recording)

        let mode: IslandViewState.Mode
        if !model.islandEnabled || capturing {
            mode = .hidden
        } else if recording {
            mode = .recording(title: model.recordingTitle ?? "Запись встречи",
                              elapsed: model.elapsedText, mic: model.micLevel,
                              system: model.systemLevel, expanded: hover.expanded,
                              muted: model.micMuted)
        } else if let event = upcoming {
            mode = .upcoming(title: event.title, startsText: Self.startsText(event.start))
        } else {
            mode = .hidden
        }
        shownEvent = (mode != .hidden && !recording) ? upcoming : nil
        apply(mode)
    }

    // MARK: - показ

    private func apply(_ mode: IslandViewState.Mode) {
        // Экран выбирается в момент появления и дальше не меняется.
        if store.state.mode == .hidden && mode != .hidden || layout == nil {
            let screen = Self.screenUnderMouse()
            let newLayout = Self.layout(for: screen)
            layout = newLayout
            panel.setFrame(newLayout.panelFrame(), display: false)
        }
        guard let layout else { return }
        if mode == .hidden { hover.reset() }

        let state = IslandViewState(mode: mode, notchWidth: layout.notchWidth,
                                    barHeight: layout.barHeight)
        if store.state != state { store.state = state }
        if !panel.isVisible { panel.orderFrontRegardless() }
    }

    /// 20 раз в секунду: наведение (с задержкой) и проход кликов сквозь окно.
    private func trackMouse() {
        guard let layout else { return }
        let state = store.state
        guard state.mode != .hidden else {
            panel.ignoresMouseEvents = true
            return
        }
        let body = state.bodySize
        let inside = layout.hitRect(bodyWidth: body.width, bodyHeight: body.height,
                                    earWidth: state.earWidth)
            .contains(NSEvent.mouseLocation)
        panel.ignoresMouseEvents = !inside

        guard case .recording = state.mode else { return }
        let wasExpanded = hover.expanded
        if hover.step(inside: inside, now: Date()) != wasExpanded { tick() }
    }

    // MARK: - действия

    private func record() {
        guard let event = shownEvent else { return }
        picker.dismiss(event.id)
        model?.recordFromIsland(meetCode: event.meetCode)
    }

    private func dismissUpcoming() {
        if let event = shownEvent { picker.dismiss(event.id) }
        shownEvent = nil
        tick()
    }

    // MARK: - экран и текст

    private static func screenUnderMouse() -> NSScreen {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
            ?? NSScreen.main ?? NSScreen.screens[0]
    }

    private static func layout(for screen: NSScreen) -> IslandLayout {
        let frame = screen.frame
        let left = screen.auxiliaryTopLeftArea
        let right = screen.auxiliaryTopRightArea
        let menuBar = frame.maxY - screen.visibleFrame.maxY
        return IslandLayout(
            screen: frame,
            notchLeftMaxX: left.map { frame.minX + $0.width },
            notchRightMinX: right.map { frame.maxX - $0.width },
            topInset: screen.safeAreaInsets.top,
            menuBarHeight: menuBar > 0 ? menuBar : 24)
    }

    /// «1 мин» до начала, «сейчас» в момент начала, «идёт N мин» после.
    static func startsText(_ start: Date, now: Date = Date()) -> String {
        let seconds = start.timeIntervalSince(now)
        if seconds > 0 { return "\(max(1, Int(ceil(seconds / 60)))) мин" }
        let since = Int(-seconds / 60)
        return since < 1 ? "сейчас" : "идёт \(since) мин"
    }
}
