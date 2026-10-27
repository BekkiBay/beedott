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


# ─── VAD: склейка кусков речи и пересчёт таймкодов ───────────────────────────


def test_group_spans_packs_neighbours_up_to_limit():
    from daemon.transcriber import group_spans
    spans = [(0, 10), (20, 30), (40, 55), (60, 100)]
    # лимит 25 сэмплов речи: 10+10 влезают, 15 — уже нет, 40 — один, сверх лимита
    assert group_spans(spans, max_len=25) == [[(0, 10), (20, 30)], [(40, 55)], [(60, 100)]]


def test_group_spans_empty():
    from daemon.transcriber import group_spans
    assert group_spans([], max_len=25) == []


def test_concat_spans_and_restore_time_map_back_to_original_timeline():
    import numpy as np
    from daemon.transcriber import concat_spans, restore_time
    sr = 16000
    audio = np.arange(10 * sr, dtype=np.float32)
    # речь 1–2 с и 5–7 с; в склейке это 0–1 с и 1–3 с
    chunk, mapping = concat_spans(audio, [(1 * sr, 2 * sr), (5 * sr, 7 * sr)])
    assert len(chunk) == 3 * sr
    assert chunk[0] == audio[1 * sr] and chunk[1 * sr] == audio[5 * sr]
    assert restore_time(0.5, mapping) == 1.5
    assert restore_time(1.5, mapping) == 5.5
    assert restore_time(3.0, mapping) == 7.0
    assert restore_time(9.0, mapping) == 7.0  # хвост за пределами склейки — конец речи


def test_transcribe_one_decodes_each_speech_group_with_prompt(tmp_path, monkeypatch):
    """Словарь (initial_prompt) идёт в каждый кусок речи, а не только в первое
    30-секундное окно, и таймкоды возвращаются на шкалу исходного файла."""
    import numpy as np
    import soundfile as sf
    from daemon import transcriber as tr
    sr = 16000
    wav = tmp_path / "mic.wav"
    sf.write(str(wav), np.zeros(80 * sr, dtype="float32"), sr)
    monkeypatch.setattr(tr, "speech_spans",
                        lambda audio: [(10 * sr, 30 * sr), (50 * sr, 70 * sr)])
    calls = []

    def fake_decode(self, audio, prompt):
        calls.append((len(audio), prompt))
        return [{"start": 1.0, "end": 2.0, "text": f"кусок {len(calls)}",
                 "no_speech_prob": 0.0}]

    t = tr.Transcriber.__new__(tr.Transcriber)
    t.initial_prompt = "Grafana, roadmap."
    t.terms = ["Grafana", "roadmap"]
    t.use_vad = True
    monkeypatch.setattr(tr.Transcriber, "_decode", fake_decode)

    segs = t._transcribe_one(str(wav))

    assert calls == [(20 * sr, "Grafana, roadmap."), (20 * sr, "Grafana, roadmap.")]
    assert segs == [{"start": 11.0, "end": 12.0, "text": "кусок 1"},
                    {"start": 51.0, "end": 52.0, "text": "кусок 2"}]


def test_transcribe_one_without_speech_returns_empty(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf
    from daemon import transcriber as tr
    wav = tmp_path / "mic.wav"
    sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000)
    monkeypatch.setattr(tr, "speech_spans", lambda audio: [])
    t = tr.Transcriber.__new__(tr.Transcriber)
    t.initial_prompt = None
    t.terms = []
    t.use_vad = True
    monkeypatch.setattr(tr.Transcriber, "_decode",
                        lambda self, a, p: pytest.fail("тишину не декодируем"))
    assert t._transcribe_one(str(wav)) == []


# ─── фильтр выдуманных фраз ──────────────────────────────────────────────────


def test_filter_hallucinations_drops_whisper_credits_and_silence_thanks():
    from daemon.transcriber import filter_hallucinations
    segs = [
        {"start": 0, "end": 1, "text": "Продолжение следует...", "no_speech_prob": 0.1},
        {"start": 1, "end": 2, "text": "Субтитры сделал DimaTorzok", "no_speech_prob": 0.1},
        {"start": 2, "end": 3, "text": "Спасибо.", "no_speech_prob": 0.6},
        {"start": 3, "end": 4, "text": "Спасибо.", "no_speech_prob": 0.05},
        {"start": 4, "end": 5, "text": "Давайте по релизу.", "no_speech_prob": 0.0},
    ]
    kept = filter_hallucinations(segs, terms=[])
    assert [s["text"] for s in kept] == ["Спасибо.", "Давайте по релизу."]


def test_filter_hallucinations_drops_glossary_echo_and_collapses_loops():
    from daemon.transcriber import filter_hallucinations
    terms = ["Grafana", "roadmap", "SLA", "backlog", "Kubernetes"]
    segs = [
        {"start": 0, "end": 1, "text": "Grafana, roadmap, SLA, backlog, Kubernetes."},
        {"start": 1, "end": 2, "text": "Мы обновили Grafana, разобрали roadmap, backlog и SLA на месте."},
        {"start": 2, "end": 3, "text": "да да да да да да"},
    ]
    kept = filter_hallucinations(segs, terms=terms)
    assert [s["text"] for s in kept] == [
        "Мы обновили Grafana, разобрали roadmap, backlog и SLA на месте.", "да да"]


