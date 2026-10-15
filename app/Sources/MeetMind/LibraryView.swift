// LibraryView.swift — Библиотека встреч + карточка встречи (экраны 05-06
// design_handoff_meetmind). Окно 720×520: тулбар (поиск FTS5 + чипы-фильтры),
// список по дням, пустые состояния; детальный пуш: табы Фоллоу-ап/Транскрипт,
// панель действий, статус-лента пайплайна.

import SwiftUI
import AppKit
import Combine
import UniformTypeIdentifiers

// MARK: - модель окна

@MainActor
final class LibraryModel: ObservableObject {
    @Published var meetings: [MeetingRow] = []
    @Published var searchQuery = ""
    @Published var searchHits: [SearchHit] = []
    @Published var filter: Filter = .all
    @Published var selected: SessionDetail?
    @Published var selectedError: String?
    @Published var loading = false
    /// «Хранить аудио» до подтверждения демоном (P10) — тоггл не прыгает назад.
    @Published var keepAudioPending: Bool?
    @Published private(set) var importing = false

    enum Filter: String, CaseIterable {
        case all = "Все", today = "Сегодня", week = "Неделя", errors = "С ошибками"
    }

    /// Аудио и видео со звуком (P13); ffmpeg демона понимает и то, чего не знает система.
    static let importableTypes: [UTType] = {
        let known: [UTType] = [.audio, .mpeg4Audio, .mp3, .wav, .aiff,
                               .movie, .mpeg4Movie, .quickTimeMovie]
        let extra = ["ogg", "opus", "flac", "webm", "mkv", "aac", "amr"]
            .compactMap { UTType(filenameExtension: $0) }
        return known + extra
    }()

    // Все вызовы сокета — через ControlClient.background (не на главном потоке).
    // Поколения отбрасывают устаревшие ответы: session_detail, пришедший после
    // «назад»/удаления/открытия другой встречи, и старый sessions_list.
    private var searchTask: Task<Void, Never>?
    private var detailGeneration = 0
    private var listGeneration = 0
    private var keepAudioInFlight = 0
    private var changesObserver: AnyCancellable?

