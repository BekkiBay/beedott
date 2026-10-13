// NotificationManager.swift — уведомления от имени приложения (продукт P16).
//
// Пока приложение поллит статус, демон не показывает уведомления сам (osascript),
// а копит их в очереди; AppModel.refreshStatus забирает её через
// notifications_pop и отдаёт сюда — каждое попнутое уведомление показывается
// ровно один раз. Показ — UNUserNotificationCenter (баннер + звук, клик открывает
// встречу в Библиотеке). Нет разрешения / сбой авторизации — фоллбек на
// osascript `display notification`, как раньше делал демон.
//
// delegate ставится в AppDelegate.applicationDidFinishLaunching — до того, как
// что-либо успеет запоститься, иначе клик по уведомлению после перезапуска не
// дойдёт до нас.

import AppKit
import UserNotifications

final class NotificationManager: NSObject, UNUserNotificationCenterDelegate, Sendable {
    static let shared = NotificationManager()

    /// Звонок в браузере без встречи в Календаре: «Записать?» (автодетект).
    static let callCategory = "CALL_DETECTED"
    static let recordAction = "RECORD_CALL"

    /// UNUserNotificationCenter.current() падает вне .app-бандла (голый бинарь,
    /// запуск без Info.plist) — тогда только osascript.
    private var center: UNUserNotificationCenter? {
        guard Bundle.main.bundleURL.pathExtension == "app",
              Bundle.main.bundleIdentifier != nil else { return nil }
        return UNUserNotificationCenter.current()
    }

    /// Делегат + запрос разрешения (.alert, .sound). Не блокирует: системный
    /// диалог отвечает асинхронно, до ответа работает osascript-фоллбек.
    func install() {
        guard let center else {
            NSLog("MeetMind: не .app-бандл — уведомления только через osascript")
            return
        }
        center.delegate = self
        center.setNotificationCategories([UNNotificationCategory(
            identifier: Self.callCategory,
            actions: [UNNotificationAction(identifier: Self.recordAction, title: "Записать",
                                           options: [])],
            intentIdentifiers: [], options: [])])
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            if let error {
                NSLog("MeetMind: авторизация уведомлений не удалась (%@) — фоллбек osascript",
                      error.localizedDescription)
            } else if !granted {
                NSLog("MeetMind: уведомления запрещены — фоллбек osascript")
            }
        }
    }

    /// Показать уведомление демона. Потокобезопасно (зовётся с actionQueue).
    func post(_ note: DaemonNotification) {
        guard let center else {
            Self.postViaAppleScript(note)
            return
        }
        center.getNotificationSettings { settings in
            let status = settings.authorizationStatus
            guard status == .authorized || status == .provisional else {
                Self.postViaAppleScript(note)
                return
            }
            let content = UNMutableNotificationContent()
            content.title = note.title
            content.body = note.message
            content.sound = .default
            if let sid = note.sessionId {
                content.userInfo = ["session_id": sid]
            }
            // Уникальный identifier: id демона после его рестарта начинается
            // заново, одинаковый identifier заменил бы прошлое уведомление.
            let request = UNNotificationRequest(
                identifier: "meetmind-\(note.id)-\(UUID().uuidString)",
                content: content, trigger: nil)
            center.add(request) { error in
                if let error {
                    NSLog("MeetMind: уведомление не добавлено (%@) — фоллбек osascript",
                          error.localizedDescription)
                    Self.postViaAppleScript(note)
                }
            }
        }
    }

    /// Уведомление от самого приложения (автодетект звонков).
    func postLocal(title: String, message: String, sessionId: String?) {
        post(DaemonNotification(id: 0, title: title, message: message, sessionId: sessionId))
    }

    /// Звонок в браузере, но в Календаре сейчас нет встречи с Google Meet —
    /// сами не пишем, предлагаем кнопкой «Записать».
    func askToRecordCall() {
        let title = "Похоже, идёт звонок в браузере"
        guard let center else {
            Self.postViaAppleScript(DaemonNotification(
                id: 0, title: title, message: "Чтобы записать — ⌘⇧R", sessionId: nil))
            return
        }
        center.getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized
                    || settings.authorizationStatus == .provisional else {
                Self.postViaAppleScript(DaemonNotification(
                    id: 0, title: title, message: "Чтобы записать — ⌘⇧R", sessionId: nil))
                return
            }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = "В Календаре сейчас нет встречи. Записать этот звонок?"
            content.sound = .default
            content.categoryIdentifier = Self.callCategory
            center.add(UNNotificationRequest(
                identifier: "meetmind-call-\(UUID().uuidString)", content: content, trigger: nil))
        }
    }

    /// Фоллбек: osascript отдельным процессом — как делал демон (daemon/notifier.py).
    /// Не NSAppleScript в нашем процессе: так уведомление атрибутировалось бы
    /// MeetMind и упёрлось бы в тот же запрет/сбой авторизации.
    static func postViaAppleScript(_ note: DaemonNotification) {
        let script = "display notification \"\(appleScriptEscaped(note.message))\""
            + " with title \"\(appleScriptEscaped(note.title))\""
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            NSLog("MeetMind: osascript не запустился — %@", error.localizedDescription)
        }
    }

    /// Литерал AppleScript-строки: сначала обратный слеш, потом кавычки;
    /// переводы строк — escape-последовательностями.
    static func appleScriptEscaped(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\r", with: "\\r")
            .replacingOccurrences(of: "\n", with: "\\n")
    }

    // MARK: UNUserNotificationCenterDelegate

    /// Баннер и звук — даже когда приложение активно (окно Библиотеки в фокусе).
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    /// Клик — открыть встречу в Библиотеке (или корень Библиотеки без session_id).
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let content = response.notification.request.content
        if content.categoryIdentifier == Self.callCategory {
            // «Записать» или клик по самому уведомлению — пишем звонок.
            if response.actionIdentifier == Self.recordAction
                || response.actionIdentifier == UNNotificationDefaultActionIdentifier {
                Task { @MainActor in AppModel.shared.recordDetectedCall() }
            }
        } else if response.actionIdentifier != UNNotificationDismissActionIdentifier {
            let sid = content.userInfo["session_id"] as? String
            Task { @MainActor in LibraryWindow.show(sessionId: sid) }
        }
        completionHandler()
    }
}
