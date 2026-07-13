// MeetMindApp.swift — @main SwiftUI App, MenuBarExtra shell (PLAN_v3 Task 3.3).
//
// LSUIElement-приложение (без Dock, см. Info.plist). Иконка-emoji в меню-баре:
// 🧠 idle / 🔴 recording / ⚠️ демон недоступен. Пункты: Start Recording (⌘⇧R),
// Stop, Cancel Recording (⌘⇧X), статус-строка (очередь / recording), Open Notes
// Folder (~/MeetMind), Quit MeetMind (гасит и демона, если это мы его спавнили).

import SwiftUI
import AppKit
import Carbon

// MARK: - AppModel

@MainActor
final class AppModel: ObservableObject {
    enum Icon: String {
        case idle = "🧠"
        case recording = "🔴"
        case daemonDown = "⚠️"
    }

    static let shared = AppModel()

    @Published private(set) var iconText: String = Icon.idle.rawValue
    @Published private(set) var statusLine: String = "Проверка демона…"
    @Published private(set) var recordingSessionId: String? = nil
    @Published private(set) var queueCount: Int = 0
    @Published private(set) var daemonHealthy: Bool = false
    @Published private(set) var busy: Bool = false
    @Published private(set) var lastErrorText: String? = nil

    let supervisor = DaemonSupervisor()

    private var statusTimer: Timer?
    private var supervisorObserver: NSObjectProtocol?
    private let actionQueue = DispatchQueue(label: "com.jvmshid.meetmind.actions")
    private var started = false

    private init() {}

    func start() {
        guard !started else { return }
        started = true

        // Следим за состоянием супервизора (heartbeat down/restarting) — влияет на иконку,
        // даже если статус-поллинг ещё не подтвердил недоступность.
        supervisorObserver = NotificationCenter.default.addObserver(
            forName: DaemonSupervisor.stateChangedNotification,
            object: supervisor, queue: .main
        ) { [weak self] note in
            guard let state = note.userInfo?["state"] as? DaemonSupervisor.RunState else { return }
            Task { @MainActor in self?.handleSupervisorState(state) }
        }

        supervisor.start()
        registerHotKeys()
        startStatusPolling()
    }

    private func handleSupervisorState(_ state: DaemonSupervisor.RunState) {
        switch state {
        case .healthy:
            break // статус-поллинг (3с) сам подтвердит через daemonHealthy
        case .down, .restarting, .unknown:
            daemonHealthy = false
            refreshIcon()
            updateStatusLine()
        }
    }

    private func registerHotKeys() {
        let mods = UInt32(cmdKey | shiftKey)
        HotKeyManager.shared.register(keyCode: HotKeyManager.KeyCode.r, modifiers: mods) {
            Task { @MainActor in AppModel.shared.toggleRecording() }
        }
        HotKeyManager.shared.register(keyCode: HotKeyManager.KeyCode.x, modifiers: mods) {
            Task { @MainActor in AppModel.shared.cancelRecording() }
        }
    }

    private func startStatusPolling() {
        statusTimer?.invalidate()
        let timer = Timer(timeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshStatus() }
        }
        RunLoop.main.add(timer, forMode: .common)
        statusTimer = timer
        refreshStatus()
    }

    func refreshStatus() {
        actionQueue.async { [weak self] in
            guard let self else { return }
            do {
                let result = try ControlClient.status()
                let recording = result["recording"] as? String
                let queueRows = (result["queue"] as? [[String: Any]]) ?? []
                let queueTotal = queueRows.reduce(0) { $0 + (($1["n"] as? Int) ?? 0) }
                let lastError = result["last_error"] as? [String: Any]
                let errText = lastError.flatMap { $0["last_error"] as? String }
                Task { @MainActor in
                    self.recordingSessionId = recording
                    self.queueCount = queueTotal
                    self.daemonHealthy = true
                    self.lastErrorText = errText
                    self.updateStatusLine()
                    self.refreshIcon()
                }
            } catch {
                Task { @MainActor in
                    self.daemonHealthy = false
                    self.updateStatusLine()
                    self.refreshIcon()
                }
            }
        }
    }

    private func updateStatusLine() {
        if !daemonHealthy {
            statusLine = "Демон недоступен"
        } else if let sid = recordingSessionId {
            statusLine = "Запись: \(sid)"
        } else if queueCount > 0 {
            statusLine = "В очереди: \(queueCount)"
        } else {
            statusLine = "Очередь пуста"
        }
    }

    private func refreshIcon() {
        if !daemonHealthy {
            iconText = Icon.daemonDown.rawValue
        } else if recordingSessionId != nil {
            iconText = Icon.recording.rawValue
        } else {
            iconText = Icon.idle.rawValue
        }
    }

    // MARK: actions

    func startRecording() { performAction { try ControlClient.startRecording() } }
    func stopRecording() { performAction { try ControlClient.stopRecording() } }
    func cancelRecording() { performAction { try ControlClient.cancelRecording() } }

    func toggleRecording() {
        if recordingSessionId != nil {
            stopRecording()
        } else {
            startRecording()
        }
    }

    private func performAction(_ action: @escaping () throws -> [String: Any]) {
        guard !busy else { return }
        busy = true
        actionQueue.async { [weak self] in
            guard let self else { return }
            do {
                _ = try action()
            } catch {
                NSLog("MeetMind: действие не удалось — %@", error.localizedDescription)
            }
            Task { @MainActor in
                self.busy = false
                self.refreshStatus()
            }
        }
    }

    func openNotesFolder() {
        let url = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("MeetMind")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        NSWorkspace.shared.open(url)
    }

    func quit() {
        supervisor.terminateOnQuit()
        NSApplication.shared.terminate(nil)
    }
}

// MARK: - MenuBarContent

struct MenuBarContent: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Button("Start Recording") { model.startRecording() }
            .keyboardShortcut("r", modifiers: [.command, .shift])
            .disabled(model.busy || model.recordingSessionId != nil || !model.daemonHealthy)

        Button("Stop") { model.stopRecording() }
            .disabled(model.busy || model.recordingSessionId == nil)

        Button("Cancel Recording") { model.cancelRecording() }
            .keyboardShortcut("x", modifiers: [.command, .shift])
            .disabled(model.busy || model.recordingSessionId == nil)

        Divider()

        Text(model.statusLine)

        Divider()

        Button("Open Notes Folder") { model.openNotesFolder() }

        Button("Quit MeetMind") { model.quit() }
            .keyboardShortcut("q", modifiers: [.command])
    }
}

// MARK: - AppDelegate

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        AppModel.shared.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        AppModel.shared.supervisor.terminateOnQuit()
    }
}

// MARK: - App

@main
struct MeetMindApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @ObservedObject private var model = AppModel.shared

    var body: some Scene {
        MenuBarExtra {
            MenuBarContent(model: model)
        } label: {
            Text(model.iconText)
        }
        .menuBarExtraStyle(.menu)
    }
}
