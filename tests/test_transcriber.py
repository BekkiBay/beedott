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


# ─── стерео: деление на спикеров по каналам (mic=Спикер 1, tap=Спикер 2) ─────


def _write_stereo_wav(path, seconds=1.0, rate=16000):
    import numpy as np
    import soundfile as sf
    n = int(seconds * rate)
    t = np.linspace(0, seconds, n, endpoint=False)
    left = 0.3 * np.sin(2 * np.pi * 440 * t)   # микрофон
    right = 0.3 * np.sin(2 * np.pi * 220 * t)  # системный звук
    sf.write(str(path), np.column_stack([left, right]), rate)


def test_transcribe_stereo_splits_speakers(tmp_path, monkeypatch):
    """Стерео-WAV из CaptureEngine (ch0=мик, ch1=системный звук): каналы
    расшифровываются раздельно, сегменты сливаются по таймлайну со спикерами."""
    from daemon.transcriber import Transcriber
    wav = tmp_path / "audio.wav"
    _write_stereo_wav(wav)

    def fake_one(self, audio_path):
        if "mic" in audio_path:
            return [{"start": 0.0, "end": 2.0, "text": "привет, начнём"},
                    {"start": 6.0, "end": 8.0, "text": "беру на себя"}]
        return [{"start": 2.5, "end": 5.5, "text": "давайте по релизу"}]

    monkeypatch.setattr(Transcriber, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(Transcriber, "_transcribe_one", fake_one)

    segments = Transcriber().transcribe(str(wav))

    assert [s["speaker"] for s in segments] == ["Спикер 1", "Спикер 2", "Спикер 1"]
    assert [s["start"] for s in segments] == [0.0, 2.5, 6.0]  # общий таймлайн
    assert segments[1]["text"] == "давайте по релизу"


def test_transcribe_mono_has_no_speaker_labels(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf
    from daemon.transcriber import Transcriber
    wav = tmp_path / "audio.wav"
    sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000)

    monkeypatch.setattr(Transcriber, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(
        Transcriber, "_transcribe_one",
        lambda self, p: [{"start": 0.0, "end": 1.0, "text": "моно"}])

    segments = Transcriber().transcribe(str(wav))
    assert segments == [{"start": 0.0, "end": 1.0, "text": "моно"}]


# ───── продукт P6: ошибки — с причиной, «нет речи» — пустой список, не None ─────


def test_transcribe_missing_file_raises_with_reason(tmp_path):
    import pytest
    from daemon.transcriber import Transcriber
    t = Transcriber.__new__(Transcriber)
    with pytest.raises(FileNotFoundError):
        t.transcribe(str(tmp_path / "нет.wav"))


def test_transcribe_backend_error_propagates(tmp_path, monkeypatch):
    """Раньше любая ошибка (например, нет ffmpeg) превращалась в None, а в
    интерфейсе было «'NoneType' object is not iterable»."""
    import numpy as np
    import pytest
    import soundfile as sf
    from daemon.transcriber import Transcriber
    wav = tmp_path / "audio.wav"
    sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000)

    def boom(self, p):
        raise RuntimeError("ffmpeg not found")

    monkeypatch.setattr(Transcriber, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(Transcriber, "_transcribe_one", boom)
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        Transcriber().transcribe(str(wav))


def test_transcribe_stereo_without_speech_returns_empty_list(tmp_path, monkeypatch):
    from daemon.transcriber import Transcriber
    wav = tmp_path / "audio.wav"
    _write_stereo_wav(wav)
    monkeypatch.setattr(Transcriber, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(Transcriber, "_transcribe_one", lambda self, p: [])
    assert Transcriber().transcribe(str(wav)) == []
