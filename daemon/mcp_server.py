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
import functools
import json
import math
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from typing_extensions import TypedDict

from daemon import control
from daemon.timeutil import to_local

# Транскрипт за один ответ: лимит вывода MCP в Claude Code ~25 тыс. токенов на
# весь ответ (шапка и фоллоу-ап — сверху), кириллица плотнее латиницы. 40 000
# символов — с запасом; 81-минутная встреча (15.10) заняла 29 000.
TRANSCRIPT_LIMIT = 40_000

# «Все встречи» для поиска и задач: дефолт демона (200) обрезал бы окно.
ALL_SESSIONS = 10_000

READ = ToolAnnotations(read_only_hint=True)
EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

INSTRUCTIONS = (
    "Это встречи пользователя, записанные BeeDott: BeeDott записывает звонки, "
    "расшифровывает их и пишет фоллоу-ап. meeting_id бери из list_meetings или "
    "search_meetings. Длинную расшифровку читай страницами: get_meeting с "
    "include_transcript=true и from_minute из подсказки в конце ответа. "
    "Править встречи можно только по просьбе пользователя. Перед update_followup "
    "прочитай встречу через get_meeting и пришли фоллоу-ап целиком, изменив только "
    "то, о чём просили; resend=true — только если пользователь просил отправить в "
    "Telegram. Удаляй встречу только по явной просьбе."
)

server = MCPServer(name="beedott", instructions=INSTRUCTIONS)

_STATUS = {"delivered": "готово", "processing": "обрабатывается", "error": "ошибка"}
_SOURCE = {"summary": "саммари", "transcript": "расшифровка"}
_ACTIVE_JOBS = ("pending", "queued", "running")  # как RuntimeLogic.activeJobs


