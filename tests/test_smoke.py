from pathlib import Path


def test_fixture_exists():
    assert Path("tests/fixtures/sample_ru.wav").stat().st_size > 100_000