    init() {
        // AppModel и так раз в 3 с тянет sessions_list (дропдаун). Поменялись
        // строки — обработка дошла, импорт готов, встречу удалили — подтягиваем
        // список и открытую карточку, если окно на экране.
        changesObserver = AppModel.shared.$recentMeetings
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] _ in self?.backgroundChanged() }
    }

    func refresh() {
        listGeneration += 1
        let generation = listGeneration
        Task { [weak self] in
            let rows = (try? await ControlClient.background {
                try ControlClient.sessionsList()
            }) ?? []
            guard let self, self.listGeneration == generation else { return }
            self.meetings = rows
        }
    }

    func queryChanged(_ query: String) {
        searchTask?.cancel()
        let trimmed = query.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else {
            searchHits = []
            return
        }
        // live-поиск с debounce 150 мс (макет, экран 05)
        searchTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 150_000_000)
            guard !Task.isCancelled else { return }
            let hits = (try? await ControlClient.background {
                try ControlClient.search(trimmed)
            }) ?? []
            guard !Task.isCancelled else { return }
            self?.searchHits = hits
        }
    }

    func open(sessionId: String) {
        detailGeneration += 1
        let generation = detailGeneration
        loading = true
        selectedError = nil
        Task { [weak self] in
            do {
                let detail = try await ControlClient.background {
                    try ControlClient.sessionDetail(sessionId)
                }
                guard let self, self.detailGeneration == generation else { return }
                if self.keepAudioInFlight == 0 || self.selected?.session.id != sessionId {
                    self.keepAudioPending = nil
                }
                self.selected = detail
                self.loading = false
            } catch {
                guard let self, self.detailGeneration == generation else { return }
                self.selectedError = error.localizedDescription
                self.loading = false
            }
        }
    }

    /// Перечитать открытую карточку (после правки или фоновых изменений).
    func reloadSelected() {
        guard let sid = selected?.session.id else { return }
        open(sessionId: sid)
    }

    /// К списку: закрыть карточку и отбросить её запросы в полёте.
    func showList() {
        detailGeneration += 1
        selected = nil
        keepAudioPending = nil
        loading = false
    }

    func closeDetail() {
        showList()
        refresh()
    }

    private func backgroundChanged() {
        guard LibraryWindow.isVisible else { return }
        refresh()
        reloadSelected()
    }

    /// После правки: список и открытая карточка, если это она.
    private func afterMutation(_ sessionId: String) {
        refresh()
        if selected?.session.id == sessionId { reloadSelected() }
    }

    func resummarize() {
        guard let sid = selected?.session.id else { return }
        Task { [weak self] in
            do {
                try await ControlClient.background {
                    try ControlClient.resummarize(sid)
                }
            } catch {
                NotificationManager.shared.postLocal(
                    title: "Пересобрать не получилось",
                    message: ControlClientError.userMessage(error),
                    sessionId: sid)
            }
            if self?.selected?.session.id == sid { self?.reloadSelected() }
        }
    }

    func retry(sessionId: String) {
        Task { [weak self] in
            try? await ControlClient.background { try ControlClient.reprocess(sessionId) }
            self?.refresh()
        }
    }

    // --- P8: переименование

    /// completion(false) — вьюха откатывает оптимистичный заголовок.
    func rename(sessionId: String, title: String,
                completion: @escaping @MainActor (Bool) -> Void) {
        Task { [weak self] in
            do {
                try await ControlClient.background {
                    try ControlClient.renameSession(sessionId, title: title)
                }
                completion(true)
                self?.afterMutation(sessionId)
            } catch {
                completion(false)
                LibraryWindow.showError("Не удалось переименовать встречу", error)
            }
        }
    }

    // --- P10: хранение аудио

    func setKeepAudio(sessionId: String, keep: Bool) {
        keepAudioPending = keep
        keepAudioInFlight += 1
        Task { [weak self] in
            do {
                try await ControlClient.background {
                    try ControlClient.setKeepAudio(sessionId, keep: keep)
                }
                self?.keepAudioInFlight -= 1
                self?.afterMutation(sessionId)
            } catch {
                self?.keepAudioInFlight -= 1
                self?.keepAudioPending = nil
                LibraryWindow.showError("Не удалось изменить хранение аудио", error)
            }
        }
    }

    // --- P14: удаление

    func confirmDelete(_ session: SessionRow) {
        let alert = NSAlert()
        alert.messageText = "Удалить встречу «\(session.displayTitle)»?"
        alert.informativeText = "Аудио, транскрипт и саммари будут удалены с этого Mac "
            + "без возможности восстановления. Заметка в ~/MeetMind и сообщение "
            + "в Telegram останутся."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Отмена")
        let delete = alert.addButton(withTitle: "Удалить")
        delete.hasDestructiveAction = true
        let sid = session.id
        LibraryWindow.present(alert) { [weak self] response in
            guard response == .alertSecondButtonReturn else { return }
            self?.delete(sessionId: sid)
        }
    }

    private func delete(sessionId: String) {
        Task { [weak self] in
            do {
                try await ControlClient.background { try ControlClient.deleteSession(sessionId) }
                guard let self else { return }
                if self.selected?.session.id == sessionId { self.showList() }
                self.meetings.removeAll { $0.id == sessionId }
                self.searchHits.removeAll { $0.sessionId == sessionId }
                self.refresh()
            } catch {
                // busy — встречу сейчас пишут/обрабатывают: message демона по-русски.
                LibraryWindow.showError("Не удалось удалить встречу", error)
            }
        }
    }

    // --- P13: импорт аудиофайла

    func chooseAudioToImport() {
        guard !importing else { return }
        let panel = NSOpenPanel()
        panel.title = "Импортировать аудио"
        panel.prompt = "Импортировать"
        panel.message = "Аудио или видео встречи — BeeDott расшифрует его и сделает фоллоу-ап."
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = Self.importableTypes
        let handler: (NSApplication.ModalResponse) -> Void = { [weak self] response in
            guard response == .OK, let url = panel.url else { return }
            self?.importAudio(url)
        }
        if let window = LibraryWindow.window {
            panel.beginSheetModal(for: window, completionHandler: handler)
        } else {
            NSApp.activate(ignoringOtherApps: true)
            panel.begin(completionHandler: handler)
        }
    }

    private func importAudio(_ url: URL) {
        let path = url.path
        importing = true
        Task { [weak self] in
            do {
                let sid = try await ControlClient.background {
                    try ControlClient.importAudio(path: path)
                }
                guard let self else { return }
                self.importing = false
                // Чтобы новая встреча была видна и после «назад» к списку.
                self.filter = .all
                self.searchQuery = ""
                self.searchHits = []
                self.refresh()
                self.open(sessionId: sid)
            } catch {
                self?.importing = false
                LibraryWindow.showError("Не удалось импортировать «\(url.lastPathComponent)»", error)
            }
        }
    }

    var filtered: [MeetingRow] {
        let cal = Calendar.current
        return meetings.filter { m in
            switch filter {
            case .all: return true
            case .errors: return m.status == "error"
            case .today:
                guard let d = m.startedDate else { return false }
                return cal.isDateInToday(d)
            case .week:
                guard let d = m.startedDate else { return false }
                return d > Date().addingTimeInterval(-7 * 86400)
            }
        }
    }

    var errorCount: Int { meetings.filter { $0.status == "error" }.count }

    var totalLine: String {
        let hours = meetings.compactMap(\.durationMin).reduce(0, +) / 60
        return "\(meetings.count) встреч · \(Int(hours.rounded())) часов"
    }

    /// Группировка по дням: «Сегодня» / «Вчера» / «12 июля».
    var grouped: [(day: String, rows: [MeetingRow])] {
        let cal = Calendar.current
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "ru_RU")
        fmt.dateFormat = "d MMMM"
        var order: [String] = []
        var buckets: [String: [MeetingRow]] = [:]
        for m in filtered {
            let day: String
            if let d = m.startedDate {
                if cal.isDateInToday(d) { day = "Сегодня" }
                else if cal.isDateInYesterday(d) { day = "Вчера" }
                else { day = fmt.string(from: d) }
            } else { day = "Без даты" }
            if buckets[day] == nil { order.append(day) }
            buckets[day, default: []].append(m)
        }
        return order.map { ($0, buckets[$0]!) }
    }
}

