// Tests/main.swift — тесты чистой логики автодетекта звонков.
// Запуск: app/Tests/run.sh (swiftc собирает логику + тесты в исполняемый файл).

import Foundation

var failures = 0
func check(_ cond: @autoclosure () -> Bool, _ msg: String, line: Int = #line) {
    if !cond() { failures += 1; print("FAIL line \(line): \(msg)") }
}

let t0 = Date(timeIntervalSince1970: 1_000_000)
func at(_ s: TimeInterval) -> Date { t0.addingTimeInterval(s) }
let talking = BrowserAudio(input: true, output: true)
let silent = BrowserAudio(input: false, output: false)
let listening = BrowserAudio(input: false, output: true)

// 1. Микрофон браузера дольше startDelay + встреча с Meet в календаре → автостарт, один раз.
do {
    var m = CallDetectionMachine()
    check(m.step(talking, now: at(0), isRecording: false, calendarHasMeet: true) == .none, "сразу не стартуем")
    check(m.step(talking, now: at(5), isRecording: false, calendarHasMeet: true) == .none, "5 с — рано")
    check(m.step(talking, now: at(9), isRecording: false, calendarHasMeet: true) == .autoStart, "8+ с — автостарт")
    check(m.step(talking, now: at(12), isRecording: false, calendarHasMeet: true) == .none, "второй раз не стартуем")
}

// 2. Без встречи в календаре — не пишем сами, а спрашиваем.
do {
    var m = CallDetectionMachine()
    _ = m.step(talking, now: at(0), isRecording: false, calendarHasMeet: false)
    check(m.step(talking, now: at(9), isRecording: false, calendarHasMeet: false) == .askToRecord, "спросить")
    check(m.step(talking, now: at(20), isRecording: false, calendarHasMeet: false) == .none, "спросить один раз")
}

// 3. Автозапись останавливается, когда браузер замолчал полностью на endQuiet.
do {
    var m = CallDetectionMachine()
    _ = m.step(talking, now: at(0), isRecording: false, calendarHasMeet: true)
    _ = m.step(talking, now: at(9), isRecording: false, calendarHasMeet: true)
    m.markAutoRecording()
    check(m.step(talking, now: at(100), isRecording: true, calendarHasMeet: true) == .none, "идёт звонок")
    check(m.step(silent, now: at(200), isRecording: true, calendarHasMeet: true) == .none, "тишина только началась")
    check(m.step(silent, now: at(230), isRecording: true, calendarHasMeet: true) == .none, "30 с тишины — рано")
    check(m.step(silent, now: at(261), isRecording: true, calendarHasMeet: true) == .stop, "60+ с тишины — стоп")
}

// 4. Короткий разрыв (сменили наушники) не останавливает запись.
do {
    var m = CallDetectionMachine()
    m.markAutoRecording()
    _ = m.step(talking, now: at(0), isRecording: true, calendarHasMeet: true)
    _ = m.step(silent, now: at(10), isRecording: true, calendarHasMeet: true)
    check(m.step(silent, now: at(40), isRecording: true, calendarHasMeet: true) == .none, "30 с")
    check(m.step(talking, now: at(45), isRecording: true, calendarHasMeet: true) == .none, "звук вернулся")
    check(m.step(silent, now: at(90), isRecording: true, calendarHasMeet: true) == .none, "отсчёт заново")
}

// 5. Микрофон выключен (mute), но звук собеседников идёт — не стоп до endNoInput.
do {
    var m = CallDetectionMachine()
    m.markAutoRecording()
    _ = m.step(talking, now: at(0), isRecording: true, calendarHasMeet: true)
    check(m.step(listening, now: at(10), isRecording: true, calendarHasMeet: true) == .none, "mute")
    check(m.step(listening, now: at(600), isRecording: true, calendarHasMeet: true) == .none, "10 мин mute")
    check(m.step(listening, now: at(911), isRecording: true, calendarHasMeet: true) == .stop,
          "15 мин без микрофона — звонок, скорее всего, кончился, играет что-то другое")
}

// 6. Ручную запись детектор не трогает.
do {
    var m = CallDetectionMachine()
    _ = m.step(silent, now: at(0), isRecording: true, calendarHasMeet: false)
    check(m.step(silent, now: at(1000), isRecording: true, calendarHasMeet: false) == .none, "ручная запись")
}

