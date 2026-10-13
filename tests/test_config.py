"""config.update_config_env: точечная правка ключа без потери остальных
настроек (аудит T4: тоггл Claude в меню не должен терять токен Telegram)."""
import stat

import config
from daemon.paths import app_dir


def test_update_migrates_dev_env_instead_of_dropping_it(tmp_path, monkeypatch):
    """Секреты лежат в dev-файле ./.env, app-level config.env ещё нет. Тоггл
    создаёт config.env — в нём должны оказаться ВСЕ ключи из dev-файла, иначе
    при следующем старте config.py прочитает только config.env и потеряет
    токены. Сам dev-файл не трогаем."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "app"))
    dev_env = tmp_path / "dev.env"
    dev_env.write_text("TELEGRAM_BOT_TOKEN=tok\nSUMMARY_BACKEND=claude_cli\n")
    monkeypatch.setattr(config, "DEV_ENV_PATH", dev_env)

    path = config.update_config_env("SUMMARY_BACKEND", "api")

    assert path == app_dir() / "config.env"
    content = path.read_text()
    assert "TELEGRAM_BOT_TOKEN=tok" in content
    assert "SUMMARY_BACKEND=api" in content
    assert "SUMMARY_BACKEND=claude_cli" not in content
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert dev_env.read_text() == "TELEGRAM_BOT_TOKEN=tok\nSUMMARY_BACKEND=claude_cli\n"


def test_update_replaces_key_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    cfg = app_dir() / "config.env"
    cfg.write_text("# комментарий\nSUMMARY_BACKEND=claude_cli\nTELEGRAM_CHAT_ID=42\n")

    config.update_config_env("SUMMARY_BACKEND", "api")

    assert cfg.read_text() == "# комментарий\nSUMMARY_BACKEND=api\nTELEGRAM_CHAT_ID=42\n"
