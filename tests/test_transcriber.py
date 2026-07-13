import pytest

from daemon.transcriber import dedupe_lines, merge_chunks


# ─── dedupe_lines ────────────────────────────────────────────────────────────


def test_dedupe():
    assert dedupe_lines("а\nа\nа\nб") == "а\nа\nб"


def test_dedupe_no_repeats_unchanged():
    assert dedupe_lines("а\nб\nв") == "а\nб\nв"


def test_dedupe_drops_blank_lines():
    assert dedupe_lines("а\n\n\nб") == "а\nб"


def test_dedupe_many_repeats_keeps_two():
    # 5 подряд одинаковых строк → одно повторение (2 строки) остаётся
    assert dedupe_lines("x\nx\nx\nx\nx") == "x\nx"


def test_dedupe_resets_after_different_line():
    assert dedupe_lines("a\na\nb\nb\nb\nc") == "a\na\nb\nb\nc"


# ─── merge_chunks ────────────────────────────────────────────────────────────


def test_merge_chunks_shifts_timestamps_and_drops_overlap_dup():
    chunk_results = [
        {
            "start": 0.0,
            "segments": [
                {"start": 0.0, "end": 2.0, "text": "a"},
                {"start": 1795.0, "end": 1800.0, "text": "b"},
            ],
        },
        {
            "start": 1795.0,
            "segments": [
                # целиком в зоне перекрытия (< overlap_sec) — дубликат хвоста
                # предыдущего чанка, должен быть отброшен
                {"start": 0.0, "end": 5.0, "text": "b"},
                {"start": 5.0, "end": 10.0, "text": "c"},
            ],
        },
    ]
    merged = merge_chunks(chunk_results, overlap_sec=5.0)
    assert merged == [
        {"start": 0.0, "end": 2.0, "text": "a"},
        {"start": 1795.0, "end": 1800.0, "text": "b"},
        {"start": 1800.0, "end": 1805.0, "text": "c"},
    ]


def test_merge_chunks_single_chunk_passthrough():
    chunk_results = [{"start": 0.0, "segments": [{"start": 0.0, "end": 1.0, "text": "x"}]}]
    assert merge_chunks(chunk_results, overlap_sec=5.0) == [
        {"start": 0.0, "end": 1.0, "text": "x"}
    ]


def test_merge_chunks_three_chunks_offsets_accumulate():
    chunk_results = [
        {"start": 0.0, "segments": [{"start": 0.0, "end": 1.0, "text": "a"}]},
        {"start": 100.0, "segments": [{"start": 10.0, "end": 11.0, "text": "b"}]},
        {"start": 200.0, "segments": [{"start": 20.0, "end": 21.0, "text": "c"}]},
    ]
    merged = merge_chunks(chunk_results, overlap_sec=5.0)
    assert merged == [
        {"start": 0.0, "end": 1.0, "text": "a"},
        {"start": 110.0, "end": 111.0, "text": "b"},
        {"start": 220.0, "end": 221.0, "text": "c"},
    ]


# ─── slow: реальный инференс на фикстуре ────────────────────────────────────


@pytest.mark.slow
def test_transcribe_fixture():
    from daemon.transcriber import Transcriber

    segs = Transcriber().transcribe("tests/fixtures/sample_ru.wav")
    assert segs is not None
    assert len(segs) > 0 and any(s["text"].strip() for s in segs)
