"""Боевые обработчики стадий пайплайна: transcribe → summarize → deliver.

Каждый принимает sqlite3.Row сессии. Исключение = failed_retry (оркестратор).
Data-plane: файлы в paths.session_dir(sid): audio.wav, transcript.{txt,json},
followup.{json,md}. Control-plane: sessions/pipeline_jobs/deliveries в SQLite.
"""
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

import soundfile as sf

import config
from daemon import anthropic_api, claude_cli, db
from daemon.notifier import notify
from daemon.orchestrator import PermanentStageError
from daemon.paths import session_dir
from daemon.render import render_followup
from daemon.sinks import NoteSink, TelegramSink
from daemon.summarizer import SummaryAuthError
from daemon.timeutil import to_local
from daemon.transcriber import Transcriber, dedupe_lines

logger = logging.getLogger(__name__)

# сессии, уже получившие «Claude недоступен» (не спамить на каждом ретрае)
_auth_notified: set[str] = set()


def _duration_min(session) -> int:
    try:
        start = datetime.fromisoformat(session["started_at"])
        end = datetime.fromisoformat(session["ended_at"]) if session["ended_at"] else None
        return max(1, round(((end - start).total_seconds() if end else 0) / 60)) if end else 0
    except (TypeError, ValueError):
        return 0


def _attendees(session) -> list[str]:
    """Участники из календаря (продукт P8), JSON-список в sessions.attendees."""
    try:
        raw = session["attendees"]
    except (IndexError, KeyError):
        return []
    try:
        names = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [n for n in names if isinstance(n, str) and n.strip()]


def _segment_line(s: dict) -> str:
    """Строка транскрипта: с меткой спикера для стерео-записей (transcriber
    STEREO_SPEAKERS), без — для старых моно."""
    speaker = s.get("speaker")
    text = s["text"].strip()
    return f"{speaker}: {text}" if speaker else text


def transcribe(session) -> None:
    sdir = session_dir(session["id"])
    segments = Transcriber().transcribe(session["audio_path"])
    text = dedupe_lines("\n".join(
        _segment_line(s) for s in segments if s["text"].strip()))
    if not text.strip():
        # Ретрай не поможет — сразу FAILED с понятной причиной (продукт P6).
        raise PermanentStageError("в записи не распознано речи")
    (sdir / "transcript.txt").write_text(text, encoding="utf-8")
    (sdir / "transcript.json").write_text(
        json.dumps(segments, ensure_ascii=False), encoding="utf-8")
    logger.info("transcribe %s: %d сегментов, %d символов",
                session["id"], len(segments), len(text))
    _compress_audio(session["id"], session["audio_path"])


def _compress_audio(sid: str, audio_path: str | None) -> None:
    """После транскрипции WAV → FLAC без потерь: ~30% размера (продукт P10:
    554 MB на час записи → ~170 MB). Исходник удаляется только после сверки
    числа кадров, каналов и частоты — данные не теряются, поэтому инвариант
    «удаление только в DELIVERED/CANCELLED» не нарушен. Любой сбой — WAV
    остаётся, стадия не падает (сжатие — оптимизация, не часть пайплайна)."""
    if not audio_path or not audio_path.endswith(".wav"):
        return
    wav = Path(audio_path)
    if not wav.exists():
        return
    flac = wav.with_suffix(".flac")
    part = wav.with_name(wav.stem + ".flac.part")
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "flac", "-f", "flac", str(part)],
            check=True, capture_output=True)
        src, dst = sf.info(str(wav)), sf.info(str(part))
        if (src.frames, src.channels, src.samplerate) != (dst.frames, dst.channels, dst.samplerate):
            raise ValueError(f"FLAC не совпал с WAV: {dst.frames} vs {src.frames} кадров")
        part.replace(flac)
        con = db.connect()
        try:
            con.execute("UPDATE sessions SET audio_path=?, updated_at=? WHERE id=?",
                        (str(flac), db.now(), sid))
            con.commit()
        finally:
            con.close()
        wav.unlink()
        logger.info("transcribe %s: аудио сжато в FLAC (%.0f MB)", sid, flac.stat().st_size / 2**20)
    except Exception:  # noqa: BLE001
        logger.warning("transcribe %s: не удалось сжать аудио — оставляю WAV", sid, exc_info=True)
        part.unlink(missing_ok=True)


def _run_summary(transcript: str, meta: dict) -> tuple[dict, str]:
    """Саммари через Claude Opus способом из config.SUMMARY_BACKEND →
    (followup, подпись движка для заметки и UI Библиотеки)."""
    backend = config.SUMMARY_BACKEND
    if backend == "claude_cli":
        return (claude_cli.summarize(transcript, meta),
                f"claude-code:{config.CLAUDE_CODE_MODEL}")
    if backend == "api":
        return (anthropic_api.summarize(transcript, meta),
                f"anthropic-api:{config.ANTHROPIC_MODEL}")
    raise PermanentStageError(
        f"неизвестный SUMMARY_BACKEND={backend!r} — ожидается claude_cli или api")


def summarize(session) -> None:
    sdir = session_dir(session["id"])
    transcript = (sdir / "transcript.txt").read_text(encoding="utf-8")
    meta = {"title": db.session_title(session), "duration_min": _duration_min(session),
            "attendees": _attendees(session),
            "date": to_local(session["started_at"]).date() if session["started_at"] else None}

    try:
        followup, engine = _run_summary(transcript, meta)
    except SummaryAuthError as exc:
        # Без человека не починится — одно уведомление на сессию, дальше
        # обычные ретраи: после claude login / нового ключа встреча дособерётся.
        logger.error("summarize %s: Claude недоступен — %s", session["id"], exc)
        if session["id"] not in _auth_notified:
            _auth_notified.add(session["id"])
            notify("BeeDott",
                   f"{exc} — встреча в очереди, саммари соберётся после исправления",
                   session_id=session["id"])
        raise

    revision = 1
    prev_path = sdir / "followup.json"
    if prev_path.exists():
        try:
            revision = int(json.loads(prev_path.read_text(encoding="utf-8")).get("revision", 1)) + 1
        except (OSError, ValueError, TypeError):
            revision = 2
    followup = dict(followup, engine=engine, revision=revision)
    (sdir / "followup.json").write_text(
        json.dumps(followup, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render_followup(
        followup, engine=engine, title=db.session_title(session),
        duration_min=_duration_min(session), revision=revision)
    (sdir / "followup.md").write_text(md, encoding="utf-8")

    # FTS-индекс для поиска в Библиотеке (экран 05). Отдельный короткоживущий
    # коннект: стадии не держат con, writer notes_fts — только summarize.
    con = db.connect()
    try:
        db.index_note(con, session["id"], title=db.session_title(session),
                      transcript=transcript, summary=md)
    finally:
        con.close()

    logger.info("summarize %s: %d решений, %d action items (%s)",
                session["id"], len(followup["decisions"]),
                len(followup["action_items"]), engine)


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
    when = to_local(session["started_at"])
    con = db.connect()

    required_failed = None
    for name, required, sink in _build_sinks():
        row = con.execute(
            "SELECT status FROM deliveries WHERE session_id=? AND sink=?",
            (sid, name)).fetchone()
        if row and row["status"] == "delivered":
            continue
        try:
            res = sink.send(md, title=db.session_title(session), when=when, session_id=sid)
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
