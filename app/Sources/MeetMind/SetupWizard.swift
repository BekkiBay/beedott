// SetupWizard.swift — мастер первичной настройки (5 шагов): распознавание речи,
// ключ Anthropic + модель и инструкция фоллоу-апа, Telegram и Claude (по желанию), готово.
// Спеки: docs/superpowers/specs/2026-10-13-first-run-setup-design.md,
// 2026-10-14-claude-mcp-design.md (шаг «Claude»).
// Вся работа — RuntimeManager (uv sync, python -m daemon.setup) на фоновой очереди.

import AppKit
import SwiftUI

struct SetupStatus {
    var model: Bool
    var summary: String?
    var telegram: Bool
    var summaryModel: String?
    var summaryCustom: Bool
    var claude: String?

    init(_ r: SetupResult) {
        model = r.bool("model")
        summary = r.string("summary")
        telegram = r.bool("telegram")
        summaryModel = r.string("summary_model")
        summaryCustom = r.bool("summary_custom")
        claude = r.string("claude")
    }
}

@MainActor
final class SetupModel: ObservableObject {
    enum Step: Int, CaseIterable { case speech = 1, summary, telegram, claude, done }
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
    /// Нет status (чистый Mac, рантайма ещё нет) — дефолт: ANTHROPIC_MODEL и
    /// файла инструкции там тоже нет.
    @Published var summaryModel = RuntimeLogic.defaultSummaryModel
    @Published var summaryModelPhase: Phase = .idle
    @Published var instructionsCustom = false
    // шаг 3
    @Published var botToken = ""
    @Published var botName: String?
    @Published var tokenPhase: Phase = .idle
    @Published var connectPhase: Phase = .idle
    @Published var telegramConfigured = false
    /// Одноразовый код из ссылки t.me/<бот>?start=<код>: daemon.setup принимает
    /// только этот /start, а не первый попавшийся из очереди бота.
    private var connectCode = ""
    // шаг 4 — Claude (MCP)
    static let claudeBundleId = "com.anthropic.claudefordesktop"
    static let claudeConfigHint = "~/Library/Application Support/Claude/claude_desktop_config.json"
    @Published var claudeState: String?
    @Published var claudeDesktopInstalled = false
    @Published var claudeChecking = false
    @Published var claudePhase: Phase = .idle
    /// Ждёт подтверждения перезапуска Claude: true — подключить, false — отключить.
    @Published var claudeConfirm: Bool?
    /// Итог регистрации в Claude Code (added | failed | absent) и команда для терминала.
    @Published var claudeCodeResult: String?
    @Published var claudeCodeCommand: String?
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
        summaryModel = status?.summaryModel ?? RuntimeLogic.defaultSummaryModel
        instructionsCustom = status?.summaryCustom ?? false
        telegramConfigured = status?.telegram ?? false
        claudeState = status?.claude
        if !speechReady {
            step = .speech
        } else if summaryMode == nil {
            step = .summary
        } else if telegramConfigured && !claudeConnected {
            step = .claude
        } else {
            step = .telegram
        }
    }

    var speechReady: Bool { runtimePhase == .done && modelPhase == .done }
    var summaryReady: Bool { summaryMode != nil && !replacingKey }
    var claudeConnected: Bool { RuntimeLogic.claudeConnection(claudeState) == .connected }
    /// Идёт фоновая работа — при повторном открытии окна модель переиспользуется,
    /// иначе вторая загрузка/второй getUpdates столкнулись бы с первыми.
    var isBusy: Bool {
        [runtimePhase, modelPhase, keyPhase, summaryModelPhase, tokenPhase, connectPhase, claudePhase].contains(.running)
    }

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

    func setSummaryModel(_ id: String) {
        guard id != summaryModel, summaryModelPhase != .running else { return }
        let previous = summaryModel
        summaryModel = id
        summaryModelPhase = .running
        queue.async { [runtime] in
            let result = runtime.runSetup("set-model", stdin: id)
            Task { @MainActor in
                if case .error(let code, let message) = result {
                    self.summaryModel = previous
                    self.summaryModelPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                    return
                }
                self.summaryModelPhase = .done
                self.applySettings()
            }
        }
    }

    func openInstructions() {
        InstructionsWindow.show { custom in self.instructionsCustom = custom }
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

    /// Открыть бота ссылкой с кодом: в Telegram появится «Запустить», и бот
    /// получит `/start <код>`.
    func openBot() {
        guard let botName, !connectCode.isEmpty else { return }
        if let tg = URL(string: "tg://resolve?domain=\(botName)&start=\(connectCode)"),
           NSWorkspace.shared.open(tg) { return }
        if let web = URL(string: "https://t.me/\(botName)?start=\(connectCode)") {
            NSWorkspace.shared.open(web)
        }
    }

    func connectTelegram() {
        let token = botToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard connectPhase != .running, botName != nil else { return }
        connectCode = String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12))
        let code = connectCode
        connectPhase = .running
        openBot()
        queue.async { [runtime] in
            let result = runtime.runSetup("telegram-connect", stdin: token + "\n" + code)
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

    // MARK: шаг 4

    /// Заново спросить status (на чистом Mac мастер открылся до рантайма) и
    /// проверить, установлен ли Claude Desktop.
    func refreshClaude() {
        claudeDesktopInstalled = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: Self.claudeBundleId) != nil
        guard !claudeChecking, claudePhase != .running else { return }
        claudeChecking = true
        queue.async { [runtime] in
            let result = runtime.runSetup("status")
            Task { @MainActor in
                if case .ok(let r) = result { self.claudeState = r.string("claude") }
                self.claudeChecking = false
            }
        }
    }

    /// Подключить или отключить. Открытый Claude держит конфиг в памяти и при
    /// выходе перезаписал бы его — поэтому сначала спрашиваем про перезапуск.
    func claudeAction(connect: Bool) {
        guard claudePhase != .running else { return }
        let running = !NSRunningApplication.runningApplications(
            withBundleIdentifier: Self.claudeBundleId).isEmpty
        if running { claudeConfirm = connect } else { performClaude(connect: connect) }
    }

    func performClaude(connect: Bool) {
        guard claudePhase != .running else { return }
        claudeConfirm = nil
        claudePhase = .running
        claudeCodeResult = nil
        claudeCodeCommand = nil
        let bundleId = Self.claudeBundleId
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleId)
        let wasRunning = !apps.isEmpty
        apps.forEach { $0.terminate() }
        queue.async { [runtime] in
            if wasRunning {
                let deadline = Date().addingTimeInterval(10)
                while Date() < deadline && apps.contains(where: { !$0.isTerminated }) {
                    Thread.sleep(forTimeInterval: 0.2)
                }
                // Закрылись все захваченные экземпляры — но за это время мог успеть
                // открыться новый (пользователь или сама macOS); пишем конфиг только
                // когда Claude точно не держит старую версию в памяти.
                let stillRunning = apps.contains(where: { !$0.isTerminated })
                    || !NSRunningApplication.runningApplications(withBundleIdentifier: bundleId).isEmpty
                if stillRunning {
                    Task { @MainActor in
                        self.claudePhase = .failed("Claude не закрылся — закрой его вручную и нажми ещё раз")
                    }
                    return
                }
            }
            let result = runtime.runSetup(connect ? "claude-connect" : "claude-disconnect")
            if wasRunning { Task { @MainActor in self.openClaude(activate: false) } }
            if case .error(let code, let message) = result {
                Task { @MainActor in
                    if code == "bad_config" { self.claudeState = "bad_config" }
                    self.claudePhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                }
                return
            }
            // Claude Code — уже при открытом Claude: его CLI медленный.
            let code = runtime.runSetup(connect ? "claude-code-connect" : "claude-code-disconnect")
            let status = runtime.runSetup("status")
            Task { @MainActor in
                if connect, case .ok(let r) = code {
                    self.claudeCodeResult = r.string("claude_code")
                    self.claudeCodeCommand = r.string("command")
                }
                if case .ok(let r) = status { self.claudeState = r.string("claude") }
                self.claudePhase = .done
            }
        }
    }

    /// Claude Desktop нет — только Claude Code.
    func connectClaudeCodeOnly() {
        guard claudePhase != .running else { return }
        claudePhase = .running
        queue.async { [runtime] in
            let result = runtime.runSetup("claude-code-connect")
            Task { @MainActor in
                switch result {
                case .ok(let r):
                    self.claudeCodeResult = r.string("claude_code")
                    self.claudeCodeCommand = r.string("command")
                    self.claudePhase = .done
                case .error(let code, let message):
                    self.claudePhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                default:
                    self.claudePhase = .failed("Что-то пошло не так")
                }
            }
        }
    }

    func openClaude(activate: Bool = true) {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: Self.claudeBundleId)
        else { return }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = activate
        NSWorkspace.shared.openApplication(at: url, configuration: config)
    }

    // MARK: общее

    private func applySettings() {
        AppModel.shared.applyNewSettings { note in self.applyNote = note }
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
            Text("Шаг \(model.step.rawValue) из 5 · \(stepName)")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Theme.textTertiary(scheme))
            Group {
                switch model.step {
                case .speech: SpeechStepView(model: model)
                case .summary: SummaryStepView(model: model)
                case .telegram: TelegramStepView(model: model)
                case .claude:
                    ScrollView { ClaudeStepView(model: model).frame(maxWidth: .infinity, alignment: .leading) }
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
        case .claude: return "Claude"
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
                    .disabled(model.step == .claude && model.claudePhase == .running)
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
            case .claude:
                if !model.claudeConnected && model.claudeCodeResult != "added" {
                    Button("Пропустить") { model.next() }
                        .disabled(model.claudePhase == .running || model.claudeConfirm != nil)
                }
                Button("Далее") { model.next() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.claudePhase == .running || model.claudeConfirm != nil
                              || (!model.claudeConnected && model.claudeCodeResult != "added"))
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
            if !model.speechReady && model.runtimePhase != .running && model.modelPhase != .running {
                Button(isFailed ? "Повторить" : "Установить") { model.installSpeech() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
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
                if mode == "api" { modelPicker }
                HStack(spacing: 8) {
                    Button("Инструкция для фоллоу-апов…") { model.openInstructions() }
                    Text(model.instructionsCustom ? "своя" : "стандартная")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textTertiary(scheme))
                }
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

    private var modelPicker: some View {
        let known = RuntimeLogic.summaryModels.first { $0.id == model.summaryModel }
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Модель").font(.system(size: 13, weight: .semibold))
                Spacer()
                if model.summaryModelPhase == .running { ProgressView().controlSize(.small) }
                Picker("", selection: Binding(get: { model.summaryModel },
                                              set: { model.setSummaryModel($0) })) {
                    ForEach(RuntimeLogic.summaryModels, id: \.id) { Text($0.title).tag($0.id) }
                    if known == nil {
                        Text("\(model.summaryModel) (вручную)").tag(model.summaryModel)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .fixedSize()
                .disabled(model.summaryModelPhase == .running)
            }
            Text(known?.caption ?? "Модель задана вручную в config.env")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
            if case .failed(let text) = model.summaryModelPhase {
                Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.rowFill(scheme)))
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
                    Text("2. Нажми «Подключить» — откроется @\(bot), там нажми «Запустить».")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textSecondary(scheme))
                    HStack {
                        if model.connectPhase == .running {
                            ProgressView().controlSize(.small)
                            Text("Жду «Запустить» в Telegram…").font(.system(size: 12))
                            Button("Открыть ещё раз") { model.openBot() }
                        } else {
                            Button(model.connectPhase == .idle ? "Подключить @\(bot)" : "Ещё раз") {
                                model.connectTelegram()
                            }
                            .buttonStyle(.borderedProminent)
                        }
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

private struct ClaudeStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            StepHeader(title: "Встречи в Claude",
                       lead: "По желанию. Подключи BeeDott к Claude Desktop — и спрашивай Claude про свои встречи.")
            if model.claudeChecking {
                busyRow("Проверяю Claude…")
            } else if let connect = model.claudeConfirm {
                Text("Claude перезапустится, открытые чаты сохранятся.").font(.system(size: 13))
                HStack {
                    Button(connect ? "Перезапустить и подключить" : "Перезапустить и отключить") {
                        model.performClaude(connect: connect)
                    }
                    .buttonStyle(.borderedProminent)
                    Button("Отмена") { model.claudeConfirm = nil }
                }
            } else if model.claudePhase == .running {
                busyRow(model.claudeConnected ? "Отключаю…" : "Подключаю…")
            } else {
                content
            }
            if case .failed(let text) = model.claudePhase {
                Text(text)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.recordingRed(scheme))
                    .fixedSize(horizontal: false, vertical: true)
            }
            codeResult
        }
        .onAppear { model.refreshClaude() }
    }

    @ViewBuilder private var content: some View {
        switch RuntimeLogic.claudeConnection(model.claudeState) {
        case .connected:
            HStack {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green(scheme))
                Text("Claude подключён").font(.system(size: 13))
                Spacer()
                if model.claudeDesktopInstalled { Button("Открыть Claude") { model.openClaude() } }
                Button("Отключить") { model.claudeAction(connect: false) }
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 10).fill(Theme.rowFill(scheme)))
            Text("Открой Claude и спроси: «что решили на последней встрече?»")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
        case .badConfig:
            Text(RuntimeLogic.setupErrorText(code: "bad_config", message: SetupModel.claudeConfigHint))
                .font(.system(size: 12))
                .foregroundStyle(Theme.orange(scheme))
                .fixedSize(horizontal: false, vertical: true)
            Button("Проверить снова") { model.refreshClaude() }
        case .disconnected, .stale:
            if model.claudeDesktopInstalled {
                examples
                Button(RuntimeLogic.claudeConnection(model.claudeState) == .stale
                       ? "Переподключить" : "Подключить") { model.claudeAction(connect: true) }
                    .buttonStyle(.borderedProminent)
            } else {
                Text("Claude Desktop не установлен.")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textSecondary(scheme))
                HStack {
                    Link("Скачать Claude", destination: URL(string: "https://claude.ai/download")!)
                    Button("Проверить снова") { model.refreshClaude() }
                    Spacer()
                    Button("У меня только Claude Code") { model.connectClaudeCodeOnly() }
                        .buttonStyle(.link)
                }
                .font(.system(size: 12))
            }
        }
    }

    private var examples: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Self.exampleRequests, id: \.self) { request in
                Text("«\(request)»").font(.system(size: 12))
            }
            Text(Self.dataNote)
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 4)
        }
    }

    // Этап 1 — только чтение; этапы 2 и 3 дописывают правку и запись.
    static let exampleRequests = [
        "Что решили на последней встрече?",
        "Какие задачи на мне за неделю?",
        "Где обсуждали минимальную корзину?",
    ]
    static let dataNote = "Claude видит встречу, только когда сам её запрашивает; ответы идут в твой аккаунт Claude."

    @ViewBuilder private var codeResult: some View {
        switch model.claudeCodeResult {
        case "added":
            Label(model.claudeConnected ? "Подключено и к Claude Code" : "Подключено к Claude Code",
                  systemImage: "checkmark.circle.fill")
                .font(.system(size: 12))
        case "failed":
            Text("Claude Code не подключился сам — выполни команду в терминале.")
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
        case "absent":
            Text("Claude Code не найден. Если он у тебя есть — выполни команду в терминале.")
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
        default:
            EmptyView()
        }
        if let command = model.claudeCodeCommand {
            DisclosureGroup("Команда для Claude Code") {
                VStack(alignment: .leading, spacing: 6) {
                    ScrollView {
                        Text(command)
                            .font(.system(size: 11, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(maxHeight: 72)
                    Button("Скопировать") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(command, forType: .string)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.system(size: 12))
        }
    }

    private func busyRow(_ text: String) -> some View {
        HStack(spacing: 6) {
            ProgressView().controlSize(.small)
            Text(text).font(.system(size: 12))
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
                row(model.claudeConnected ? "Claude — подключён"
                                          : "Claude — не подключён (можно позже: меню → Настройка…)",
                    ok: model.claudeConnected)
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
    private static var current: SetupModel?

    static func show(status: SetupStatus?) {
        // Закрыли окно посреди установки и открыли снова — показываем ту же модель:
        // новая запустила бы вторую загрузку параллельно первой.
        if let current, current.isBusy {
            NSApp.activate(ignoringOtherApps: true)
            controller?.showWindow(nil)
            return
        }
        let model = SetupModel(status: status)
        current = model
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
