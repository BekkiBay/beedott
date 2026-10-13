// Models.swift — типизированные модели ответов ControlSocket для UI
// (экраны 02/05/06 design_handoff_meetmind) + Codable-обёртки ControlClient.
//
// ControlClient.request отдаёт [String: Any]; здесь — прогон через
// JSONSerialization → JSONDecoder(.convertFromSnakeCase), чтобы SwiftUI-слой
// работал со структурами, а не словарями.

import Foundation

// MARK: - модели

struct MeetingRow: Decodable, Identifiable, Equatable {
    let id: String
    let title: String?
    let startedAt: String
    let durationMin: Double?
    let state: String
    let status: String // delivered | processing | error
    let error: String?
    let engine: String?
    let actionItems: Int?
    let sinks: [SinkBrief]
    // P10/P13 — optional: старый демон этих полей не присылает.
    let keepAudio: LenientBool?
    let hasAudio: LenientBool?
    let source: String? // manual | import | …

    var displayTitle: String { title?.isEmpty == false ? title! : "Без названия" }
    var startedDate: Date? { ISO8601DateFormatter().date(from: startedAt) }
    var isClaude: Bool { engine?.lowercased().contains("claude") == true }
    var keepsAudio: Bool { keepAudio?.value == true }
}

struct SinkBrief: Decodable, Equatable {
    let sink: String
    let status: String
}

struct SessionsListResponse: Decodable {
    let sessions: [MeetingRow]
}

struct TranscriptSegment: Decodable, Identifiable {
    let start: Double
    let end: Double
    let text: String
    let speaker: String? // «Спикер 1» (микрофон) / «Спикер 2» (системный звук)
    var id: String { "\(start)-\(speaker ?? "")" }

    /// Индекс для палитры Theme.speaker: «Спикер N» → N-1.
    var speakerIndex: Int {
        guard let speaker, let n = Int(speaker.split(separator: " ").last ?? "") else { return 0 }
        return max(0, n - 1)
    }

    var timecode: String {
        let s = Int(start)
        return String(format: "%02d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
    }
}

struct ActionItem: Decodable, Identifiable {
    let task: String
    let owner: String
    let deadline: String?
    var id: String { task + owner }
}

struct Followup: Decodable {
    let context: String
    let discussed: [String]
    let decisions: [String]
    let actionItems: [ActionItem]
    let openQuestions: [String]
    let engine: String?
    let degraded: Bool?
}

struct PipelineStep: Decodable, Identifiable {
    let step: String // recorded | transcribed | summarized | delivered
    let ok: Bool?    // true пройден / false упал / nil ещё не дошло
    let at: String?
    let error: String?
    var id: String { step }

    var label: String {
        switch step {
        case "recorded": return "Записано"
        case "transcribed": return "Расшифровано"
        case "summarized": return "Саммари"
        case "delivered": return "Доставлено"
        default: return step
        }
    }
}

struct SinkDetail: Decodable, Identifiable {
    let sink: String
    let status: String
    let deliveredAt: String?
    let detail: String?
    var id: String { sink }
}

struct SessionRow: Decodable {
    let id: String
    let title: String?
    let state: String
    let startedAt: String
    let endedAt: String?
    let audioPath: String?
    // P8/P10/P13 — optional: старый демон этих колонок не знает.
    let keepAudio: LenientBool?   // в строке БД — INTEGER 0/1
    let attendees: NameList?      // в строке БД — JSON-массив имён строкой
    let source: String?

    var displayTitle: String { title?.isEmpty == false ? title! : "Без названия" }
    var keepsAudio: Bool { keepAudio?.value == true }
    var attendeeNames: [String] { attendees?.names ?? [] }

    /// Аудио ещё на диске (демон обнуляет audio_path при удалении; файл
    /// проверяем сами — приложение на том же Mac).
    var hasAudioFile: Bool {
        guard let audioPath else { return false }
        return FileManager.default.fileExists(atPath: audioPath)
    }

    /// Обработка закончена (доставлено/упало) — аудио уже не появится.
    var isFinished: Bool { state == "DELIVERED" || state == "FAILED" }

