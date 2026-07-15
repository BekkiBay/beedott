// CaptureEngine.swift — нативный захват аудио на Core Audio Process Taps (PLAN_v3 Task 4.1-4.2).
//
// Перенос/обобщение spikes/ProcessTapSpike/main.swift (проверено записью, см.
// spikes/SPIKE_RESULTS.md — GO). Отличия от спайка:
//   - тап не только на конкретный PID (M5 — конкретное приложение-звонилка), но и
//     "весь системный звук" (.global, CATapDescription(stereoGlobalTapButExcludeProcesses:) —
//     реальное Swift-имя рефайнед-инициализатора; в ТЗ фигурировало под именем
//     stereoGlobalTapButExcludingProcesses, компилятор подсказал точный вариант)
//     — годится для ручных встреч, когда PID звонилки ещё не задетектирован;
//   - к тапу добавлен sub-device (default-микрофон) в том же aggregate, поэтому
//     ОДИН IOProc получает и микрофон, и системный звук одним колбэком.
//
// Формат буферов на выходе aggregate — ПОДТВЕРЖДЕНО ЭМПИРИЧЕСКИ (headless прогон
// --capture-test 5 /tmp/cap_test.wav при afplay на фоне, встроенный микрофон
// MacBook, см. финальный отчёт по Task 4.1-4.2):
//
//   CaptureEngine: aggregate IOProc buffer shape — count=2
//     [buf[0]: ch=1 bytes=2048 framesEst=512; buf[1]: ch=2 bytes=4096 framesEst=512;]
//     micCh=1 tapCh=2
//
// AudioBufferList приходит с двумя буферами — buffer[0] соответствует sub-device'у
// (микрофон, 1 канал на этой машине), buffer[1] соответствует тапу (системный звук,
// 2 канала interleaved Float32, 48kHz). Порядок соответствует порядку объявления в
// kAudioAggregateDeviceSubDeviceListKey (сначала) / kAudioAggregateDeviceTapListKey
// (потом). Код НЕ полагается жёстко на этот порядок: каждый буфер сопоставляется
// с ролью по числу каналов (micChannelCount/tapChannelCount, прочитаны заранее из
// самого устройства/тапа), позиционный порядок — только fallback, если сопоставление
// по каналам неоднозначно. Каждый буфер даунмиксится в моно суммированием каналов
// (если их >1), поэтому переживёт устройство с другим числом каналов (например,
// стерео AirPods-микрофон).
//
// Итоговый WAV: стерео 48kHz, PCM16, ch0 = микс микрофона, ch1 = микс тапа
// (системный звук). Этот порядок каналов ОБРАТНЫЙ по отношению к
// SPEC_v3 §3 ("ch0=система, ch1=мик") и PLAN_v3 Task 4.1
// ("Process Tap → ch0; микрофон → ch1"); реализовано по явному ТЗ этой задачи.
// См. финальный отчёт агента — расхождение зафиксировано, требуется решение на
// уровне SPEC/PLAN (поменять документ или инвертировать каналы здесь) до M7
// (диаризация в M7 Task 7.1 ожидает ch1=мик).

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

enum CaptureMode {
    case global
    case pid(pid_t)
}

enum CaptureError: Error, LocalizedError {
    case tapUnavailable(String)
    case aggregateUnavailable(String)
    case ioProcUnavailable(String)
    case fileUnavailable(String)
    case micUnavailable(String)
    case alreadyRunning
    case notRunning

    var errorDescription: String? {
        switch self {
        case .tapUnavailable(let s): return "Process Tap недоступен: \(s)"
        case .aggregateUnavailable(let s): return "aggregate device: \(s)"
        case .ioProcUnavailable(let s): return "IOProc: \(s)"
        case .fileUnavailable(let s): return "выходной файл: \(s)"
        case .micUnavailable(let s): return "микрофон: \(s)"
        case .alreadyRunning: return "CaptureEngine уже пишет"
        case .notRunning: return "CaptureEngine не пишет"
        }
    }
}

