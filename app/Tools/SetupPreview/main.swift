// Tools/SetupPreview/main.swift — превью мастера первичной настройки в PNG (без
// запуска приложения). Запуск: app/Tools/setup_preview.sh <папка>.
// Окно рисуется в скрытом NSWindow (cacheDisplay) — ImageRenderer не рисует
// AppKit-контролы (поля ввода, кнопки).

import AppKit
import SwiftUI

let outDir = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ".")
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
_ = NSApplication.shared
NSApp.setActivationPolicy(.prohibited)

@MainActor
func render(_ name: String, dark: Bool = false, _ configure: (SetupModel) -> Void) {
    let model = SetupModel(status: nil)
    configure(model)
    let view = NSHostingView(rootView: SetupRootView(model: model))
    view.frame = NSRect(x: 0, y: 0, width: 520, height: 460)
    let window = NSWindow(contentRect: view.frame, styleMask: [.borderless], backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    window.contentView = view
    window.backgroundColor = .windowBackgroundColor
    view.layoutSubtreeIfNeeded()
    RunLoop.main.run(until: Date().addingTimeInterval(0.3))
    guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
    view.cacheDisplay(in: view.bounds, to: rep)
    let url = outDir.appendingPathComponent("\(name).png")
    try? rep.representation(using: .png, properties: [:])?.write(to: url)
    print("saved \(url.path)")
}

MainActor.assumeIsolated {
    render("1-speech-idle") { m in
        m.runtimePhase = .idle; m.modelPhase = .idle; m.step = .speech
    }
    render("2-speech-downloading") { m in
        m.step = .speech; m.runtimePhase = .done
        m.modelPhase = .running; m.modelDone = 671_088_640; m.modelTotal = 1_613_979_758
    }
    render("3-speech-failed") { m in
        m.step = .speech; m.runtimePhase = .failed("Не удалось поставить пакеты. Проверь интернет и нажми «Повторить».")
        m.runtimeDetails = "error: Failed to fetch: `https://pypi.org/simple/mlx/`\n  Caused by: dns error"
    }
    render("4-summary-invalid") { m in
        m.step = .summary; m.runtimePhase = .done; m.modelPhase = .done
        m.apiKey = "sk-ant-api03-xxxxxxxx"
        m.keyPhase = .failed(RuntimeLogic.setupErrorText(code: "invalid_key", message: ""))
    }
    render("5-summary-configured") { m in
        m.step = .summary; m.runtimePhase = .done; m.modelPhase = .done; m.summaryMode = "api"
    }
    render("6-telegram-waiting") { m in
        m.step = .telegram; m.runtimePhase = .done; m.modelPhase = .done; m.summaryMode = "api"
        m.botToken = "123456:ABC"; m.botName = "beedott_notes_bot"; m.tokenPhase = .done
        m.connectPhase = .running
    }
    render("7-done", dark: false) { m in
        m.step = .done; m.runtimePhase = .done; m.modelPhase = .done; m.summaryMode = "api"
    }
    render("8-speech-downloading-dark", dark: true) { m in
        m.step = .speech; m.runtimePhase = .done
        m.modelPhase = .running; m.modelDone = 671_088_640; m.modelTotal = 1_613_979_758
    }
}
