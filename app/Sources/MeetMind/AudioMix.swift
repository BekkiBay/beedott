// AudioMix.swift — сборка стерео-кадров для WAV из двух моно-потоков
// (ch0 = микрофон, ch1 = системный звук). Чистая функция — тесты в app/Tests.
//
// micMuted: ручной mute (кнопка в островке / меню, ⌃⌥⌘M) — вместо микрофона
// пишется тишина, системный звук (собеседники) идёт как есть. Твои слова за
// это время не попадают ни в запись, ни в транскрипт.

enum AudioMix {
    static func interleave(mic: [Float32], tap: [Float32], micMuted: Bool) -> [Int16] {
        let frames = max(mic.count, tap.count)
        var out = [Int16](repeating: 0, count: frames * 2)
        for i in 0..<frames {
            if !micMuted, i < mic.count { out[i * 2] = toInt16(mic[i]) }
            if i < tap.count { out[i * 2 + 1] = toInt16(tap[i]) }
        }
        return out
    }

    static func toInt16(_ sample: Float32) -> Int16 {
        let clamped = max(-1.0, min(1.0, sample))
        return Int16(clamped * Float32(Int16.max))
    }
}