/// Движок записи на Core Audio Process Taps: системный звук (весь / конкретный PID)
/// + микрофон в одном aggregate-устройстве → один IOProc → стерео WAV.
///
/// Не потокобезопасен для конкурентных start()/stop() — вызывающий (AppModel)
/// сериализует действия через свою actionQueue.
final class CaptureEngine {
    static let sampleRate: Double = 48000

    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var file: AVAudioFile?
    private var outputFormat: AVAudioFormat?
    private var framesWritten: UInt64 = 0
    private var isRunning = false
    private var loggedBufferShape = false

    // Роль канала внутри AudioBufferList — определяется по числу каналов буфера
    // относительно известного числа каналов sub-device'а (микрофон) и тапа.
    private var micChannelCount: UInt32 = 1
    private var tapChannelCount: UInt32 = 2

    // Живые уровни для дропдауна (экран 02): пишутся IOProc-потоком в
    // handleInput, читаются UI-таймером через levels(). NSLock — самый простой
    // корректный вариант для пары Float между двумя потоками.
    private let levelLock = NSLock()
    private var micPeak: Float32 = 0
    private var tapPeak: Float32 = 0

    /// Текущие пики (0…1) микрофона и системного звука; после чтения затухают
    /// сами по мере поступления новых буферов.
    func levels() -> (mic: Float, system: Float) {
        levelLock.lock()
        defer { levelLock.unlock() }
        return (mic: min(1, micPeak), system: min(1, tapPeak))
    }

    private func updateLevels(mic: [Float32], tap: [Float32]) {
        func peak(_ samples: [Float32]) -> Float32 {
            var p: Float32 = 0
            for s in samples { p = max(p, abs(s)) }
            return p
        }
        let m = peak(mic), t = peak(tap)
        levelLock.lock()
        // мгновенная атака, медленное затухание — как в аппаратных VU-метрах
        micPeak = max(m, micPeak * 0.85)
        tapPeak = max(t, tapPeak * 0.85)
        levelLock.unlock()
    }

    deinit {
        if isRunning { _ = stop() }
    }

    // ------------------------------------------------------------- start/stop

