// Tools/AppIcon/main.swift — иконка приложения MeetMind, нарисованная кодом (SwiftUI).
//
//   app/Tools/app_icon.sh                      — собрать app/Resources/AppIcon.icns
//   <бинарь> sheet <папка>                     — лист сравнения всех вариантов (PNG)
//   <бинарь> iconset <вариант> <папка.iconset> — PNG всех размеров для iconutil
//
// Всё рисуется в сетке 1024×1024 по шаблону иконок macOS: «сквиркл» 824×824 по
// центру, скругление 185, мягкая тень в поле.

import AppKit
import SwiftUI

enum IconStyle: String, CaseIterable {
    /// Звук превращается в заметки: тёплая волна слева, строки текста справа.
    case waveNotes
    /// Островок у чёлки: чёрная пилюля с точкой записи и волной.
    case island
    /// Классический диктофон: белая волна на коралловом фоне.
    case redRecorder
    /// Островок, точка записи с буквой B внутри.
    case islandB
    /// Островок сверху, крупная B снизу.
    case islandBigB
    /// Островок: B вместо точки, точка записи — «индексом» у буквы.
    case islandLetter

    var title: String {
        switch self {
        case .waveNotes: return "1 · Волна → заметки"
        case .island: return "2 · Островок"
        case .redRecorder: return "3 · Красный диктофон"
        case .islandB: return "2a · B в точке записи"
        case .islandBigB: return "2b · Островок и большая B"
        case .islandLetter: return "2c · B в островке"
        }
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
    }
}

struct AppIcon: View {
    let style: IconStyle
    let size: CGFloat

    private var s: CGFloat { size / 1024 }

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 185 * s, style: .continuous)
                .fill(background)
                .overlay(  // лёгкий блик сверху, как у системных иконок
                    RoundedRectangle(cornerRadius: 185 * s, style: .continuous)
                        .fill(LinearGradient(colors: [.white.opacity(0.10), .clear],
                                             startPoint: .top, endPoint: .center)))
                .frame(width: 824 * s, height: 824 * s)
                .shadow(color: .black.opacity(0.28), radius: 14 * s, y: 9 * s)
            glyph
        }
        .frame(width: size, height: size)
    }

    private var background: LinearGradient {
        switch style {
        case .waveNotes:
            return LinearGradient(colors: [Color(hex: 0x262B4F), Color(hex: 0x0B0D1A)],
                                  startPoint: .top, endPoint: .bottom)
        case .island, .islandB, .islandBigB, .islandLetter:
            return LinearGradient(colors: [Color(hex: 0x5B86FF), Color(hex: 0x9B5BFF)],
                                  startPoint: .topLeading, endPoint: .bottomTrailing)
        case .redRecorder:
            return LinearGradient(colors: [Color(hex: 0xFF7A5C), Color(hex: 0xE0245E)],
                                  startPoint: .topLeading, endPoint: .bottomTrailing)
        }
    }

    private static let coral = LinearGradient(colors: [Color(hex: 0xFF9A5C), Color(hex: 0xFF3D6E)],
                                              startPoint: .top, endPoint: .bottom)
    private static let recRed = Color(hex: 0xFF453A)

    @ViewBuilder private var glyph: some View {
        switch style {
        case .waveNotes:
            HStack(alignment: .center, spacing: 70 * s) {
                bars([200, 370, 280, 160], width: 58, spacing: 30, fill: Self.coral)
                VStack(alignment: .leading, spacing: 38 * s) {
                    ForEach([250, 205, 150], id: \.self) { w in
                        Capsule().fill(Color.white)
                            .frame(width: CGFloat(w) * s, height: 54 * s)
                    }
                }
            }
        case .island:
            ZStack {
                Capsule().fill(Color.black)
                    .frame(width: 620 * s, height: 230 * s)
                    .shadow(color: .black.opacity(0.35), radius: 20 * s, y: 12 * s)
                HStack(spacing: 56 * s) {
                    Circle().fill(Self.recRed)
                        .frame(width: 92 * s, height: 92 * s)
                        .shadow(color: Self.recRed.opacity(0.9), radius: 22 * s)
                    bars([70, 130, 96, 150, 64], width: 34, spacing: 22, fill: Color.white)
                }
            }
        case .redRecorder:
            bars([150, 270, 390, 490, 390, 270, 150], width: 56, spacing: 34, fill: Color.white)
        case .islandB:
            ZStack {
                islandPill(width: 640, height: 240)
                HStack(spacing: 50 * s) {
                    ZStack {
                        Circle().fill(Self.recRed)
                            .frame(width: 150 * s, height: 150 * s)
                            .shadow(color: Self.recRed.opacity(0.9), radius: 24 * s)
                        letterB(size: 108)
                    }
                    bars([70, 130, 96, 150, 64], width: 34, spacing: 22, fill: Color.white)
                }
            }
        case .islandBigB:
            VStack(spacing: 4 * s) {
                ZStack {
                    islandPill(width: 470, height: 170)
                    HStack(spacing: 40 * s) {
                        Circle().fill(Self.recRed)
                            .frame(width: 68 * s, height: 68 * s)
                            .shadow(color: Self.recRed.opacity(0.9), radius: 16 * s)
                        bars([52, 96, 70, 110, 46], width: 26, spacing: 17, fill: Color.white)
                    }
                }
                letterB(size: 330)
                    .shadow(color: .black.opacity(0.25), radius: 16 * s, y: 10 * s)
            }
            .offset(y: 12 * s)  // строка текста ниже буквы пустая — визуальный центр
        case .islandLetter:
            ZStack {
                islandPill(width: 640, height: 240)
                HStack(spacing: 46 * s) {
                    letterB(size: 190)
                        .overlay(alignment: .topTrailing) {
                            Circle().fill(Self.recRed)
                                .frame(width: 52 * s, height: 52 * s)
                                .shadow(color: Self.recRed.opacity(0.9), radius: 12 * s)
                                .offset(x: 30 * s, y: 4 * s)
                        }
                    bars([70, 130, 96, 150, 64], width: 34, spacing: 22, fill: Color.white)
                }
            }
        }
    }

    private func islandPill(width: CGFloat, height: CGFloat) -> some View {
        Capsule().fill(Color.black)
            .frame(width: width * s, height: height * s)
            .shadow(color: .black.opacity(0.35), radius: 20 * s, y: 12 * s)
    }

    private func letterB(size: CGFloat) -> some View {
        Text("B")
            .font(.system(size: size * s, weight: .heavy, design: .rounded))
            .foregroundStyle(Color.white)
    }

    private func bars<F: ShapeStyle>(_ heights: [CGFloat], width: CGFloat, spacing: CGFloat,
                                     fill: F) -> some View {
        HStack(alignment: .center, spacing: spacing * s) {
            ForEach(Array(heights.enumerated()), id: \.offset) { _, h in
                Capsule().fill(fill).frame(width: width * s, height: h * s)
            }
        }
    }
}

