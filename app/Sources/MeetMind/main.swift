// main.swift — entry point. Top-level код (требуется для --capture-test,
// PLAN_v3 Task 4.1-4.2 п.4) допустим ТОЛЬКО в файле с этим именем, поэтому
// build.sh собирает без -parse-as-library (см. комментарий там) и @main
// перенесён из MeetMindApp.swift сюда же — если тут добавить top-level код
// одновременно с -parse-as-library, компиляция падает ("statements are not
// allowed at the top level"), проверено эмпирически при написании этого файла.
//
// --capture-test <seconds> <out.wav> — headless-проверка CaptureEngine без GUI:
// пишет <seconds> секунд global-тапа (весь системный звук) + микрофон в <out.wav>,
// печатает число записанных кадров, exit(0) если кадров > 0, иначе exit(1).
// Используется в проверке M4 (см. отчёт по Task 4.1-4.2) — GUI НЕ поднимается.

import AppKit
import Foundation

func runCaptureTest(seconds: Double, outPath: String) -> Int32 {
    let outURL = URL(fileURLWithPath: outPath)
    let engine = CaptureEngine()
    do {
        try engine.start(mode: .global, outURL: outURL)
    } catch {
        FileHandle.standardError.write(
            "capture-test: start failed: \(error)\n".data(using: .utf8)!)
        return 1
    }
    print("capture-test: recording \(seconds)s -> \(outPath)")
    Thread.sleep(forTimeInterval: seconds)
    let frames = engine.stop()
    print("capture-test: wrote \(frames) frames -> \(outPath)")
    return frames > 0 ? 0 : 1
}

let cliArgs = CommandLine.arguments
if let flagIdx = cliArgs.firstIndex(of: "--capture-test") {
    guard cliArgs.count > flagIdx + 2, let seconds = Double(cliArgs[flagIdx + 1]) else {
        FileHandle.standardError.write(
            "usage: MeetMind --capture-test <seconds> <out.wav>\n".data(using: .utf8)!)
        exit(1)
    }
    let outPath = cliArgs[flagIdx + 2]
    exit(runCaptureTest(seconds: seconds, outPath: outPath))
}

MeetMindApp.main()