def _title(s: dict) -> str:
    """display_title — демон текущей версии; старый демон мог его не писать
    (совместимость со старой БД/протоколом) — тогда title, а дальше заглушка,
    как в db.session_title."""
    return s.get("display_title") or s.get("title") or "Без названия"


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
        if s.get("state") in ("DETECTED", "RECORDING"):
            status = "идёт запись"
        else:
            status = _STATUS.get(s['status'], s['status'])
            if s.get("status") == "error" and s.get("error"):
                status += f": {_trim_error(s['error'])}"
        lines.append(f"- **{_title(s)}** — {_when(s['started_at'])}, "
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
        title = _title(s) if s else (hits[0].get("title") or "Без названия")
        lines.append(f"### {title} — {_when(s['started_at'] if s else None)} · id `{sid}`")
        for hit in hits:
            # Перевод строки в сниппете ломает Markdown-список — на пробел.
            snippet = hit["snippet"].replace("\n", " ").replace("[[", "**").replace("]]", "**")
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
    if state == "CANCELLED":
        return "отменена"
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
    lines = [f"# {_title(s)}",
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
        out.append(f"### {_title(s)} — {_when(s['started_at'])} · id `{s['id']}`")
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


# ─────────────────────────────── инструменты ─────────────────────────────────


def _call(method: str, params: dict | None = None) -> dict:
    return control.request(method, params)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def tool_errors(fn):
    """Любая беда → ToolError с понятным текстом: непойманное исключение
    MCPServer заменяет на безликое «Error executing tool» и теряет текст."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except control.DaemonUnavailable as exc:
            raise ToolError("BeeDott не запущен или не отвечает — открой приложение "
                            "BeeDott и повтори") from exc
        except control.DaemonError as exc:
            if exc.code == "not_found":
                raise ToolError(f"Встреча не найдена ({exc.message}) — возьми id из "
                                "list_meetings или search_meetings") from exc
            raise ToolError(f"BeeDott: {exc.message}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"BeeDott: ошибка — {type(exc).__name__}: {exc}") from exc
    return wrapper


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def list_meetings(limit: int = 20) -> str:
    """Последние встречи пользователя, новые сверху: название, дата, длительность,
    статус, число задач и id. limit — от 1 до 100."""
    sessions = _call("sessions_list", {"limit": _clamp(limit, 1, 100)})["sessions"]
    return format_meeting_list(sessions)


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def search_meetings(query: str, limit: int = 10) -> str:
    """Полнотекстовый поиск по саммари и расшифровкам встреч. Результат
    сгруппирован по встречам: название, дата, id и фрагменты с совпадениями.
    limit — число встреч, от 1 до 50.
    Слова ищутся по началу, и нужны все сразу: давай 1–2 ключевых слова
    основой без окончания (корзин, минимальн). Пусто — попробуй короче или
    синоним."""
    results = _call("search", {"query": query})["results"]
    sessions = _call("sessions_list", {"limit": ALL_SESSIONS})["sessions"]
    return format_search(query, results, sessions, _clamp(limit, 1, 50))


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def get_meeting(meeting_id: str, include_transcript: bool = False,
                from_minute: int = 0) -> str:
    """Карточка встречи: название, дата, участники, статус и фоллоу-ап (коротко,
    что обсуждали, решения, задачи, открытые вопросы). include_transcript=true
    добавляет расшифровку с таймкодами с минуты from_minute; длинная приходит
    страницами — в конце подсказка, с какой минуты продолжить."""
    detail = _call("session_detail", {"session_id": meeting_id})
    return format_meeting(detail, include_transcript=include_transcript,
                          from_minute=_clamp(from_minute, 0, 100_000))


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def action_items(days: int = 7) -> str:
    """Задачи из фоллоу-апов встреч за последние days дней (от 1 до 90):
    задача, ответственный, срок и встреча."""
    days = _clamp(days, 1, 90)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for s in _call("sessions_list", {"limit": ALL_SESSIONS})["sessions"]:
        # session_detail тащит весь транскрипт — только встречи в окне и с задачами
        if not s.get("action_items") or to_local(s["started_at"]) < since:
            continue
        try:
            followup = _call("session_detail", {"session_id": s["id"]}).get("followup")
        except control.DaemonError as exc:
            if exc.code == "not_found":  # встречу удалили между sessions_list и этим вызовом
                continue
            raise
        if followup:
            rows.append((s, followup))
    return format_action_items(rows, days)


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def recording_status() -> str:
    """Идёт ли сейчас запись (название, с какого времени), сколько стадий
    обработки в очереди и сколько встреч с ошибкой."""
    return format_status(_call("status"))


class ActionItem(TypedDict):
    task: str
    owner: str
    deadline: str | None


class Followup(TypedDict):
    """Фоллоу-ап по схеме SPEC_v3 §7; окончательная проверка — в демоне
    (summarizer.validate_followup)."""
    context: str
    discussed: list[str]
    decisions: list[str]
    action_items: list[ActionItem]
    open_questions: list[str]


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def rename_meeting(meeting_id: str, title: str) -> str:
    """Переименовать встречу. Библиотека и поиск увидят новое название; уже
    отправленные сообщения Telegram и заметки не меняются."""
    r = _call("rename_session", {"session_id": meeting_id, "title": title})
    return f"Встреча переименована: «{r['title']}»."


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def resummarize_meeting(meeting_id: str) -> str:
    """Заново собрать фоллоу-ап встречи через Claude из той же расшифровки —
    как кнопка «Пересобрать саммари» в карточке."""
    _call("resummarize", {"session_id": meeting_id})
    return ("Пересобираю фоллоу-ап — новая версия появится в Библиотеке через пару "
            "минут и, если подключён Telegram, придёт туда ответом на прежнюю.")


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def update_followup(meeting_id: str, followup: Followup, resend: bool = False) -> str:
    """Заменить фоллоу-ап встречи исправленной версией. Сначала прочитай встречу
    через get_meeting и пришли фоллоу-ап ЦЕЛИКОМ (все пять разделов), изменив
    только то, о чём просил пользователь. deadline — дата, "asap" или null.
    resend=true — ещё и отправить новую версию в Telegram ответом на прежнюю
    (только если пользователь просил)."""
    r = _call("update_followup", {"session_id": meeting_id, "followup": dict(followup),
                                  "resend": resend})
    sent = {
        "pending": "Новая версия уйдёт в Telegram ответом на прежнюю.",
        "unchanged": "В Telegram не отправляю.",
        "off": "Telegram не подключён.",
    }.get(r.get("telegram"), "")
    return (f"Фоллоу-ап обновлён (ревизия {r['revision']}); заметка в ~/MeetMind "
            f"обновится. {sent} Прежняя версия сохранена.").strip()


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def set_keep_audio(meeting_id: str, keep: bool) -> str:
    """Хранить аудио встречи (keep=true): автоочистка старых записей его не
    удалит. keep=false — снова по общим правилам."""
    _call("set_keep_audio", {"session_id": meeting_id, "keep": keep})
    return ("Аудио закреплено — автоочистка его не удалит." if keep
            else "Аудио снова удаляется по общим правилам.")


@server.tool(annotations=DELETE, structured_output=False)
@tool_errors
def delete_meeting(meeting_id: str, title: str) -> str:
    """Удалить встречу навсегда: аудио, расшифровку, фоллоу-ап и поиск. Заметки
    в ~/MeetMind и сообщения в Telegram останутся. Только по явной просьбе
    пользователя. title — точное название встречи из list_meetings или
    get_meeting: если не совпадёт, ничего не удалится."""
    _call("delete_session", {"session_id": meeting_id, "title": title})
    return f"Встреча «{title}» удалена. Заметки в ~/MeetMind и сообщения в Telegram остались."


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
