// ControlClient.swift — синхронный клиент ControlSocket (SPEC_v3 §6).
//
// Протокол: unix-сокет ~/Library/Application Support/MeetMind/control.sock,
// кадр — одна строка JSON, `\n`-delimited.
//   запрос:  {"id": 1, "method": "status", "params": {...}}
//   ответ:   {"id": 1, "result": {...}} | {"id": 1, "error": {"code":..., "message":...}}
//   notification демона (без нашего id): {"method": "heartbeat"} — пропускаем.
//
// Реализовано на голых POSIX-сокетах (socket/connect/send/recv), а не NWConnection —
// синхронно и детерминированно для CLI-подобных вызовов из Supervisor/UI-actions.

import Foundation

enum ControlClientError: Error, LocalizedError {
    case notConnected(String)
    case timeout
    case protocolError(String)
    case remote(code: String, message: String)

    var errorDescription: String? {
        switch self {
        case .notConnected(let s): return "не удалось подключиться к демону: \(s)"
        case .timeout: return "таймаут ControlSocket"
        case .protocolError(let s): return "протокол ControlSocket: \(s)"
        case .remote(let code, let message): return "\(code): \(message)"
        }
    }

    /// Текст для NSAlert: у ошибок демона (busy, not_found…) message уже
    /// человекочитаемый по-русски — показываем его без кода.
    static func userMessage(_ error: Error) -> String {
        if case ControlClientError.remote(_, let message) = error, !message.isEmpty {
            return message
        }
        return error.localizedDescription
    }
}

enum ControlClient {
    static let timeout: TimeInterval = 10
    private static let requestId = 1

    /// Каталог данных демона (daemon/paths.py: app_dir) — сокет, БД, sessions/.
    static var appSupportDir: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/MeetMind")
    }

    static var socketPath: String {
        appSupportDir.appendingPathComponent("control.sock").path
    }

    /// Один синхронный запрос-ответ: открывает новое unix-соединение, шлёт строку
    /// JSON с id=1, читает строки — пропускает notification-кадры без "id" (heartbeat),
    /// возвращает result нашего ответа или бросает ошибку.
    @discardableResult
    static func request(_ method: String, params: [String: Any] = [:]) throws -> [String: Any] {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            throw ControlClientError.notConnected("socket() errno=\(errno)")
        }
        defer { close(fd) }

        var tv = timeval(tv_sec: Int(timeout), tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        let path = socketPath
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathCapacity = MemoryLayout.size(ofValue: addr.sun_path)
        guard path.utf8.count < pathCapacity else {
            throw ControlClientError.notConnected("путь сокета слишком длинный (\(path.utf8.count) байт)")
        }
        withUnsafeMutablePointer(to: &addr.sun_path) { rawPtr in
            rawPtr.withMemoryRebound(to: CChar.self, capacity: pathCapacity) { cptr in
                _ = path.withCString { strncpy(cptr, $0, pathCapacity - 1) }
            }
        }
#if canImport(Darwin)
        addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
#endif

        let connectResult = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                connect(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connectResult == 0 else {
            throw ControlClientError.notConnected(
                "демон не запущен (нет сокета \(path)), errno=\(errno)")
        }

        let payload: [String: Any] = ["id": requestId, "method": method, "params": params]
        var line = try JSONSerialization.data(withJSONObject: payload, options: [])
        line.append(0x0A) // \n

        var offset = 0
        let sendResult: Int32 = line.withUnsafeBytes { (buf: UnsafeRawBufferPointer) -> Int32 in
            while offset < buf.count {
                let n = send(fd, buf.baseAddress!.advanced(by: offset), buf.count - offset, 0)
                if n <= 0 { return -1 }
                offset += n
            }
            return 0
        }
        guard sendResult == 0 else {
            throw ControlClientError.notConnected("send() errno=\(errno)")
        }

        var buffer = Data()
        let deadline = Date().addingTimeInterval(timeout)
        while true {
            if Date() > deadline { throw ControlClientError.timeout }
            var chunk = [UInt8](repeating: 0, count: 65536)
            let n = chunk.withUnsafeMutableBytes { buf -> Int in
                recv(fd, buf.baseAddress, buf.count, 0)
            }
            if n < 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK {
                    throw ControlClientError.timeout
                }
                throw ControlClientError.notConnected("recv() errno=\(errno)")
            }
            if n == 0 {
                throw ControlClientError.protocolError("демон закрыл соединение без ответа")
            }
            buffer.append(contentsOf: chunk[0..<n])

            while let nlRange = buffer.range(of: Data([0x0A])) {
                let lineData = buffer.subdata(in: buffer.startIndex..<nlRange.lowerBound)
                buffer.removeSubrange(buffer.startIndex..<nlRange.upperBound)
                guard !lineData.isEmpty,
                      let obj = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any]
                else { continue }

                // Notification-кадры (heartbeat) без нашего id — пропускаем.
                guard let respId = obj["id"] as? Int, respId == requestId else { continue }

                if let err = obj["error"] as? [String: Any] {
                    let code = err["code"] as? String ?? "unknown"
                    let message = err["message"] as? String ?? ""
                    throw ControlClientError.remote(code: code, message: message)
                }
                return (obj["result"] as? [String: Any]) ?? [:]
            }
        }
    }

    // ------------------------------------------------------------ обёртки

    /// audioPathOwner: "app" (Swift CaptureEngine пишет WAV сам, PLAN_v3 Task 4.1-4.2)
    /// | "daemon" (Python Recorder, дефолт демона при nil) | nil.
    /// title/attendees — контекст из Календаря (P8, CalendarContext); nil/пусто —
    /// не передаём, встречу назовёт демон.
    static func startRecording(title: String? = nil, attendees: [String]? = nil,
                               audioPathOwner: String? = nil, source: String? = nil,
                               meetingKey: String? = nil) throws -> [String: Any] {
        var params: [String: Any] = [:]
        if let source { params["source"] = source }
        if let meetingKey { params["meeting_key"] = meetingKey }
        if let title, !title.isEmpty { params["title"] = title }
        if let attendees, !attendees.isEmpty { params["attendees"] = attendees }
        if let audioPathOwner { params["audio_path_owner"] = audioPathOwner }
        return try request("start_recording", params: params)
    }

    /// sessionId — остановить конкретную запись (осиротевшую после краша
    /// приложения); nil — активную.
    static func stopRecording(sessionId: String? = nil) throws -> [String: Any] {
        try request("stop_recording", params: sessionId.map { ["session_id": $0] } ?? [:])
    }

    static func cancelRecording() throws -> [String: Any] {
        try request("cancel_recording")
    }

    static func status() throws -> [String: Any] {
        try request("status")
    }

    static func health() throws -> [String: Any] {
        try request("health")
    }

    // ------------------------------------------------------------ async-мост для UI

    /// Синхронный вызов сокета на глобальной очереди: MainActor не стоит на
    /// время запроса (до `timeout` секунд, если демон подвис). Для SwiftUI-моделей
    /// (Библиотека) — `try await ControlClient.background { try ControlClient.… }`.
    static func background<T: Sendable>(
        _ work: @escaping @Sendable () throws -> T) async throws -> T {
        try await withCheckedThrowingContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                cont.resume(with: Result(catching: work))
            }
        }
    }
}