// 7. Остановил автозапись вручную посреди звонка — повторно не стартуем,
//    пока звонок не кончится; следующий звонок — снова автостарт.
do {
    var m = CallDetectionMachine()
    _ = m.step(talking, now: at(0), isRecording: false, calendarHasMeet: true)
    _ = m.step(talking, now: at(9), isRecording: false, calendarHasMeet: true)
    m.markAutoRecording()
    _ = m.step(talking, now: at(20), isRecording: true, calendarHasMeet: true)
    check(m.step(talking, now: at(30), isRecording: false, calendarHasMeet: true) == .none, "сам остановил")
    check(m.step(talking, now: at(60), isRecording: false, calendarHasMeet: true) == .none, "звонок идёт — молчим")
    _ = m.step(silent, now: at(100), isRecording: false, calendarHasMeet: true)
    _ = m.step(silent, now: at(161), isRecording: false, calendarHasMeet: true)  // звонок кончился
    _ = m.step(talking, now: at(300), isRecording: false, calendarHasMeet: true)
    check(m.step(talking, now: at(309), isRecording: false, calendarHasMeet: true) == .autoStart, "новый звонок")
}

// 7б. Автозапись остановилась «без микрофона 15 мин», браузер дальше играет
//     звук — следующий звонок всё равно ловим.
do {
    var m = CallDetectionMachine()
    m.markAutoRecording()
    _ = m.step(talking, now: at(0), isRecording: true, calendarHasMeet: true)
    _ = m.step(listening, now: at(10), isRecording: true, calendarHasMeet: true)
    check(m.step(listening, now: at(911), isRecording: true, calendarHasMeet: true) == .stop, "стоп")
    _ = m.step(listening, now: at(920), isRecording: false, calendarHasMeet: true)
    _ = m.step(talking, now: at(1000), isRecording: false, calendarHasMeet: true)
    check(m.step(talking, now: at(1009), isRecording: false, calendarHasMeet: true) == .autoStart,
          "новый звонок поверх играющего звука")
}

// 8. Какие процессы считаются браузером.
check(BrowserAudio.browserFamily(bundleID: "org.mozilla.firefox", safariRunning: false) == "firefox", "firefox")
check(BrowserAudio.browserFamily(bundleID: "com.google.Chrome.helper", safariRunning: false) == "chrome", "chrome helper")
check(BrowserAudio.browserFamily(bundleID: "com.apple.WebKit.GPU", safariRunning: true) == "safari", "safari")
check(BrowserAudio.browserFamily(bundleID: "com.apple.WebKit.GPU", safariRunning: false) == nil, "webkit без safari")
check(BrowserAudio.browserFamily(bundleID: "ru.keepcoder.Telegram", safariRunning: true) == nil, "не браузер")

// 9. Ссылка на Meet в событии календаря.
check(CallDetectionMachine.meetCode(in: "Присоединиться: https://meet.google.com/abc-defg-hij?authuser=0") == "abc-defg-hij", "код Meet")
check(CallDetectionMachine.meetCode(in: "https://meet.google.com/landing") == nil, "не встреча")
check(CallDetectionMachine.meetCode(in: nil) == nil, "пусто")

// ───────── Островок у чёлки: какую встречу напомнить ─────────

func event(_ id: String, startsIn: TimeInterval, minutes: TimeInterval = 30, meet: String? = nil) -> CalendarEventInfo {
    CalendarEventInfo(id: id, title: "Встреча \(id)", start: at(startsIn),
                      end: at(startsIn + minutes * 60), meetCode: meet)
}

// 10. Окно напоминания: за 60 с до начала и до 5 минут после.
do {
    var p = UpcomingMeetingPicker()
    check(p.pick([event("a", startsIn: 90)], now: at(0), isRecording: false) == nil, "за 90 с — рано")
    check(p.pick([event("a", startsIn: 90)], now: at(31), isRecording: false)?.id == "a", "за 59 с — показать")
    check(p.pick([event("a", startsIn: 90)], now: at(90 + 240), isRecording: false)?.id == "a", "4 мин после начала — ещё висит")
    check(p.pick([event("a", startsIn: 90)], now: at(90 + 301), isRecording: false) == nil, "5+ мин — спрятать")
}

// 11. ✕ / «Записать» — больше не показываем эту встречу.
do {
    var p = UpcomingMeetingPicker()
    let e = [event("a", startsIn: 30)]
    check(p.pick(e, now: at(0), isRecording: false)?.id == "a", "показ")
    p.dismiss("a")
    check(p.pick(e, now: at(1), isRecording: false) == nil, "закрыли")
}

