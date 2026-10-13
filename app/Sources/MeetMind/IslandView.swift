// IslandView.swift — вид островка у чёлки. Дизайн:
// docs/superpowers/specs/2026-10-09-notch-island-design.md.
//
// Компакт записи и напоминание — «ушки» в строке меню по бокам от чёлки
// (правка 2026-10-12: вниз ничего не висит). Раскрытая запись «стекает» из
// чёлки вниз: «шея» ровно ширины чёлки и карточка с содержимым под строкой
// меню. Форма пружинно анимируется внутри окна фиксированного размера: ушки
// выдвигаются из чёлки, втягиваются в неё при раскрытии — без рывков и зазоров.
//
// Принимает простое состояние (без AppModel) — поэтому рендерится и в
// скриншоты-превью. Островок всегда чёрный с белым текстом, как Dynamic Island.

import SwiftUI

struct IslandViewState: Equatable {
    enum Mode: Equatable {
        case hidden
        case upcoming(title: String, startsText: String)
        case recording(title: String, elapsed: String, mic: Double, system: Double, expanded: Bool,
                       muted: Bool = false)
    }

    var mode: Mode
    var notchWidth: CGFloat
    var barHeight: CGFloat

    /// Размер карточки под чёлкой для режима (контроллер считает по нему зону мыши).
    var bodySize: CGSize {
        switch mode {
        case .hidden, .upcoming, .recording(_, _, _, _, false, _):
            return CGSize(width: notchWidth, height: 0)
        case .recording(_, _, _, _, true, _): return CGSize(width: 420, height: 84)
        }
    }

    /// Ширина каждого «ушка» в строке меню (0 — ушек нет).
    var earWidth: CGFloat {
        switch mode {
        case .hidden, .recording(_, _, _, _, true, _): return 0
        case .upcoming(let title, let startsText):
            return IslandEars.upcoming(title: title, startsText: startsText)
        case .recording(_, let elapsed, _, _, false, _): return IslandEars.compact(elapsed: elapsed)
        }
    }

    /// Где рисуется содержимое: полоса меню с ушками или карточка под чёлкой.
    var contentSize: CGSize {
        earWidth > 0 ? CGSize(width: notchWidth + 2 * earWidth, height: barHeight) : bodySize
    }
    var contentOffset: CGFloat { earWidth > 0 ? 0 : barHeight + contentInset }

    /// Отступ содержимого сверху карточки — под «плечи» формы.
    var contentInset: CGFloat {
        let body = bodySize
        return min(10, max(0, (body.width - notchWidth) / 2), body.height / 3) / 2
    }

    /// Смена этого ключа — анимация формы (таймер и уровни форму не дёргают).
    var shapeKey: String {
        switch mode {
        case .hidden: return "hidden"
        case .upcoming: return "upcoming-\(Int(earWidth))"
        case .recording(_, _, _, _, let expanded, _):
            return expanded ? "expanded" : "compact-\(Int(earWidth))"
        }
    }
}

/// Хранилище состояния для живого окна: SwiftUI анимирует переходы между
/// значениями, а окно не пересоздаётся.
final class IslandStore: ObservableObject {
    @Published var state: IslandViewState
    init(state: IslandViewState) { self.state = state }
}

struct IslandContainer: View {
    @ObservedObject var store: IslandStore
    var onRecord: () -> Void = {}
    var onDismiss: () -> Void = {}
    var onStop: () -> Void = {}
    var onToggleMute: () -> Void = {}

    var body: some View {
        IslandView(state: store.state, onRecord: onRecord, onDismiss: onDismiss, onStop: onStop,
                   onToggleMute: onToggleMute)
    }
}

struct IslandView: View {
    let state: IslandViewState
    var onRecord: () -> Void = {}
    var onDismiss: () -> Void = {}
    var onStop: () -> Void = {}
    var onToggleMute: () -> Void = {}

    static let red = Color(red: 1.0, green: 0.27, blue: 0.23)
    static let spring = Animation.spring(response: 0.42, dampingFraction: 0.78)

