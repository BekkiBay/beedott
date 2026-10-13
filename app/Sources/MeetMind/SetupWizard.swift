// SetupWizard.swift — мастер первичной настройки (4 шага): распознавание речи,
// ключ Anthropic, Telegram (по желанию), готово.
// Спека: docs/superpowers/specs/2026-10-13-first-run-setup-design.md.
// Вся работа — RuntimeManager (uv sync, python -m daemon.setup) на фоновой очереди.

import AppKit
import SwiftUI

struct SetupStatus {
    var model: Bool
    var summary: String?
    var telegram: Bool

    init(_ r: SetupResult) {
        model = r.bool("model")
        summary = r.string("summary")
        telegram = r.bool("telegram")
    }
}

@MainActor
final class SetupModel: ObservableObject {
    enum Step: Int, CaseIterable { case speech = 1, summary, telegram, done }
    enum Phase: Equatable { case idle, running, done, failed(String) }

    static let minFreeBytes: Int64 = 4 * 1_073_741_824

    @Published var step: Step = .speech
    // шаг 1
    @Published var runtimePhase: Phase = .idle
    @Published var runtimeLine = ""
    @Published var runtimeDetails = ""
    @Published var modelPhase: Phase = .idle
    @Published var modelDone: Int64 = 0
    @Published var modelTotal: Int64 = 0
    @Published var diskWarning: String?
    // шаг 2
    @Published var apiKey = ""
    @Published var keyPhase: Phase = .idle
    @Published var summaryMode: String?
    @Published var replacingKey = false
    // шаг 3
    @Published var botToken = ""
    @Published var botName: String?
    @Published var tokenPhase: Phase = .idle
    @Published var connectPhase: Phase = .idle
    @Published var telegramConfigured = false
    // применение настроек
    @Published var applyNote: String?

    private let runtime = RuntimeManager.shared
    private let queue = DispatchQueue(label: "com.jvmshid.meetmind.setup")
    var onClose: () -> Void = {}

    init(status: SetupStatus?) {
        if let status, runtime.hasRuntime, !runtime.needsSync {
            runtimePhase = .done
            if status.model { modelPhase = .done }
        }
        summaryMode = status?.summary
        telegramConfigured = status?.telegram ?? false
        step = !speechReady ? .speech : (summaryMode == nil ? .summary : .telegram)
    }

    var speechReady: Bool { runtimePhase == .done && modelPhase == .done }
    var summaryReady: Bool { summaryMode != nil && !replacingKey }

    // MARK: шаг 1

    func installSpeech() {
        if runtimePhase == .running || modelPhase == .running { return }
        if AppModel.shared.supervisor.state == .updating {
            // Супервизор уже делает uv sync — второй параллельный sync сломал бы venv.
            diskWarning = "Сейчас обновляются компоненты — подожди минуту и нажми ещё раз"
            return
        }
        let free = runtime.freeDiskBytes()
        if !speechReady && free < Self.minFreeBytes {
            let gb = Double(Self.minFreeBytes - free) / 1_073_741_824
            diskWarning = String(format: "Не хватает места: освободи ещё %.1f ГБ", gb)
            return
        }
        diskWarning = nil
        let needRuntime = runtimePhase != .done
        if needRuntime {
            runtimePhase = .running
            runtimeDetails = ""
        }
        queue.async { [runtime] in
            if needRuntime {
                if runtime.hasRuntime && !runtime.needsSync {
                    Task { @MainActor in self.runtimePhase = .done }
                } else {
                    let failure = runtime.syncRuntime { line in
                        Task { @MainActor in self.runtimeLine = line }
                    }
                    if let failure {
                        Task { @MainActor in
                            self.runtimeDetails = failure
                            self.runtimePhase = .failed("Не удалось поставить пакеты. Проверь интернет и нажми «Повторить».")
                        }
                        return
                    }
                    Task { @MainActor in
                        self.runtimePhase = .done
                        AppModel.shared.supervisor.runtimeReady()
                    }
                }
            }
            Task { @MainActor in self.modelPhase = .running }
            let result = runtime.runSetup("install-model") { event in
                if case .progress(_, let done, let total) = event {
                    Task { @MainActor in
                        self.modelDone = done
                        self.modelTotal = total
                    }
                }
            }
            Task { @MainActor in
                switch result {
                case .ok: self.modelPhase = .done
                case .error(let code, let message):
                    self.modelPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                default: self.modelPhase = .failed("Что-то пошло не так")
                }
            }
        }
    }

