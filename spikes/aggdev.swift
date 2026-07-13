// aggdev — хелпер: программное создание/удаление Aggregate Device.
// Использование:
//   aggdev create <имя> <subDeviceUID,...>   -> печатает "<aggID> <aggUID>"
//   aggdev destroy <aggID>
//   aggdev list-inputs                        -> "<uid>\t<имя>" по строке
// Компиляция: swiftc -O -o aggdev aggdev.swift

import CoreAudio
import Foundation

func fail(_ m: String) -> Never {
    FileHandle.standardError.write(("FAIL: " + m + "\n").data(using: .utf8)!)
    exit(1)
}

func globalAddr(_ sel: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel,
                               mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func deviceUID(_ id: AudioObjectID) -> String? {
    var addr = globalAddr(kAudioDevicePropertyDeviceUID)
    var uid: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let err = withUnsafeMutablePointer(to: &uid) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0)
    }
    return err == noErr ? (uid as String) : nil
}

func deviceName(_ id: AudioObjectID) -> String {
    var addr = globalAddr(kAudioObjectPropertyName)
    var name: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let err = withUnsafeMutablePointer(to: &name) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0)
    }
    return err == noErr ? (name as String) : "?"
}

func inputDevices() -> [AudioObjectID] {
    var addr = globalAddr(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    return ids.filter { id in
        var a = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                           mScope: kAudioDevicePropertyScopeInput,
                                           mElement: kAudioObjectPropertyElementMain)
        var s: UInt32 = 0
        AudioObjectGetPropertyDataSize(id, &a, 0, nil, &s)
        return s > 0
    }
}

let args = CommandLine.arguments
switch args.count > 1 ? args[1] : "" {
case "list-inputs":
    for id in inputDevices() {
        print("\(deviceUID(id) ?? "?")\t\(deviceName(id))")
    }
case "create":
    guard args.count == 4 else { fail("usage: aggdev create <name> <uid1,uid2,...>") }
    let subDevices = args[3].split(separator: ",").map {
        [kAudioSubDeviceUIDKey: String($0)] as [String: Any]
    }
    let aggUID = "meetmind-agg-" + UUID().uuidString
    let desc: [String: Any] = [
        kAudioAggregateDeviceNameKey: args[2],
        kAudioAggregateDeviceUIDKey: aggUID,
        kAudioAggregateDeviceSubDeviceListKey: subDevices,
    ]
    var aggID = AudioObjectID(kAudioObjectUnknown)
    let err = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggID)
    guard err == noErr else { fail("create: err=\(err)") }
    print("\(aggID) \(aggUID)")
case "destroy":
    guard args.count == 3, let id = AudioObjectID(args[2]) else { fail("usage: aggdev destroy <aggID>") }
    let err = AudioHardwareDestroyAggregateDevice(id)
    guard err == noErr else { fail("destroy: err=\(err)") }
    print("OK")
default:
    fail("commands: create | destroy | list-inputs")
}
