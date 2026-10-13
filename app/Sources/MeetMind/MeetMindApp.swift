// MeetMindApp.swift — SwiftUI App, MenuBarExtra shell (PLAN_v3 Task 3.3).
// Entry point (@main) живёт в main.swift, чтобы --capture-test мог отработать
// headless до запуска GUI (PLAN_v3 Task 4.1-4.2).
//
// LSUIElement-приложение (без Dock, см. Info.plist). Иконка в меню-баре:
// idle / recording / демон недоступен. Хоткеи: ⌘⇧R — старт/стоп, ⌃⌥⌘X — отмена
// записи с подтверждением (аудит T8: ⌘⇧X без вопроса удалял запись и перехватывал
// зачёркивание в Slack/Docs). Quit сначала останавливает и сохраняет запись,
// потом гасит демона, если это мы его спавнили.

import SwiftUI
import AppKit
import Carbon

// MARK: - NativeCaptureHolder

/// Держатель активного CaptureEngine + owner активной записи (PLAN_v3 Task 4.1-4.2).
/// Намеренно НЕ MainActor: читается/пишется только из AppModel.actionQueue (serial
/// DispatchQueue), поэтому доступ уже сериализован порядком вызовов — не нужен ещё
/// один слой синхронизации. Вынесен из AppModel, чтобы actionQueue-замыкания могли
/// трогать его без прыжка на MainActor (AppModel сам — @MainActor).
final class NativeCaptureHolder {
    var engine: CaptureEngine?
    /// Сессия демона, в которую пишет engine (для сверки со status.recording).
    var sessionId: String?
    /// "app" — активная запись пишется нативным CaptureEngine (Process Tap);
    /// "daemon" — Python Recorder; используется как маркер вне активной записи тоже
    /// (последнее известное значение), но реально осмыслен только пока recordingSessionId != nil.
    var owner: String = "daemon"
}

// MARK: - AppModel

@MainActor
final class AppModel: ObservableObject {
    enum Icon: String {
        case idle = "🧠"
        case recording = "🔴"
        case daemonDown = "⚠️"
    }

    static let shared = AppModel()

    @Published private(set) var iconText: String = Icon.idle.rawValue
    @Published private(set) var statusLine: String = "Проверка демона…"
    @Published private(set) var recordingSessionId: String? = nil
    @Published private(set) var queueCount: Int = 0
    @Published private(set) var daemonHealthy: Bool = false
    @Published private(set) var busy: Bool = false
    @Published private(set) var lastErrorText: String? = nil
    /// Состояние рантайма для футера дропдауна: «Нужна настройка», «Обновляю компоненты…».
    @Published private(set) var runtimeNote: String? = nil
    /// Задачи, которые реально идут или ждут очереди (pending/queued/running).
    /// queueCount считает и failed/failed_retry/skipped — по нему отложенный
    /// рестарт мог не наступить никогда.
    @Published private(set) var activeJobs: Int = 0
    static let setupCompletedKey = "setupCompleted"
    private var pendingDaemonRestart = false
    /// Первая настройка не пройдена, а супервизор обновляет компоненты — проверить
    /// настройку, когда он закончит (иначе мастер после прерванной установки не откроется).
    private var setupCheckAfterUpdate = false

    // --- данные для DropdownView / Библиотеки (экраны 02, 05-06)
    @Published private(set) var recordingTitle: String? = nil
    @Published private(set) var recordingStartedAt: Date? = nil
    @Published private(set) var recentMeetings: [MeetingRow] = []
    @Published private(set) var problemCount: Int = 0
    @Published private(set) var micLevel: Double = 0
    @Published private(set) var systemLevel: Double = 0
    @Published private(set) var elapsedText: String = "00:00"
    /// Ручной mute микрофона в идущей записи (только нативная запись Swift).
    @Published private(set) var micMuted: Bool = false
    @Published private(set) var canMute: Bool = false

