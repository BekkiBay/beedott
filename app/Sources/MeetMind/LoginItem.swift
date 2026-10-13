// LoginItem.swift — автозапуск MeetMind при входе в систему (PLAN_v3 Task 6.2).
//
// SMAppService.mainApp: приложение появляется в «Системные настройки → Основные →
// Объекты входа». Включается само при первом запуске (MeetMind должен быть
// всегда запущен, иначе встречи не пишутся); выключается переключателем в меню —
// это решение запоминается, и при следующих запусках мы его не перебиваем.

import Foundation
import ServiceManagement

enum LoginItem {
    private static let configuredKey = "loginItemConfigured"

    static var status: SMAppService.Status { SMAppService.mainApp.status }
    static var isEnabled: Bool { status == .enabled }
    /// macOS ждёт подтверждения пользователя в Системных настройках.
    static var needsApproval: Bool { status == .requiresApproval }

    /// Первый запуск: включить автозапуск. Дальше — только по переключателю.
    static func enableOnFirstLaunch() {
        guard !UserDefaults.standard.bool(forKey: configuredKey) else { return }
        do {
            try SMAppService.mainApp.register()
            UserDefaults.standard.set(true, forKey: configuredKey)
            NSLog("MeetMind: автозапуск при входе включён (status=%ld)", status.rawValue)
        } catch {
            NSLog("MeetMind: не удалось включить автозапуск — %@", error.localizedDescription)
        }
    }

    static func setEnabled(_ enabled: Bool) {
        UserDefaults.standard.set(true, forKey: configuredKey)
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            NSLog("MeetMind: автозапуск %@ не удался — %@",
                  enabled ? "включить" : "выключить", error.localizedDescription)
        }
    }

    static func openSystemSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }
}
