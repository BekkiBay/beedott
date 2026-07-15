// DropdownView.swift — дропдаун из menubar (экран 02 design_handoff_meetmind).
// Ширина 312, padding 6, gap 2. Состояния recording/idle, уровни двух
// источников, последние 3 встречи, «Все встречи…», Claude-тоггл, футер.

import SwiftUI

struct DropdownView: View {
    @ObservedObject var model: AppModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if model.recordingSessionId != nil {
                currentMeetingRow
                levelsBlock
            } else {
                idleHeader
            }
            recordButton
                .padding(.vertical, 4)

            divider

            recentSection

            divider

            claudeRow

            divider

            footer
        }
        .padding(6)
        .frame(width: 312)
    }

    // MARK: - верх: запись / idle

    private var currentMeetingRow: some View {
        HStack(spacing: 8) {
            PulsingDot(color: Theme.recordingRed(scheme))
            VStack(alignment: .leading, spacing: 1) {
                Text(model.recordingTitle ?? "Встреча")
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                Text("Идёт запись · \(model.elapsedText)")
                    .font(.system(size: 11))
                    .monospacedDigit()
                    .foregroundStyle(Theme.recordingRed(scheme))
            }
            Spacer()
            Image(systemName: "mic.fill")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textTertiary(scheme))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
    }

    private var idleHeader: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text("Запись не идёт")
                    .font(.system(size: 13, weight: .semibold))
                HStack(spacing: 4) {
                    Text("Микрофон + системный звук")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textTertiary(scheme))
                    Image(systemName: "lock.fill")
                        .font(.system(size: 9))
                        .foregroundStyle(Theme.green(scheme))
                }
            }
            Spacer()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
    }

    private var levelsBlock: some View {
        VStack(spacing: 6) {
            LevelRow(icon: "mic.fill", label: "Микрофон",
                     level: model.micLevel, gradientTail: true)
            LevelRow(icon: "speaker.wave.2.fill", label: "Системный звук",
                     level: model.systemLevel, gradientTail: false)
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 6)
    }

    private var recordButton: some View {
        Button {
            model.toggleRecording()
        } label: {
            HStack(spacing: 7) {
                Image(systemName: model.recordingSessionId != nil
                      ? "stop.fill" : "record.circle.fill")
                    .font(.system(size: 10))
                Text(model.recordingSessionId != nil
                     ? "Остановить запись" : "Начать запись")
                    .font(.system(size: 13, weight: .semibold))
                Spacer()
                Text("⌘⇧R")
                    .font(.system(size: 10.5, weight: .medium))
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Color.white.opacity(0.16), in: RoundedRectangle(cornerRadius: 4))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 10)
            .frame(height: 32)
            .background(Theme.recordButton, in: RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .disabled(model.busy || !model.daemonHealthy)
        .opacity(model.busy || !model.daemonHealthy ? 0.5 : 1)
        .padding(.horizontal, 4)
    }

    // MARK: - последние встречи

    private var recentSection: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Последние встречи")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Theme.textTertiary(scheme))
                .padding(.horizontal, 10)
                .padding(.top, 6)

            if model.recentMeetings.isEmpty {
                Text("Пока ни одной встречи")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textTertiary(scheme))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
            } else {
                ForEach(model.recentMeetings.prefix(3)) { meeting in
                    RecentMeetingRow(meeting: meeting) {
                        model.openLibrary(sessionId: meeting.id)
                    }
                }
            }

            allMeetingsRow
        }
        .padding(.bottom, 4)
    }

    private var allMeetingsRow: some View {
        HoverRow {
            model.openLibrary(sessionId: nil)
        } content: {
            HStack(spacing: 8) {
                Image(systemName: "list.bullet")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textSecondary(scheme))
                Text("Все встречи…")
                    .font(.system(size: 13))
                Spacer()
                if model.problemCount > 0 {
                    Text("\(model.problemCount)")
                        .font(.system(size: 10.5, weight: .bold))
                        .foregroundStyle(Theme.orange(scheme))
                        .padding(.horizontal, 6)
                        .frame(height: 16)
                        .background(Theme.orange(scheme).opacity(0.14), in: Capsule())
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
                    .opacity(0.35)
            }
        }
    }

    // MARK: - Claude

    private var claudeRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Image(systemName: "sparkle")
                    .font(.system(size: 13))
                    .foregroundStyle(Theme.claudeSpark)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Claude-бустер")
                        .font(.system(size: 13))
                    Text("Улучшает саммари в облаке · opt-in")
                        .font(.system(size: 11))
                        .foregroundStyle(Theme.textTertiary(scheme))
                }
                Spacer()
                Toggle("", isOn: Binding(
                    get: { model.claudeEnabled },
                    set: { model.setClaude(enabled: $0) }))
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .labelsHidden()
            }
            HStack(spacing: 4) {
                Image(systemName: "lock.fill")
                    .font(.system(size: 8))
                    .foregroundStyle(Theme.green(scheme))
                Text("Без бустера аудио и текст не покидают этот Mac")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Theme.textTertiary(scheme))
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
    }

    private var footer: some View {
        HStack {
            if !model.daemonHealthy {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 10))
                    Text("Демон недоступен")
                        .font(.system(size: 11))
                }
                .foregroundStyle(Theme.orange(scheme))
            } else {
                Button("Открыть заметки") { model.openNotesFolder() }
                    .buttonStyle(.plain)
                    .font(.system(size: 13))
            }
            Spacer()
            Button("Выйти") { model.quit() }
                .buttonStyle(.plain)
                .font(.system(size: 13))
                .foregroundStyle(Theme.textTertiary(scheme))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
    }

    private var divider: some View {
        Rectangle()
            .fill(Theme.divider(scheme))
            .frame(height: 1)
            .padding(.horizontal, 4)
    }
}

