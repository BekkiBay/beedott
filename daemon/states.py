"""Конечный автомат сессии. Инвариант: файлы удаляются только в DELIVERED/CANCELLED."""
from enum import StrEnum


class State(StrEnum):
    DETECTED = "DETECTED"; RECORDING = "RECORDING"; RECORDED = "RECORDED"
    TRANSCRIBING = "TRANSCRIBING"; TRANSCRIBED = "TRANSCRIBED"
    SUMMARIZING = "SUMMARIZING"; SUMMARIZED = "SUMMARIZED"
    DELIVERING = "DELIVERING"; DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"; FAILED = "FAILED"


_CHAIN = [State.DETECTED, State.RECORDING, State.RECORDED, State.TRANSCRIBING,
          State.TRANSCRIBED, State.SUMMARIZING, State.SUMMARIZED,
          State.DELIVERING, State.DELIVERED]
ALLOWED: dict[State, set[State]] = {a: {b} for a, b in zip(_CHAIN, _CHAIN[1:])}
TERMINAL = {State.DELIVERED, State.CANCELLED, State.FAILED}
for s in State:
    if s not in TERMINAL:
        ALLOWED.setdefault(s, set()).update({State.CANCELLED, State.FAILED})
        ALLOWED[s].add(s)  # ретрай этапа = повторный вход в то же состояние

# /reprocess — обычный выход из FAILED: откат к состоянию перед самой ранней
# непройденной стадией (transcribe→RECORDED, summarize→TRANSCRIBED,
# deliver→SUMMARIZED). Джобы этой стадии сбрасываются в queued (см. db.reprocess).
# Правка фоллоу-апа из Claude тоже может вывести FAILED — всегда в SUMMARIZED,
# тем же db.redeliver, что и из DELIVERED ниже.
ALLOWED[State.FAILED] = {State.RECORDED, State.TRANSCRIBED, State.SUMMARIZED}

# «Пересобрать саммари» (экран 06, db.resummarize) — один из двух выходов из
# DELIVERED: откат к TRANSCRIBED, транскрипт остаётся, summarize+deliver
# перегоняются заново. Аудио к этому моменту могло быть удалено retention —
# не мешает, транскрипция не перегоняется.
ALLOWED[State.DELIVERED] = {State.TRANSCRIBED}
# Второй выход из DELIVERED — правка фоллоу-апа из Claude (daemon/followup_edit.py,
# db.redeliver): новая ревизия уже записана — заново прогоняется только deliver.
ALLOWED[State.DELIVERED].add(State.SUMMARIZED)


def assert_transition(a: State, b: State) -> None:
    if b not in ALLOWED.get(a, set()):
        raise ValueError(f"illegal transition {a} -> {b}")


def may_delete_files(s: State) -> bool:
    return s in (State.DELIVERED, State.CANCELLED)