# ─── словарь терминов ────────────────────────────────────────────────────────


def test_load_glossary_skips_comments_and_blanks(tmp_path):
    from daemon.transcriber import load_glossary
    f = tmp_path / "terms.txt"
    f.write_text("# комментарий\nGrafana\n\n  Load Balancer  \nClaude\n", encoding="utf-8")
    assert load_glossary(f) == ["Grafana", "Load Balancer", "Claude"]
    assert load_glossary(tmp_path / "нет.txt") == []


def test_glossary_prompt_is_terms_only():
    """Служебные слова («Термины:») модель дописывает в начало расшифровки,
    поэтому в подсказке только сами термины."""
    from daemon.transcriber import glossary_prompt
    assert glossary_prompt(["Grafana", "Claude"]) == "Grafana, Claude."
    assert glossary_prompt([]) is None


# ─── эхо: голоса собеседников из динамиков попадают в микрофон ───────────────


def test_drop_echo_removes_mic_copy_of_system_speech():
    from daemon.transcriber import drop_echo
    sys_segs = [
        {"start": 17.0, "end": 26.0,
         "text": "Давай, наверное, Миша, расскажи, что там тебе осталось по продукту?"},
        {"start": 27.0, "end": 37.0, "text": "Да, мне осталось поправить отчёт и выкатить."},
    ]
    mic_segs = [
        # эхо: тот же текст с ошибками распознавания, сдвиг по времени
        {"start": 15.0, "end": 25.0,
         "text": "Так, давай, наверное, Мища, расскажи, что там тебе осталось по продукту"},
        # своя реплика в то же время — другой текст
        {"start": 27.5, "end": 30.0, "text": "Подожди, а метрики уже посчитали?"},
        # своё короткое «да» не совпадает по времени с чужим — остаётся
        {"start": 40.0, "end": 40.5, "text": "Да."},
    ]
    kept = drop_echo(mic_segs, sys_segs)
    assert [s["text"] for s in kept] == ["Подожди, а метрики уже посчитали?", "Да."]


def test_drop_echo_short_phrase_dropped_only_on_exact_same_time():
    from daemon.transcriber import drop_echo
    sys_segs = [{"start": 10.0, "end": 10.6, "text": "Вот."}]
    mic_echo = [{"start": 10.3, "end": 10.9, "text": "Вот"}]
    mic_late = [{"start": 14.0, "end": 14.5, "text": "Вот."}]
    assert drop_echo(mic_echo, sys_segs) == []
    assert drop_echo(mic_late, sys_segs) == mic_late


def test_transcribe_stereo_drops_echo_from_mic(tmp_path, monkeypatch):
    from daemon.transcriber import Transcriber
    wav = tmp_path / "audio.wav"
    _write_stereo_wav(wav)

    def fake_one(self, audio_path):
        if "mic" in audio_path:
            return [{"start": 0.0, "end": 2.0, "text": "привет, начнём"},
                    {"start": 2.6, "end": 5.4, "text": "давайте по релизу"}]
        return [{"start": 2.5, "end": 5.5, "text": "давайте по релизу"}]

    monkeypatch.setattr(Transcriber, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(Transcriber, "_transcribe_one", fake_one)

    segments = Transcriber().transcribe(str(wav))
    assert [(s["speaker"], s["text"]) for s in segments] == [
        ("Спикер 1", "привет, начнём"), ("Спикер 2", "давайте по релизу")]


def test_drop_echo_keeps_own_phrase_whose_words_are_scattered_in_long_system_text():
    """Своя короткая реплика не эхо, даже если её слова по отдельности есть в
    длинной реплике собеседника рядом."""
    from daemon.transcriber import drop_echo
    sys_segs = [{"start": 10.0, "end": 30.0,
                 "text": "Смотри, альфа у нас стоит старая, а вот бета уже новая, "
                         "и мы это обсуждали, да, на прошлой неделе"}]
    mic_segs = [{"start": 20.0, "end": 21.0, "text": "Альфа-бета, да?"},
                {"start": 22.0, "end": 23.5, "text": "Ну это мы на неделе решим"}]
    assert drop_echo(mic_segs, sys_segs) == mic_segs


def test_filter_hallucinations_drops_foreign_script_garbage():
    """На шуме Whisper выдаёт кашу из разных алфавитов; при language=ru
    иероглифы и хангыль — всегда мусор. Латиница с русскими окончаниями — нет."""
    from daemon.transcriber import filter_hallucinations
    segs = [{"start": 0, "end": 1, "text": "показываешьмоANE Ske государству석 144ほど"},
            {"start": 1, "end": 2, "text": "Grafanу подняли, SLAшка не горит, Ü-образно."}]
    assert [s["text"] for s in filter_hallucinations(segs, terms=[])] == [
        "Grafanу подняли, SLAшка не горит, Ü-образно."]