// MARK: - вспомогательные вьюхи

struct PulsingDot: View {
    let color: Color
    @State private var pulsing = false

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 10, height: 10)
            .shadow(color: color.opacity(0.8), radius: pulsing ? 8 : 2)
            .scaleEffect(pulsing ? 1.3 : 1)
            .opacity(pulsing ? 0.45 : 1)
            .animation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true),
                       value: pulsing)
            .onAppear { pulsing = true }
    }
}

struct LevelRow: View {
    let icon: String
    let label: String
    let level: Double // 0…1
    let gradientTail: Bool
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 11))
                .foregroundStyle(Theme.textSecondary(scheme))
                .frame(width: 14)
            Text(label)
                .font(.system(size: 11))
                .foregroundStyle(Theme.textSecondary(scheme))
                .frame(width: 98, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Theme.levelTrack(scheme))
                    Capsule()
                        .fill(fillStyle)
                        .frame(width: max(3, geo.size.width * level))
                        .animation(.linear(duration: 0.1), value: level)
                }
            }
            .frame(height: 5)
        }
    }

    private var fillStyle: LinearGradient {
        if gradientTail {
            // у микрофона последние ~20% уходят в жёлтый (пик)
            return LinearGradient(
                stops: [
                    .init(color: Theme.green(scheme), location: 0),
                    .init(color: Theme.green(scheme), location: 0.8),
                    .init(color: Theme.yellow(scheme), location: 1),
                ],
                startPoint: .leading, endPoint: .trailing)
        }
        return LinearGradient(colors: [Theme.green(scheme)],
                              startPoint: .leading, endPoint: .trailing)
    }
}

struct RecentMeetingRow: View {
    let meeting: MeetingRow
    let action: () -> Void
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        HoverRow(action: action) {
            HStack(spacing: 8) {
                statusIcon
                    .frame(width: 14)
                VStack(alignment: .leading, spacing: 1) {
                    Text(meeting.displayTitle)
                        .font(.system(size: 13))
                        .lineLimit(1)
                    subtitle
                        .font(.system(size: 11))
                        .lineLimit(1)
                }
                Spacer()
            }
        }
    }

    @ViewBuilder private var statusIcon: some View {
        switch meeting.status {
        case "delivered":
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 13))
                .foregroundStyle(Theme.green(scheme))
        case "error":
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 12))
                .foregroundStyle(Theme.orange(scheme))
        default:
            ProgressView()
                .controlSize(.small)
                .scaleEffect(0.6)
        }
    }

    @ViewBuilder private var subtitle: some View {
        switch meeting.status {
        case "delivered":
            let sinks = meeting.sinks
                .filter { $0.status == "delivered" }
                .map { $0.sink == "telegram" ? "Telegram" : $0.sink.capitalized }
            Text("Отправлено\(sinks.isEmpty ? "" : " в " + sinks.joined(separator: ", "))")
                .foregroundStyle(Theme.textTertiary(scheme))
        case "error":
            Text("Ошибка — нажмите, чтобы открыть")
                .foregroundStyle(Theme.orange(scheme))
        default:
            Text(processingLabel)
                .foregroundStyle(Theme.textTertiary(scheme))
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
}

/// Строка с hover-подсветкой (radius 7, как в макете) и кликом.
struct HoverRow<Content: View>: View {
    let action: () -> Void
    @ViewBuilder let content: () -> Content
    @State private var hovered = false
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        Button(action: action) {
            content()
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(hovered ? Theme.rowHover(scheme) : .clear,
                            in: RoundedRectangle(cornerRadius: 7))
        }
        .buttonStyle(.plain)
        .onHover { hovered = $0 }
    }
}
