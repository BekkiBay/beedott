"""Ревизии фоллоу-апа: общий счётчик и резервная копия для stages.summarize
(пересборка) и followup_edit.apply (правка из Claude) — раньше каждый считал
следующую ревизию от прежнего followup.json сам, стадия падала на не-словаре
и не делала бэкап вовсе.
"""
import json
import shutil
from pathlib import Path

FOLLOWUP = "followup.json"


def previous(sdir: Path) -> dict | None:
    """Прежний followup.json. Файла нет вовсе (первая сборка) — None; файл
    есть, но нечитаем или не словарь (повреждён, старый формат) — {}: ревизия
    всё равно продвигается, стадия не падает."""
    path = sdir / FOLLOWUP
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def next_revision(prev: dict | None) -> int:
    if prev is None:
        return 1
    try:
        return int(prev.get("revision", 1)) + 1
    except (TypeError, ValueError):
        return 2


def backup(sdir: Path, revision: int) -> None:
    """Прежний followup.json остаётся на диске как followup.r{revision-1}.json
    — на случай отката вручную. Вызывать до перезаписи followup.json."""
    path = sdir / FOLLOWUP
    if path.exists():
        shutil.copy2(path, sdir / f"followup.r{revision - 1}.json")