    // --- автозапуск и автодетект звонков
    @Published private(set) var launchAtLogin: Bool = false
    @Published private(set) var loginNeedsApproval: Bool = false
    @Published private(set) var autoDetectCalls: Bool =
        UserDefaults.standard.object(forKey: AppModel.autoDetectKey) as? Bool ?? true
    @Published private(set) var calendarStatus: CalendarContext.Status = .noAccess
    nonisolated static let autoDetectKey = "autoDetectCalls"

    // --- островок у чёлки
    @Published private(set) var islandEnabled: Bool =
        UserDefaults.standard.object(forKey: AppModel.islandKey) as? Bool ?? true
    /// Ближайшие встречи из Календаря (обновляются раз в ~6 с).
    @Published private(set) var upcomingEvents: [CalendarEventInfo] = []
    nonisolated static let islandKey = "notchIsland"
    private var island: IslandController?

    let supervisor = DaemonSupervisor()

    private var statusTimer: Timer?
    private var uiTimer: Timer?
    private var supervisorObserver: NSObjectProtocol?
    private let actionQueue = DispatchQueue(label: "com.jvmshid.meetmind.actions")
    private var started = false
    // Счётчик опросов статуса: Календарь для островка — каждый 2-й (~6 с).
    nonisolated(unsafe) private var statusTicks = 0
    // См. NativeCaptureHolder — намеренно не MainActor-изолирован, доступ только
    // из actionQueue (сериализован порядком выполнения тем же способом, что и
    // остальные действия record/stop/cancel в этом классе).
    nonisolated(unsafe) private let capture = NativeCaptureHolder()
    // Автодетект звонков (CallDetectionMachine) — как capture, только с actionQueue.
    nonisolated(unsafe) private var callMachine = CallDetectionMachine()

    private init() {}

    func start() {
        guard !started else { return }
        started = true

        // Из Загрузок macOS запускает read-only копию — ставить рантайм и
        // регистрировать автозапуск на неё нельзя.
        if RuntimeLogic.isTranslocated(Bundle.main.bundlePath) {
            SetupWindow.showMoveToApplications()
            return
        }

        // Следим за состоянием супервизора (heartbeat down/restarting) — влияет на иконку,
        // даже если статус-поллинг ещё не подтвердил недоступность.
        supervisorObserver = NotificationCenter.default.addObserver(
            forName: DaemonSupervisor.stateChangedNotification,
            object: supervisor, queue: .main
        ) { [weak self] note in
            guard let state = note.userInfo?["state"] as? DaemonSupervisor.RunState else { return }
            Task { @MainActor in self?.handleSupervisorState(state) }
        }

        supervisor.start()
        registerHotKeys()
        startStatusPolling()
        startUITimer()
        // P8: доступ к Календарю — в фоне; старт приложения и записи диалог не ждут.
        CalendarContext.requestAccessIfNeeded()
        // MeetMind должен быть запущен всегда, иначе встречи не пишутся.
        LoginItem.enableOnFirstLaunch()
        refreshLoginItem()
        island = IslandController(model: self)
        checkSetupOnLaunch()
    }

    private func handleSupervisorState(_ state: DaemonSupervisor.RunState) {
        if setupCheckAfterUpdate {
            switch state {
            case .healthy:
                setupCheckAfterUpdate = false
                checkSetupOnLaunch()
            case .updateFailed:
                setupCheckAfterUpdate = false
                SetupWindow.show(status: nil)
            default:
                break
            }
        }
        switch state {
        case .healthy:
            runtimeNote = nil // статус-поллинг (3с) сам подтвердит через daemonHealthy
        case .notInstalled:
            runtimeNote = "Нужна настройка"
            daemonHealthy = false
            updateStatusLine()
            refreshIcon()
        case .updating:
            runtimeNote = "Обновляю компоненты…"
            daemonHealthy = false
            updateStatusLine()
            refreshIcon()
        case .updateFailed:
            runtimeNote = "Компоненты не обновились — см. setup.log"
            daemonHealthy = false
            updateStatusLine()
            refreshIcon()
        case .down, .restarting, .unknown:
            daemonHealthy = false
            refreshIcon()
            updateStatusLine()
        }
    }

