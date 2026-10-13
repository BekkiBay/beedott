// DaemonSupervisor.swift — Swift владеет жизнью Python-демона (PLAN_v3 Task 3.4).
//
// Запускает демон как child Process — команду даёт RuntimeManager (dev: uv run python
// main.py из репо; bundled: python из Application Support/MeetMind/runtime),
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
        case notInstalled          // bundled-рантайма нет — ждём мастер (runtimeReady)
        case updating              // uv sync после обновления приложения
        case updateFailed(String)  // uv sync упал — демон не запущен
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
        let runtime = RuntimeManager.shared
        if (try? ControlClient.health()) != nil {
            // Демон от прошлой версии приложения (оно упало и оставило его) на
            // старом venv — просим выйти, чтобы обновить компоненты и запустить свой.
            if !(runtime.needsSync && runtime.hasRuntime && shutdownExternalDaemon()) {
                log("health() ok на старте — демон уже запущен снаружи, супервизор только поллит")
                setState(.healthy)
                startPolling()
                return
            }
        }
        guard runtime.hasRuntime else {
            log("рантайм не установлен — жду мастер первичной настройки")
            setState(.notInstalled)
            return
        }
        spawnProcess()
        startPolling()
    }

    /// Просит демон, запущенный не нами, завершиться (control-метод shutdown) и
    /// ждёт, пока сокет замолчит. false — не вышло (старый демон без shutdown,
    /// идёт запись).
    private func shutdownExternalDaemon() -> Bool {
        do {
            try ControlClient.request("shutdown")
        } catch {
            log("демон снаружи не остановился: \(error.localizedDescription)")
            return false
        }
        let deadline = Date().addingTimeInterval(Self.terminateGrace)
        while Date() < deadline {
            if (try? ControlClient.health()) == nil { return true }
            Thread.sleep(forTimeInterval: 0.2)
        }
        log("демон снаружи не завершился за \(Self.terminateGrace)с")
        return false
    }

    /// Мастер поставил рантайм — запустить демон, если ещё не запущен.
    func runtimeReady() {
        workQueue.async { [weak self] in
            guard let self, self.process?.isRunning != true, !self.isRestarting else { return }
            self.spawnProcess()
            self.startPolling()
        }
    }

    /// Перезапуск, чтобы демон перечитал config.env. Демон, запущенный не нами
    /// (остался от упавшего приложения), просим выйти через control-метод shutdown.
    func restart() {
        workQueue.async { [weak self] in
            // Рестарт по backoff уже запланирован — новый процесс и так прочитает config.env.
            guard let self, !self.isRestarting else { return }
            self.log("перезапуск демона: новые настройки")
            if self.weSpawned {
                self.terminateProcessGracefully()
            } else if !self.shutdownExternalDaemon() {
                return
            }
            self.consecutiveMisses = 0
            self.spawnProcess()
            self.startPolling()
        }
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
        let runtime = RuntimeManager.shared
        // После обновления приложения (и перед рестартом по backoff) — сначала
        // компоненты под новый uv.lock, иначе новый main.py упадёт на старом venv.
        if runtime.hasRuntime && runtime.needsSync {
            log("uv.lock приложения изменился — обновляю компоненты")
            setState(.updating)
            if let failure = runtime.syncRuntime(onLine: { _ in }) {
                log("обновление компонентов не удалось: \(failure)")
                setState(.updateFailed(failure))
                return
            }
        }
        guard let launch = runtime.daemonLaunch() else {
            spawnFailed("рантайм демона не установлен — пройди первичную настройку")
            setState(.notInstalled)
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launch.executable)
        p.arguments = launch.arguments
        p.currentDirectoryURL = URL(fileURLWithPath: launch.cwd)
        p.environment = launch.environment

        // O_APPEND: демон дописывает в конец, не затирая строки супервизора (log()).
        if let handle = RuntimeLogic.openForAppend(logURL.path) {
            p.standardOutput = handle
            p.standardError = handle
        }

        do {
            try p.run()
            process = p
            weSpawned = true
            lastSpawnFailure = nil
            log("демон запущен: \(([launch.executable] + launch.arguments).joined(separator: " ")) (cwd=\(launch.cwd), pid=\(p.processIdentifier))")
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
        if let data = line.data(using: .utf8), let handle = RuntimeLogic.openForAppend(logURL.path) {
            handle.write(data)
            try? handle.close()
        }
        FileHandle.standardError.write(line.data(using: .utf8) ?? Data())
    }
}