// MARK: - окно

@MainActor
enum LibraryWindow {
    private static var controller: NSWindowController?
    static let model = LibraryModel()

    /// Окно Библиотеки, если оно на экране (родитель для листов NSAlert/NSOpenPanel).
    static var window: NSWindow? {
        guard let window = controller?.window, window.isVisible else { return nil }
        return window
    }

    static var isVisible: Bool { window != nil }

    static func show(sessionId: String?) {
        if controller == nil {
            let view = LibraryRootView(model: model)
            let hosting = NSHostingController(rootView: view)
            let window = NSWindow(contentViewController: hosting)
            window.title = "BeeDott — Встречи"
            window.setContentSize(NSSize(width: 720, height: 520))
            window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            window.isReleasedWhenClosed = false
            window.center()
            controller = NSWindowController(window: window)
        }
        model.refresh()
        if let sessionId {
            model.open(sessionId: sessionId)
        } else {
            model.showList()
        }
        NSApp.activate(ignoringOtherApps: true)
        controller?.showWindow(nil)
    }

    /// NSAlert листом на окне Библиотеки — не блокирует главный поток. Без окна —
    /// модальный цикл через RunLoop (как confirmCancelRecording в AppModel):
    /// runModal внутри MainActor-задачи застопорил бы главную очередь.
    static func present(_ alert: NSAlert,
                        completion: @escaping @MainActor (NSApplication.ModalResponse) -> Void = { _ in }) {
        if let window {
            alert.beginSheetModal(for: window) { response in
                MainActor.assumeIsolated { completion(response) }
            }
        } else {
            RunLoop.main.perform(inModes: [.common]) {
                MainActor.assumeIsolated {
                    NSApp.activate(ignoringOtherApps: true)
                    completion(alert.runModal())
                }
            }
        }
    }

    /// Ошибка действия в Библиотеке: у ошибок демона — его русский message.
    static func showError(_ title: String, _ error: Error) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = ControlClientError.userMessage(error)
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        present(alert)
    }
}

struct LibraryRootView: View {
    @ObservedObject var model: LibraryModel

    var body: some View {
        Group {
            if let detail = model.selected {
                // id: при переходе к другой встрече состояние карточки (правка
                // заголовка, вкладка) не переезжает на неё.
                MeetingDetailView(model: model, detail: detail)
                    .id(detail.session.id)
            } else {
                LibraryListView(model: model)
            }
        }
        .frame(minWidth: 680, minHeight: 480)
    }
}

// MARK: - экран 05: список