    // MARK: шаг 2

    func checkKey() {
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty, keyPhase != .running else { return }
        keyPhase = .running
        queue.async { [runtime] in
            let result = runtime.runSetup("check-key", stdin: key)
            Task { @MainActor in
                if case .error(let code, let message) = result {
                    self.keyPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                    return
                }
                self.keyPhase = .done
                self.summaryMode = "api"
                self.replacingKey = false
                self.apiKey = ""
                self.applySettings()
            }
        }
    }

    // MARK: шаг 3

    func checkToken() {
        let token = botToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty, tokenPhase != .running else { return }
        tokenPhase = .running
        botName = nil
        queue.async { [runtime] in
            let result = runtime.runSetup("telegram-check", stdin: token)
            Task { @MainActor in
                switch result {
                case .ok(let r):
                    self.botName = r.string("bot")
                    self.tokenPhase = .done
                case .error(let code, let message):
                    self.tokenPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                default:
                    break
                }
            }
        }
    }

    func openBot() {
        guard let botName else { return }
        if let tg = URL(string: "tg://resolve?domain=\(botName)"), NSWorkspace.shared.open(tg) { return }
        if let web = URL(string: "https://t.me/\(botName)") { NSWorkspace.shared.open(web) }
    }

    func connectTelegram() {
        let token = botToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard connectPhase != .running else { return }
        connectPhase = .running
        queue.async { [runtime] in
            let result = runtime.runSetup("telegram-connect", stdin: token)
            Task { @MainActor in
                if case .error(let code, let message) = result {
                    self.connectPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                    return
                }
                self.connectPhase = .done
                self.telegramConfigured = true
                self.botToken = ""
                self.applySettings()
            }
        }
    }

    // MARK: общее

    private func applySettings() {
        let app = AppModel.shared
        if app.recordingSessionId != nil || app.activeJobs > 0 {
            app.restartDaemonWhenIdle()
            applyNote = "Применится после текущей встречи"
        } else if !app.supervisor.restart() {
            applyNote = "Перезапусти демон, чтобы применить"
        } else {
            applyNote = nil
        }
    }

    func next() {
        if let nextStep = Step(rawValue: step.rawValue + 1) { step = nextStep }
    }

    func back() {
        if let prev = Step(rawValue: step.rawValue - 1) { step = prev }
    }

    func finish() {
        UserDefaults.standard.set(true, forKey: AppModel.setupCompletedKey)
        onClose()
    }
}

// MARK: - вью

struct SetupRootView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Шаг \(model.step.rawValue) из 4 · \(stepName)")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Theme.textTertiary(scheme))
            Group {
                switch model.step {
                case .speech: SpeechStepView(model: model)
                case .summary: SummaryStepView(model: model)
                case .telegram: TelegramStepView(model: model)
                case .done: DoneStepView(model: model)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            footer
        }
        .padding(EdgeInsets(top: 18, leading: 28, bottom: 24, trailing: 28))
        .frame(width: 520, height: 460)
    }

    private var stepName: String {
        switch model.step {
        case .speech: return "Распознавание речи"
        case .summary: return "Саммари"
        case .telegram: return "Telegram"
        case .done: return "Готово"
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            HStack(spacing: 6) {
                ForEach(SetupModel.Step.allCases, id: \.rawValue) { s in
                    Circle()
                        .fill(s == model.step ? Theme.accent(scheme) : Theme.levelTrack(scheme))
                        .frame(width: 7, height: 7)
                }
            }
            Spacer()
            if model.step != .speech && model.step != .done {
                Button("Назад") { model.back() }
                    .buttonStyle(.plain)
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.textSecondary(scheme))
            }
            switch model.step {
            case .speech:
                Button("Далее") { model.next() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.speechReady)
            case .summary:
                Button("Далее") { model.next() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.summaryReady)
            case .telegram:
                if !model.telegramConfigured {
                    Button("Пропустить") { model.next() }
                }
                Button("Далее") { model.next() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.telegramConfigured)
            case .done:
                Button("Начать") { model.finish() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .controlSize(.large)
    }
}

private struct StepHeader: View {
    let title: String
    let lead: String
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 20, weight: .bold))
            Text(lead)
                .font(.system(size: 13))
                .foregroundStyle(Theme.textSecondary(scheme))
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct PhaseRow: View {
    let title: String
    let phase: SetupModel.Phase
    var detail: String = ""
    var progress: Double?
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                icon.frame(width: 16)
                Text(title).font(.system(size: 13, weight: .semibold))
                Spacer()
                if !detail.isEmpty {
                    Text(detail)
                        .font(.system(size: 11).monospacedDigit())
                        .foregroundStyle(Theme.textTertiary(scheme))
                        .lineLimit(1)
                }
            }
            if let progress, phase == .running {
                ProgressView(value: progress).progressViewStyle(.linear)
            }
            if case .failed(let text) = phase {
                Text(text)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.recordingRed(scheme))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.rowFill(scheme)))
    }

    @ViewBuilder private var icon: some View {
        switch phase {
        case .idle: Image(systemName: "circle").foregroundStyle(Theme.textTertiary(scheme))
        case .running: ProgressView().controlSize(.small)
        case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green(scheme))
        case .failed: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.orange(scheme))
        }
    }
}

