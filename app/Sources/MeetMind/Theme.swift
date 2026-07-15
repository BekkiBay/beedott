// Theme.swift — дизайн-токены из design_handoff_meetmind/README.md.
// Цвета — системная палитра Apple (семантические где есть), размеры/радиусы —
// по макету. Dark/light выбирается через @Environment(\.colorScheme).

import SwiftUI

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255)
    }
}

enum Theme {
    static let recordButton = Color(hex: 0xE5372D)
    static let recordButtonHover = Color(hex: 0xD22F26)
    static let claudeSpark = Color(hex: 0xD97757)

    static func accent(_ scheme: ColorScheme) -> Color {
        Color(hex: scheme == .dark ? 0x0A84FF : 0x007AFF)
    }
    static func recordingRed(_ scheme: ColorScheme) -> Color {
        Color(hex: scheme == .dark ? 0xFF453A : 0xFF3B30)
    }
    static func green(_ scheme: ColorScheme) -> Color {
        Color(hex: scheme == .dark ? 0x30D158 : 0x34C759)
    }
    static func orange(_ scheme: ColorScheme) -> Color {
        Color(hex: scheme == .dark ? 0xFF9F0A : 0xFF9500)
    }
    static func yellow(_ scheme: ColorScheme) -> Color {
        Color(hex: scheme == .dark ? 0xFFD60A : 0xFFCC00)
    }

    static func textSecondary(_ scheme: ColorScheme) -> Color {
        scheme == .dark
            ? Color(.sRGB, red: 235 / 255, green: 235 / 255, blue: 245 / 255).opacity(0.6)
            : Color(.sRGB, red: 60 / 255, green: 60 / 255, blue: 67 / 255).opacity(0.6)
    }
    static func textTertiary(_ scheme: ColorScheme) -> Color {
        scheme == .dark
            ? Color(.sRGB, red: 235 / 255, green: 235 / 255, blue: 245 / 255).opacity(0.45)
            : Color(.sRGB, red: 60 / 255, green: 60 / 255, blue: 67 / 255).opacity(0.5)
    }
    static func rowFill(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color.white.opacity(0.06) : Color.black.opacity(0.045)
    }
    static func rowHover(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color.white.opacity(0.08) : Color.black.opacity(0.05)
    }
    static func chipFill(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color.white.opacity(0.08) : Color.black.opacity(0.06)
    }
    static func divider(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color.white.opacity(0.09) : Color.black.opacity(0.08)
    }
    static func levelTrack(_ scheme: ColorScheme) -> Color {
        scheme == .dark ? Color.white.opacity(0.12) : Color.black.opacity(0.1)
    }

    /// Цвета спикеров транскрипта (экран 06); dark-варианты светлее.
    static func speaker(_ index: Int, _ scheme: ColorScheme) -> Color {
        let dark: [UInt32] = [0x5FA8FF, 0xB88CE0, 0xE0916A, 0x63C583]
        let light: [UInt32] = [0x0B63CE, 0x7A3FB8, 0xB04A17, 0x137A3E]
        let palette = scheme == .dark ? dark : light
        return Color(hex: palette[index % palette.count])
    }
}
