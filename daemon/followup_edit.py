"""Правка фоллоу-апа из Claude (MCP-инструмент update_followup).

Claude присылает исправленный фоллоу-ап целиком; демон проверяет его той же
схемой, что ответ модели (summarizer.validate_followup), пишет новую ревизию,
обновляет поиск и заново доставляет заметку — и Telegram, если просили resend.
Спек: docs/superpowers/specs/2026-10-14-claude-mcp-design.md, «Правка фоллоу-апа».
"""
import json
import os
from pathlib import Path

import config
from daemon import db, revisions, stages, summarizer
from daemon.paths import session_dir
from daemon.render import render_followup
from daemon.states import State


class EditError(Exception):
    """Отказ с кодом для ControlError: not_found, busy, no_followup, bad_params."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def prepare_telegram_resend(sdir: Path) -> None:
    """Новая версия уйдёт в Telegram ответом на первое отправленное сообщение
    (продукт P12); прогресс отправки забывается — иначе побайтно тот же текст
    «уже отправлен» и сообщение не придёт. Общая для resummarize и правки."""
    progress = sdir / "telegram_progress.json"
    try:
        sent = json.loads(progress.read_text(encoding="utf-8")).get("sent") or []
    except (OSError, json.JSONDecodeError):
        sent = []
    if sent:
        (sdir / "telegram_reply_to").write_text(str(sent[0]), encoding="utf-8")
    progress.unlink(missing_ok=True)


def _write_atomic(path: Path, text: str) -> None:
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def _check_editable(con, row, sid: str) -> Path:
    if row is None or row["state"] == State.CANCELLED:
        raise EditError("not_found", f"нет сессии {sid}")
    state = State(row["state"])
    if state in (State.DETECTED, State.RECORDING):
        raise EditError("busy", "идёт запись этой встречи")
    running = con.execute("SELECT 1 FROM pipeline_jobs WHERE session_id=? AND status='running'",
                          (sid,)).fetchone()
    # SUMMARIZED — тоже прежняя правка: redeliver уже перевёл сессию туда и
    # поставил deliver в очередь, но демон её ещё не довёл до DELIVERED.
    # SUMMARIZED -> SUMMARIZED — разрешённый самопереход (daemon/states.py),
    # вторая правка подряд не должна упираться в busy.
    if state not in (State.DELIVERED, State.FAILED, State.SUMMARIZED) or running:
        raise EditError("busy", "встреча сейчас обрабатывается — попробуй, когда закончится")
    sdir = session_dir(sid)
    if not (sdir / "followup.json").exists():
        if (sdir / "transcript.txt").exists():
            raise EditError("no_followup", "фоллоу-апа ещё нет — сначала пересобери саммари")
        raise EditError("no_followup", "встреча не обработалась — повтори обработку в BeeDott")
    return sdir


def _telegram_status(con, sid: str, *, resend: bool) -> str:
    """Честный статус Telegram в ответе apply() — не выдавать «доставлено»,
    пока это не так. "off" — канал не настроен (та же проверка, что
    stages._build_sinks: config.TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID). Иначе
    "pending" — просили resend, или строки deliveries для telegram нет, или
    она не 'delivered' (упала/в процессе). Иначе — прежнее сообщение никто
    не трогал, "unchanged"."""
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return "off"
    if resend:
        return "pending"
    row = con.execute(
        "SELECT status FROM deliveries WHERE session_id=? AND sink='telegram'", (sid,)).fetchone()
    if row is None or row["status"] != "delivered":
        return "pending"
    return "unchanged"


def apply(con, sid: str, followup, *, resend: bool) -> dict:
    """Записать исправленный фоллоу-ап новой ревизией и поставить повторную
    доставку. Вызывать под локом демона (DaemonApp.update_followup)."""
    row = db.get_session(con, sid)
    sdir = _check_editable(con, row, sid)
    try:
        clean = summarizer.validate_followup(summarizer.normalize_followup(followup))
    except ValueError as exc:
        raise EditError("bad_params", f"фоллоу-ап не по схеме: {exc}") from exc
    if not (clean["context"].strip() or clean["discussed"] or clean["decisions"]
            or clean["action_items"] or clean["open_questions"]):
        raise EditError("bad_params",
                        "пустой фоллоу-ап — пришли его целиком, меняя только нужное")

    path = sdir / "followup.json"
    prev = revisions.previous(sdir)
    revision = revisions.next_revision(prev)
    prev_engine = (prev or {}).get("engine")  # файл проверен в _check_editable, но вдруг удалён
    engine = prev_engine if isinstance(prev_engine, str) and prev_engine else "claude"
    new = dict(clean, engine=engine, revision=revision)
    title = db.session_title(row)
    md = render_followup(new, engine=engine, title=title,
                         duration_min=stages._duration_min(row), revision=revision)
    revisions.backup(sdir, revision)
    _write_atomic(path, json.dumps(new, ensure_ascii=False, indent=1))
    _write_atomic(sdir / "followup.md", md)

    tpath = sdir / "transcript.txt"
    transcript = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
    telegram = _telegram_status(con, sid, resend=resend)
    try:
        db.index_note(con, sid, title=title, transcript=transcript, summary=md)
        sinks = ["note", "obsidian"]
        if resend:
            prepare_telegram_resend(sdir)
            sinks.append("telegram")
        # без отдельного commit — уйдёт одной транзакцией с redeliver
        con.execute(
            f"UPDATE deliveries SET status='pending', delivered_at=NULL"
            f" WHERE session_id=? AND sink IN ({', '.join('?' * len(sinks))})", (sid, *sinks))
        db.redeliver(con, sid)
    except Exception:
        con.rollback()
        raise
    return {"session_id": sid, "revision": revision, "resend": resend, "telegram": telegram}