private struct SpeechStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            StepHeader(title: "Ставлю распознавание речи",
                       lead: "Скачаю Python-пакеты (~1 ГБ) и модель Whisper (1,5 ГБ). Расшифровка работает на этом Mac — аудио никуда не уходит.")
            PhaseRow(title: "Python и пакеты", phase: model.runtimePhase,
                     detail: model.runtimePhase == .running ? model.runtimeLine : "")
            if !model.runtimeDetails.isEmpty {
                DisclosureGroup("Подробности") {
                    Text(model.runtimeDetails)
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .font(.system(size: 12))
            }
            PhaseRow(title: "Модель Whisper", phase: model.modelPhase,
                     detail: modelDetail, progress: modelProgress)
            if let warning = model.diskWarning {
                Text(warning).font(.system(size: 12)).foregroundStyle(Theme.orange(scheme))
            }
            if !model.speechReady {
                Button(isFailed ? "Повторить" : "Установить") { model.installSpeech() }
                    .controlSize(.large)
                    .disabled(model.runtimePhase == .running || model.modelPhase == .running)
            }
        }
    }

    private var isFailed: Bool {
        if case .failed = model.runtimePhase { return true }
        if case .failed = model.modelPhase { return true }
        return false
    }

    private var modelProgress: Double? {
        model.modelTotal > 0 ? Double(model.modelDone) / Double(model.modelTotal) : nil
    }

    private var modelDetail: String {
        guard model.modelPhase == .running, model.modelTotal > 0 else { return "" }
        let mb = { (b: Int64) in Int((Double(b) / 1_048_576).rounded()) }
        return "\(mb(model.modelDone)) из \(mb(model.modelTotal)) МБ"
    }
}

private struct SummaryStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            StepHeader(title: "Саммари делает Claude",
                       lead: "Нужен ключ Anthropic API. В Anthropic уходит только текст расшифровки, аудио — никогда.")
            if let mode = model.summaryMode, !model.replacingKey {
                HStack {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green(scheme))
                    Text(mode == "api" ? "Anthropic API — ключ сохранён" : "claude CLI (Max)")
                        .font(.system(size: 13))
                    Spacer()
                    Button("Заменить ключ") {
                        model.replacingKey = true
                        model.keyPhase = .idle
                    }
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.rowFill(scheme)))
            } else {
                SecureField("sk-ant-…", text: $model.apiKey)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { model.checkKey() }
                HStack {
                    Link("Где взять ключ", destination: URL(string: "https://console.anthropic.com/settings/keys")!)
                        .font(.system(size: 12))
                    Spacer()
                    if model.replacingKey {
                        Button("Отмена") {
                            model.replacingKey = false
                            model.apiKey = ""
                        }
                    }
                    Button("Проверить") { model.checkKey() }
                        .disabled(model.apiKey.isEmpty || model.keyPhase == .running)
                }
                keyStatus
            }
            if let note = model.applyNote {
                Text(note).font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
            }
        }
    }

    @ViewBuilder private var keyStatus: some View {
        switch model.keyPhase {
        case .running:
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Проверяю…").font(.system(size: 12))
            }
        case .failed(let text):
            Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
        default:
            EmptyView()
        }
    }
}

