// Tools/IslandPreview/main.swift — превью островка у чёлки в PNG (без запуска приложения).
// Запуск: app/Tools/island_preview.sh <папка>. Рисует верх экрана MacBook
// (обои, строка меню, чёлка) и островок поверх — для дизайна и скриншотов.

import AppKit
import SwiftUI

struct ScreenTop<Content: View>: View {
    let width: CGFloat
    let height: CGFloat
    let barHeight: CGFloat
    let notchWidth: CGFloat
    let hasNotch: Bool
    @ViewBuilder let island: Content

    var body: some View {
        ZStack(alignment: .top) {
            LinearGradient(colors: [Color(red: 0.16, green: 0.32, blue: 0.62),
                                    Color(red: 0.55, green: 0.42, blue: 0.75)],
                           startPoint: .topLeading, endPoint: .bottomTrailing)
            // строка меню
            HStack(spacing: 18) {
                Image(systemName: "applelogo").font(.system(size: 13, weight: .semibold))
                Text("Firefox").font(.system(size: 13, weight: .bold))
                Text("Файл").font(.system(size: 13))
                Text("Правка").font(.system(size: 13))
                Text("Вид").font(.system(size: 13))
                Spacer()
                Image(systemName: "waveform").font(.system(size: 13))
                Image(systemName: "wifi").font(.system(size: 13))
                Image(systemName: "battery.75percent").font(.system(size: 14))
                Text("Чт 9 окт. 14:59").font(.system(size: 13))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 16)
            .frame(height: barHeight)
            .background(Color.black.opacity(0.18))
            if hasNotch {
                IslandShape(neckWidth: notchWidth, neckHeight: barHeight, bodyWidth: notchWidth,
                            bodyHeight: 0, topFlare: 0)
                    .fill(Color.black)
                    .frame(width: notchWidth, height: barHeight)
            }
            island
        }
        .frame(width: width, height: height)
    }
}

@MainActor
func render<V: View>(_ view: V, to url: URL) {
    let r = ImageRenderer(content: view)
    r.scale = 2
    guard let image = r.nsImage, let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else {
        print("render failed: \(url.lastPathComponent)"); return
    }
    try? png.write(to: url)
    print("saved \(url.path)")
}

@MainActor
func shot(_ name: String, _ mode: IslandViewState.Mode, notch: Bool, dir: URL) {
    let barHeight: CGFloat = notch ? 32 : 28
    let state = IslandViewState(mode: mode, notchWidth: 185, barHeight: barHeight)
    let view = ScreenTop(width: 1000, height: 150, barHeight: barHeight, notchWidth: 185,
                         hasNotch: notch) {
        IslandView(state: state)
            .frame(width: max(IslandLayout.maxBodyWidth, 185 + 2 * IslandEars.upcomingMax)
                       + 2 * IslandLayout.margin,
                   height: barHeight + IslandLayout.maxBodyHeight + IslandLayout.margin)
    }
    render(view, to: dir.appendingPathComponent("\(name).png"))
}

let outDir = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ".")
MainActor.assumeIsolated {
    _ = NSApplication.shared
    shot("1-upcoming", .upcoming(title: "Синк по релизу", startsText: "1 мин"), notch: true, dir: outDir)
    shot("2-recording", .recording(title: "Синк по релизу", elapsed: "24:13", mic: 0.62, system: 0.4,
                                   expanded: false), notch: true, dir: outDir)
    shot("3-recording-hover", .recording(title: "Синк по релизу", elapsed: "24:13", mic: 0.62,
                                         system: 0.4, expanded: true), notch: true, dir: outDir)
    shot("5-recording-muted", .recording(title: "Синк по релизу", elapsed: "24:13", mic: 0.62,
                                         system: 0.4, expanded: true, muted: true), notch: true, dir: outDir)
    shot("6-compact-muted", .recording(title: "Синк по релизу", elapsed: "24:13", mic: 0.62,
                                       system: 0.4, expanded: false, muted: true), notch: true, dir: outDir)
    shot("4-external-monitor", .recording(title: "Синк по релизу", elapsed: "24:13", mic: 0.62,
                                          system: 0.4, expanded: false), notch: false, dir: outDir)
    shot("7-compact-hour", .recording(title: "Синк по релизу", elapsed: "1:02:13", mic: 0.62,
                                      system: 0.4, expanded: false), notch: true, dir: outDir)
    shot("8-upcoming-long-title", .upcoming(title: "Ops Product Sync — еженедельный",
                                            startsText: "идёт 3 мин"), notch: true, dir: outDir)
    shot("9-upcoming-external", .upcoming(title: "Синк по релизу", startsText: "1 мин"),
         notch: false, dir: outDir)
}
