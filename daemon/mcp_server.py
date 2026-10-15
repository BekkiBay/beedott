"""MCP-сервер BeeDott: Claude Desktop и Claude Code спрашивают встречи.

Спек: docs/superpowers/specs/2026-10-14-claude-mcp-design.md. Запуск:
`python -m daemon.mcp_server` (stdio; Claude запускает процесс сам по записи в
своём конфиге — её пишет `python -m daemon.setup claude-connect`).

Всё — через control-сокет демона (daemon.control.request): в БД и файлы сессий
сервер не лезет. Импортирует только daemon.control, daemon.paths и
daemon.timeutil — не config и не daemon.db: Claude запускает процесс с
произвольным cwd, а `import config` делает mkdir("sessions") от cwd.
stdout — канал протокола MCP: print() здесь ломает протокол.
"""
import json
import math
from datetime import datetime, timezone

from daemon.timeutil import to_local

# Транскрипт за один ответ: лимит вывода MCP в Claude Code ~25 тыс. токенов,
# кириллица плотнее латиницы; часовая встреча — около 54 000 символов.
TRANSCRIPT_LIMIT = 60_000

_STATUS = {"delivered": "готово", "processing": "обрабатывается", "error": "ошибка"}
_SOURCE = {"summary": "саммари", "transcript": "расшифровка"}
_ACTIVE_JOBS = ("pending", "queued", "running")  # как RuntimeLogic.activeJobs


def _when(iso: str | None) -> str:
    return to_local(iso).strftime("%d.%m.%Y %H:%M") if iso else "дата неизвестна"


def _minutes(value) -> str:
    return f"{round(value)} мин" if value is not None else "длительность неизвестна"


def _trim_error(text: str) -> str:
    """Первая строка сообщения об ошибке, не длиннее 200 символов — простыня
    трейсбека не должна разворачиваться в списке встреч или карточке."""
    return text.splitlines()[0][:200] if text else ""


def format_meeting_list(sessions: list[dict]) -> str:
    if not sessions:
        return "Встреч пока нет."
    lines = []
    for s in sessions:
        tasks = f", задач: {s['action_items']}" if s.get("action_items") else ""
        status = _STATUS.get(s['status'], s['status'])
        if s.get("status") == "error" and s.get("error"):
            status += f": {_trim_error(s['error'])}"
        lines.append(f"- **{s['display_title']}** — {_when(s['started_at'])}, "
                     f"{_minutes(s.get('duration_min'))}, "
                     f"{status}{tasks} · id `{s['id']}`")
    return "\n".join(lines)


def format_search(query: str, results: list[dict], sessions: list[dict], limit: int) -> str:
    """Попадания демона (по строке на источник) → группы по встречам, в
    порядке sessions_list (новые сначала); встречи не из списка — в конец.
    Усечение по limit — c подсказкой, сколько встреч скрыто."""
    order = {s["id"]: i for i, s in enumerate(sessions)}
    by_id = {s["id"]: s for s in sessions}
    groups: dict[str, list[dict]] = {}
    for hit in results:
        groups.setdefault(hit["session_id"], []).append(hit)
    if not groups:
        return f"По запросу «{query}» ничего не нашлось."
    ordered = sorted(groups.items(), key=lambda kv: order.get(kv[0], len(sessions)))
    total = len(ordered)
    lines = []
    for sid, hits in ordered[:limit]:
        s = by_id.get(sid)
        title = s["display_title"] if s else (hits[0].get("title") or "Без названия")
        lines.append(f"### {title} — {_when(s['started_at'] if s else None)} · id `{sid}`")
        for hit in hits:
            snippet = hit["snippet"].replace("[[", "**").replace("]]", "**")
            lines.append(f"- {_SOURCE.get(hit['source'], hit['source'])}: {snippet}")
    if total > limit:
        lines.append(f"(показаны {limit} из {total} встреч — уточни запрос или увеличь limit)")
    return "\n".join(lines)


def _session_minutes(s: dict) -> int | None:
    if not s.get("ended_at"):
        return None
    try:
        delta = datetime.fromisoformat(s["ended_at"]) - datetime.fromisoformat(s["started_at"])
    except (TypeError, ValueError):
        return None
    return round(delta.total_seconds() / 60)


def _attendees(s: dict) -> list[str]:
    try:
        names = json.loads(s.get("attendees") or "[]")
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _state_text(detail: dict) -> str:
    state = detail["session"]["state"]
    if state == "DELIVERED":
        return "готово"
    if state == "FAILED":
        errors = [p["error"] for p in detail.get("pipeline") or [] if p.get("ok") is False]
        return "ошибка" + (f": {_trim_error(errors[0])}" if errors else "")
    if state in ("DETECTED", "RECORDING"):
        return "идёт запись"
    return "обрабатывается"


def _bullets(items: list[str]) -> list[str]:
    return [f"- {x}" for x in items] or ["нет"]


def _task_line(t: dict) -> str:
    deadline = f", срок: {t['deadline']}" if t.get("deadline") else ""
    return f"- {t['task']} — {t['owner']}{deadline}"