    var body: some View {
        let body = state.bodySize
        ZStack(alignment: .top) {
            IslandShape(neckWidth: state.notchWidth, neckHeight: state.barHeight,
                        bodyWidth: body.width, bodyHeight: body.height, earWidth: state.earWidth)
                .fill(Color.black)

            content
                .frame(width: state.contentSize.width, height: state.contentSize.height)
                .offset(y: state.contentOffset)
                .id(state.shapeKey)
                .transition(.opacity.animation(.easeOut(duration: 0.18).delay(0.08)))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .foregroundStyle(.white)
        .opacity(state.mode == .hidden ? 0 : 1)
        .animation(Self.spring, value: state.shapeKey)
    }

    // MARK: - содержимое карточки

    @ViewBuilder private var content: some View {
        switch state.mode {
        case .hidden:
            Color.clear
        case .upcoming(let title, let startsText):
            EarsRow(notchWidth: state.notchWidth, earWidth: state.earWidth) {
                HStack(spacing: 6) {
                    Image(systemName: "calendar")
                        .font(.system(size: 11.5, weight: .semibold))
                        .foregroundStyle(.white.opacity(0.85))
                    Text(title)
                        .font(.system(size: 12.5, weight: .semibold))
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                .padding(.leading, 12)
                .padding(.trailing, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
            } trailing: {
                HStack(spacing: 6) {
                    Text(startsText)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.white.opacity(0.6))
                        .lineLimit(1)
                        .fixedSize()
                    Button(action: onRecord) {
                        HStack(spacing: 5) {
                            Circle().fill(Color.white).frame(width: 7, height: 7)
                            Text("Записать").font(.system(size: 12, weight: .semibold))
                        }
                        .padding(.horizontal, 10)
                        .frame(height: 22)
                        .background(Capsule().fill(Self.red))
                    }
                    .buttonStyle(.plain)
                    Button(action: onDismiss) {
                        Image(systemName: "xmark")
                            .font(.system(size: 9.5, weight: .bold))
                            .foregroundStyle(.white.opacity(0.7))
                            .frame(width: 20, height: 20)
                            .background(Circle().fill(Color.white.opacity(0.14)))
                    }
                    .buttonStyle(.plain)
                }
                .padding(.trailing, 10)
                .frame(maxWidth: .infinity, alignment: .trailing)
            }
        case .recording(_, let elapsed, _, _, false, let muted):
            EarsRow(notchWidth: state.notchWidth, earWidth: state.earWidth) {
                HStack(spacing: 5) {
                    PulsingRecDot(color: Self.red)
                    if muted {
                        Image(systemName: "mic.slash.fill")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.white.opacity(0.75))
                    }
                }
            } trailing: {
                Text(elapsed)
                    .font(.system(size: 12, weight: .semibold).monospacedDigit())
                    .foregroundStyle(Self.red)
                    .lineLimit(1)
                    .fixedSize()
            }
        case .recording(let title, let elapsed, let mic, let system, true, let muted):
            VStack(spacing: 10) {
                HStack(spacing: 8) {
                    PulsingRecDot(color: Self.red)
                    Text(title)
                        .font(.system(size: 13, weight: .semibold))
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Spacer(minLength: 8)
                    Text(elapsed)
                        .font(.system(size: 12.5, weight: .semibold).monospacedDigit())
                        .fixedSize()
                    Button(action: onToggleMute) {
                        Image(systemName: muted ? "mic.slash.fill" : "mic.fill")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(muted ? Self.red : .white)
                            .frame(width: 24, height: 24)
                            .background(Circle().fill(Color.white.opacity(muted ? 0.92 : 0.16)))
                    }
                    .buttonStyle(.plain)
                    .help(muted ? "Включить микрофон (⌃⌥⌘M)" : "Не записывать микрофон (⌃⌥⌘M)")
                    Button(action: onStop) {
                        HStack(spacing: 5) {
                            RoundedRectangle(cornerRadius: 1.5)
                                .fill(Self.red).frame(width: 8, height: 8)
                            Text("Стоп").font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(.black)
                        }
                        .padding(.horizontal, 11)
                        .frame(height: 24)
                        .background(Capsule().fill(Color.white))
                    }
                    .buttonStyle(.plain)
                }
                HStack(spacing: 18) {
                    IslandLevel(icon: muted ? "mic.slash.fill" : "mic.fill",
                                label: muted ? "Микрофон выключен" : "Микрофон",
                                level: muted ? 0 : mic)
                    IslandLevel(icon: "speaker.wave.2.fill", label: "Собеседники", level: system)
                }
            }
            .padding(.horizontal, 16)
        }
    }
}

/// Чёрная форма: «шея» под чёлкой (с маленькими вогнутыми уголками у края
/// экрана, как у настоящей чёлки), по бокам — «ушки» в строке меню, под ней —
/// карточка. Если карточка шире шеи — между ними вогнутые «плечи». Ушки и
/// размер карточки анимируются.
struct IslandShape: Shape {
    var neckWidth: CGFloat
    var neckHeight: CGFloat
    var bodyWidth: CGFloat
    var bodyHeight: CGFloat
    var earWidth: CGFloat = 0
    var topFlare: CGFloat = 6

    var animatableData: AnimatablePair<CGFloat, AnimatablePair<CGFloat, CGFloat>> {
        get { AnimatablePair(earWidth, AnimatablePair(bodyWidth, bodyHeight)) }
        set {
            earWidth = newValue.first
            bodyWidth = newValue.second.first
            bodyHeight = newValue.second.second
        }
    }