// 12. Во время записи не показываем; встреча считается обработанной.
do {
    var p = UpcomingMeetingPicker()
    let e = [event("a", startsIn: 30)]
    check(p.pick(e, now: at(0), isRecording: true) == nil, "идёт запись")
    check(p.pick(e, now: at(10), isRecording: false) == nil, "запись кончилась — не всплываем снова")
}

// 13. Несколько встреч — ближайшая по началу.
do {
    var p = UpcomingMeetingPicker()
    let e = [event("late", startsIn: 50), event("early", startsIn: -60), event("far", startsIn: 3600)]
    check(p.pick(e, now: at(0), isRecording: false)?.id == "early", "ближайшая")
}

// ───────── Островок: рамка окна и зона мыши ─────────

// 14. Экран с чёлкой: окно фиксированного размера по центру, прижато к верху;
//     «шея» островка ровно под чёлкой, карточка — ниже строки меню.
do {
    let screen = CGRect(x: 0, y: 0, width: 1512, height: 982)
    let layout = IslandLayout(screen: screen, notchLeftMaxX: 663.5, notchRightMinX: 848.5,
                              topInset: 32, menuBarHeight: 32)
    check(layout.hasNotch, "чёлка есть")
    check(layout.notchWidth == 185 && layout.barHeight == 32, "размер чёлки")
    let panel = layout.panelFrame()
    check(panel.midX == 756 && panel.maxY == 982, "окно по центру и у верхнего края")
    check(panel.width >= IslandLayout.maxBodyWidth && panel.height >= 32 + IslandLayout.maxBodyHeight,
          "окно вмещает самую большую карточку")
    let hit = layout.hitRect(bodyWidth: 400, bodyHeight: 46)
    check(hit.midX == 756 && hit.maxY == 982, "зона мыши — от верха экрана")
    check(hit.height == 32 + 46 && hit.width == 400, "зона мыши = шея + карточка")
    let hidden = layout.hitRect(bodyWidth: 185, bodyHeight: 0)
    check(hidden.height == 32 && hidden.width == 185, "свёрнутый — только чёлка")
}

// 15. Экран без чёлки: имитация чёлки той же ширины по центру сверху.
do {
    let screen = CGRect(x: 1512, y: -100, width: 1920, height: 1080)
    let layout = IslandLayout(screen: screen, notchLeftMaxX: nil, notchRightMinX: nil,
                              topInset: 0, menuBarHeight: 24)
    check(!layout.hasNotch, "чёлки нет")
    check(layout.notchWidth == IslandLayout.fakeNotchWidth && layout.barHeight == 28, "имитация")
    let panel = layout.panelFrame()
    check(panel.midX == 1512 + 960 && panel.maxY == 980, "окно сверху по центру")
}

// 15b. «Ушки» в строке меню (правка 2026-10-12): компакт записи и напоминание —
//      по бокам от чёлки, а не под ней. Окно и зона мыши их вмещают.
do {
    check(IslandEars.compact(elapsed: "24:13") == IslandEars.compactShort, "таймер до часа")
    check(IslandEars.compact(elapsed: "1:02:13") > IslandEars.compactShort, "час и больше — ушко шире")
    let short = IslandEars.upcoming(title: "Синк", startsText: "1 мин")
    let long = IslandEars.upcoming(title: String(repeating: "Очень длинная встреча ", count: 4),
                                   startsText: "1 мин")
    check(short < long && long == IslandEars.upcomingMax, "ушки напоминания — по названию, с потолком")
    check(short >= IslandEars.upcomingTrailing(startsText: "1 мин"), "кнопки справа всегда влезают")
    check(IslandEars.upcoming(title: "A", startsText: "идёт 12 мин")
          >= IslandEars.upcomingTrailing(startsText: "идёт 12 мин"), "и с длинным «идёт N мин»")
    check(short > IslandEars.compactLong, "напоминание шире компакта записи")
    let screen = CGRect(x: 0, y: 0, width: 1512, height: 982)
    let layout = IslandLayout(screen: screen, notchLeftMaxX: 663.5, notchRightMinX: 848.5,
                              topInset: 32, menuBarHeight: 32)
    check(layout.panelFrame().width >= 185 + 2 * IslandEars.upcomingMax, "окно вмещает напоминание")
    let ears = layout.hitRect(bodyWidth: 185, bodyHeight: 0, earWidth: 56)
    check(ears.width == 185 + 2 * 56 && ears.height == 32, "зона мыши = полоса с ушками")
    check(ears.midX == 756 && ears.maxY == 982, "полоса по центру у верхнего края")
    let card = layout.hitRect(bodyWidth: 420, bodyHeight: 84, earWidth: 0)
    check(card.width == 420 && card.height == 32 + 84, "раскрытие — шея и карточка")
}

