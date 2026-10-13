// CallDetector.swift — живые сигналы для автодетекта звонков (логика —
// CallDetectionMachine.swift).
//
// BrowserAudioProbe: какие процессы прямо сейчас пишут микрофон / играют звук —
// Core Audio process objects (macOS 14.2+), без разрешений и без чтения вкладок
// браузера. Свой процесс (наша запись тоже держит микрофон) не учитывается —
// он и не браузер.

import AppKit
import CoreAudio
import Foundation

enum BrowserAudioProbe {
    /// Сводное состояние всех браузеров: хоть один пишет микрофон / играет звук.
    static func current() -> BrowserAudio {
        let safariRunning = NSWorkspace.shared.runningApplications
            .contains { $0.bundleIdentifier == "com.apple.Safari" }
        var audio = BrowserAudio(input: false, output: false)
        for process in processObjects() {
            guard let bundleID = stringProperty(process, kAudioProcessPropertyBundleID),
                  BrowserAudio.browserFamily(bundleID: bundleID, safariRunning: safariRunning) != nil
            else { continue }
            if uintProperty(process, kAudioProcessPropertyIsRunningInput) != 0 { audio.input = true }
            if uintProperty(process, kAudioProcessPropertyIsRunningOutput) != 0 { audio.output = true }
        }
        return audio
    }

    private static func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                   mElement: kAudioObjectPropertyElementMain)
    }

    private static func processObjects() -> [AudioObjectID] {
        var addr = address(kAudioHardwarePropertyProcessObjectList)
        var size: UInt32 = 0
        let system = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr, size > 0
        else { return [] }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return [] }
        return ids
    }

    private static func uintProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32 {
        var addr = address(selector)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &value) == noErr ? value : 0
    }

    private static func stringProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var addr = address(selector)
        var value: CFString = "" as CFString
        var size = UInt32(MemoryLayout<CFString>.size)
        let err = withUnsafeMutablePointer(to: &value) { ptr in
            AudioObjectGetPropertyData(object, &addr, 0, nil, &size, ptr)
        }
        let s = value as String
        return err == noErr && !s.isEmpty ? s : nil
    }
}
