// RuntimeLogic.swift — чистая логика первичной настройки (без AppKit, тесты — app/Tests).
//
// SetupEvent — строка JSON от `python -m daemon.setup` (daemon/setup.py);
// RuntimeMode — откуда запускать демон: dev (репо + uv) или bundled (рантайм в
// Application Support, docs/superpowers/specs/2026-10-13-first-run-setup-design.md).

import CryptoKit
import Foundation

struct SetupResult {
    let fields: [String: Any]

    func string(_ key: String) -> String? { fields[key] as? String }
    func bool(_ key: String) -> Bool { (fields[key] as? Bool) ?? false }
    func int(_ key: String) -> Int? { (fields[key] as? NSNumber)?.intValue }
}

enum SetupEvent {
    case progress(stage: String, done: Int64, total: Int64)
    case info(String)
    case ok(SetupResult)
    case error(code: String, message: String)

    /// nil — строка не событие (вывод библиотек, пустая строка).
    static func parse(_ line: String) -> SetupEvent? {
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let event = obj["event"] as? String else { return nil }
        switch event {
        case "progress":
            return .progress(stage: obj["stage"] as? String ?? "",
                             done: (obj["done"] as? NSNumber)?.int64Value ?? 0,
                             total: (obj["total"] as? NSNumber)?.int64Value ?? 0)
        case "info":
            return .info(obj["message"] as? String ?? "")
        case "ok":
            var fields = obj
            fields["event"] = nil
            return .ok(SetupResult(fields: fields))
        case "error":
            return .error(code: obj["code"] as? String ?? "internal",
                          message: obj["message"] as? String ?? "")
        default:
            return nil
        }
    }
}

enum RuntimeMode: Equatable {
    /// Репозиторий + uv в системе (машина разработчика): `uv run python main.py`.
    case dev(repo: String, uv: String, command: [String])
    /// Чистый Mac: Python и пакеты в Application Support/MeetMind/runtime.
    case bundled
}

/// Модель саммари в мастере. Тот же список id — daemon/setup.py SUMMARY_MODELS.
struct SummaryModelOption: Equatable {
    let id: String
    let title: String
    let caption: String
}

enum RuntimeLogic {
    static let defaultSummaryModel = "claude-opus-5-5"
    static let summaryModels: [SummaryModelOption] = [
        SummaryModelOption(id: "claude-opus-5-5", title: "Opus 5.5",
                           caption: "По умолчанию — лучшее качество за свои деньги"),
        SummaryModelOption(id: "claude-sonnet-5-5", title: "Sonnet 5.5",
                           caption: "Быстрее и в 2 раза дешевле"),
        SummaryModelOption(id: "claude-fable-5-1", title: "Fable 5.1",
                           caption: "Самая сильная, в 2,5 раза дороже Opus"),
    ]

    /// Порядок поиска репо — как раньше в DaemonSupervisor (аудит T2):
    /// UserDefaults repoPath → env MEETMIND_REPO → `<repo>/app/build/BeeDott.app`.
    static func resolveMode(savedRepo: String?, envRepo: String?, derivedRepo: String,
                            uvCandidates: [String], commandOverride: [String]?,
                            isRepo: (String) -> Bool,
                            isExecutable: (String) -> Bool) -> RuntimeMode {
        guard let repo = [savedRepo, envRepo, derivedRepo].compactMap({ $0 }).first(where: isRepo)
        else { return .bundled }
        let uv = uvCandidates.first(where: isExecutable)
        // Своя команда демона (defaults daemonCommand) работает и с uv вне типовых мест.
        if let command = commandOverride, !command.isEmpty {
            return .dev(repo: repo, uv: uv ?? command[0], command: command)
        }
        guard let uv else { return .bundled }
        return .dev(repo: repo, uv: uv, command: [uv, "run", "python", "main.py"])
    }

    /// Задачи демона, которые реально идут или ждут (status.queue: строки
    /// {stage, status, n}). failed/failed_retry/skipped не считаем — иначе
    /// «дождаться простоя» не наступит никогда.
    static func activeJobs(_ queueRows: [[String: Any]]) -> Int {
        queueRows
            .filter { ["pending", "queued", "running"].contains($0["status"] as? String ?? "") }
            .reduce(0) { $0 + (($1["n"] as? Int) ?? 0) }
    }

    /// Дескриптор лога в режиме O_APPEND: каждая запись — в текущий конец файла.
    /// daemon.log пишут двое (stdout демона и строки супервизора); с обычным
    /// FileHandle(forWritingTo:) демон пишет со своей позиции и затирает чужие строки.
    static func openForAppend(_ path: String) -> FileHandle? {
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        return fd >= 0 ? FileHandle(fileDescriptor: fd, closeOnDealloc: true) : nil
    }

    /// Запуск прямо из Загрузок: macOS исполняет копию из read-only пути.
    static func isTranslocated(_ bundlePath: String) -> Bool {
        bundlePath.contains("/AppTranslocation/")
    }

    static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func needsSync(bundledLockHash: String?, installedLockHash: String?,
                          venvPythonExists: Bool) -> Bool {
        !venvPythonExists || installedLockHash == nil || bundledLockHash != installedLockHash
    }

    /// Текст для человека по коду ошибки daemon/setup.py.
    static func setupErrorText(code: String, message: String) -> String {
        switch code {
        case "no_network": return "Нет интернета — проверь подключение и нажми «Повторить»"
        case "disk_full": return "Не хватает места на диске" + (message.isEmpty ? "" : ": \(message)")
        case "invalid_key": return "Ключ не подходит — проверь, что скопировал его целиком"
        case "invalid_token": return "Токен не подходит — скопируй его из @BotFather целиком"
        case "bot_busy": return "Этот бот уже подключён к другому сервису — создай нового в @BotFather"
        case "timeout": return "Бот не получил /start — открой его в Telegram и нажми «Запустить»"
        case "bad_model": return "Такой модели нет в списке"
        case "too_long": return "Инструкция длиннее 20 000 символов — сократи"
        default: return "Что-то пошло не так: \(message)"
        }
    }
}
