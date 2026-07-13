// HotKey.swift — глобальные хоткеи через Carbon RegisterEventHotKey.
//
// ⌘⇧R (keyCode 15) — toggle start/stop; ⌘⇧X (keyCode 7) — cancel.
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
    @discardableResult
    func register(keyCode: UInt32, modifiers: UInt32, handler: @escaping Handler) -> Bool {
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
            return false
        }
        hotKeyRefs[id] = ref
        handlers[id] = handler
        return true
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