struct LibraryListView: View {
    @ObservedObject var model: LibraryModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Rectangle().fill(Theme.divider(scheme)).frame(height: 1)
            content
        }
        .onAppear { model.refresh() }
    }

    private var toolbar: some View {
        HStack(spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.textSecondary(scheme))
                TextField("Найти по словам из встречи…", text: $model.searchQuery)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12))
                    .onChange(of: model.searchQuery) { _, q in model.queryChanged(q) }
                if !model.searchQuery.isEmpty {
                    Button {
                        model.searchQuery = ""
                        model.searchHits = []
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(Theme.textTertiary(scheme))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 8)
            .frame(height: 28)
            .background(Theme.chipFill(scheme), in: RoundedRectangle(cornerRadius: 7))

            ForEach(LibraryModel.Filter.allCases, id: \.self) { f in
                FilterChip(
                    title: f.rawValue,
                    badge: f == .errors && model.errorCount > 0 ? model.errorCount : nil,
                    active: model.filter == f
                ) { model.filter = f }
            }

            Spacer()

            Text(model.searchQuery.isEmpty
                 ? model.totalLine
                 : "\(model.searchHits.count) совпадения")
                .font(.system(size: 11))
                .foregroundStyle(Theme.textTertiary(scheme))

            importButton
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    /// Импорт аудиофайла (P13). Иконкой: строка тулбара и так плотная (поиск +
    /// 4 чипа + итог), подпись — в подсказке и в пустом состоянии.
    private var importButton: some View {
        Button {
            model.chooseAudioToImport()
        } label: {
            Group {
                if model.importing {
                    ProgressView().controlSize(.small).scaleEffect(0.6)
                } else {
                    Image(systemName: "square.and.arrow.down")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Theme.textSecondary(scheme))
                }
            }
            .frame(width: 28, height: 28)
            .background(Theme.chipFill(scheme), in: RoundedRectangle(cornerRadius: 7))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(model.importing)
        .help("Импортировать аудио…")
        .accessibilityLabel("Импортировать аудио…")
    }

    @ViewBuilder private var content: some View {
        if !model.searchQuery.trimmingCharacters(in: .whitespaces).isEmpty {
            searchResults
        } else if model.filtered.isEmpty {
            emptyState
        } else {
            meetingList
        }
    }

    private var meetingList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 2) {
                ForEach(model.grouped, id: \.day) { group in
                    Text(group.day.uppercased())
                        .font(.system(size: 11, weight: .bold))
                        .kerning(0.7)
                        .foregroundStyle(Theme.textTertiary(scheme))
                        .padding(.horizontal, 10)
                        .padding(.top, 10)
                        .padding(.bottom, 4)
                    ForEach(group.rows) { m in
                        MeetingListRow(meeting: m) {
                            if m.status == "error" { model.retry(sessionId: m.id) }
                            model.open(sessionId: m.id)
                        }
                    }
                }
            }
            .padding(.horizontal, 6)
            .padding(.bottom, 8)
        }
    }

    private var searchResults: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 2) {
                if model.searchHits.isEmpty {
                    emptySearch
                } else {
                    ForEach(model.searchHits) { hit in
                        SearchHitRow(hit: hit) { model.open(sessionId: hit.sessionId) }
                    }
                }
            }
            .padding(6)
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Spacer()
            Image(systemName: "waveform")
                .font(.system(size: 26))
                .opacity(0.3)
            Text("Пока ни одной встречи")
                .font(.system(size: 13, weight: .semibold))
            Text("Нажмите ⌘⇧R или кнопку в меню — первая встреча появится здесь сама.")
                .font(.system(size: 11.5))
                .foregroundStyle(Theme.textTertiary(scheme))
            Button("Импортировать аудио…") { model.chooseAudioToImport() }
                .buttonStyle(.plain)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Theme.accent(scheme))
                .disabled(model.importing)
                .padding(.top, 2)
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    private var emptySearch: some View {
        VStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 24))
                .opacity(0.3)
            Text("Ничего не нашлось")
                .font(.system(size: 13, weight: .semibold))
            Button("Сбросить поиск") {
                model.searchQuery = ""
                model.searchHits = []
            }
            .buttonStyle(.plain)
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(Theme.accent(scheme))
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 120)
    }
}

struct FilterChip: View {
    let title: String
    let badge: Int?
    let active: Bool
    let action: () -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Text(title)
                    .font(.system(size: 11.5, weight: active ? .semibold : .regular))
                if let badge {
                    Text("\(badge)")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(active ? .white : Theme.orange(scheme))
                        .padding(.horizontal, 4)
                        .frame(height: 15)
                        .background(
                            active ? Color.white.opacity(0.25)
                                   : Theme.orange(scheme).opacity(0.14),
                            in: Capsule())
                }
            }
            .foregroundStyle(active ? .white : .primary)
            .padding(.horizontal, 10)
            .frame(height: 24)
            .background(active ? Theme.accent(scheme) : Theme.chipFill(scheme),
                        in: Capsule())
        }
        .buttonStyle(.plain)
    }
}