    func path(in rect: CGRect) -> Path {
        let cx = rect.midX
        // Ушки расширяют шею в строке меню: полоса = чёлка + 2 × ушко.
        let nw = neckWidth + 2 * max(0, earWidth)
        let nh = neckHeight, bh = max(0, bodyHeight), f = topFlare
        let nx0 = cx - nw / 2, nx1 = cx + nw / 2
        // «Плечи» — ниже строки меню (y ≥ nh): строку меню островок не задевает.
        let shoulder = min(10, max(0, (bodyWidth - nw) / 2), bh / 3)
        let bw = shoulder < 0.5 ? nw : max(bodyWidth, nw)
        let bx0 = cx - bw / 2, bx1 = cx + bw / 2
        let bottom = nh + bh
        let top = nh + shoulder                      // верх карточки
        let rt = shoulder < 0.5 ? 0 : min(12, (bottom - top) / 2)
        let rb = shoulder < 0.5
            ? min(bh > 0 ? 14 : nh * 0.42, bottom / 2)
            : min(16, (bottom - top) / 2)

        var p = Path()
        p.move(to: CGPoint(x: nx0 - f, y: 0))
        p.addQuadCurve(to: CGPoint(x: nx0, y: f), control: CGPoint(x: nx0, y: 0))
        if shoulder >= 0.5 {
            p.addLine(to: CGPoint(x: nx0, y: nh))
            p.addQuadCurve(to: CGPoint(x: nx0 - shoulder, y: top), control: CGPoint(x: nx0, y: top))
            p.addLine(to: CGPoint(x: bx0 + rt, y: top))
            p.addQuadCurve(to: CGPoint(x: bx0, y: top + rt), control: CGPoint(x: bx0, y: top))
        }
        p.addLine(to: CGPoint(x: bx0, y: bottom - rb))
        p.addQuadCurve(to: CGPoint(x: bx0 + rb, y: bottom), control: CGPoint(x: bx0, y: bottom))
        p.addLine(to: CGPoint(x: bx1 - rb, y: bottom))
        p.addQuadCurve(to: CGPoint(x: bx1, y: bottom - rb), control: CGPoint(x: bx1, y: bottom))
        if shoulder >= 0.5 {
            p.addLine(to: CGPoint(x: bx1, y: top + rt))
            p.addQuadCurve(to: CGPoint(x: bx1 - rt, y: top), control: CGPoint(x: bx1, y: top))
            p.addLine(to: CGPoint(x: nx1 + shoulder, y: top))
            p.addQuadCurve(to: CGPoint(x: nx1, y: nh), control: CGPoint(x: nx1, y: top))
        }
        p.addLine(to: CGPoint(x: nx1, y: f))
        p.addQuadCurve(to: CGPoint(x: nx1 + f, y: 0), control: CGPoint(x: nx1, y: 0))
        p.closeSubpath()
        return p
    }
}

/// Содержимое по бокам от чёлки: слева и справа — по «ушку» в строке меню,
/// посередине — место под саму чёлку.
struct EarsRow<Leading: View, Trailing: View>: View {
    let notchWidth: CGFloat
    let earWidth: CGFloat
    @ViewBuilder let leading: Leading
    @ViewBuilder let trailing: Trailing

    var body: some View {
        HStack(spacing: 0) {
            leading.frame(width: earWidth)
            Color.clear.frame(width: notchWidth)
            trailing.frame(width: earWidth)
        }
    }
}

/// Мигающая красная точка записи.
struct PulsingRecDot: View {
    let color: Color
    @State private var dim = false

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 9, height: 9)
            .shadow(color: color.opacity(0.8), radius: dim ? 1 : 4)
            .opacity(dim ? 0.45 : 1)
            .task {
                // Мигание начинается после появления (в превью-рендере точка яркая).
                try? await Task.sleep(nanoseconds: 50_000_000)
                withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) {
                    dim = true
                }
            }
    }
}

/// Уровень источника звука: иконка, подпись и бар.
struct IslandLevel: View {
    let icon: String
    let label: String
    let level: Double

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: icon)
                .font(.system(size: 10))
                .foregroundStyle(.white.opacity(0.7))
                .frame(width: 12)
            Text(label)
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(.white.opacity(0.6))
                .fixedSize()
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.white.opacity(0.14))
                    if level > 0.005 {
                        Capsule()
                            .fill(LinearGradient(colors: [.green, level > 0.8 ? .yellow : .green],
                                                 startPoint: .leading, endPoint: .trailing))
                            .frame(width: max(3, geo.size.width * min(1, level)))
                            .animation(.linear(duration: 0.1), value: level)
                    }
                }
            }
            .frame(height: 4)
        }
    }
}