// 16. Наведение с задержкой: раскрытие через 0,15 с, сворачивание через 0,35 с,
//     короткие выходы мыши не дёргают.
do {
    var h = HoverTracker()
    check(h.step(inside: true, now: at(0)) == false, "только навели")
    check(h.step(inside: true, now: at(0.2)) == true, "раскрылось")
    check(h.step(inside: false, now: at(0.3)) == true, "вышли на миг — держим")
    check(h.step(inside: true, now: at(0.4)) == true, "вернулись — держим")
    check(h.step(inside: false, now: at(0.5)) == true, "снова вышли")
    check(h.step(inside: false, now: at(0.8)) == true, "0,3 с — ещё держим")
    check(h.step(inside: false, now: at(0.9)) == false, "0,4 с — свернулось")
    check(h.step(inside: true, now: at(0.95)) == false, "мимолётное касание не раскрывает")
    check(h.step(inside: false, now: at(1.0)) == false, "ушли")
}

// ───────── Mute микрофона: в WAV вместо микрофона тишина ─────────

// 17. Без mute: каналы чередуются ch0 = мик, ch1 = система, float → int16.
do {
    let out = AudioMix.interleave(mic: [0.5, -0.5], tap: [0.25, 0], micMuted: false)
    check(out == [16383, 8191, -16383, 0], "чередование и масштаб: \(out)")
}

// 18. Mute: ch0 — тишина, системный звук пишется как есть.
do {
    let out = AudioMix.interleave(mic: [0.9, 0.9], tap: [0.5, -0.5], micMuted: true)
    check(out == [0, 16383, 0, -16383], "микрофон заглушён: \(out)")
}

// 19. Разная длина буферов — недостающее добивается тишиной, клиппинг ±1.
do {
    let out = AudioMix.interleave(mic: [2.0], tap: [0.0, -3.0], micMuted: false)
    check(out == [32767, 0, 0, -32767], "добивка и клиппинг: \(out)")
}

