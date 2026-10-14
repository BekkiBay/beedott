// InstructionsWindow.swift — редактор инструкции фоллоу-апа («Настройка…» →
// Саммари → «Инструкция для фоллоу-апов…»). Текст — daemon.setup
// summary-settings / set-instructions; формат JSON демон добавляет сам.
// Спека: docs/superpowers/specs/2026-10-14-summary-settings-design.md.

import AppKit
import SwiftUI

@MainActor
final class InstructionsModel: ObservableObject {
    enum Load: Equatable { case loading, ready, failed(String) }

    @Published var load: Load = .loading
    @Published var text = ""
    @Published var saving = false
    @Published var error: String?
    private var savedText = ""
    private var defaultText = ""
    var onSaved: (Bool) -> Void = { _ in }
    var onClose: () -> Void = {}
    private let queue = DispatchQueue(label: "com.jvmshid.meetmind.instructions")

    var dirty: Bool { load == .ready && text != savedText }

    func reload() {
        load = .loading
        error = nil
        queue.async {
            let result = RuntimeManager.shared.runSetup("summary-settings")
            Task { @MainActor in
                switch result {
                case .ok(let r):
                    self.savedText = r.string("instructions") ?? ""
                    self.defaultText = r.string("default_instructions") ?? ""
                    self.text = self.savedText
                    self.load = .ready
                case .error(let code, let message):
                    self.load = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                default:
                    self.load = .failed("Что-то пошло не так")
                }
            }
        }
    }

    func resetToDefault() { text = defaultText }

    func save(then done: @escaping () -> Void = {}) {
        guard !saving, load == .ready else { return }
        saving = true
        error = nil
        let body = text
        queue.async {
            let result = RuntimeManager.shared.runSetup("set-instructions", stdin: body)
            Task { @MainActor in
                self.saving = false
                switch result {
                case .ok(let r):
                    self.savedText = body
                    self.onSaved(r.bool("custom"))
                    done()
                case .error(let code, let message):
                    self.error = RuntimeLogic.setupErrorText(code: code, message: message)
                default:
                    self.error = "Что-то пошло не так"
                }
            }
        }
    }
}

struct InstructionsView: View {
    @ObservedObject var model: InstructionsModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Инструкция для фоллоу-апов").font(.system(size: 17, weight: .bold))
            Group {
                switch model.load {
                case .loading:
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Загружаю…").font(.system(size: 12))
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                case .failed(let text):
                    VStack(spacing: 8) {
                        Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
                        Button("Повторить") { model.reload() }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                case .ready:
                    TextEditor(text: $model.text)
                        .font(.system(size: 13))
                        .scrollContentBackground(.hidden)
                        .padding(6)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.rowFill(scheme)))
                }
            }
            Text("Формат ответа (разделы и JSON) BeeDott добавляет сам. Новая инструкция применится к следующим встречам; пересобрать старую — Библиотека → «Пересобрать саммари».")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
                .fixedSize(horizontal: false, vertical: true)
            if let error = model.error {
                Text(error).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
            }
            HStack {
                Button("Сбросить к стандартной") { model.resetToDefault() }
                    .disabled(model.load != .ready)
                Spacer()
                if model.saving { ProgressView().controlSize(.small) }
                Button("Отмена") { model.onClose() }
                // ⌘S, а не Return: Return в редакторе — перевод строки.
                Button("Сохранить") { model.save { model.onClose() } }
                    .keyboardShortcut("s", modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(model.load != .ready || model.saving)
            }
        }
        .padding(20)
        .frame(minWidth: 520, idealWidth: 640, minHeight: 420, idealHeight: 560)
    }
}

@MainActor
enum InstructionsWindow {
    private static var controller: NSWindowController?
    private static var model: InstructionsModel?
    private static let guardDelegate = CloseGuard()

    static func show(onSaved: @escaping (Bool) -> Void) {
        if let controller, controller.window?.isVisible == true {
            model?.onSaved = onSaved
            NSApp.activate(ignoringOtherApps: true)
            controller.showWindow(nil)
            return
        }
        let model = InstructionsModel()
        model.onSaved = onSaved
        model.onClose = { controller?.close() }  // close() не спрашивает windowShouldClose
        self.model = model
        if controller == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 560),
                                  styleMask: [.titled, .closable, .resizable],
                                  backing: .buffered, defer: false)
            window.title = "Инструкция для фоллоу-апов"
            window.isReleasedWhenClosed = false
            window.delegate = guardDelegate
            window.center()
            controller = NSWindowController(window: window)
        }
        let hosting = NSHostingController(rootView: InstructionsView(model: model))
        hosting.sizingOptions = [.minSize]
        controller?.window?.contentViewController = hosting
        NSApp.activate(ignoringOtherApps: true)
        controller?.showWindow(nil)
        model.reload()
    }

    /// Крестик окна при несохранённых правках — спросить.
    fileprivate static func shouldClose() -> Bool {
        guard let model, model.dirty else { return true }
        let alert = NSAlert()
        alert.messageText = "Сохранить изменения инструкции?"
        alert.addButton(withTitle: "Сохранить")
        alert.addButton(withTitle: "Не сохранять")
        alert.addButton(withTitle: "Отмена")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            model.save { controller?.close() }
            return false
        case .alertSecondButtonReturn:
            return true
        default:
            return false
        }
    }
}

private final class CloseGuard: NSObject, NSWindowDelegate {
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        MainActor.assumeIsolated { InstructionsWindow.shouldClose() }
    }
}