def format_followup(followup: dict) -> list[str]:
    """Все пять разделов всегда, пустые — «нет»: Claude правит фоллоу-ап
    целиком (update_followup) и должен видеть и пустые разделы."""
    lines = [f"## Фоллоу-ап (ревизия {followup.get('revision', 1)})",
             "### Коротко", followup.get("context") or "нет",
             "### Что обсуждали", *_bullets(followup.get("discussed") or []),
             "### Решения", *_bullets(followup.get("decisions") or []),
             "### Задачи"]
    tasks = followup.get("action_items") or []
    lines += [_task_line(t) for t in tasks] if tasks else ["нет"]
    lines += ["### Открытые вопросы", *_bullets(followup.get("open_questions") or [])]
    return lines


def format_transcript(segments: list[dict], from_minute: int = 0,
                       meeting_id: str = "meeting_id") -> str:
    """Строки `[мм:сс] Спикер N: текст` с минуты from_minute. Длиннее
    TRANSCRIPT_LIMIT — обрезка по строке и подсказка, с какой минуты дальше.
    meeting_id — для подсказки-продолжения: format_meeting передаёт реальный
    id, прямой вызов без него получает плейсхолдер."""
    if not segments:
        return "Расшифровки нет."
    # Сегменты сортированы по start (стерео-дорожки двух спикеров), у
    # последнего по порядку конец не обязательно самый поздний.
    total = math.ceil(max(seg["end"] for seg in segments) / 60)
    start = from_minute * 60
    lines: list[str] = []
    size = 0
    next_minute = None
    for seg in segments:
        text = seg["text"].strip()
        if seg["end"] <= start or not text:
            continue
        mm, ss = divmod(int(seg["start"]), 60)
        speaker = seg.get("speaker")
        line = f"[{mm:02d}:{ss:02d}] {speaker}: {text}" if speaker else f"[{mm:02d}:{ss:02d}] {text}"
        if lines and size + len(line) + 1 > TRANSCRIPT_LIMIT:
            # Следующая страница начнётся с минуты этой строки (повтор пары
            # строк лучше потери); не раньше from_minute + 1 — без зацикливания.
            next_minute = max(int(seg["start"] // 60), from_minute + 1)
            break
        lines.append(line)
        size += len(line) + 1
    if not lines:
        return f"С {from_minute}-й минуты расшифровки нет — всего {total} мин."
    if next_minute is not None:
        lines.append(f"(Показано с {from_minute} по {next_minute} минуту из {total}; "
                     f"продолжение — get_meeting(\"{meeting_id}\", include_transcript=true, "
                     f"from_minute={next_minute}).)")
    return "\n".join(lines)


def format_meeting(detail: dict, *, include_transcript: bool = False,
                   from_minute: int = 0) -> str:
    s = detail["session"]
    minutes = _session_minutes(s)
    duration = f"{minutes} мин" if minutes is not None else "длительность неизвестна"
    lines = [f"# {s['display_title']}",
             f"{_when(s['started_at'])} · {duration} · {_state_text(detail)} · id `{s['id']}`"]
    names = _attendees(s)
    if names:
        lines.append("Участники: " + ", ".join(names))
    if s.get("has_audio"):
        lines.append("Аудио: есть, закреплено" if s.get("keep_audio") else "Аудио: есть")
    else:
        lines.append("Аудио: нет")
    lines.append("")
    followup = detail.get("followup")
    lines += format_followup(followup) if followup else ["Фоллоу-апа пока нет."]
    if include_transcript:
        lines += ["", "## Расшифровка",
                  format_transcript(detail.get("transcript") or [], from_minute,
                                     meeting_id=s["id"])]
    return "\n".join(lines)


def format_action_items(rows: list[tuple[dict, dict]], days: int) -> str:
    out = []
    for s, followup in rows:
        tasks = followup.get("action_items") or []
        if not tasks:
            continue
        out.append(f"### {s['display_title']} — {_when(s['started_at'])} · id `{s['id']}`")
        out += [_task_line(t) for t in tasks]
    return "\n".join(out) if out else f"За последние {days} дн. задач нет."


def format_status(st: dict, now: datetime | None = None) -> str:
    """status демона → текст. last_error не показываем: он не сбрасывается
    после успеха и показывал бы старые ошибки."""
    now = now or datetime.now(timezone.utc)
    lines = []
    if st.get("recording"):
        started = st.get("recording_started_at")
        since = ""
        if started:
            minutes = max(0, round((now - to_local(started)).total_seconds() / 60))
            since = f" с {to_local(started).strftime('%H:%M')} ({minutes} мин)"
        title = st.get("recording_display_title") or "Без названия"
        lines.append(f"Идёт запись **{title}**{since} · id `{st['recording']}`")
    else:
        lines.append("Запись не идёт.")
    active = sum(r.get("n", 0) for r in st.get("queue") or [] if r.get("status") in _ACTIVE_JOBS)
    lines.append(f"В обработке стадий: {active}." if active else "Очередь обработки пуста.")
    failed = (st.get("sessions") or {}).get("FAILED", 0)
    if failed:
        lines.append(f"Встреч с ошибкой: {failed} — подробности в list_meetings.")
    return "\n".join(lines)
