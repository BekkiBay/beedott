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

    /// Последняя ошибка запуска — чтобы не писать одно и то же в лог каждые 60с.
    private var lastSpawnFailure: String?

    private var daemonCommand: [String]? {
        if let saved = UserDefaults.standard.array(forKey: "daemonCommand") as? [String],
           !saved.isEmpty {
            return saved
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = ["/opt/homebrew/bin/uv", "/usr/local/bin/uv",
                          "\(home)/.local/bin/uv", "\(home)/.cargo/bin/uv"]
        guard let uv = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) })
        else { return nil }
        return [uv, "run", "python", "main.py"]
    }

    /// Каталог репозитория с main.py (аудит T2: путь больше не зашит в код —
    /// после переезда репо демон не стартовал). Порядок: UserDefaults repoPath →
    /// env MEETMIND_REPO → dev-сборка `<repo>/app/build/MeetMind.app`.
    private var repoPath: String? {
        let fm = FileManager.default
        func isRepo(_ path: String) -> Bool {
            fm.fileExists(atPath: (path as NSString).appendingPathComponent("main.py"))
        }
        if let saved = UserDefaults.standard.string(forKey: "repoPath"), isRepo(saved) {
            return saved
        }
        if let env = ProcessInfo.processInfo.environment["MEETMIND_REPO"], isRepo(env) {
            return env
        }
        let derived = Bundle.main.bundleURL
            .deletingLastPathComponent()  // build/
            .deletingLastPathComponent()  // app/
            .deletingLastPathComponent()  // <repo>/
            .path
        return isRepo(derived) ? derived : nil
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

    private func spawnFailed(_ message: String) {
        guard message != lastSpawnFailure else { return }
        lastSpawnFailure = message
        log(message)
    }

    private func spawnProcess() {
        guard let repoPath else {
            spawnFailed("не найден репозиторий MeetMind (нет main.py) — задайте путь: "
                        + "defaults write com.jvmshid.meetmind repoPath <путь к репо>")
            return
        }
        guard let cmd = daemonCommand else {
            spawnFailed("не найден uv (/opt/homebrew/bin, /usr/local/bin, ~/.local/bin) — "
                        + "задайте команду: defaults write com.jvmshid.meetmind daemonCommand -array …")
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
            lastSpawnFailure = nil
            log("демон запущен: \(cmd.joined(separator: " ")) (cwd=\(repoPath), pid=\(p.processIdentifier))")
        } catch {
            spawnFailed("не удалось запустить демон: \(error.localizedDescription)")
        }
    }

    /// Погасит ли Quit демона (true, только если это мы его спавнили).
    var terminatesDaemonOnQuit: Bool { weSpawned }

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
