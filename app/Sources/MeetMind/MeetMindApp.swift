// MeetMindApp.swift — SwiftUI App, MenuBarExtra shell (PLAN_v3 Task 3.3).
// Entry point (@main) живёт в main.swift, чтобы --capture-test мог отработать
// headless до запуска GUI (PLAN_v3 Task 4.1-4.2).
//
// LSUIElement-приложение (без Dock, см. Info.plist). Иконка-emoji в меню-баре:
// 🧠 idle / 🔴 recording / ⚠️ демон недоступен. Пункты: Start Recording (⌘⇧R),
// Stop, Cancel Recording (⌘⇧X), статус-строка (очередь / recording), Open Notes
// Folder (~/MeetMind), Quit MeetMind (гасит и демона, если это мы его спавнили).

import SwiftUI
import AppKit
import Carbon

// MARK: - NativeCaptureHolder

/// Держатель активного CaptureEngine + owner активной записи (PLAN_v3 Task 4.1-4.2).
/// Намеренно НЕ MainActor: читается/пишется только из AppModel.actionQueue (serial
/// DispatchQueue), поэтому доступ уже сериализован порядком вызовов — не нужен ещё
/// один слой синхронизации. Вынесен из AppModel, чтобы actionQueue-замыкания могли
/// трогать его без прыжка на MainActor (AppModel сам — @MainActor).
final class NativeCaptureHolder {
    var engine: CaptureEngine?
    /// "app" — активная запись пишется нативным CaptureEngine (Process Tap);
    /// "daemon" — Python Recorder; используется как маркер вне активной записи тоже
    /// (последнее известное значение), но реально осмыслен только пока recordingSessionId != nil.
    var owner: String = "daemon"
}

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

    // --- данные для DropdownView / Библиотеки (экраны 02, 05-06)
    @Published private(set) var recordingTitle: String? = nil
    @Published private(set) var recordingStartedAt: Date? = nil
    @Published private(set) var claudeEnabled: Bool = false
    @Published private(set) var recentMeetings: [MeetingRow] = []
    @Published private(set) var problemCount: Int = 0
    @Published private(set) var micLevel: Double = 0
    @Published private(set) var systemLevel: Double = 0
    @Published private(set) var elapsedText: String = "00:00"

    let supervisor = DaemonSupervisor()

    private var statusTimer: Timer?
    private var uiTimer: Timer?
    private var supervisorObserver: NSObjectProtocol?
    private let actionQueue = DispatchQueue(label: "com.jvmshid.meetmind.actions")
    private var started = false
    // См. NativeCaptureHolder — намеренно не MainActor-изолирован, доступ только
    // из actionQueue (сериализован порядком выполнения тем же способом, что и
    // остальные действия record/stop/cancel в этом классе).
    nonisolated(unsafe) private let capture = NativeCaptureHolder()

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
        startUITimer()
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
                let recTitle = result["recording_title"] as? String
                let recStarted = (result["recording_started_at"] as? String)
                    .flatMap { ISO8601DateFormatter().date(from: $0) }
                let claude = (result["claude_enabled"] as? Bool) ?? false
                let queueRows = (result["queue"] as? [[String: Any]]) ?? []
                let queueTotal = queueRows.reduce(0) { $0 + (($1["n"] as? Int) ?? 0) }
                let lastError = result["last_error"] as? [String: Any]
                let errText = lastError.flatMap { $0["last_error"] as? String }
                // Свежий список встреч тем же заходом (дропдаун: последние 3 +
                // бейдж проблемных на «Все встречи…»).
                let meetings = (try? ControlClient.sessionsList(limit: 50)) ?? []
                Task { @MainActor in
                    self.recordingSessionId = recording
                    self.recordingTitle = recTitle
                    self.recordingStartedAt = recStarted
                    self.claudeEnabled = claude
                    self.queueCount = queueTotal
                    self.daemonHealthy = true
                    self.lastErrorText = errText
                    self.recentMeetings = meetings
                    self.problemCount = meetings.filter { $0.status == "error" }.count
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

    /// Быстрый UI-тик (0.5с): таймер записи + уровни двух источников из
    /// CaptureEngine. capture.engine пишется actionQueue-ом, читается здесь —
    /// levels() внутри залочен, чтение ссылки engine — приемлемая гонка
    /// (worst case: nil на один тик).
    private func startUITimer() {
        uiTimer?.invalidate()
        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.uiTick() }
        }
        RunLoop.main.add(timer, forMode: .common)
        uiTimer = timer
    }

    private func uiTick() {
        if let start = recordingStartedAt {
            let s = max(0, Int(Date().timeIntervalSince(start)))
            elapsedText = s >= 3600
                ? String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
                : String(format: "%02d:%02d", s / 60, s % 60)
        } else {
            elapsedText = "00:00"
        }
        if recordingSessionId != nil, let lv = capture.engine?.levels() {
            micLevel = Double(lv.mic)
            systemLevel = Double(lv.system)
        } else {
            micLevel = 0
            systemLevel = 0
        }
    }

    // MARK: - действия дропдауна (экран 02)

    func setClaude(enabled: Bool) {
        claudeEnabled = enabled // оптимистично; статус-поллинг подтвердит
        actionQueue.async { try? ControlClient.claudeSet(enabled: enabled) }
    }

    func openLibrary(sessionId: String?) {
        LibraryWindow.show(sessionId: sessionId)
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

    func startRecording() { performAction { [weak self] in try self?.performStartRecording() } }
    func stopRecording() { performAction { [weak self] in try self?.performStopRecording() } }
    func cancelRecording() { performAction { [weak self] in try self?.performCancelRecording() } }

    func toggleRecording() {
        if recordingSessionId != nil {
            stopRecording()
        } else {
            startRecording()
        }
    }

    private func performAction(_ action: @escaping () throws -> Void) {
        guard !busy else { return }
        busy = true
        actionQueue.async { [weak self] in
            guard let self else { return }
            do {
                try action()
            } catch {
                NSLog("MeetMind: действие не удалось — %@", error.localizedDescription)
            }
            Task { @MainActor in
                self.busy = false
                self.refreshStatus()
            }
        }
    }

    // ------------------------------------------------------- capture path (PLAN_v3 Task 4.1-4.2)
    // Всё ниже выполняется на actionQueue (НЕ MainActor) — см. performAction. Трогает
    // только capture (NativeCaptureHolder, nonisolated(unsafe)) и ControlClient
    // (свободные функции), никаких @Published MainActor-полей — их правит только
    // performAction/refreshStatus после возврата на MainActor.

    /// (a) Пробуем нативный Process Tap (CaptureEngine) на временный файл — если
    /// AudioHardwareCreateProcessTap доступен (macOS ≥14.4, TCC ok), запись пойдёт
    /// через Swift (audio_path_owner=app), демон не поднимает Python Recorder.
    /// (b) CaptureError.tapUnavailable (или любая другая ошибка тапа) → вечный
    /// fallback: audio_path_owner=daemon, Python Recorder как раньше (SPEC_v3 D1).
    nonisolated private func performStartRecording() throws {
        let probeURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("meetmind-tap-probe-\(UUID().uuidString).wav")
        let probe = CaptureEngine()
        do {
            try probe.start(mode: .global, outURL: probeURL)
            probe.stop()
            try? FileManager.default.removeItem(at: probeURL)
        } catch {
            NSLog("MeetMind: CaptureEngine недоступен (%@) — fallback на daemon Recorder (audio_path_owner=daemon)",
                  error.localizedDescription)
            capture.owner = "daemon"
            capture.engine = nil
            _ = try ControlClient.startRecording(audioPathOwner: "daemon")
            return
        }

        NSLog("MeetMind: Process Tap доступен — нативная запись (audio_path_owner=app)")
        let result: [String: Any]
        do {
            result = try ControlClient.startRecording(audioPathOwner: "app")
        } catch {
            NSLog("MeetMind: start_recording(app) не удался (%@) — fallback на daemon Recorder",
                  error.localizedDescription)
            capture.owner = "daemon"
            capture.engine = nil
            _ = try ControlClient.startRecording(audioPathOwner: "daemon")
            return
        }
        guard let audioPath = result["audio_path"] as? String else {
            _ = try? ControlClient.cancelRecording()
            throw ControlClientError.protocolError("start_recording(app): нет audio_path в ответе")
        }
        NSLog("MeetMind: нативный захват пишет в %@", audioPath)

        let engine = CaptureEngine()
        do {
            try engine.start(mode: .global, outURL: URL(fileURLWithPath: audioPath))
        } catch {
            NSLog("MeetMind: engine.start(real audio_path) не удался (%@) — отменяю сессию у демона",
                  error.localizedDescription)
            _ = try? ControlClient.cancelRecording()
            throw error
        }
        capture.engine = engine
        capture.owner = "app"
    }

    /// Если писали сами (owner=app) — engine.stop() ДО stop_recording (файл должен
    /// быть закрыт и дописан до того, как демон поставит транскрипцию в очередь).
    nonisolated private func performStopRecording() throws {
        if capture.owner == "app", let engine = capture.engine {
            let frames = engine.stop()
            NSLog("MeetMind: CaptureEngine.stop() — записано кадров: %llu", frames)
            capture.engine = nil
        }
        defer { capture.owner = "daemon" }
        _ = try ControlClient.stopRecording()
    }

    nonisolated private func performCancelRecording() throws {
        if capture.owner == "app", let engine = capture.engine {
            let frames = engine.stop()
            NSLog("MeetMind: CaptureEngine.stop() (cancel) — записано кадров: %llu", frames)
            capture.engine = nil
        }
        defer { capture.owner = "daemon" }
        _ = try ControlClient.cancelRecording()
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

// MARK: - MenuBarLabel (иконка, экран 01: 4 состояния)

struct MenuBarLabel: View {
    @ObservedObject var model: AppModel

    var body: some View {
        // MenuBarExtra-label ограничен (template-рендер, без кастомного фона):
        // плашку записи из макета отображаем как symbol + tabular-таймер.
        if !model.daemonHealthy {
            Image(systemName: "exclamationmark.triangle")
        } else if model.recordingSessionId != nil {
            HStack(spacing: 3) {
                Image(systemName: "record.circle.fill")
                Text(model.elapsedText).monospacedDigit()
            }
        } else if model.queueCount > 0 {
            Image(systemName: "waveform.badge.magnifyingglass")
        } else {
            Image(systemName: "waveform")
        }
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

// Без @main здесь: entry point — main.swift (top-level код нужен для
// --capture-test до запуска GUI, PLAN_v3 Task 4.1-4.2 п.4), который вызывает
// MeetMindApp.main() явно. См. app/build.sh — swiftc без -parse-as-library.
struct MeetMindApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @ObservedObject private var model = AppModel.shared

    var body: some Scene {
        MenuBarExtra {
            DropdownView(model: model)
        } label: {
            MenuBarLabel(model: model)
        }
        .menuBarExtraStyle(.window)
    }
}
