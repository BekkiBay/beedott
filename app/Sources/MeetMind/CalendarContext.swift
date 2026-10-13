// CalendarContext.swift — контекст встречи из Календаря (продукт P8).
//
// На старте записи (AppModel.performStartRecording, actionQueue) синхронно ищем
// текущее событие: не «весь день», пересекает [now−30 мин, now+15 мин], начнётся
// не позже чем через 15 мин и ещё не закончилось; из нескольких — то, чьё начало
// ближе всего к now. Отдаём название + участников (имя, иначе email из mailto:,
// без нас самих) — демон кладёт их в сессию и фоллоу-ап.
//
// Доступ к Календарю запрашивается в фоне при старте приложения: ни старт
// приложения, ни старт записи системный диалог не ждут. Нет доступа / нет
// подходящего события → nil, встречу назовёт демон.

import EventKit
import Foundation

enum CalendarContext {
    struct Meeting {
        let title: String?
        let attendees: [String]
        /// Код встречи Google Meet из ссылки в событии (abc-defg-hij) — для
        /// автодетекта звонков и meeting_key сессии.
        var meetCode: String? = nil
    }

    /// Что видно про Календарь — для строки статуса в меню.
    enum Status: Equatable {
        case noAccess          // доступ не выдан / запрещён
        case noGoogle          // доступ есть, но Google-календарей нет
        case google(Int)       // доступ есть, Google-календарей N
    }

    /// Google-аккаунт подключается в «Системные настройки → Учётные записи
    /// интернета»: его календари приходят в EventKit как CalDAV-источник
    /// с почтой gmail/google в названии.
    static func status() -> Status {
        guard hasAccess else { return .noAccess }
        let store = lookupStore ?? EKEventStore()
        lookupStore = store
        let google = store.calendars(for: .event).filter { calendar in
            let source = calendar.source?.title.lowercased() ?? ""
            return source.contains("google") || source.contains("gmail")
        }
        return google.isEmpty ? .noGoogle : .google(google.count)
    }

    /// Окно поиска вокруг момента старта записи.
    static let lookBehind: TimeInterval = 30 * 60
    static let lookAhead: TimeInterval = 15 * 60

    // Хранилище под системный запрос доступа: должно жить, пока висит диалог.
    // Трогается только с главного потока (AppModel.start).
    nonisolated(unsafe) private static var accessStore: EKEventStore?
    // Хранилище для поиска — создаётся лениво, уже после выдачи доступа
    // (созданное до выдачи может не видеть календари); только с actionQueue.
    nonisolated(unsafe) private static var lookupStore: EKEventStore?

    static var hasAccess: Bool {
        EKEventStore.authorizationStatus(for: .event) == .fullAccess
    }

    /// Fire-and-forget: системный диалог — только если пользователь ещё не решал.
    static func requestAccessIfNeeded() {
        guard EKEventStore.authorizationStatus(for: .event) == .notDetermined else { return }
        let store = EKEventStore()
        accessStore = store
        store.requestFullAccessToEvents { granted, error in
            if let error {
                NSLog("MeetMind: доступ к Календарю не получен — %@", error.localizedDescription)
            } else {
                NSLog("MeetMind: доступ к Календарю %@", granted ? "выдан" : "запрещён")
            }
        }
    }

    /// Текущая встреча из Календаря или nil. Синхронно, без диалогов.
    static func currentMeeting(at now: Date = Date()) -> Meeting? {
        guard hasAccess else { return nil }
        let store: EKEventStore
        if let existing = lookupStore {
            store = existing
        } else {
            store = EKEventStore()
            lookupStore = store
        }

        let horizon = now.addingTimeInterval(lookAhead)
        let predicate = store.predicateForEvents(
            withStart: now.addingTimeInterval(-lookBehind), end: horizon, calendars: nil)
        let candidates = store.events(matching: predicate).filter { event in
            guard let start = event.startDate, let end = event.endDate else { return false }
            return !event.isAllDay
                && event.status != .canceled
                && start <= horizon && end >= now
                && !declinedByMe(event)
        }
        guard let event = candidates.min(by: {
            abs($0.startDate.timeIntervalSince(now)) < abs($1.startDate.timeIntervalSince(now))
        }) else { return nil }

        let title = event.title?.trimmingCharacters(in: .whitespacesAndNewlines)
        var seen = Set<String>()
        let attendees = (event.attendees ?? [])
            .filter { !$0.isCurrentUser && $0.participantType != .room
                && $0.participantType != .resource }
            .compactMap(displayName)
            .filter { seen.insert($0.lowercased()).inserted }

        let meetCode = [event.url?.absoluteString, event.location, event.notes]
            .lazy.compactMap { CallDetectionMachine.meetCode(in: $0) }.first
        let meeting = Meeting(title: title?.isEmpty == false ? title : nil,
                              attendees: attendees, meetCode: meetCode)
        guard meeting.title != nil || !meeting.attendees.isEmpty || meetCode != nil else { return nil }
        NSLog("MeetMind: встреча из Календаря — «%@», участников: %d",
              meeting.title ?? "", meeting.attendees.count)
        return meeting
    }

    /// Встречи с началом в [now−6 мин, now+10 мин] — для напоминания островка
    /// (выбор, какую показать, — UpcomingMeetingPicker). Только с actionQueue.
    static func upcomingEvents(at now: Date = Date()) -> [CalendarEventInfo] {
        guard hasAccess else { return [] }
        let store = lookupStore ?? EKEventStore()
        lookupStore = store
        let predicate = store.predicateForEvents(
            withStart: now.addingTimeInterval(-6 * 60), end: now.addingTimeInterval(10 * 60),
            calendars: nil)
        return store.events(matching: predicate).compactMap { event in
            guard let start = event.startDate, let end = event.endDate,
                  !event.isAllDay, event.status != .canceled, !declinedByMe(event),
                  start >= now.addingTimeInterval(-6 * 60) else { return nil }
            let title = event.title?.trimmingCharacters(in: .whitespacesAndNewlines)
            let meetCode = [event.url?.absoluteString, event.location, event.notes]
                .lazy.compactMap { CallDetectionMachine.meetCode(in: $0) }.first
            return CalendarEventInfo(
                // У повторяющихся событий общий идентификатор — различаем по началу.
                id: "\(event.calendarItemIdentifier)@\(Int(start.timeIntervalSince1970))",
                title: title?.isEmpty == false ? title! : "Встреча",
                start: start, end: end, meetCode: meetCode)
        }
    }

    /// Событие, от которого мы отказались, — не наша встреча.
    private static func declinedByMe(_ event: EKEvent) -> Bool {
        event.attendees?.contains { $0.isCurrentUser && $0.participantStatus == .declined } == true
    }

    /// Имя участника, иначе email из mailto:-URL.
    private static func displayName(_ participant: EKParticipant) -> String? {
        if let name = participant.name?.trimmingCharacters(in: .whitespacesAndNewlines),
           !name.isEmpty {
            return name
        }
        let raw = participant.url.absoluteString
        guard raw.lowercased().hasPrefix("mailto:") else { return nil }
        let email = String(raw.dropFirst("mailto:".count))
        let decoded = (email.removingPercentEncoding ?? email)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return decoded.isEmpty ? nil : decoded
    }
}