// 20. RuntimeLogic: события daemon.setup.
do {
    if case .progress(let stage, let done, let total)? =
        SetupEvent.parse(#"{"event": "progress", "stage": "model", "done": 5, "total": 10}"#) {
        check(stage == "model" && done == 5 && total == 10, "progress разобран")
    } else { check(false, "progress не разобран") }

    if case .ok(let r)? = SetupEvent.parse(#"{"event": "ok", "model": true, "summary": null, "telegram": false, "chat_id": 555, "bot": "b"}"#) {
        check(r.bool("model") && !r.bool("telegram"), "bool-поля")
        check(r.string("summary") == nil && r.string("bot") == "b", "string/null")
        check(r.int("chat_id") == 555, "int")
    } else { check(false, "ok не разобран") }

    if case .error(let code, let message)? = SetupEvent.parse(#"{"event": "error", "code": "no_network", "message": "x"}"#) {
        check(code == "no_network" && message == "x", "error разобран")
    } else { check(false, "error не разобран") }

    check(SetupEvent.parse("Downloading cpython-3.11") == nil, "не-JSON строка игнорируется")
    check(SetupEvent.parse(#"{"foo": 1}"#) == nil, "JSON без event игнорируется")
}

// 21. RuntimeLogic: режим запуска демона.
do {
    let repo = "/r"
    let uv = "/opt/homebrew/bin/uv"
    func mode(saved: String? = nil, env: String? = nil, derived: String = "/nope",
              uvs: [String] = ["/opt/homebrew/bin/uv"], repos: Set<String> = ["/r"],
              override: [String]? = nil) -> RuntimeMode {
        RuntimeLogic.resolveMode(savedRepo: saved, envRepo: env, derivedRepo: derived,
                                 uvCandidates: uvs, commandOverride: override,
                                 isRepo: { repos.contains($0) },
                                 isExecutable: { $0 == "/opt/homebrew/bin/uv" })
    }
    check(mode(saved: repo) == .dev(repo: repo, uv: uv, command: [uv, "run", "python", "main.py"]), "repoPath + uv → dev")
    check(mode(env: repo) == .dev(repo: repo, uv: uv, command: [uv, "run", "python", "main.py"]), "MEETMIND_REPO → dev")
    check(mode(derived: repo) == .dev(repo: repo, uv: uv, command: [uv, "run", "python", "main.py"]), "рядом со сборкой → dev")
    check(mode(saved: repo, uvs: []) == .bundled, "репо без uv → bundled")
    check(mode() == .bundled, "чистый Mac → bundled")
    check(mode(saved: repo, override: ["/x/python", "main.py"]) == .dev(repo: repo, uv: uv, command: ["/x/python", "main.py"]), "daemonCommand из настроек")
    check(mode(saved: repo, uvs: [], override: ["/opt/uv", "run", "python", "main.py"])
          == .dev(repo: repo, uv: "/opt/uv", command: ["/opt/uv", "run", "python", "main.py"]), "daemonCommand без найденного uv")
    let rows: [[String: Any]] = [["status": "running", "n": 1], ["status": "failed", "n": 3],
                                 ["status": "queued", "n": 2], ["status": "failed_retry", "n": 1]]
    check(RuntimeLogic.activeJobs(rows) == 3, "активные задачи без failed")
}

// 22. RuntimeLogic: транслокация, хэш, обновление рантайма, тексты ошибок.
do {
    check(RuntimeLogic.isTranslocated("/private/var/folders/x/T/AppTranslocation/ABC/d/BeeDott.app"), "транслокация")
    check(!RuntimeLogic.isTranslocated("/Applications/BeeDott.app"), "из Программ — нет")
    check(RuntimeLogic.sha256Hex(Data("abc".utf8)) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "sha256")
    check(RuntimeLogic.needsSync(bundledLockHash: "a", installedLockHash: "a", venvPythonExists: true) == false, "тот же lock — не синхронизируем")
    check(RuntimeLogic.needsSync(bundledLockHash: "b", installedLockHash: "a", venvPythonExists: true), "новый lock — синхронизируем")
    check(RuntimeLogic.needsSync(bundledLockHash: "a", installedLockHash: "a", venvPythonExists: false), "нет python — синхронизируем")
    check(RuntimeLogic.needsSync(bundledLockHash: "a", installedLockHash: nil, venvPythonExists: true), "нет lock.sha — синхронизируем")
    check(RuntimeLogic.setupErrorText(code: "no_network", message: "").contains("интернет"), "текст no_network")
    check(RuntimeLogic.setupErrorText(code: "weird", message: "boom").contains("boom"), "неизвестный код — сообщение как есть")
}

// 23. Лог демона: супервизор и демон пишут в один daemon.log. Без O_APPEND демон
// пишет со своей позиции и затирает строки, дописанные супервизором после его старта.
do {
    let path = NSTemporaryDirectory() + "meetmind-append-\(UUID().uuidString).log"
    FileManager.default.createFile(atPath: path, contents: nil)
    let daemon = RuntimeLogic.openForAppend(path)
    let supervisor = RuntimeLogic.openForAppend(path)
    check(daemon != nil && supervisor != nil, "лог открылся на дозапись")
    daemon?.write(Data("daemon-1\n".utf8))
    supervisor?.write(Data("supervisor\n".utf8))
    daemon?.write(Data("daemon-2\n".utf8))
    try? daemon?.close()
    try? supervisor?.close()
    let text = (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
    check(text == "daemon-1\nsupervisor\ndaemon-2\n", "строки не затёрты: \(text.debugDescription)")
    try? FileManager.default.removeItem(atPath: path)
}

// 24. Настройки саммари: модели в мастере и тексты ошибок команд.
do {
    check(RuntimeLogic.summaryModels.map(\.id) == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"], "три модели по порядку")
    check(RuntimeLogic.summaryModels.contains { $0.id == RuntimeLogic.defaultSummaryModel }, "дефолт в списке")
    check(RuntimeLogic.setupErrorText(code: "bad_model", message: "").contains("модели"), "текст bad_model")
    check(RuntimeLogic.setupErrorText(code: "too_long", message: "").contains("20 000"), "текст too_long")
}

// 25. Шаг «Claude»: состояние подключения из status и текст ошибки конфига.
do {
    check(RuntimeLogic.claudeConnection("connected") == .connected, "connected")
    check(RuntimeLogic.claudeConnection("stale") == .stale, "stale")
    check(RuntimeLogic.claudeConnection("bad_config") == .badConfig, "bad_config")
    check(RuntimeLogic.claudeConnection(nil) == .disconnected, "нет ключа")
    check(RuntimeLogic.setupErrorText(code: "bad_config", message: "/x.json").contains("/x.json"),
          "путь в тексте bad_config")
}

if failures == 0 { print("OK — все тесты прошли") } else { print("\(failures) провалено"); exit(1) }
