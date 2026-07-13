"""Общие фикстуры для тестов MeetMind."""

# Эталонный валидный followup-объект (схема SPEC_v3 §7).
# Используется в test_summarizer.py и test_render.py.
GOOD_FOLLOWUP = {
    "context": "x",
    "discussed": [],
    "decisions": ["решили"],
    "action_items": [{"task": "сделать", "owner": "Bekzod", "deadline": None}],
    "open_questions": [],
}
