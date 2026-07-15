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

    var displayTitle: String { title?.isEmpty == false ? title! : "Без названия" }
    var startedDate: Date? { ISO8601DateFormatter().date(from: startedAt) }
    var isClaude: Bool { engine?.lowercased().contains("claude") == true }
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

    /// Каталог сессии (transcript/followup/аудио) — родитель audio_path.
    var sessionDir: URL? {
        audioPath.map { URL(fileURLWithPath: $0).deletingLastPathComponent() }
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

    /// engine: "local" (чистая Ollama) | "claude" (форс бустера) | nil (по конфигу).
    static func resummarize(_ sessionId: String, engine: String? = nil) throws {
        var params: [String: Any] = ["session_id": sessionId]
        if let engine { params["engine"] = engine }
        _ = try request("resummarize", params: params)
    }

    static func reprocess(_ sessionId: String) throws {
        _ = try request("reprocess", params: ["session_id": sessionId])
    }

    static func claudeSet(enabled: Bool) throws {
        _ = try request("claude_set", params: ["enabled": enabled])
    }
}
