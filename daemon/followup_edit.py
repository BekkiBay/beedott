"""Правка фоллоу-апа из Claude (MCP-инструмент update_followup).

Claude присылает исправленный фоллоу-ап целиком; демон проверяет его той же
схемой, что ответ модели (summarizer.validate_followup), пишет новую ревизию,
обновляет поиск и заново доставляет заметку — и Telegram, если просили resend.
Спек: docs/superpowers/specs/2026-10-14-claude-mcp-design.md, «Правка фоллоу-апа».
"""
import json
import os
from pathlib import Path

from daemon import db, stages, summarizer
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
    if state not in (State.DELIVERED, State.FAILED) or running:
        raise EditError("busy", "встреча сейчас обрабатывается — попробуй, когда закончится")
    sdir = session_dir(sid)
    if not (sdir / "followup.json").exists():
        if (sdir / "transcript.txt").exists():
            raise EditError("no_followup", "фоллоу-апа ещё нет — сначала пересобери саммари")
        raise EditError("no_followup", "встреча не обработалась — повтори обработку в BeeDott")
    return sdir


def apply(con, sid: str, followup, *, resend: bool) -> dict:
    """Записать исправленный фоллоу-ап новой ревизией и поставить повторную
    доставку. Вызывать под локом демона (DaemonApp.update_followup)."""
    row = db.get_session(con, sid)
    sdir = _check_editable(con, row, sid)
    try:
        clean = summarizer.validate_followup(summarizer.normalize_followup(followup))
    except ValueError as exc:
        raise EditError("bad_params", f"фоллоу-ап не по схеме: {exc}") from exc

    path = sdir / "followup.json"
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    try:
        revision = int(prev.get("revision", 1)) + 1
    except (TypeError, ValueError):
        revision = 2
    engine = prev.get("engine") or "claude"
    new = dict(clean, engine=engine, revision=revision)
    title = db.session_title(row)
    md = render_followup(new, engine=engine, title=title,
                         duration_min=stages._duration_min(row), revision=revision)
    _write_atomic(path, json.dumps(new, ensure_ascii=False, indent=1))
    _write_atomic(sdir / "followup.md", md)

    tpath = sdir / "transcript.txt"
    transcript = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
    db.index_note(con, sid, title=title, transcript=transcript, summary=md)

    sinks = ["note", "obsidian"]
    if resend:
        prepare_telegram_resend(sdir)
        sinks.append("telegram")
    # без commit — уйдёт одной транзакцией с redeliver
    con.execute(
        f"UPDATE deliveries SET status='pending', delivered_at=NULL"
        f" WHERE session_id=? AND sink IN ({', '.join('?' * len(sinks))})", (sid, *sinks))
    db.redeliver(con, sid)
    return {"session_id": sid, "revision": revision, "resend": resend}