/// Лист сравнения: каждый вариант крупно и в мелких размерах (как в Dock/Finder/меню).
struct Sheet: View {
    var styles: [IconStyle] = IconStyle.allCases

    var body: some View {
        HStack(alignment: .top, spacing: 48) {
            ForEach(styles, id: \.self) { style in
                VStack(spacing: 18) {
                    AppIcon(style: style, size: 320)
                    HStack(alignment: .bottom, spacing: 18) {
                        AppIcon(style: style, size: 128)
                        AppIcon(style: style, size: 64)
                        AppIcon(style: style, size: 32)
                        AppIcon(style: style, size: 16)
                    }
                    Text(style.title)
                        .font(.system(size: 22, weight: .semibold))
                        .foregroundStyle(Color(hex: 0x1D1D1F))
                }
            }
        }
        .padding(48)
        .background(Color(hex: 0xECECF1))
    }
}

@MainActor
func writePNG<V: View>(_ view: V, scale: CGFloat = 1, to url: URL) {
    let r = ImageRenderer(content: view)
    r.scale = scale
    guard let cg = r.cgImage else { fatalError("render failed: \(url.lastPathComponent)") }
    let rep = NSBitmapImageRep(cgImage: cg)
    guard let png = rep.representation(using: .png, properties: [:]) else {
        fatalError("png failed: \(url.lastPathComponent)")
    }
    try! png.write(to: url)
}

let args = CommandLine.arguments
MainActor.assumeIsolated {
    _ = NSApplication.shared
    switch args.count >= 3 ? args[1] : "" {
    case "sheet":
        let dir = URL(fileURLWithPath: args[2])
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let styles = args.count >= 4
            ? args[3].split(separator: ",").compactMap { IconStyle(rawValue: String($0)) }
            : IconStyle.allCases
        writePNG(Sheet(styles: styles), scale: 2, to: dir.appendingPathComponent("icons-sheet.png"))
        print("saved \(dir.path)/icons-sheet.png")
    case "iconset" where args.count >= 4:
        guard let style = IconStyle(rawValue: args[2]) else { fatalError("нет варианта \(args[2])") }
        let dir = URL(fileURLWithPath: args[3])
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for base in [16, 32, 128, 256, 512] {
            for mult in [1, 2] {
                let px = CGFloat(base * mult)
                let name = "icon_\(base)x\(base)\(mult == 2 ? "@2x" : "").png"
                writePNG(AppIcon(style: style, size: px), to: dir.appendingPathComponent(name))
            }
        }
        print("saved \(dir.path)")
    default:
        print("usage: AppIcon sheet <dir> | AppIcon iconset <\(IconStyle.allCases.map(\.rawValue).joined(separator: "|"))> <dir.iconset>")
        exit(2)
    }
}
