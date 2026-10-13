"""Время для человека. В БД всё хранится в UTC (ISO8601), а всё, что видит
пользователь, — название «Встреча HH:MM», папки и имена заметок — в местном
времени (продукт P7: раньше там был UTC, и встреча в 10:10 по Ташкенту
называлась «Встреча 05:10»).

Зона — системная; env MEETMIND_TZ (IANA, например Asia/Tashkent) её
переопределяет — для тестов и на случай, если демон запущен с чужим TZ.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def local_tz():
    name = os.environ.get("MEETMIND_TZ")
    return ZoneInfo(name) if name else None  # None → системная зона в astimezone()


def to_local(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz())
