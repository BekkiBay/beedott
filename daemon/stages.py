"""Боевые обработчики стадий пайплайна: transcribe → summarize → deliver.

Каждый принимает sqlite3.Row сессии. Исключение = failed_retry (оркестратор).
Data-plane: файлы в paths.session_dir(sid): audio.wav, transcript.{txt,json},
followup.{json,md}. Control-plane: sessions/pipeline_jobs/deliveries в SQLite.
"""
import json
import logging
from datetime import datetime
from pathlib import Path

import config
from daemon import claude_booster, db
from daemon.notifier import notify
from daemon.ollama_client import OllamaDown, OllamaModelMissing
from daemon.paths import session_dir
from daemon.render import render_followup
from daemon.sinks import NoteSink, TelegramSink
from daemon.summarizer import summarize_ollama, validate_followup
from daemon.transcriber import Transcriber, dedupe_lines

logger = logging.getLogger(__name__)

# сессии, уже получившие быстрое «Ollama не запущена» (не спамить на каждом ретрае)
_ollama_notified: set[str] = set()


def _duration_min(session) -> int:
    try:
        start = datetime.fromisoformat(session["started_at"])
        end = datetime.fromisoformat(session["ended_at"]) if session["ended_at"] else None
        return max(1, round(((end - start).total_seconds() if end else 0) / 60)) if end else 0
    except (TypeError, ValueError):
        return 0


def _title(session) -> str:
    if session["title"]:
        return session["title"]
    hhmm = session["started_at"][11:16] if session["started_at"] else "??:??"
    return f"Встреча {hhmm}"


def transcribe(session) -> None:
    sdir = session_dir(session["id"])
    segments = Transcriber().transcribe(session["audio_path"])
    text = dedupe_lines("\n".join(s["text"].strip() for s in segments if s["text"].strip()))
    if not text.strip():
        raise RuntimeError("пустой транскрипт — в записи нет распознанной речи")
    (sdir / "transcript.txt").write_text(text, encoding="utf-8")
    (sdir / "transcript.json").write_text(
        json.dumps(segments, ensure_ascii=False), encoding="utf-8")
    logger.info("transcribe %s: %d сегментов, %d символов",
                session["id"], len(segments), len(text))


def summarize(session) -> None:
    sdir = session_dir(session["id"])
    transcript = (sdir / "transcript.txt").read_text(encoding="utf-8")
    try:
        followup = summarize_ollama(transcript, {
            "title": _title(session),
            "duration_min": _duration_min(session),
        })
    except (OllamaDown, OllamaModelMissing) as exc:
        # SPEC_v3 §10 «Ollama не запущена»: демон уже попытался поднять её сам
        # (daemon.ollama_client.ensure_running) — не вышло, автопереключения
        # на Claude нет. Уведомляем СРАЗУ (не после 8 ретраев), один раз на
        # сессию; оркестратор дальше делает штатный failed_retry.
        logger.error("summarize %s: Ollama недоступна — %s", session["id"], exc)
        if session["id"] not in _ollama_notified:
            _ollama_notified.add(session["id"])
            notify("MeetMind", f"{exc} — встреча в очереди, обработается после запуска")
        raise
    validate_followup({k: v for k, v in followup.items() if k != "degraded"})

    # Claude-бустер (PLAN_v3 Task 7.4, opt-in): best-effort улучшение поверх
    # готовой Ollama-версии. Любой сбой внутри boost() → None, стадия не падает.
    engine = config.OLLAMA_MODEL
    if config.USE_CLAUDE_CODE:
        boosted = claude_booster.boost(transcript, followup, {
            "title": _title(session),
            "duration_min": _duration_min(session),
        })
        if boosted is not None:
            followup = boosted
            engine = f"claude-code:{config.CLAUDE_CODE_MODEL}"

    (sdir / "followup.json").write_text(
        json.dumps(followup, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render_followup(
        followup, engine=engine, title=_title(session),
        duration_min=_duration_min(session))
    (sdir / "followup.md").write_text(md, encoding="utf-8")
    logger.info("summarize %s: %d решений, %d action items%s",
                session["id"], len(followup.get("decisions", [])),
                len(followup.get("action_items", [])),
                " (degraded)" if followup.get("degraded") else "")


def _build_sinks() -> list:
    sinks = []
    if getattr(config, "TELEGRAM_BOT_TOKEN", None) and getattr(config, "TELEGRAM_CHAT_ID", None):
        sinks.append(("telegram", True,
                      TelegramSink(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)))
    base_dir = config.MARKDOWN_BASE_DIR or "~/MeetMind"
    sinks.append(("note", False, NoteSink(name="note", base_dir=Path(base_dir).expanduser())))
    if config.OBSIDIAN_VAULT_PATH:
        sinks.append(("obsidian", False, NoteSink(
            name="obsidian",
            base_dir=Path(config.OBSIDIAN_VAULT_PATH).expanduser() / config.OBSIDIAN_SUBFOLDER,
            frontmatter=True)))
    return sinks


def deliver(session) -> None:
    """Telegram обязателен (без него исключение → retry); note/obsidian — best-effort.
    Идемпотентность: sink со строкой deliveries.status='delivered' не шлётся повторно."""
    sid = session["id"]
    sdir = session_dir(sid)
    md = (sdir / "followup.md").read_text(encoding="utf-8")
    when = datetime.fromisoformat(session["started_at"])
    con = db.connect()

    required_failed = None
    for name, required, sink in _build_sinks():
        row = con.execute(
            "SELECT status FROM deliveries WHERE session_id=? AND sink=?",
            (sid, name)).fetchone()
        if row and row["status"] == "delivered":
            continue
        try:
            res = sink.send(md, title=_title(session), when=when, session_id=sid)
            ok, detail = res.success, res.detail
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)
        con.execute(
            "INSERT INTO deliveries(session_id, sink, status, attempts, delivered_at, detail)"
            " VALUES(?,?,?,1,?,?)"
            " ON CONFLICT(session_id, sink) DO UPDATE SET status=excluded.status,"
            " attempts=attempts+1, delivered_at=excluded.delivered_at, detail=excluded.detail",
            (sid, name, "delivered" if ok else "failed_retry",
             db.now() if ok else None, detail))
        con.commit()
        logger.info("deliver %s -> %s: %s", sid, name, "ok" if ok else f"FAIL {detail}")
        if required and not ok:
            required_failed = f"{name}: {detail}"
    if required_failed:
        raise RuntimeError(f"обязательный sink не доставлен — {required_failed}")


def make_stages() -> dict:
    return {"transcribe": transcribe, "summarize": summarize, "deliver": deliver}