struct MeetingListRow: View {
    let meeting: MeetingRow
    let action: () -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        HoverRow(action: action) {
            HStack(spacing: 10) {
                statusIcon.frame(width: 16)
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 5) {
                        Text(meeting.displayTitle)
                            .font(.system(size: 13, weight: .medium))
                            .lineLimit(1)
                        if meeting.keepsAudio {
                            Image(systemName: "lock.fill")
                                .font(.system(size: 9))
                                .foregroundStyle(Theme.textTertiary(scheme))
                                .help("Аудио хранится")
                        }
                    }
                    metaLine
                        .font(.system(size: 11))
                        .lineLimit(1)
                }
                Spacer()
                if meeting.isClaude {
                    Image(systemName: "sparkle")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.claudeSpark)
                } else if meeting.engine != nil {
                    Image(systemName: "cpu")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textTertiary(scheme))
                }
                Text(meeting.actionItems.map { "\($0) задач" } ?? "—")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Theme.textSecondary(scheme))
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Theme.chipFill(scheme), in: RoundedRectangle(cornerRadius: 5))
            }
        }
    }

    @ViewBuilder private var statusIcon: some View {
        switch meeting.status {
        case "delivered":
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 14))
                .foregroundStyle(Theme.green(scheme))
        case "error":
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 13))
                .foregroundStyle(Theme.orange(scheme))
        default:
            ProgressView().controlSize(.small).scaleEffect(0.65)
        }
    }

    @ViewBuilder private var metaLine: some View {
        switch meeting.status {
        case "error":
            Text(meeting.error ?? "Ошибка — нажмите, чтобы повторить")
                .foregroundStyle(Theme.orange(scheme))
        case "processing":
            Text(processingLabel).foregroundStyle(Theme.accent(scheme))
        default:
            Text(deliveredMeta).foregroundStyle(Theme.textTertiary(scheme))
        }
    }

    private var processingLabel: String {
        switch meeting.state {
        case "RECORDED", "TRANSCRIBING": return "Транскрипция…"
        case "TRANSCRIBED", "SUMMARIZING": return "Саммари…"
        case "SUMMARIZED", "DELIVERING": return "Доставка…"
        case "RECORDING": return "Идёт запись"
        default: return "Обработка…"
        }
    }

    private var deliveredMeta: String {
        var parts: [String] = []
        if let d = meeting.startedDate {
            let fmt = DateFormatter()
            fmt.dateFormat = "HH:mm"
            parts.append(fmt.string(from: d))
        }
        if let mins = meeting.durationMin { parts.append("\(Int(mins.rounded())) мин") }
        for s in meeting.sinks {
            let name = s.sink == "telegram" ? "Telegram"
                : s.sink == "note" ? "Заметка" : s.sink.capitalized
            parts.append("\(name) \(s.status == "delivered" ? "✓" : "✕")")
        }
        return parts.joined(separator: " · ")
    }
}

struct SearchHitRow: View {
    let hit: SearchHit
    let action: () -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        HoverRow(action: action) {
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(hit.title?.isEmpty == false ? hit.title! : "Без названия")
                        .font(.system(size: 13, weight: .semibold))
                    Spacer()
                    Text(hit.source == "transcript" ? "транскрипт" : "саммари")
                        .font(.system(size: 10.5))
                        .foregroundStyle(Theme.textTertiary(scheme))
                }
                snippetText
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textSecondary(scheme))
                    .lineLimit(2)
            }
        }
    }

    /// Сниппет из FTS5 с маркерами [[слово]] → подсветка жёлтым (макет).
    private var snippetText: Text {
        var result = Text("")
        var rest = hit.snippet[...]
        while let open = rest.range(of: "[[") {
            result = result + Text(rest[..<open.lowerBound])
            rest = rest[open.upperBound...]
            if let close = rest.range(of: "]]") {
                result = result + Text(rest[..<close.lowerBound])
                    .fontWeight(.semibold)
                    .foregroundColor(.primary)
                rest = rest[close.upperBound...]
            } else { break }
        }
        return result + Text(rest)
    }
}

// MARK: - экран 06: карточка встречи

struct MeetingDetailView: View {
    @ObservedObject var model: LibraryModel
    let detail: SessionDetail
    @State private var tab: Tab = .followup
    @State private var copiedTimecode: String?
    // P8: переименование — Enter/потеря фокуса сохраняет, Esc отменяет.
    @State private var editingTitle = false
    @State private var titleDraft = ""
    /// Новое имя до ответа демона — без мигания старого заголовка.
    @State private var optimisticTitle: String?
    @FocusState private var titleFocused: Bool
    @Environment(\.colorScheme) private var scheme

