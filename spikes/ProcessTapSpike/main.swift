// ProcessTapSpike — go/no-go проверка Core Audio Process Taps (macOS 14.4+).
// Использование: ProcessTapSpike <pid> <seconds> <out.wav>
// Захватывает системный звук ТОЛЬКО указанного процесса в WAV.
// По образцу insidegui/AudioCap (упрощено до спайка).

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(("FAIL: " + msg + "\n").data(using: .utf8)!)
    exit(1)
}

guard CommandLine.arguments.count == 4,
      let pid = pid_t(CommandLine.arguments[1]),
      let seconds = Double(CommandLine.arguments[2])
else { fail("usage: ProcessTapSpike <pid> <seconds> <out.wav>") }
let outURL = URL(fileURLWithPath: CommandLine.arguments[3])

// 1. pid -> AudioObjectID процесса
var processObject = AudioObjectID(kAudioObjectUnknown)
var pidValue = pid
var addr = AudioObjectPropertyAddress(
    mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
var size = UInt32(MemoryLayout<AudioObjectID>.size)
var err = withUnsafeMutablePointer(to: &pidValue) { pidPtr in
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                               UInt32(MemoryLayout<pid_t>.size), pidPtr,
                               &size, &processObject)
}
guard err == noErr, processObject != kAudioObjectUnknown else {
    fail("translate pid -> process object: err=\(err)")
}
print("OK process object: \(processObject)")

// 2. Создать process tap
let tapDesc = CATapDescription(stereoMixdownOfProcesses: [processObject])
tapDesc.isPrivate = true
tapDesc.muteBehavior = .unmuted
var tapID = AudioObjectID(kAudioObjectUnknown)
err = AudioHardwareCreateProcessTap(tapDesc, &tapID)
guard err == noErr else {
    fail("AudioHardwareCreateProcessTap: err=\(err) (нет TCC 'System Audio Recording'? запусти из GUI-терминала)")
}
print("OK tap created: \(tapID)")

// 3. Формат тапа
var fmtAddr = AudioObjectPropertyAddress(
    mSelector: kAudioTapPropertyFormat,
    mScope: kAudioObjectPropertyScopeGlobal,
    mElement: kAudioObjectPropertyElementMain)
var asbd = AudioStreamBasicDescription()
size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
err = AudioObjectGetPropertyData(tapID, &fmtAddr, 0, nil, &size, &asbd)
guard err == noErr else { fail("read tap format: err=\(err)") }
print("OK tap format: \(asbd.mSampleRate) Hz, \(asbd.mChannelsPerFrame) ch")

// 4. Приватный aggregate device с тапом
let aggUID = UUID().uuidString
let aggDesc: [String: Any] = [
    kAudioAggregateDeviceNameKey: "MeetMindSpikeAgg",
    kAudioAggregateDeviceUIDKey: aggUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [] as [[String: Any]],
    kAudioAggregateDeviceTapListKey: [
        [kAudioSubTapUIDKey: tapDesc.uuid.uuidString,
         kAudioSubTapDriftCompensationKey: true]
    ],
]
var aggID = AudioObjectID(kAudioObjectUnknown)
err = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
guard err == noErr else { fail("AudioHardwareCreateAggregateDevice: err=\(err)") }
print("OK aggregate device: \(aggID)")

// 5. IOProc: пишем в WAV
guard let format = AVAudioFormat(streamDescription: &asbd) else { fail("AVAudioFormat") }
let file: AVAudioFile
do {
    file = try AVAudioFile(forWriting: outURL, settings: format.settings,
                           commonFormat: .pcmFormatFloat32,
                           interleaved: format.isInterleaved)
} catch { fail("open wav: \(error)") }

var framesWritten: UInt64 = 0
var ioProcID: AudioDeviceIOProcID?
err = AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggID, nil) {
    _, inInputData, _, _, _ in
    guard let buf = AVAudioPCMBuffer(pcmFormat: format,
                                     bufferListNoCopy: inInputData,
                                     deallocator: nil) else { return }
    do {
        try file.write(from: buf)
        framesWritten += UInt64(buf.frameLength)
    } catch {}
}
guard err == noErr, let procID = ioProcID else { fail("create IOProc: err=\(err)") }
err = AudioDeviceStart(aggID, procID)
guard err == noErr else { fail("AudioDeviceStart: err=\(err)") }
print("recording \(seconds)s ...")
Thread.sleep(forTimeInterval: seconds)
AudioDeviceStop(aggID, procID)
AudioDeviceDestroyIOProcID(aggID, procID)
AudioHardwareDestroyAggregateDevice(aggID)
AudioHardwareDestroyProcessTap(tapID)
file.close()  // финализирует WAV-header; exit() минует deinit
print("OK wrote \(framesWritten) frames -> \(outURL.path)")
exit(framesWritten > 0 ? 0 : 2)