    // MARK: - первичная настройка (SetupWizard.swift)

    /// Мастер открывается сам, если без него BeeDott не работает: нет рантайма или
    /// модели, либо настройка не пройдена и саммари не настроено. На уже
    /// настроенном Mac — молча отмечаем настройку пройденной.
    private func checkSetupOnLaunch() {
        let runtime = RuntimeManager.shared
        if runtime.hasRuntime && runtime.needsSync {
            // Супервизор обновляет компоненты (uv sync) — не мешаем. Если первая
            // настройка не пройдена (установку прервали), проверим после него.
            if !UserDefaults.standard.bool(forKey: AppModel.setupCompletedKey) {
                setupCheckAfterUpdate = true
            }
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            var status: SetupStatus?
            if runtime.hasRuntime, case .ok(let r) = runtime.runSetup("status") { status = SetupStatus(r) }
            Task { @MainActor in
                let completed = UserDefaults.standard.bool(forKey: AppModel.setupCompletedKey)
                let needed = status.map { !$0.model || (!completed && $0.summary == nil) } ?? true
                if needed {
                    SetupWindow.show(status: status)
                } else if !completed {
                    UserDefaults.standard.set(true, forKey: AppModel.setupCompletedKey)
                }
            }
        }
    }

    /// «Настройка…» в дропдауне.
    func openSetup() {
        let runtime = RuntimeManager.shared
        DispatchQueue.global(qos: .userInitiated).async {
            var status: SetupStatus?
            if runtime.hasRuntime, case .ok(let r) = runtime.runSetup("status") { status = SetupStatus(r) }
            Task { @MainActor in SetupWindow.show(status: status) }
        }
    }

    /// Мастер сохранил ключ/Telegram — перезапустить демон, чтобы он перечитал
    /// config.env. Решаем по свежему статусу демона (не по поллингу трёхсекундной
    /// давности): во время записи или обработки — отложить до простоя.
    func applyNewSettings(_ done: @escaping @MainActor (String?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let status = try? ControlClient.status()
            let recording = status?["recording"] as? String
            let active = RuntimeLogic.activeJobs((status?["queue"] as? [[String: Any]]) ?? [])
            Task { @MainActor in
                if recording != nil || active > 0 {
                    self.pendingDaemonRestart = true
                    done("Применится после текущей встречи")
                } else {
                    self.supervisor.restart()
                    done(nil)
                }
            }
        }
    }

    private func registerHotKeys() {
        HotKeyManager.shared.register(
            keyCode: HotKeyManager.KeyCode.r, modifiers: UInt32(cmdKey | shiftKey)
        ) {
            Task { @MainActor in AppModel.shared.toggleRecording() }
        }
        HotKeyManager.shared.register(
            keyCode: HotKeyManager.KeyCode.m, modifiers: UInt32(controlKey | optionKey | cmdKey)
        ) {
            Task { @MainActor in AppModel.shared.toggleMute() }
        }
        HotKeyManager.shared.register(
            keyCode: HotKeyManager.KeyCode.x, modifiers: UInt32(controlKey | optionKey | cmdKey)
        ) {
            // Через RunLoop, а не Task: модальный NSAlert внутри задачи главной
            // очереди заблокировал бы её — статус, таймер и предохранители
            // стояли бы, пока открыт диалог.
            RunLoop.main.perform(inModes: [.common]) {
                MainActor.assumeIsolated { AppModel.shared.confirmCancelRecording() }
            }
        }
    }