    enum Tab: String, CaseIterable {
        case followup = "Фоллоу-ап", transcript = "Транскрипт"
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            tabs
            content
            actionBar
            pipelineStrip
        }
    }

    private var header: some View {
        HStack(alignment: .top) {
            Button {
                model.closeDetail()
            } label: {
                Image(systemName: "chevron.left")
                    .font(.system(size: 13, weight: .semibold))
            }
            .buttonStyle(.plain)
            .padding(.top, 4)

            VStack(alignment: .leading, spacing: 2) {
                titleView
                Text(metaLine)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textTertiary(scheme))
                if !detail.session.attendeeNames.isEmpty {
                    Text("Участники: " + detail.session.attendeeNames.joined(separator: ", "))
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textSecondary(scheme))
                        .lineLimit(2)
                        .textSelection(.enabled)
                        .padding(.top, 2)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 8) {
                HStack(spacing: 4) {
                    Image(systemName: "lock.fill").font(.system(size: 10))
                    Text(detail.followup?.engine?.lowercased().contains("claude") == true
                         ? "Саммари · Claude" : "Локально · Whisper large-v3")
                        .font(.system(size: 11, weight: .medium))
                }
                .foregroundStyle(Theme.green(scheme))
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Theme.green(scheme).opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6)
                    .stroke(Theme.green(scheme).opacity(0.28), lineWidth: 1))

                keepAudioToggle
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .padding(.bottom, 10)
        .onChange(of: detail.session.title) { _, _ in optimisticTitle = nil }
    }

    // --- P8: заголовок с переименованием

    @ViewBuilder private var titleView: some View {
        if editingTitle {
            TextField("Название встречи", text: $titleDraft)
                .textFieldStyle(.plain)
                .font(.system(size: 18, weight: .bold))
                .focused($titleFocused)
                .onSubmit { commitTitle() }
                .onExitCommand { cancelTitleEdit() }
                .onChange(of: titleFocused) { _, focused in
                    if !focused { commitTitle() }
                }
                .onAppear { DispatchQueue.main.async { titleFocused = true } }
                .padding(.horizontal, 6)
                .padding(.vertical, 1)
                .background(Theme.chipFill(scheme), in: RoundedRectangle(cornerRadius: 6))
                .padding(.horizontal, -6)
                .frame(maxWidth: 420, alignment: .leading)
        } else {
            HStack(spacing: 6) {
                Text(optimisticTitle ?? detail.session.displayTitle)
                    .font(.system(size: 18, weight: .bold))
                    .onTapGesture { beginTitleEdit() }
                Button {
                    beginTitleEdit()
                } label: {
                    Image(systemName: "pencil")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Theme.textTertiary(scheme))
                }
                .buttonStyle(.plain)
                .help("Переименовать")
            }
        }
    }

    private func beginTitleEdit() {
        titleDraft = optimisticTitle ?? detail.session.title ?? ""
        editingTitle = true
    }

    private func cancelTitleEdit() {
        editingTitle = false
    }

    /// Enter и потеря фокуса приходят оба — сохраняет только первый.
    private func commitTitle() {
        guard editingTitle else { return }
        editingTitle = false
        let newTitle = titleDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        let current = optimisticTitle ?? detail.session.title ?? ""
        guard !newTitle.isEmpty, newTitle != current else { return }
        optimisticTitle = newTitle
        model.rename(sessionId: detail.session.id, title: newTitle) { ok in
            if !ok { optimisticTitle = nil }
        }
    }

    // --- P10: хранение аудио

    private var keepAudioToggle: some View {
        let session = detail.session
        // Аудио нет и уже не появится (обработка закончена). Пока идёт запись или
        // конвертация импорта, файла может ещё не быть — тоггл доступен.
        let audioGone = !session.hasAudioFile && session.isFinished
        return VStack(alignment: .trailing, spacing: 2) {
            Toggle(isOn: Binding(
                get: { model.keepAudioPending ?? session.keepsAudio },
                set: { model.setKeepAudio(sessionId: session.id, keep: $0) })
            ) {
                Text("Хранить аудио")
                    .font(.system(size: 11.5))
                    .foregroundStyle(audioGone ? Theme.textTertiary(scheme) : Theme.textSecondary(scheme))
            }
            .toggleStyle(.switch)
            .controlSize(.mini)
            .disabled(audioGone)
            .help(audioGone ? "Аудио этой встречи уже удалено"
                            : "Не удалять аудио этой встречи автоматически")
            if audioGone {
                Text("аудио уже удалено")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textTertiary(scheme))
            }
        }
    }

    private var metaLine: String {
        var parts: [String] = []
        if let d = ISO8601DateFormatter().date(from: detail.session.startedAt) {
            let fmt = DateFormatter()
            fmt.locale = Locale(identifier: "ru_RU")
            fmt.dateFormat = "d MMMM, HH:mm"
            parts.append(fmt.string(from: d))
        }
        if let end = detail.session.endedAt,
           let s = ISO8601DateFormatter().date(from: detail.session.startedAt),
           let e = ISO8601DateFormatter().date(from: end) {
            parts.append("\(Int(e.timeIntervalSince(s) / 60)) мин")
        }
        return parts.joined(separator: " · ")
    }

    private var tabs: some View {
        HStack(spacing: 2) {
            ForEach(Tab.allCases, id: \.self) { t in
                Button {
                    withAnimation(.spring(duration: 0.25)) { tab = t }
                } label: {
                    Text(t.rawValue)
                        .font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 4)
                        .background(
                            tab == t
                                ? (scheme == .dark ? Color.white.opacity(0.16) : .white)
                                : .clear,
                            in: RoundedRectangle(cornerRadius: 6))
                        .shadow(color: tab == t ? .black.opacity(0.12) : .clear,
                                radius: 1, y: 1)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(2)
        .background(Theme.chipFill(scheme), in: RoundedRectangle(cornerRadius: 8))
        .padding(.horizontal, 24)
        .padding(.bottom, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private var content: some View {
        ScrollView {
            switch tab {
            case .followup: followupView
            case .transcript: transcriptView
            }
        }
        .frame(maxHeight: .infinity)
    }

    // --- фоллоу-ап

    @ViewBuilder private var followupView: some View {
        if let f = detail.followup {
            VStack(alignment: .leading, spacing: 14) {
                section("Контекст") {
                    Text(f.context.isEmpty ? "нет" : f.context)
                        .font(.system(size: 13))
                        .lineSpacing(4)
                }
                section("Решения") {
                    bullets(f.decisions, square: true)
                }
                section("Задачи") {
                    if f.actionItems.isEmpty {
                        Text("нет").font(.system(size: 13))
                            .foregroundStyle(Theme.textTertiary(scheme))
                    }
                    ForEach(f.actionItems) { item in
                        HStack(spacing: 8) {
                            Image(systemName: "square")
                                .font(.system(size: 13))
                                .foregroundStyle(Theme.textTertiary(scheme))
                            Text("**\(item.owner)** — \(item.task)")
                                .font(.system(size: 13))
                            Spacer()
                            if let dl = item.deadline {
                                Text(dl)
                                    .font(.system(size: 11, weight: .medium))
                                    .foregroundStyle(Theme.orange(scheme))
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 2)
                                    .background(Theme.orange(scheme).opacity(0.12),
                                                in: RoundedRectangle(cornerRadius: 5))
                            }
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 9)
                        .background(Theme.rowFill(scheme), in: RoundedRectangle(cornerRadius: 8))
                    }
                }
                section("Открытые вопросы") {
                    bullets(f.openQuestions, square: false)
                }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 16)
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            VStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Фоллоу-ап ещё не готов")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textTertiary(scheme))
            }
            .padding(.top, 60)
        }
    }

    private func section(_ title: String, @ViewBuilder body: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .font(.system(size: 11, weight: .bold))
                .kerning(0.8)
                .foregroundStyle(Theme.textTertiary(scheme))
            body()
        }
    }

    @ViewBuilder private func bullets(_ items: [String], square: Bool) -> some View {
        if items.isEmpty {
            Text("нет").font(.system(size: 13))
                .foregroundStyle(Theme.textTertiary(scheme))
        }
        ForEach(items, id: \.self) { item in
            HStack(alignment: .top, spacing: 8) {
                Group {
                    if square {
                        Rectangle().fill(Theme.accent(scheme))
                    } else {
                        Circle().fill(Theme.textTertiary(scheme))
                    }
                }
                .frame(width: 5, height: 5)
                .padding(.top, 6)
                Text(item).font(.system(size: 13)).lineSpacing(3)
            }
        }
    }

    // --- транскрипт

    @ViewBuilder private var transcriptView: some View {
        if let segments = detail.transcript, !segments.isEmpty {
            LazyVStack(alignment: .leading, spacing: 10) {
                ForEach(segments) { seg in
                    HStack(alignment: .top, spacing: 10) {
                        Button {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(seg.timecode, forType: .string)
                            copiedTimecode = seg.timecode
                            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                                if copiedTimecode == seg.timecode { copiedTimecode = nil }
                            }
                        } label: {
                            Text(copiedTimecode == seg.timecode ? "Скопировано" : seg.timecode)
                                .font(.system(size: 11, design: .monospaced))
                                .monospacedDigit()
                                .foregroundStyle(
                                    copiedTimecode == seg.timecode
                                        ? Theme.accent(scheme)
                                        : Theme.textTertiary(scheme).opacity(0.85))
                        }
                        .buttonStyle(.plain)
                        .frame(width: 76, alignment: .leading)

                        VStack(alignment: .leading, spacing: 1) {
                            if let speaker = seg.speaker {
                                Text(speaker)
                                    .font(.system(size: 11, weight: .semibold))
                                    .foregroundStyle(
                                        Theme.speaker(seg.speakerIndex, scheme))
                            }
                            Text(seg.text)
                                .font(.system(size: 13))
                                .lineSpacing(4)
                        }
                    }
                }
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 16)
        } else {
            Text("Транскрипт недоступен")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textTertiary(scheme))
                .padding(.top, 60)
        }
    }

    // --- действия

    private var actionBar: some View {
        HStack(spacing: 8) {
            Button {
                openFollowupNote()
            } label: {
                Label("Открыть заметку", systemImage: "doc.text")
                    .font(.system(size: 12.5, weight: .semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 12)
                    .frame(height: 30)
                    .background(Theme.accent(scheme), in: RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)

            Button {
                model.resummarize()
            } label: {
                Text("Пересобрать саммари")
                    .font(.system(size: 12.5, weight: .semibold))
                    .padding(.horizontal, 12)
                    .frame(height: 30)
                    .background(Theme.chipFill(scheme), in: RoundedRectangle(cornerRadius: 8))
            }
            .buttonStyle(.plain)

            Spacer()

            Button {
                if let dir = detail.session.sessionDir {
                    NSWorkspace.shared.activateFileViewerSelecting([dir])
                }
            } label: {
                Label("Показать в Finder", systemImage: "folder")
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.textSecondary(scheme))
            }
            .buttonStyle(.plain)

            Button {
                model.confirmDelete(detail.session)
            } label: {
                Label("Удалить встречу", systemImage: "trash")
                    .font(.system(size: 12.5))
                    .foregroundStyle(Theme.recordingRed(scheme))
            }
            .buttonStyle(.plain)
            .padding(.leading, 8)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 10)
    }

    private func openFollowupNote() {
        guard let dir = detail.session.sessionDir else { return }
        let md = dir.appendingPathComponent("followup.md")
        if FileManager.default.fileExists(atPath: md.path) {
            NSWorkspace.shared.open(md)
        } else {
            NSWorkspace.shared.activateFileViewerSelecting([dir])
        }
    }

    // --- статус-лента пайплайна

    private var pipelineStrip: some View {
        VStack(spacing: 0) {
            Rectangle().fill(Theme.divider(scheme)).frame(height: 1)
            HStack(spacing: 0) {
                ForEach(Array(detail.pipeline.enumerated()), id: \.element.id) { idx, step in
                    if idx > 0 {
                        Rectangle()
                            .fill(connectorColor(step))
                            .frame(height: 1.5)
                            .frame(maxWidth: .infinity)
                    }
                    PipelineStepView(step: step) {
                        model.retry(sessionId: detail.session.id)
                        model.open(sessionId: detail.session.id)
                    }
                }
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 11)
        }
        .background(scheme == .dark ? Color.white.opacity(0.03) : Color.black.opacity(0.025))
    }

    private func connectorColor(_ step: PipelineStep) -> Color {
        if step.ok == false { return Theme.orange(scheme) }
        if step.ok == true { return Theme.green(scheme).opacity(0.45) }
        return Theme.divider(scheme)
    }
}

