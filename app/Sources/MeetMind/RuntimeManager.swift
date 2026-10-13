// RuntimeManager.swift — где живёт Python-рантайм демона и как его ставить
// (docs/superpowers/specs/2026-10-13-first-run-setup-design.md).
//
// dev     — репозиторий + uv (твой Mac): как раньше, `uv run python main.py`.
// bundled — чистый Mac: Contents/Resources/backend (исходники) + bin/uv;
//           `uv sync` ставит Python и пакеты в Application Support/MeetMind/runtime.
// Все методы, запускающие процессы, блокирующие — звать не с main.

import Darwin
import Foundation

final class RuntimeManager: @unchecked Sendable {
    static let shared = RuntimeManager()

    struct Launch {
        let executable: String
        let arguments: [String]
        let cwd: String
        let environment: [String: String]
    }

    private let fm = FileManager.default
    private let lock = NSLock()
    private var children: [Process] = []
    /// Один uv sync за раз (мастер и супервизор после обновления приложения).
    private let syncLock = NSLock()

    private init() {
        // Запись секрета в stdin процесса, который уже вышел, — SIGPIPE убил бы приложение.
        signal(SIGPIPE, SIG_IGN)
    }

    var runtimeDir: URL { ControlClient.appSupportDir.appendingPathComponent("runtime") }
    var binDir: URL { runtimeDir.appendingPathComponent("bin") }
    var venvPython: URL { runtimeDir.appendingPathComponent("venv/bin/python") }
    private var lockShaFile: URL { runtimeDir.appendingPathComponent("lock.sha") }
    var backendDir: URL? { resource("backend") }
    private var bundledUv: URL? { resource("bin/uv") }

    var setupLogURL: URL {
        let dir = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/MeetMind")
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("setup.log")
    }

    private func resource(_ path: String) -> URL? {
        guard let url = Bundle.main.resourceURL?.appendingPathComponent(path),
              fm.fileExists(atPath: url.path) else { return nil }
        return url
    }

    // MARK: - режим

    var mode: RuntimeMode {
        let home = fm.homeDirectoryForCurrentUser.path
        let derived = Bundle.main.bundleURL
            .deletingLastPathComponent()  // build/
            .deletingLastPathComponent()  // app/
            .deletingLastPathComponent()  // <repo>/
            .path
        let fm = self.fm
        return RuntimeLogic.resolveMode(
            savedRepo: UserDefaults.standard.string(forKey: "repoPath"),
            envRepo: ProcessInfo.processInfo.environment["MEETMIND_REPO"],
            derivedRepo: derived,
            uvCandidates: ["/opt/homebrew/bin/uv", "/usr/local/bin/uv",
                           "\(home)/.local/bin/uv", "\(home)/.cargo/bin/uv"],
            commandOverride: UserDefaults.standard.array(forKey: "daemonCommand") as? [String],
            isRepo: { fm.fileExists(atPath: ($0 as NSString).appendingPathComponent("main.py")) },
            isExecutable: { fm.isExecutableFile(atPath: $0) })
    }

    var isDev: Bool {
        if case .dev = mode { return true }
        return false
    }

    /// Рантайм есть: dev — всегда; bundled — venv создан (может требовать sync).
    var hasRuntime: Bool { isDev || fm.isExecutableFile(atPath: venvPython.path) }