private struct TelegramStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            StepHeader(title: "Фоллоу-апы в Telegram",
                       lead: "По желанию. Бот присылает только готовый текст — ни аудио, ни расшифровку целиком.")
            if model.telegramConfigured {
                HStack {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green(scheme))
                    Text("Telegram подключён").font(.system(size: 13))
                }
            } else {
                Text("1. В Telegram открой @BotFather → /newbot → скопируй токен.")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textSecondary(scheme))
                HStack {
                    SecureField("123456:ABC…", text: $model.botToken)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { model.checkToken() }
                    Button("Проверить") { model.checkToken() }
                        .disabled(model.botToken.isEmpty || model.tokenPhase == .running)
                }
                if case .failed(let text) = model.tokenPhase {
                    Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
                }
                if let bot = model.botName {
                    Text("2. Открой @\(bot) и нажми «Запустить» (/start).")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textSecondary(scheme))
                    HStack {
                        Button("Открыть @\(bot)") { model.openBot() }
                        Button(model.connectPhase == .running ? "Жду /start…" : "Я нажал /start") {
                            model.connectTelegram()
                        }
                        .disabled(model.connectPhase == .running)
                        if model.connectPhase == .running { ProgressView().controlSize(.small) }
                    }
                    if case .failed(let text) = model.connectPhase {
                        Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
                    }
                }
            }
            if let note = model.applyNote {
                Text(note).font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
            }
        }
    }
}

private struct DoneStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            StepHeader(title: "Готово",
                       lead: "BeeDott живёт в строке меню. Записывай встречу кнопкой или ⌘⇧R — фоллоу-ап придёт сам.")
            VStack(alignment: .leading, spacing: 8) {
                row("Распознавание речи — Whisper на этом Mac", ok: model.speechReady)
                row("Саммари — Claude", ok: model.summaryMode != nil)
                row(model.telegramConfigured ? "Telegram — подключён"
                                             : "Telegram — не подключён (можно позже: меню → Настройка…)",
                    ok: model.telegramConfigured)
            }
            Text("Фоллоу-апы: меню → Библиотека, заметки в ~/MeetMind\(model.telegramConfigured ? ", Telegram" : "").")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
            Text("При первой записи macOS спросит доступ к микрофону и звуку — нажми «Разрешить».")
                .font(.system(size: 12, weight: .semibold))
        }
    }

    private func row(_ text: String, ok: Bool) -> some View {
        HStack(spacing: 8) {
            Image(systemName: ok ? "checkmark.circle.fill" : "minus.circle")
                .foregroundStyle(ok ? Theme.green(scheme) : Theme.textTertiary(scheme))
            Text(text).font(.system(size: 13))
        }
    }
}

// MARK: - окно

@MainActor
enum SetupWindow {
    private static var controller: NSWindowController?

    static func show(status: SetupStatus?) {
        let model = SetupModel(status: status)
        if controller == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 460),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Настройка BeeDott"
            window.isReleasedWhenClosed = false
            window.center()
            controller = NSWindowController(window: window)
        }
        model.onClose = { controller?.close() }
        controller?.window?.contentViewController = NSHostingController(rootView: SetupRootView(model: model))
        NSApp.activate(ignoringOtherApps: true)
        controller?.showWindow(nil)
    }

    /// Запуск из Загрузок (App Translocation): рантайм не ставим, просим перенести.
    static func showMoveToApplications() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Перенеси BeeDott в папку «Программы»"
        alert.informativeText = "macOS запустила BeeDott из временной копии, потому что приложение открыто прямо из Загрузок. Перетащи BeeDott в «Программы» и открой оттуда."
        alert.addButton(withTitle: "Открыть Загрузки")
        alert.addButton(withTitle: "Выйти")
        if alert.runModal() == .alertFirstButtonReturn {
            let downloads = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
            NSWorkspace.shared.open(downloads)
        }
        NSApp.terminate(nil)
    }
}
