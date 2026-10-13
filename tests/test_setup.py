"""daemon/setup.py — команды мастера первичной настройки (JSON-строки в stdout)."""
import io
import json

import pytest

# config при первом импорте делает load_dotenv(репо/.env) — импортируем до
# фикстур, иначе он вернёт в os.environ ключи, которые фикстура удалила.
import config  # noqa: F401
from daemon import setup


def events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


@pytest.fixture
def appdir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "app"))
    for key in ("SUMMARY_BACKEND", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "DEV_ENV_PATH", tmp_path / "no-dev.env")
    monkeypatch.setattr(setup, "model_cached", lambda repo=setup.MODEL_REPO: True)
    from daemon import claude_auth
    monkeypatch.setattr(claude_auth, "claude_bin", lambda: None)
    return tmp_path / "app"


def write_config(appdir, text):
    appdir.mkdir(parents=True, exist_ok=True)
    (appdir / "config.env").write_text(text)


def test_status_clean_mac(appdir, capsys):
    assert setup.main(["status"]) == 0
    assert events(capsys) == [{"event": "ok", "model": True, "summary": None, "telegram": False}]


def test_status_api_key(appdir, capsys):
    write_config(appdir, "SUMMARY_BACKEND=api\nANTHROPIC_API_KEY=sk-ant-x\n")
    setup.main(["status"])
    assert events(capsys)[-1]["summary"] == "api"


def test_status_api_without_key_is_not_configured(appdir, capsys):
    write_config(appdir, "SUMMARY_BACKEND=api\n")
    setup.main(["status"])
    assert events(capsys)[-1]["summary"] is None


def test_status_claude_cli_default_needs_binary(appdir, capsys, monkeypatch):
    """SUMMARY_BACKEND не задан — дефолт кода claude_cli; годится, только если
    claude_bin() нашёл бинарь (твой Mac), а на чистом Mac — None."""
    from daemon import claude_auth
    monkeypatch.setattr(claude_auth, "claude_bin", lambda: "/x/claude")
    setup.main(["status"])
    assert events(capsys)[-1]["summary"] == "claude_cli"


def test_status_telegram_needs_token_and_chat(appdir, capsys):
    write_config(appdir, "TELEGRAM_BOT_TOKEN=1:abc\n")
    setup.main(["status"])
    assert events(capsys)[-1]["telegram"] is False
    write_config(appdir, "TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_CHAT_ID=42\n")
    setup.main(["status"])
    assert events(capsys)[-1]["telegram"] is True


def test_status_model_missing(appdir, capsys, monkeypatch):
    monkeypatch.setattr(setup, "model_cached", lambda repo=setup.MODEL_REPO: False)
    setup.main(["status"])
    assert events(capsys)[-1]["model"] is False


def test_model_cached_checks_config_and_weights(tmp_path, monkeypatch):
    """Офлайн-проверка по кэшу HF: нужны config.json и веса; сеть не трогаем."""
    from huggingface_hub import constants
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    repo = "org/model"
    snap = tmp_path / "models--org--model" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (tmp_path / "models--org--model" / "refs").mkdir()
    (tmp_path / "models--org--model" / "refs" / "main").write_text("abc")
    (snap / "config.json").write_text("{}")
    assert setup.model_cached(repo) is False
    (snap / "weights.safetensors").write_bytes(b"x")
    assert setup.model_cached(repo) is True


def test_unknown_command(capsys):
    assert setup.main(["nope"]) == 2
    assert events(capsys)[-1]["event"] == "error"


def test_model_repo_matches_transcriber():
    from daemon import transcriber
    assert setup.MODEL_REPO == transcriber.MODEL_REPO