    /// bundled: uv.lock в приложении новее установленного (или рантайма нет).
    var needsSync: Bool {
        guard !isDev else { return false }
        let bundled = backendDir.flatMap { try? Data(contentsOf: $0.appendingPathComponent("uv.lock")) }
            .map(RuntimeLogic.sha256Hex)
        let installed = (try? String(contentsOf: lockShaFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return RuntimeLogic.needsSync(bundledLockHash: bundled, installedLockHash: installed,
                                      venvPythonExists: fm.isExecutableFile(atPath: venvPython.path))
    }

    func freeDiskBytes() -> Int64 {
        let values = try? ControlClient.appSupportDir.deletingLastPathComponent()
            .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage ?? .max
    }

    // MARK: - команды запуска

    private func bundledEnvironment(extra: [String: String] = [:]) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = binDir.path + ":" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONPYCACHEPREFIX"] = runtimeDir.appendingPathComponent("pycache").path
        env.merge(extra) { _, new in new }
        return env
    }

    /// Команда демона; nil — bundled-рантайм не установлен.
    func daemonLaunch() -> Launch? {
        switch mode {
        case .dev(let repo, _, let command):
            return Launch(executable: command[0], arguments: Array(command.dropFirst()),
                          cwd: repo, environment: ProcessInfo.processInfo.environment)
        case .bundled:
            guard let backendDir, fm.isExecutableFile(atPath: venvPython.path) else { return nil }
            // cwd = runtime/: config.py делает mkdir("sessions") от cwd — не внутри .app.
            return Launch(executable: venvPython.path,
                          arguments: [backendDir.appendingPathComponent("main.py").path],
                          cwd: runtimeDir.path, environment: bundledEnvironment())
        }
    }

    private func setupLaunch(_ command: String) -> Launch? {
        switch mode {
        case .dev(let repo, let uv, _):
            return Launch(executable: uv, arguments: ["run", "python", "-m", "daemon.setup", command],
                          cwd: repo, environment: ProcessInfo.processInfo.environment)
        case .bundled:
            guard let backendDir, fm.isExecutableFile(atPath: venvPython.path) else { return nil }
            return Launch(executable: venvPython.path, arguments: ["-m", "daemon.setup", command],
                          cwd: runtimeDir.path,
                          environment: bundledEnvironment(extra: ["PYTHONPATH": backendDir.path]))
        }
    }

    // MARK: - процессы

    private func makeProcess(_ launch: Launch) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launch.executable)
        p.arguments = launch.arguments
        p.currentDirectoryURL = URL(fileURLWithPath: launch.cwd)
        p.environment = launch.environment
        return p
    }

    private func track(_ p: Process) { lock.lock(); children.append(p); lock.unlock() }
    private func untrack(_ p: Process) { lock.lock(); children.removeAll { $0 === p }; lock.unlock() }

    /// macOS не убивает детей вместе с родителем — зовётся из applicationWillTerminate.
    func terminateChildren() {
        lock.lock()
        let running = children
        lock.unlock()
        for p in running where p.isRunning { p.terminate() }
    }

    private func appendLog(_ text: String) {
        guard let data = text.data(using: .utf8),
              let h = RuntimeLogic.openForAppend(setupLogURL.path) else { return }
        h.write(data)
        try? h.close()
    }

    /// Построчно читает handle до EOF.
    private func readLines(_ handle: FileHandle, _ onLine: (String) -> Void) {
        var buffer = Data()
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = String(decoding: buffer[buffer.startIndex..<nl], as: UTF8.self)
                buffer.removeSubrange(buffer.startIndex...nl)
                onLine(line)
            }
        }
        if !buffer.isEmpty { onLine(String(decoding: buffer, as: UTF8.self)) }
    }

    /// `python -m daemon.setup <command>`; секрет — первой строкой stdin. Возвращает
    /// финальное событие (.ok или .error), промежуточные отдаёт в onEvent.
    func runSetup(_ command: String, stdin secret: String? = nil,
                  onEvent: (SetupEvent) -> Void = { _ in }) -> SetupEvent {
        guard let launch = setupLaunch(command) else {
            return .error(code: "internal", message: "компоненты не установлены")
        }
        let p = makeProcess(launch)
        let out = Pipe(), input = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardInput = input
        p.standardError = err
        do { try p.run() } catch {
            return .error(code: "internal", message: error.localizedDescription)
        }
        track(p)
        defer { untrack(p) }
        if let secret { try? input.fileHandleForWriting.write(contentsOf: Data((secret + "\n").utf8)) }
        try? input.fileHandleForWriting.close()
        // stderr (предупреждения HF, вывод uv run) — в лог, не в поток событий.
        let errDone = DispatchSemaphore(value: 0)
        DispatchQueue.global(qos: .utility).async { [self] in
            readLines(err.fileHandleForReading) { appendLog("[\(command)] \($0)\n") }
            errDone.signal()
        }
        var final: SetupEvent?
        readLines(out.fileHandleForReading) { line in
            guard let event = SetupEvent.parse(line) else { return }
            switch event {
            case .ok, .error: final = event
            case .progress, .info: onEvent(event)
            }
        }
        p.waitUntilExit()
        errDone.wait()
        return final ?? .error(code: "internal", message: "\(command): код выхода \(p.terminationStatus)")
    }

    /// bundled: копия uv без карантина → `uv sync` → link-ffmpeg → lock.sha.
    /// onLine — строки вывода uv. nil — успех, иначе текст ошибки (хвост вывода).
    func syncRuntime(onLine: @escaping (String) -> Void) -> String? {
        syncLock.lock()
        defer { syncLock.unlock() }
        guard let backendDir, let bundledUv else {
            return "в приложении нет компонентов — сборка без Resources/backend"
        }
        do {
            try fm.createDirectory(at: binDir, withIntermediateDirectories: true)
            let uv = binDir.appendingPathComponent("uv")
            if !fm.contentsEqual(atPath: uv.path, andPath: bundledUv.path) {
                try? fm.removeItem(at: uv)
                try fm.copyItem(at: bundledUv, to: uv)
                try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: uv.path)
            }
            // Скачанный zip помечает всё карантином — снимаем только с копии, бандл не трогаем.
            removexattr(uv.path, "com.apple.quarantine", 0)

            let p = Process()
            p.executableURL = uv
            p.arguments = ["sync", "--frozen", "--no-dev", "--python", "3.11",
                           "--project", backendDir.path]
            var env = ProcessInfo.processInfo.environment
            env["UV_PROJECT_ENVIRONMENT"] = runtimeDir.appendingPathComponent("venv").path
            env["UV_PYTHON_INSTALL_DIR"] = runtimeDir.appendingPathComponent("python").path
            env["UV_CACHE_DIR"] = runtimeDir.appendingPathComponent("uv-cache").path
            env["UV_PYTHON_PREFERENCE"] = "only-managed"
            env["NO_COLOR"] = "1"
            p.environment = env
            p.currentDirectoryURL = runtimeDir
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            appendLog("\n== uv sync \(Date())\n")
            try p.run()
            track(p)
            defer { untrack(p) }
            var tail: [String] = []
            readLines(pipe.fileHandleForReading) { line in
                appendLog(line + "\n")
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                guard !trimmed.isEmpty else { return }
                tail = Array((tail + [trimmed]).suffix(6))
                onLine(trimmed)
            }
            p.waitUntilExit()
            guard p.terminationStatus == 0 else { return tail.joined(separator: "\n") }
        } catch {
            return error.localizedDescription
        }
        if case .error(let code, let message) = runSetup("link-ffmpeg") {
            return RuntimeLogic.setupErrorText(code: code, message: message)
        }
        if let data = try? Data(contentsOf: backendDir.appendingPathComponent("uv.lock")) {
            try? RuntimeLogic.sha256Hex(data).write(to: lockShaFile, atomically: true, encoding: .utf8)
        }
        return nil
    }
}
