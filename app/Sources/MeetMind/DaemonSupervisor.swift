// DaemonSupervisor.swift — Swift владеет жизнью Python-демона (PLAN_v3 Task 3.4).
//
// Запускает демон как child Process (dev-режим: uv run python main.py из UserDefaults),
// пишет stdout/stderr в ~/Library/Logs/MeetMind/daemon.log (append), поллит health()
// каждые 5с. 3 промаха подряд (15с) → terminate(), ждём 5с, kill -9, рестарт с backoff
// 1,2,4,...60с. Если сокет уже отвечает при старте — демон запущен снаружи, супервизор
// только поллит и НЕ спавнит второй процесс. Quit-приложения завершает демон только
// если это мы его когда-то заспавнили.

import Foundation

final class DaemonSupervisor: ObservableObject {
    enum RunState: Equatable {
        case unknown
        case healthy
        case down(consecutiveMisses: Int)
        case restarting
    }

    static let missesBeforeRestart = 3
    static let pollInterval: TimeInterval = 5
    static let terminateGrace: TimeInterval = 5
    static let initialBackoff: Double = 1
    static let maxBackoff: Double = 60

    /// Постится на main queue при каждом изменении state; userInfo["state"] = RunState.
    /// (Проще, чем тащить Combine-подписку через actor-границу AppModel.)
    static let stateChangedNotification = Notification.Name("MeetMind.DaemonSupervisor.stateChanged")

    @Published private(set) var state: RunState = .unknown

    private var process: Process?
    private var weSpawned = false
    private var pollTimer: Timer?
    private var consecutiveMisses = 0
    private var backoffSeconds: Double = DaemonSupervisor.initialBackoff
    private var isRestarting = false
    private let logURL: URL
    private let workQueue = DispatchQueue(label: "com.jvmshid.meetmind.supervisor")

    private var daemonCommand: [String] {
        if let saved = UserDefaults.standard.array(forKey: "daemonCommand") as? [String],
           !saved.isEmpty {
            return saved
        }
        return ["/opt/homebrew/bin/uv", "run", "python", "main.py"]
    }

    private var repoPath: String {
        UserDefaults.standard.string(forKey: "repoPath") ?? "/Users/b.botirov/AIApp/meetmind"
    }

    init() {
        let logsDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/MeetMind")
        try? FileManager.default.createDirectory(at: logsDir, withIntermediateDirectories: true)
        self.logURL = logsDir.appendingPathComponent("daemon.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
    }

    // ------------------------------------------------------------ lifecycle

    func start() {
        workQueue.async { [weak self] in
            self?.bootstrap()
        }
    }

    private func bootstrap() {
        if (try? ControlClient.health()) != nil {
            log("health() ok на старте — демон уже запущен снаружи, супервизор только поллит")
            setState(.healthy)
        } else {
            spawnProcess()
        }
        startPolling()
    }

    private func startPolling() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.pollTimer?.invalidate()
            let timer = Timer(timeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
                self?.workQueue.async { self?.pollHealth() }
            }
            RunLoop.main.add(timer, forMode: .common)
            self.pollTimer = timer
        }
    }

    private func pollHealth() {
        do {
            _ = try ControlClient.health()
            consecutiveMisses = 0
            backoffSeconds = Self.initialBackoff
            setState(.healthy)
        } catch {
            consecutiveMisses += 1
            log("health() промах #\(consecutiveMisses): \(error.localizedDescription)")
            setState(.down(consecutiveMisses: consecutiveMisses))
            if consecutiveMisses >= Self.missesBeforeRestart && !isRestarting {
                restartDaemon()
            }
        }
    }

    private func restartDaemon() {
        isRestarting = true
        setState(.restarting)
        log("3 промаха heartbeat подряд — перезапуск демона")
        terminateProcessGracefully()

        let delay = backoffSeconds
        log("рестарт через \(delay)с (backoff)")
        workQueue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self else { return }
            self.spawnProcess()
            self.consecutiveMisses = 0
            self.backoffSeconds = min(self.backoffSeconds * 2, Self.maxBackoff)
            self.isRestarting = false
        }
    }

    private func terminateProcessGracefully() {
        guard let p = process, p.isRunning else { return }
        log("terminate() pid=\(p.processIdentifier), жду \(Self.terminateGrace)с")
        p.terminate() // SIGTERM
        let deadline = Date().addingTimeInterval(Self.terminateGrace)
        while p.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.2)
        }
        if p.isRunning {
            log("SIGTERM не помог — kill -9 pid=\(p.processIdentifier)")
            kill(p.processIdentifier, SIGKILL)
        }
        process = nil
    }

    private func spawnProcess() {
        let cmd = daemonCommand
        guard !cmd.isEmpty else {
            log("daemonCommand пуст — не могу запустить демон")
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cmd[0])
        p.arguments = Array(cmd.dropFirst())
        p.currentDirectoryURL = URL(fileURLWithPath: repoPath)

        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            p.standardOutput = handle
            p.standardError = handle
        }

        do {
            try p.run()
            process = p
            weSpawned = true
            log("демон запущен: \(cmd.joined(separator: " ")) (cwd=\(repoPath), pid=\(p.processIdentifier))")
        } catch {
            log("не удалось запустить демон: \(error.localizedDescription)")
        }
    }

    /// Вызывается при Quit приложения. Останавливает демон, только если это МЫ его
    /// когда-то заспавнили (внешне запущенный демон не трогаем).
    func terminateOnQuit() {
        guard weSpawned else {
            log("Quit: демон не наш child — не трогаем")
            return
        }
        pollTimer?.invalidate()
        terminateProcessGracefully()
    }

    private func setState(_ newState: RunState) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.state = newState
            NotificationCenter.default.post(
                name: Self.stateChangedNotification, object: self, userInfo: ["state": newState])
        }
    }

    private func log(_ s: String) {
        let ts = ISO8601DateFormatter().string(from: Date())
        let line = "[\(ts)] \(s)\n"
        if let data = line.data(using: .utf8) {
            if let handle = try? FileHandle(forWritingTo: logURL) {
                handle.seekToEndOfFile()
                handle.write(data)
                try? handle.close()
            }
        }
        FileHandle.standardError.write(line.data(using: .utf8) ?? Data())
    }
}