    /// Отмена удаляет аудио безвозвратно — только после явного подтверждения.
    func confirmCancelRecording() {
        guard recordingSessionId != nil else { return }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Удалить текущую запись?"
        alert.informativeText = "Аудио будет удалено без возможности восстановления, фоллоу-ап не придёт."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Продолжить запись")
        let delete = alert.addButton(withTitle: "Удалить запись")
        delete.hasDestructiveAction = true
        if alert.runModal() == .alertSecondButtonReturn {
            cancelRecording()
        }
    }

    private func startStatusPolling() {
        statusTimer?.invalidate()
        let timer = Timer(timeInterval: 3, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshStatus() }
        }
        RunLoop.main.add(timer, forMode: .common)
        statusTimer = timer
        refreshStatus()
    }

    func refreshStatus() {
        actionQueue.async { [weak self] in
            guard let self else { return }
            do {
                let result = try ControlClient.status()
                var recording = result["recording"] as? String
                // Старый демон (без протокола T3/T7) поля recording_owner не
                // присылает — тогда ничего не трогаем.
                let ownerKnown = result.keys.contains("recording_owner")
                if let sid = recording, (result["recording_owner"] as? String) == "app" {
                    if self.capture.engine == nil {
                        // Запись числится за приложением, но движка у нас нет —
                        // прошлый экземпляр упал или вышел, не закрыв её.
                        // Закрываем: демон починит заголовок WAV и обработает
                        // записанное (пустую запись отменит) — аудит T3.
                        NSLog("MeetMind: осиротевшая запись %@ — закрываю", sid)
                        _ = try? ControlClient.stopRecording(sessionId: sid)
                        recording = nil
                    } else if sid == self.capture.sessionId,
                              let reason = result["stop_requested"] as? String {
                        // Предохранитель демона (лимит длительности, диск,
                        // размер WAV) — аудит T7.
                        NSLog("MeetMind: предохранитель (%@) — останавливаю запись", reason)
                        try? self.performStopRecording()
                        recording = nil
                    }
                } else if ownerKnown, let engine = self.capture.engine,
                          recording != self.capture.sessionId {
                    // Движок пишет, а у демона нашей записи уже нет — демон
                    // закрыл её сам (файл долго не менялся). Гасим движок, чтобы
                    // микрофон не писал «в никуда», а индикатор записи не врал.
                    let frames = engine.stop()
                    self.capture.engine = nil
                    self.capture.sessionId = nil
                    self.capture.owner = "daemon"
                    NSLog("MeetMind: демон закрыл запись — движок остановлен (%llu кадров)", frames)
                }
                // P16: пока мы поллим, демон копит уведомления для нас (вместо
                // osascript) — забираем и показываем каждое ровно один раз.
                // Ошибку (старый демон: unknown_method) молча пропускаем.
                if let notes = try? ControlClient.notificationsPop() {
                    for note in notes { NotificationManager.shared.post(note) }
                }
                let recTitle = result["recording_title"] as? String
                let recStarted = (result["recording_started_at"] as? String)
                    .flatMap { ISO8601DateFormatter().date(from: $0) }
                let queueRows = (result["queue"] as? [[String: Any]]) ?? []
                let queueTotal = queueRows.reduce(0) { $0 + (($1["n"] as? Int) ?? 0) }
                let activeTotal = RuntimeLogic.activeJobs(queueRows)
                let lastError = result["last_error"] as? [String: Any]
                let errText = lastError.flatMap { $0["last_error"] as? String }
                // Свежий список встреч тем же заходом (дропдаун: последние 3 +
                // бейдж проблемных на «Все встречи…»).
                let meetings = (try? ControlClient.sessionsList(limit: 50)) ?? []
                if UserDefaults.standard.object(forKey: AppModel.autoDetectKey) as? Bool ?? true {
                    recording = self.detectCall(recording: recording)
                }
                let calendar = CalendarContext.status()
                self.statusTicks += 1
                let upcoming = self.statusTicks % 2 == 1 ? CalendarContext.upcomingEvents() : nil
                let recordingNow = recording
                let mutable = recording != nil && self.capture.engine != nil
                Task { @MainActor in
                    self.canMute = mutable
                    if recordingNow == nil { self.micMuted = false }
                    self.calendarStatus = calendar
                    if let upcoming { self.upcomingEvents = upcoming }
                    self.recordingSessionId = recordingNow
                    self.recordingTitle = recTitle
                    self.recordingStartedAt = recStarted
                    self.queueCount = queueTotal
                    self.activeJobs = activeTotal
                    self.daemonHealthy = true
                    self.lastErrorText = errText
                    self.recentMeetings = meetings
                    self.problemCount = meetings.filter { $0.status == "error" }.count
                    self.updateStatusLine()
                    self.refreshIcon()
                    if self.pendingDaemonRestart && recordingNow == nil && activeTotal == 0 {
                        self.pendingDaemonRestart = false
                        self.supervisor.restart()
                    }
                }
            } catch {
                Task { @MainActor in
                    self.daemonHealthy = false
                    self.updateStatusLine()
                    self.refreshIcon()
                }
            }
        }
    }

    /// Быстрый UI-тик (0.5с): таймер записи + уровни двух источников из
    /// CaptureEngine. capture.engine пишется actionQueue-ом, читается здесь —
    /// levels() внутри залочен, чтение ссылки engine — приемлемая гонка
    /// (worst case: nil на один тик).
    private func startUITimer() {
        uiTimer?.invalidate()
        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.uiTick() }
        }
        RunLoop.main.add(timer, forMode: .common)
        uiTimer = timer
    }

    private func uiTick() {
        if let start = recordingStartedAt {
            let s = max(0, Int(Date().timeIntervalSince(start)))
            elapsedText = s >= 3600
                ? String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
                : String(format: "%02d:%02d", s / 60, s % 60)
        } else {
            elapsedText = "00:00"
        }
        if recordingSessionId != nil, let lv = capture.engine?.levels() {
            micLevel = Double(lv.mic)
            systemLevel = Double(lv.system)
        } else {
            micLevel = 0
            systemLevel = 0
        }
        island?.tick()
    }

    // MARK: - mute микрофона

    /// Не записывать микрофон (собеседники пишутся дальше). Работает для
    /// нативной записи (Process Tap); у запасного Python Recorder mute нет.
    func toggleMute() {
        guard recordingSessionId != nil else { return }
        let muted = !micMuted
        micMuted = muted
        actionQueue.async { [weak self] in
            self?.capture.engine?.setMicMuted(muted)
        }
        NSLog("MeetMind: микрофон в записи %@", muted ? "выключен" : "включён")
    }

    // MARK: - островок у чёлки

    func setIslandEnabled(_ enabled: Bool) {
        islandEnabled = enabled
        UserDefaults.standard.set(enabled, forKey: AppModel.islandKey)
        island?.tick()
    }

    /// «Записать» в напоминании островка. Встреча с Meet — запись остановится
    /// сама в конце звонка (как при автодетекте), без Meet — ручной стоп.
    func recordFromIsland(meetCode: String?) {
        performAction { [weak self] in
            guard let self else { return }
            _ = try self.performStartRecording(source: "calendar")
            if meetCode != nil { self.callMachine.markAutoRecording() }
        }
    }

    // MARK: - действия дропдауна (экран 02)

    func openLibrary(sessionId: String?) {
        LibraryWindow.show(sessionId: sessionId)
    }

    private func updateStatusLine() {
        if !daemonHealthy {
            statusLine = "Демон недоступен"
        } else if let sid = recordingSessionId {
            statusLine = "Запись: \(sid)"
        } else if queueCount > 0 {
            statusLine = "В очереди: \(queueCount)"
        } else {
            statusLine = "Очередь пуста"
        }
    }

    private func refreshIcon() {
        if !daemonHealthy {
            iconText = Icon.daemonDown.rawValue
        } else if recordingSessionId != nil {
            iconText = Icon.recording.rawValue
        } else {
            iconText = Icon.idle.rawValue
        }
    }

    // MARK: actions

    func startRecording() { performAction { [weak self] in try self?.performStartRecording() } }
    func stopRecording() { performAction { [weak self] in try self?.performStopRecording() } }
    func cancelRecording() { performAction { [weak self] in try self?.performCancelRecording() } }

    /// Решение «старт или стоп» принимается на actionQueue по фактическому
    /// состоянию (свой движок + статус демона), а не по закэшированному
    /// recordingSessionId — устаревший статус больше не обрывает запись (T9).
    func toggleRecording() { performAction { [weak self] in try self?.performToggle() } }

    private func performAction(_ action: @escaping () throws -> Void) {
        guard !busy else { return }
        busy = true
        actionQueue.async { [weak self] in
            guard let self else { return }
            do {
                try action()
            } catch {
                NSLog("MeetMind: действие не удалось — %@", error.localizedDescription)
            }
            Task { @MainActor in
                self.busy = false
                self.refreshStatus()
            }
        }
    }

    // ------------------------------------------------------- capture path (PLAN_v3 Task 4.1-4.2)
    // Всё ниже выполняется на actionQueue (НЕ MainActor) — см. performAction. Трогает
    // только capture (NativeCaptureHolder, nonisolated(unsafe)) и ControlClient
    // (свободные функции), никаких @Published MainActor-полей — их правит только
    // performAction/refreshStatus после возврата на MainActor.

    /// (a) Пробуем нативный Process Tap (CaptureEngine) на временный файл — если
    /// AudioHardwareCreateProcessTap доступен (macOS ≥14.4, TCC ok), запись пойдёт
    /// через Swift (audio_path_owner=app), демон не поднимает Python Recorder.
    /// (b) CaptureError.tapUnavailable (или любая другая ошибка тапа) → вечный
    /// fallback: audio_path_owner=daemon, Python Recorder как раньше (SPEC_v3 D1).
    nonisolated private func performToggle() throws {
        if capture.engine != nil {
            try performStopRecording()
            return
        }
        let status = try ControlClient.status()
        if status["recording"] as? String != nil {
            try performStopRecording()
        } else {
            try performStartRecording()
        }
    }

    // MARK: - автодетект звонков (Google Meet)

    /// Шаг детектора на actionQueue: браузер пишет микрофон + встреча с Meet в
    /// Календаре → пишем сами; без встречи — спрашиваем уведомлением; звонок
    /// кончился → останавливаем автозапись. Возвращает актуальный id записи.
    nonisolated private func detectCall(recording: String?) -> String? {
        let audio = BrowserAudioProbe.current()
        let meeting = recording == nil && audio.input ? CalendarContext.currentMeeting() : nil
        let action = callMachine.step(audio, now: Date(), isRecording: recording != nil,
                                      calendarHasMeet: meeting?.meetCode != nil)
        switch action {
        case .none:
            return recording
        case .autoStart:
            do {
                let sid = try performStartRecording(source: "auto_meet")
                callMachine.markAutoRecording()
                NotificationManager.shared.postLocal(
                    title: "Запись Google Meet началась",
                    message: "«\(meeting?.title ?? "Встреча")» — пишу автоматически. "
                        + "Остановить — ⌘⇧R.", sessionId: sid)
                return sid
            } catch {
                NSLog("MeetMind: автостарт записи не удался — %@", error.localizedDescription)
                return recording
            }
        case .askToRecord:
            NotificationManager.shared.askToRecordCall()
            return recording
        case .stop:
            do {
                try performStopRecording()
                NotificationManager.shared.postLocal(
                    title: "Звонок закончился",
                    message: "Запись остановлена — фоллоу-ап придёт через несколько минут.",
                    sessionId: recording)
            } catch {
                NSLog("MeetMind: автостоп записи не удался — %@", error.localizedDescription)
            }
            return nil
        }
    }

    /// «Записать» в уведомлении о звонке без события в Календаре.
    func recordDetectedCall() {
        performAction { [weak self] in
            guard let self else { return }
            _ = try self.performStartRecording(source: "auto_meet")
            self.callMachine.markAutoRecording()
        }
    }

    func setAutoDetectCalls(_ enabled: Bool) {
        autoDetectCalls = enabled
        UserDefaults.standard.set(enabled, forKey: AppModel.autoDetectKey)
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        LoginItem.setEnabled(enabled)
        refreshLoginItem()
        if LoginItem.needsApproval { LoginItem.openSystemSettings() }
    }

    func openLoginItemsSettings() { LoginItem.openSystemSettings() }

    private func refreshLoginItem() {
        launchAtLogin = LoginItem.isEnabled
        loginNeedsApproval = LoginItem.needsApproval
    }

    func openCalendarSettings() {
        // Google-аккаунт → «Учётные записи интернета»; доступ приложения → «Конфиденциальность → Календари».
        let target = calendarStatus == .noAccess
            ? "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars"
            : "x-apple.systempreferences:com.apple.Internet-Accounts-Settings.extension"
        if let url = URL(string: target) { NSWorkspace.shared.open(url) }
    }

    @discardableResult
    nonisolated private func performStartRecording(source: String = "manual") throws -> String? {
        // Аудит T9: повторный старт не должен трогать идущую запись — раньше
        // ошибка start_recording обнуляла capture.engine, и запись обрывалась.
        guard capture.engine == nil else {
            throw ControlClientError.protocolError("запись уже идёт")
        }
        // P8: название и участники текущей встречи из Календаря (синхронно, без
        // диалога). Нет доступа / события — nil, встречу назовёт демон.
        let meeting = CalendarContext.currentMeeting()
        let meetingKey = meeting?.meetCode.map { "meet:\($0)" }
        let probeURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("meetmind-tap-probe-\(UUID().uuidString).wav")
        let probe = CaptureEngine()
        do {
            try probe.start(mode: .global, outURL: probeURL)
            probe.stop()
            try? FileManager.default.removeItem(at: probeURL)
        } catch {
            NSLog("MeetMind: CaptureEngine недоступен (%@) — fallback на daemon Recorder (audio_path_owner=daemon)",
                  error.localizedDescription)
            capture.owner = "daemon"
            capture.engine = nil
            let result = try ControlClient.startRecording(
                title: meeting?.title, attendees: meeting?.attendees, audioPathOwner: "daemon",
                source: source, meetingKey: meetingKey)
            return result["session_id"] as? String
        }

        NSLog("MeetMind: Process Tap доступен — нативная запись (audio_path_owner=app)")
        // Ошибка демона (busy, таймаут) — не повод уходить на Python Recorder:
        // тап работает, а второй start_recording всё равно упрётся в то же.
        // Если демон успел завести сессию, но ответ не дошёл, её закроет
        // проверка осиротевшей записи в refreshStatus.
        let result = try ControlClient.startRecording(
            title: meeting?.title, attendees: meeting?.attendees, audioPathOwner: "app",
            source: source, meetingKey: meetingKey)
        guard let audioPath = result["audio_path"] as? String,
              let sessionId = result["session_id"] as? String else {
            _ = try? ControlClient.cancelRecording()
            throw ControlClientError.protocolError("start_recording(app): нет audio_path/session_id в ответе")
        }
        NSLog("MeetMind: нативный захват пишет в %@", audioPath)

        let engine = CaptureEngine()
        do {
            try engine.start(mode: .global, outURL: URL(fileURLWithPath: audioPath))
        } catch {
            NSLog("MeetMind: engine.start(real audio_path) не удался (%@) — отменяю сессию у демона",
                  error.localizedDescription)
            _ = try? ControlClient.cancelRecording()
            throw error
        }
        capture.engine = engine
        capture.sessionId = sessionId
        capture.owner = "app"
        return sessionId
    }

    /// Если писали сами (owner=app) — engine.stop() ДО stop_recording (файл должен
    /// быть закрыт и дописан до того, как демон поставит транскрипцию в очередь).
    nonisolated private func performStopRecording() throws {
        if capture.owner == "app", let engine = capture.engine {
            let frames = engine.stop()
            NSLog("MeetMind: CaptureEngine.stop() — записано кадров: %llu", frames)
            capture.engine = nil
            capture.sessionId = nil
        }
        defer { capture.owner = "daemon" }
        _ = try ControlClient.stopRecording()
    }

    nonisolated private func performCancelRecording() throws {
        if capture.owner == "app", let engine = capture.engine {
            let frames = engine.stop()
            NSLog("MeetMind: CaptureEngine.stop() (cancel) — записано кадров: %llu", frames)
            capture.engine = nil
            capture.sessionId = nil
        }
        defer { capture.owner = "daemon" }
        _ = try ControlClient.cancelRecording()
    }

    func openNotesFolder() {
        let url = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("MeetMind")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        NSWorkspace.shared.open(url)
    }

    func quit() {
        finishRecordingBeforeExit()
        supervisor.terminateOnQuit()
        NSApplication.shared.terminate(nil)
    }

    /// Аудит T10: при выходе закрыть WAV и отдать запись демону на обработку —
    /// раньше заголовок оставался незакрытым, а сессия висела в RECORDING.
    /// Синхронно на actionQueue: дожидаемся идущего действия и не гоняемся с ним.
    func finishRecordingBeforeExit() {
        let daemonDiesWithUs = supervisor.terminatesDaemonOnQuit
        actionQueue.sync {
            if capture.owner == "app", capture.engine != nil {
                do {
                    try performStopRecording()
                } catch {
                    NSLog("MeetMind: stop_recording при выходе не удался (%@) — "
                          + "WAV закрыт, демон обработает запись при следующем старте",
                          error.localizedDescription)
                }
            } else if daemonDiesWithUs,
                      let status = try? ControlClient.status(),
                      status["recording"] as? String != nil,
                      (status["recording_owner"] as? String) == "daemon" {
                // Пишет Python Recorder, а демона мы сейчас погасим — закрываем
                // запись штатно, а не через SIGTERM и восстановление.
                _ = try? ControlClient.stopRecording()
            }
        }
    }
}

