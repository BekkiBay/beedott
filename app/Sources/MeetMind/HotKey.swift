// HotKey.swift — глобальные хоткеи через Carbon RegisterEventHotKey.
//
// ⌘⇧R (keyCode 15) — toggle start/stop; ⌃⌥⌘X (keyCode 7) — cancel (с подтверждением);
// ⌃⌥⌘M (keyCode 46) — mute/unmute микрофона в идущей записи; F5 (keyCode 96) — то же,
// но регистрируется только на время записи. На MacBook верхний ряд по умолчанию —
// медиа-клавиши (F5 = диктовка), так что F5 приходит только с Fn.
// RegisterEventHotKey не требует Accessibility permission (в отличие от
// CGEventTap/NSEvent.addGlobalMonitorForEvents), работает даже когда MeetMind
// не активное приложение (LSUIElement, без Dock/фокуса).

import Carbon
import Foundation

final class HotKeyManager {
    static let shared = HotKeyManager()

    typealias Handler = () -> Void

    /// ANSI virtual keycodes (US layout) для нужных нам букв.
    enum KeyCode {
        static let r: UInt32 = 15
        static let x: UInt32 = 7
        static let m: UInt32 = 46
        static let f5: UInt32 = 96
    }

    private var hotKeyRefs: [UInt32: EventHotKeyRef] = [:]
    private var handlers: [UInt32: Handler] = [:]
    private var eventHandlerRef: EventHandlerRef?
    private var nextId: UInt32 = 1
    private static let signature: OSType = 0x4D4D_686B // 'MMhk'

    private init() {
        installEventHandler()
    }

    private func installEventHandler() {
        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: OSType(kEventHotKeyPressed))
        let selfPtr = Unmanaged.passUnretained(self).toOpaque()

        InstallEventHandler(
            GetApplicationEventTarget(),
            { (_, eventRef, userData) -> OSStatus in
                guard let eventRef, let userData else { return noErr }
                let manager = Unmanaged<HotKeyManager>.fromOpaque(userData).takeUnretainedValue()
                var hkID = EventHotKeyID()
                let status = GetEventParameter(
                    eventRef,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &hkID)
                if status == noErr {
                    manager.fire(id: hkID.id)
                }
                return noErr
            },
            1,
            &eventType,
            selfPtr,
            &eventHandlerRef)
    }

    /// Регистрирует глобальный хоткей. modifiers — Carbon-маска (cmdKey|shiftKey|...).
    /// Возвращает id для unregister, nil — клавишу уже занял кто-то другой.
    @discardableResult
    func register(keyCode: UInt32, modifiers: UInt32, handler: @escaping Handler) -> UInt32? {
        let id = nextId
        nextId += 1
        let hotKeyID = EventHotKeyID(signature: Self.signature, id: id)

        var hotKeyRef: EventHotKeyRef?
        let status = RegisterEventHotKey(
            keyCode, modifiers, hotKeyID, GetApplicationEventTarget(), 0, &hotKeyRef)
        guard status == noErr, let ref = hotKeyRef else {
            FileHandle.standardError.write(
                "HotKeyManager: RegisterEventHotKey не удался (status=\(status))\n"
                    .data(using: .utf8)!)
            return nil
        }
        hotKeyRefs[id] = ref
        handlers[id] = handler
        return id
    }

    /// Отдаёт клавишу обратно системе и другим приложениям.
    func unregister(id: UInt32) {
        if let ref = hotKeyRefs.removeValue(forKey: id) {
            UnregisterEventHotKey(ref)
        }
        handlers[id] = nil
    }

    private func fire(id: UInt32) {
        handlers[id]?()
    }

    deinit {
        for (_, ref) in hotKeyRefs {
            UnregisterEventHotKey(ref)
        }
        if let eventHandlerRef {
            RemoveEventHandler(eventHandlerRef)
        }
    }
}