    /// Каталог сессии (transcript/followup/аудио): <app_dir>/sessions/<id>, как
    /// у демона (daemon/paths.py). Фоллбек — родитель audio_path. По audio_path
    /// одному нельзя: после удаления аудио он null, у импорта может указывать
    /// на исходный файл пользователя.
    var sessionDir: URL? {
        let canonical = ControlClient.appSupportDir
            .appendingPathComponent("sessions", isDirectory: true)
            .appendingPathComponent(id, isDirectory: true)
        if FileManager.default.fileExists(atPath: canonical.path) { return canonical }
        return audioPath.map { URL(fileURLWithPath: $0).deletingLastPathComponent() }
    }
}

struct SessionDetail: Decodable {
    let session: SessionRow
    let followup: Followup?
    let transcript: [TranscriptSegment]?
    let pipeline: [PipelineStep]
    let sinks: [SinkDetail]
}

struct SearchHit: Decodable, Identifiable {
    let sessionId: String
    let title: String?
    let source: String // transcript | summary
    let snippet: String
    var id: String { sessionId + source + snippet }
}

struct SearchResponse: Decodable {
    let results: [SearchHit]
}

/// Уведомление демона из очереди notifications_pop (P16).
struct DaemonNotification: Sendable {
    let id: Int
    let title: String
    let message: String
    let sessionId: String?
}

// MARK: - терпимые декодеры

/// Bool, который демон может прислать как true/false (sessions_list) или как
/// INTEGER 0/1 (сырая строка БД в session_detail).
struct LenientBool: Decodable, Equatable {
    let value: Bool

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let b = try? c.decode(Bool.self) {
            value = b
        } else if let i = try? c.decode(Int.self) {
            value = i != 0
        } else if let s = try? c.decode(String.self) {
            value = ["1", "true", "yes"].contains(s.lowercased())
        } else {
            value = false
        }
    }
}

/// Список имён участников: в строке БД — JSON-массив строкой ('["Аня", "Борис"]'),
/// на всякий случай принимаем и настоящий массив, и «Аня, Борис».
struct NameList: Decodable, Equatable {
    let names: [String]

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        var raw: [String] = []
        if let list = try? c.decode([String].self) {
            raw = list
        } else if let s = try? c.decode(String.self) {
            if let data = s.data(using: .utf8),
               let list = try? JSONSerialization.jsonObject(with: data) as? [Any] {
                raw = list.compactMap { $0 as? String }
            } else {
                raw = s.split(separator: ",").map(String.init)
            }
        }
        names = raw
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }
}

// MARK: - ControlClient: типизированные обёртки

extension ControlClient {
    private static func requestDecoded<T: Decodable>(
        _ method: String, params: [String: Any] = [:]) throws -> T {
        let dict = try request(method, params: params)
        let data = try JSONSerialization.data(withJSONObject: dict)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: data)
    }

    static func sessionsList(limit: Int = 200) throws -> [MeetingRow] {
        let resp: SessionsListResponse =
            try requestDecoded("sessions_list", params: ["limit": limit])
        return resp.sessions
    }

    static func sessionDetail(_ sessionId: String) throws -> SessionDetail {
        try requestDecoded("session_detail", params: ["session_id": sessionId])
    }

    static func search(_ query: String) throws -> [SearchHit] {
        let resp: SearchResponse = try requestDecoded("search", params: ["query": query])
        return resp.results
    }

    static func resummarize(_ sessionId: String) throws {
        _ = try request("resummarize", params: ["session_id": sessionId])
    }

    static func reprocess(_ sessionId: String) throws {
        _ = try request("reprocess", params: ["session_id": sessionId])
    }

    // --- карточка встречи: переименование / хранение аудио / удаление (P8, P10, P14)

    static func renameSession(_ sessionId: String, title: String) throws {
        _ = try request("rename_session", params: ["session_id": sessionId, "title": title])
    }

    static func setKeepAudio(_ sessionId: String, keep: Bool) throws {
        _ = try request("set_keep_audio", params: ["session_id": sessionId, "keep": keep])
    }

    /// busy — встречу сейчас пишут или обрабатывают (message — по-русски).
    static func deleteSession(_ sessionId: String) throws {
        _ = try request("delete_session", params: ["session_id": sessionId])
    }

    /// Импорт аудиофайла (P13): конвертация идёт у демона асинхронно, сессия
    /// сразу видна в sessions_list как processing. Возвращает session_id.
    static func importAudio(path: String, title: String? = nil) throws -> String {
        var params: [String: Any] = ["path": path]
        if let title, !title.isEmpty { params["title"] = title }
        let result = try request("import_audio", params: params)
        guard let sid = result["session_id"] as? String else {
            throw ControlClientError.protocolError("import_audio: нет session_id в ответе")
        }
        return sid
    }

    /// Очередь уведомлений демона для приложения (P16) — каждое отдаётся один раз.
    /// Разбор вручную и терпимо: попнутое, но не разобранное — потеряно.
    static func notificationsPop() throws -> [DaemonNotification] {
        let result = try request("notifications_pop")
        let rows = (result["notifications"] as? [[String: Any]]) ?? []
        return rows.compactMap { row in
            let title = (row["title"] as? String) ?? "BeeDott"
            let message = (row["message"] as? String) ?? ""
            guard !(title.isEmpty && message.isEmpty) else { return nil }
            return DaemonNotification(
                id: (row["id"] as? Int) ?? 0,
                title: title.isEmpty ? "BeeDott" : title,
                message: message,
                sessionId: (row["session_id"] as? String).flatMap { $0.isEmpty ? nil : $0 })
        }
    }
}
