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

if failures == 0 { print("OK — все тесты прошли") } else { print("\(failures) провалено"); exit(1) }