    func start(mode: CaptureMode, outURL: URL) throws {
        guard !isRunning else { throw CaptureError.alreadyRunning }

        let tapDescription = try Self.makeTapDescription(mode: mode)
        tapDescription.isPrivate = true
        tapDescription.muteBehavior = .unmuted

        var newTapID = AudioObjectID(kAudioObjectUnknown)
        var err = AudioHardwareCreateProcessTap(tapDescription, &newTapID)
        guard err == noErr else {
            throw CaptureError.tapUnavailable(
                "AudioHardwareCreateProcessTap err=\(err) (нет TCC 'System Audio Recording'? "
                + "нужен headless-запуск из терминала с разрешением)")
        }
        tapID = newTapID

        do {
            let tapASBD = try Self.readTapFormat(tapID: tapID)
            tapChannelCount = max(1, tapASBD.mChannelsPerFrame)

            guard let micUID = Self.defaultInputDeviceUID() else {
                throw CaptureError.micUnavailable("нет default input device / UID не прочитан")
            }
            micChannelCount = Self.inputChannelCount(forUID: micUID) ?? 1

            let aggUID = "com.jvmshid.meetmind.capture." + UUID().uuidString
            let aggDesc: [String: Any] = [
                kAudioAggregateDeviceNameKey: "MeetMind Capture",
                kAudioAggregateDeviceUIDKey: aggUID,
                kAudioAggregateDeviceIsPrivateKey: true,
                kAudioAggregateDeviceTapAutoStartKey: true,
                kAudioAggregateDeviceSubDeviceListKey: [
                    [kAudioSubDeviceUIDKey: micUID,
                     kAudioSubDeviceDriftCompensationKey: true],
                ] as [[String: Any]],
                kAudioAggregateDeviceTapListKey: [
                    [kAudioSubTapUIDKey: tapDescription.uuid.uuidString,
                     kAudioSubTapDriftCompensationKey: true],
                ] as [[String: Any]],
            ]
            var newAggID = AudioObjectID(kAudioObjectUnknown)
            err = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &newAggID)
            guard err == noErr else {
                throw CaptureError.aggregateUnavailable("AudioHardwareCreateAggregateDevice err=\(err)")
            }
            aggID = newAggID

            guard let outFormat = AVAudioFormat(
                commonFormat: .pcmFormatInt16, sampleRate: Self.sampleRate,
                channels: 2, interleaved: true)
            else { throw CaptureError.fileUnavailable("не удалось построить выходной AVAudioFormat") }
            outputFormat = outFormat

            let newFile: AVAudioFile
            do {
                newFile = try AVAudioFile(
                    forWriting: outURL, settings: outFormat.settings,
                    commonFormat: .pcmFormatInt16, interleaved: true)
            } catch {
                throw CaptureError.fileUnavailable("\(error)")
            }
            file = newFile
            framesWritten = 0
            loggedBufferShape = false

            var newIOProcID: AudioDeviceIOProcID?
            err = AudioDeviceCreateIOProcIDWithBlock(&newIOProcID, aggID, nil) {
                [weak self] _, inInputData, _, _, _ in
                self?.handleInput(inInputData)
            }
            guard err == noErr, let procID = newIOProcID else {
                throw CaptureError.ioProcUnavailable("AudioDeviceCreateIOProcIDWithBlock err=\(err)")
            }
            ioProcID = procID

            err = AudioDeviceStart(aggID, procID)
            guard err == noErr else {
                throw CaptureError.ioProcUnavailable("AudioDeviceStart err=\(err)")
            }

            isRunning = true
        } catch {
            teardown()
            throw error
        }
    }

    /// Останавливает захват, финализирует WAV, чистит tap/aggregate/IOProc.
    /// Возвращает количество записанных кадров (0, если запись не шла).
    @discardableResult
    func stop() -> UInt64 {
        guard isRunning else { return framesWritten }
        let frames = framesWritten
        teardown()
        return frames
    }

    private func teardown() {
        if let procID = ioProcID, aggID != kAudioObjectUnknown {
            AudioDeviceStop(aggID, procID)
            AudioDeviceDestroyIOProcID(aggID, procID)
        }
        ioProcID = nil
        if aggID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggID)
            aggID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
        // Грабля спайка (SPIKE_RESULTS.md): AVAudioFile нужно явно закрыть перед
        // exit()/деаллокацией, иначе WAV-header не финализируется (0 фреймов при
        // чтении) — exit() обрывает процесс без прогона деинициализаторов.
        // AVAudioFile.close() появился только в macOS 15 (деплой-таргет здесь —
        // 14.4, см. app/build.sh); на 15+ используем его явно, на более старых
        // системах последняя strong-ссылка (file = nil) синхронно (ARC, не GC)
        // деаллоцирует AVAudioFile ДО следующей строки — это выполняется здесь,
        // а не в exit()-пути, поэтому финализация header гарантированно происходит
        // раньше вызова exit() в --capture-test/AppModel.
        if #available(macOS 15.0, *) {
            file?.close()
        }
        file = nil
        isRunning = false
    }

    // ------------------------------------------------------------------- IO

    /// Вызывается на realtime-потоке HAL. Не должен блокироваться/аллоцировать
    /// сверх необходимого; AVAudioFile.write делает синхронный disk I/O — как и в
    /// спайке (ProcessTapSpike/main.swift) это принято как компромисс для M4
    /// (headless-CLI и ручных записей; см. daemon/recorder.py про realtime-safety
    /// на Python-стороне, где это уже вынесено в отдельный writer-поток — для
    /// Swift-тапа тот же паттерн можно добавить отдельной задачей, если появятся
    /// glitches/overrun в проде).
    private func handleInput(_ bufferList: UnsafePointer<AudioBufferList>) {
        guard isRunning, let file, let outputFormat else { return }

        let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: bufferList))
        if !loggedBufferShape {
            loggedBufferShape = true
            var shape = "CaptureEngine: aggregate IOProc buffer shape — count=\(abl.count) ["
            for (i, buf) in abl.enumerated() {
                let frameEstimate = buf.mNumberChannels > 0
                    ? buf.mDataByteSize / (buf.mNumberChannels * UInt32(MemoryLayout<Float32>.size))
                    : 0
                shape += "buf[\(i)]: ch=\(buf.mNumberChannels) bytes=\(buf.mDataByteSize) framesEst=\(frameEstimate); "
            }
            shape += "] micCh=\(micChannelCount) tapCh=\(tapChannelCount)"
            NSLog("%@", shape)
        }

        // Роль буфера определяем по совпадению числа каналов с известным числом
        // каналов источника. Если оба совпадают неоднозначно (напр. оба стерео),
        // используем порядок объявления в aggregate description: sub-devices
        // (микрофон) идут раньше tap-ов — см. комментарий в шапке файла.
        var micBuffers: [AudioBuffer] = []
        var tapBuffers: [AudioBuffer] = []
        var unassigned: [AudioBuffer] = []
        for buf in abl {
            if buf.mNumberChannels == micChannelCount && micBuffers.isEmpty && tapBuffers.isEmpty {
                micBuffers.append(buf)
            } else if buf.mNumberChannels == tapChannelCount && !micBuffers.isEmpty {
                tapBuffers.append(buf)
            } else {
                unassigned.append(buf)
            }
        }
        // Фолбэк по позиции: 2 буфера и роли не распределились по каналам —
        // buf[0]=микрофон (sub-device), buf[1]=тап (tap), порядок объявления
        // в kAudioAggregateDeviceSubDeviceListKey/TapListKey.
        if micBuffers.isEmpty && tapBuffers.isEmpty && abl.count >= 2 {
            micBuffers = [abl[0]]
            tapBuffers = [abl[1]]
            unassigned = abl.count > 2 ? Array(abl[2...]) : []
        } else if micBuffers.isEmpty && tapBuffers.isEmpty && abl.count == 1 {
            // Только один поток данных (напр. тап без реального мик-сигнала) —
            // трактуем как тап, микрофон молчит.
            tapBuffers = [abl[0]]
        }
        _ = unassigned // намеренно не пишем — неопознанные потоки игнорируем, не роняем захват

        let micMono = Self.downmixToMono(micBuffers)
        let tapMono = Self.downmixToMono(tapBuffers)
        updateLevels(mic: micMono, tap: tapMono)
        let frameCount = max(micMono.count, tapMono.count)
        guard frameCount > 0 else { return }

        guard let pcmBuffer = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: AVAudioFrameCount(frameCount))
        else { return }
        pcmBuffer.frameLength = AVAudioFrameCount(frameCount)
        guard let ch = pcmBuffer.int16ChannelData else { return }
        // interleaved int16, 2 каналов: [ch0_mic, ch1_tap, ch0_mic, ch1_tap, ...]
        let interleaved = ch[0]
        for i in 0..<frameCount {
            let micSample = i < micMono.count ? micMono[i] : 0
            let tapSample = i < tapMono.count ? tapMono[i] : 0
            interleaved[i * 2] = Self.floatToInt16(micSample)
            interleaved[i * 2 + 1] = Self.floatToInt16(tapSample)
        }

        do {
            try file.write(from: pcmBuffer)
            framesWritten += UInt64(frameCount)
        } catch {
            NSLog("CaptureEngine: write() failed: %@", "\(error)")
        }
    }

    /// Даунмикс всех переданных буферов (каждый может быть многоканальным
    /// interleaved Float32) в один моно Float32-массив суммированием каналов
    /// попарно (все каналы буфера складываются в одну дорожку).
    private static func downmixToMono(_ buffers: [AudioBuffer]) -> [Float32] {
        guard !buffers.isEmpty else { return [] }
        var result: [Float32] = []
        for buf in buffers {
            guard let data = buf.mData else { continue }
            let channels = max(1, Int(buf.mNumberChannels))
            let totalFloats = Int(buf.mDataByteSize) / MemoryLayout<Float32>.size
            let frames = totalFloats / channels
            let ptr = data.assumingMemoryBound(to: Float32.self)
            var mono = [Float32](repeating: 0, count: frames)
            for f in 0..<frames {
                var sum: Float32 = 0
                for c in 0..<channels {
                    sum += ptr[f * channels + c]
                }
                mono[f] = sum
            }
            if result.isEmpty {
                result = mono
            } else {
                // несколько буферов одной роли (напр. неск. тапов) — суммируем поверх
                let n = max(result.count, mono.count)
                if result.count < n { result.append(contentsOf: repeatElement(0, count: n - result.count)) }
                for i in 0..<mono.count { result[i] += mono[i] }
            }
        }
        return result
    }

    private static func floatToInt16(_ sample: Float32) -> Int16 {
        let clamped = max(-1.0, min(1.0, sample))
        return Int16(clamped * Float32(Int16.max))
    }

    // -------------------------------------------------------- CoreAudio helpers

    private static func makeTapDescription(mode: CaptureMode) throws -> CATapDescription {
        switch mode {
        case .global:
            return CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        case .pid(let pid):
            var processObject = AudioObjectID(kAudioObjectUnknown)
            var pidValue = pid
            var addr = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            var size = UInt32(MemoryLayout<AudioObjectID>.size)
            let err = withUnsafeMutablePointer(to: &pidValue) { pidPtr in
                AudioObjectGetPropertyData(
                    AudioObjectID(kAudioObjectSystemObject), &addr,
                    UInt32(MemoryLayout<pid_t>.size), pidPtr, &size, &processObject)
            }
            guard err == noErr, processObject != kAudioObjectUnknown else {
                throw CaptureError.tapUnavailable("translate pid \(pid) -> process object: err=\(err)")
            }
            return CATapDescription(stereoMixdownOfProcesses: [processObject])
        }
    }

    private static func readTapFormat(tapID: AudioObjectID) throws -> AudioStreamBasicDescription {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let err = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &asbd)
        guard err == noErr else {
            throw CaptureError.tapUnavailable("kAudioTapPropertyFormat err=\(err)")
        }
        return asbd
    }

    private static func defaultInputDeviceID() -> AudioObjectID? {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var deviceID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let err = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID)
        guard err == noErr, deviceID != kAudioObjectUnknown else { return nil }
        return deviceID
    }

    private static func deviceUID(_ deviceID: AudioObjectID) -> String? {
        var uidAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var uid: CFString = "" as CFString
        var uidSize = UInt32(MemoryLayout<CFString>.size)
        let err = withUnsafeMutablePointer(to: &uid) { ptr in
            AudioObjectGetPropertyData(deviceID, &uidAddr, 0, nil, &uidSize, ptr)
        }
        guard err == noErr else { return nil }
        return uid as String
    }

    private static func defaultInputDeviceUID() -> String? {
        guard let deviceID = defaultInputDeviceID() else { return nil }
        return deviceUID(deviceID)
    }

    /// Число входных каналов устройства по его UID (для сопоставления ролей буферов).
    /// UID передаётся как qualifier kAudioHardwarePropertyTranslateUIDToDevice —
    /// AudioObjectID приходит обратно как обычные данные свойства (не через
    /// устаревший AudioValueTranslation).
    private static func inputChannelCount(forUID uid: String) -> UInt32? {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslateUIDToDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var cfUID = uid as CFString
        var deviceID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let err = withUnsafeMutablePointer(to: &cfUID) { uidPtr -> OSStatus in
            AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject), &addr,
                UInt32(MemoryLayout<CFString>.size), uidPtr, &size, &deviceID)
        }
        guard err == noErr, deviceID != kAudioObjectUnknown else { return nil }

        var streamAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var dataSize: UInt32 = 0
        var streamErr = AudioObjectGetPropertyDataSize(deviceID, &streamAddr, 0, nil, &dataSize)
        guard streamErr == noErr, dataSize > 0 else { return nil }
        let ablPtr = UnsafeMutableRawPointer.allocate(byteCount: Int(dataSize), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { ablPtr.deallocate() }
        streamErr = AudioObjectGetPropertyData(deviceID, &streamAddr, 0, nil, &dataSize, ablPtr)
        guard streamErr == noErr else { return nil }
        let list = ablPtr.assumingMemoryBound(to: AudioBufferList.self)
        let buffers = UnsafeMutableAudioBufferListPointer(list)
        var total: UInt32 = 0
        for buf in buffers { total += buf.mNumberChannels }
        return total > 0 ? total : nil
    }
}