struct PipelineStepView: View {
    let step: PipelineStep
    let retry: () -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(spacing: 3) {
            icon
            Text(step.label)
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(step.ok == false ? Theme.orange(scheme) : .primary)
            if let error = step.error {
                HStack(spacing: 4) {
                    Text(error)
                        .font(.system(size: 9.5))
                        .foregroundStyle(Theme.orange(scheme))
                        .lineLimit(1)
                        .frame(maxWidth: 140)
                    Button {
                        retry()
                    } label: {
                        HStack(spacing: 3) {
                            Image(systemName: "arrow.clockwise").font(.system(size: 8))
                            Text("Повторить").font(.system(size: 10.5, weight: .semibold))
                        }
                        .foregroundStyle(Theme.orange(scheme))
                        .padding(.horizontal, 6)
                        .frame(height: 22)
                        .background(Theme.orange(scheme).opacity(0.14),
                                    in: RoundedRectangle(cornerRadius: 5))
                    }
                    .buttonStyle(.plain)
                }
            } else if let at = step.at,
                      let d = ISO8601DateFormatter().date(from: at) {
                let fmt = DateFormatter()
                let _ = fmt.dateFormat = "HH:mm"
                Text(fmt.string(from: d))
                    .font(.system(size: 9.5))
                    .foregroundStyle(Theme.textTertiary(scheme))
            }
        }
    }

    @ViewBuilder private var icon: some View {
        if step.ok == true {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 13))
                .foregroundStyle(Theme.green(scheme))
        } else if step.ok == false {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 12))
                .foregroundStyle(Theme.orange(scheme))
        } else {
            ProgressView().controlSize(.small).scaleEffect(0.55)
        }
    }
}
