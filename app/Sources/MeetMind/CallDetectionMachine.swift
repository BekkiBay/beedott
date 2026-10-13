// CallDetectionMachine.swift — логика автодетекта звонков в браузере (Google Meet).
//
// Без чтения вкладок и истории браузера: сигнал звонка — «браузер прямо сейчас
// пишет микрофон» (macOS отдаёт это по каждому процессу через Core Audio, без
// разрешений), подтверждение — «в Календаре сейчас встреча со ссылкой на Meet».
//   • микрофон браузера ≥ startDelay и в Календаре встреча с Meet → автостарт;
//   • то же без встречи → вопрос «Записать?» уведомлением, сами не пишем;
//   • автозапись останавливается, когда браузер замолчал полностью (ни мик, ни
//     звук) на endQuiet, или когда микрофона нет дольше endNoInput (звонок
//     кончился, а браузер играет что-то другое);
//   • ручную запись не трогаем; если автозапись остановили руками — до конца
//     этого звонка повторно не стартуем.
// Чистая логика (Foundation, без AppKit/CoreAudio) — тесты в app/Tests.

import Foundation

/// Аудио браузеров в моменте: пишут ли микрофон / играют ли звук.
struct BrowserAudio: Equatable {
    var input: Bool
    var output: Bool

    /// Процесс Core Audio → семейство браузера (nil — не браузер). Safari пишет
    /// звук через общий процесс WebKit.GPU — считаем его браузером, только если
    /// Safari запущен.
    static func browserFamily(bundleID: String, safariRunning: Bool) -> String? {
        let families: [(prefix: String, name: String)] = [
            ("org.mozilla.firefox", "firefox"),
            ("com.google.Chrome", "chrome"),
            ("company.thebrowser", "arc"),
            ("com.microsoft.edgemac", "edge"),
            ("com.brave.Browser", "brave"),
            ("ru.yandex.desktop.yandex-browser", "yandex"),
            ("com.operasoftware", "opera"),
            ("com.vivaldi.Vivaldi", "vivaldi"),
        ]
        if let hit = families.first(where: { bundleID.hasPrefix($0.prefix) }) { return hit.name }
        if bundleID == "com.apple.WebKit.GPU" && safariRunning { return "safari" }
        return nil
    }
}

enum CallDetectionAction: Equatable {
    case none
    case autoStart     // встреча с Meet в Календаре — пишем сами
    case askToRecord   // звонок без встречи в Календаре — спросить
    case stop          // автозапись: звонок закончился
}

struct CallDetectionMachine {
    var startDelay: TimeInterval = 8
    var endQuiet: TimeInterval = 60
    var endNoInput: TimeInterval = 15 * 60

    private var inputSince: Date?
    private var quietSince: Date?
    private var inputLostAt: Date?
    /// Текущий звонок уже обработан (стартовали / спросили / остановили руками).
    private var handledThisCall = false
    /// Текущая запись начата детектором — её и останавливаем.
    private var autoRecording = false

    /// Вызвать после успешного автостарта (или «Записать» в уведомлении).
    mutating func markAutoRecording() {
        autoRecording = true
        handledThisCall = true
    }

    mutating func step(_ audio: BrowserAudio, now: Date, isRecording: Bool,
                       calendarHasMeet: Bool) -> CallDetectionAction {
        let active = audio.input || audio.output
        quietSince = active ? nil : (quietSince ?? now)
        inputLostAt = audio.input ? nil : (inputLostAt ?? now)
        inputSince = audio.input ? (inputSince ?? now) : nil

        if isRecording {
            guard autoRecording else { return .none }  // ручную запись не трогаем
            if let q = quietSince, now.timeIntervalSince(q) >= endQuiet { return finishAuto() }
            if let l = inputLostAt, now.timeIntervalSince(l) >= endNoInput { return finishAuto() }
            return .none
        }

        // Не пишем: автозапись остановили руками (или она закончилась) — звонок
        // остаётся «обработанным», пока браузер не замолчит.
        autoRecording = false
        if let q = quietSince, now.timeIntervalSince(q) >= endQuiet {
            handledThisCall = false  // звонок кончился — следующий снова ловим
        }
        guard !handledThisCall, let since = inputSince,
              now.timeIntervalSince(since) >= startDelay else { return .none }
        handledThisCall = true
        return calendarHasMeet ? .autoStart : .askToRecord
    }

    private mutating func finishAuto() -> CallDetectionAction {
        // Звонок кончился — следующий микрофон браузера будет новым звонком,
        // даже если браузер продолжает играть звук.
        autoRecording = false
        handledThisCall = false
        return .stop
    }

    /// Код встречи Google Meet (abc-defg-hij) из текста события: ссылка, место, заметки.
    static func meetCode(in text: String?) -> String? {
        guard let text,
              let range = text.range(of: #"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})"#,
                                     options: .regularExpression)
        else { return nil }
        return String(text[range].split(separator: "/").last ?? "")
    }
}
