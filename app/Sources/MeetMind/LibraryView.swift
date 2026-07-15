// LibraryView.swift — Библиотека встреч + карточка встречи (экраны 05-06
// design_handoff_meetmind). Окно 720×520: тулбар (поиск FTS5 + чипы-фильтры),
// список по дням, пустые состояния; детальный пуш: табы Фоллоу-ап/Транскрипт,
// панель действий, статус-лента пайплайна.

import SwiftUI
import AppKit

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

    enum Filter: String, CaseIterable {
        case all = "Все", today = "Сегодня", week = "Неделя", errors = "С ошибками"
    }

    private var searchTask: Task<Void, Never>?

    func refresh() {
        Task.detached { [weak self] in
            let rows = (try? ControlClient.sessionsList()) ?? []
            await MainActor.run { self?.meetings = rows }
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
            let hits = (try? ControlClient.search(trimmed)) ?? []
            guard !Task.isCancelled else { return }
            self?.searchHits = hits
        }
    }

    func open(sessionId: String) {
        loading = true
        selectedError = nil
        Task.detached { [weak self] in
            do {
                let detail = try ControlClient.sessionDetail(sessionId)
                await MainActor.run {
                    self?.selected = detail
                    self?.loading = false
                }
            } catch {
                await MainActor.run {
                    self?.selectedError = error.localizedDescription
                    self?.loading = false
                }
            }
        }
    }

    func closeDetail() {
        selected = nil
        refresh()
    }

    func resummarize(engine: String) {
        guard let sid = selected?.session.id else { return }
        Task.detached { [weak self] in
            try? ControlClient.resummarize(sid, engine: engine)
            await MainActor.run { self?.open(sessionId: sid) }
        }
    }

    func retry(sessionId: String) {
        Task.detached { [weak self] in
            try? ControlClient.reprocess(sessionId)
            await MainActor.run { self?.refresh() }
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

    static func show(sessionId: String?) {
        if controller == nil {
            let view = LibraryRootView(model: model)
            let hosting = NSHostingController(rootView: view)
            let window = NSWindow(contentViewController: hosting)
            window.title = "MeetMind — Встречи"
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
            model.selected = nil
        }
        NSApp.activate(ignoringOtherApps: true)
        controller?.showWindow(nil)
    }
}

struct LibraryRootView: View {
    @ObservedObject var model: LibraryModel

    var body: some View {
        Group {
            if let detail = model.selected {
                MeetingDetailView(model: model, detail: detail)
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
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
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
                    Text(meeting.displayTitle)
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(1)
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
                Text(detail.session.title?.isEmpty == false
                     ? detail.session.title! : "Без названия")
                    .font(.system(size: 18, weight: .bold))
                Text(metaLine)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textTertiary(scheme))
            }
            Spacer()
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
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .padding(.bottom, 10)
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
                section("Action items") {
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

                        Text(seg.text)
                            .font(.system(size: 13))
                            .lineSpacing(4)
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

            Menu {
                Button {
                    model.resummarize(engine: "local")
                } label: {
                    Label("Локальная модель — на устройстве", systemImage: "cpu")
                }
                Button {
                    model.resummarize(engine: "claude")
                } label: {
                    Label("Claude-бустер — в облако уйдёт текст", systemImage: "sparkle")
                }
            } label: {
                Text("Пересобрать саммари")
                    .font(.system(size: 12.5, weight: .semibold))
            }
            .menuStyle(.borderlessButton)
            .frame(width: 170, height: 30)
            .background(Theme.chipFill(scheme), in: RoundedRectangle(cornerRadius: 8))

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