// MARK: - MenuBarLabel (иконка, экран 01: 4 состояния)

struct MenuBarLabel: View {
    @ObservedObject var model: AppModel

    var body: some View {
        // MenuBarExtra-label ограничен (template-рендер, без кастомного фона):
        // плашку записи из макета отображаем как symbol + tabular-таймер.
        if !model.daemonHealthy {
            Image(systemName: "exclamationmark.triangle")
        } else if model.recordingSessionId != nil {
            HStack(spacing: 3) {
                Image(systemName: "record.circle.fill")
                Text(model.elapsedText).monospacedDigit()
            }
        } else if model.queueCount > 0 {
            Image(systemName: "waveform.badge.magnifyingglass")
        } else {
            Image(systemName: "waveform")
        }
    }
}

// MARK: - AppDelegate

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Делегат уведомлений — до всего, что может запостить (P16).
        NotificationManager.shared.install()
        AppModel.shared.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated {
            AppModel.shared.finishRecordingBeforeExit()
            AppModel.shared.supervisor.terminateOnQuit()
            RuntimeManager.shared.terminateChildren()
        }
    }
}

// MARK: - App

// Без @main здесь: entry point — main.swift (top-level код нужен для
// --capture-test до запуска GUI, PLAN_v3 Task 4.1-4.2 п.4), который вызывает
// MeetMindApp.main() явно. См. app/build.sh — swiftc без -parse-as-library.
struct MeetMindApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @ObservedObject private var model = AppModel.shared

    var body: some Scene {
        MenuBarExtra {
            DropdownView(model: model)
        } label: {
            MenuBarLabel(model: model)
        }
        .menuBarExtraStyle(.window)
    }
}
