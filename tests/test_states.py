import pytest
from daemon.states import State, assert_transition, TERMINAL, may_delete_files


def test_happy_chain():
    chain = [State.DETECTED, State.RECORDING, State.RECORDED, State.TRANSCRIBING,
             State.TRANSCRIBED, State.SUMMARIZING, State.SUMMARIZED,
             State.DELIVERING, State.DELIVERED]
    for a, b in zip(chain, chain[1:]):
        assert_transition(a, b)  # не бросает


def test_illegal_jump():
    with pytest.raises(ValueError):
        assert_transition(State.RECORDED, State.DELIVERED)


def test_cancel_from_anywhere_nonterminal():
    assert_transition(State.RECORDING, State.CANCELLED)
    with pytest.raises(ValueError):
        assert_transition(State.DELIVERED, State.CANCELLED)


def test_file_deletion_invariant():
    assert may_delete_files(State.DELIVERED)
    assert may_delete_files(State.CANCELLED)
    for s in State:
        if s not in TERMINAL:
            assert not may_delete_files(s)


def test_reprocess_from_failed_allowed():
    # /reprocess: откат из FAILED к состоянию перед упавшей стадией
    for target in (State.RECORDED, State.TRANSCRIBED, State.SUMMARIZED):
        assert_transition(State.FAILED, target)
    with pytest.raises(ValueError):
        assert_transition(State.FAILED, State.DELIVERED)  # напрямую в конец нельзя


def test_followup_edit_redelivers_from_delivered():
    """Правка фоллоу-апа из Claude (daemon/followup_edit.py) заново прогоняет
    только deliver."""
    assert_transition(State.DELIVERED, State.SUMMARIZED)
