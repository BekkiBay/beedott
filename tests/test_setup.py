"""daemon/setup.py — команды мастера первичной настройки (JSON-строки в stdout)."""
import io
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

# config при первом импорте делает load_dotenv(репо/.env) — импортируем до
# фикстур, иначе он вернёт в os.environ ключи, которые фикстура удалила.
import config
from daemon import setup, summarizer


def events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


@pytest.fixture
def appdir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "app"))
    for key in ("SUMMARY_BACKEND", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
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
    assert events(capsys) == [{"event": "ok", "model": True, "summary": None, "telegram": False,
                               "summary_model": "claude-opus-5-5", "summary_custom": False,
                               "claude": None}]


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


class FakeModels:
    def __init__(self, exc=None):
        self.exc = exc

    def list(self, limit):
        if self.exc:
            raise self.exc
        return []


def fake_anthropic(monkeypatch, exc=None):
    import anthropic

    class Client:
        def __init__(self, *, api_key, **kwargs):
            self.api_key = api_key
            self.models = FakeModels(exc)

    monkeypatch.setattr(anthropic, "Anthropic", Client)


def run_with_stdin(monkeypatch, argv, text, *more_lines):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join([text, *more_lines]) + "\n"))
    return setup.main(argv)


def test_check_key_ok_writes_backend_and_key(appdir, capsys, monkeypatch):
    import stat
    fake_anthropic(monkeypatch)
    assert run_with_stdin(monkeypatch, ["check-key"], "sk-ant-good") == 0
    assert events(capsys) == [{"event": "ok"}]
    cfg = appdir / "config.env"
    text = cfg.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-good" in text and "SUMMARY_BACKEND=api" in text
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_check_key_rejected(appdir, capsys, monkeypatch):
    import anthropic
    import httpx
    resp = httpx.Response(401, request=httpx.Request("GET", "https://api.anthropic.com/v1/models"))
    fake_anthropic(monkeypatch, anthropic.AuthenticationError("bad sk-ant-bad", response=resp, body=None))
    assert run_with_stdin(monkeypatch, ["check-key"], "sk-ant-bad") == 1
    ev = events(capsys)[-1]
    assert ev["code"] == "invalid_key"
    assert "sk-ant-bad" not in ev["message"]
    assert not (appdir / "config.env").exists()


def test_check_key_no_network(appdir, capsys, monkeypatch):
    import anthropic
    import httpx
    fake_anthropic(monkeypatch, anthropic.APIConnectionError(
        request=httpx.Request("GET", "https://api.anthropic.com/v1/models")))
    run_with_stdin(monkeypatch, ["check-key"], "sk-ant-x")
    assert events(capsys)[-1]["code"] == "no_network"


def test_check_key_empty(appdir, capsys, monkeypatch):
    assert run_with_stdin(monkeypatch, ["check-key"], "") == 1
    assert events(capsys)[-1]["code"] == "invalid_key"


class FakeTelegram:
    """Подмена requests.post для Bot API: очередь ответов по методу."""

    def __init__(self, responses):
        self.responses = responses  # {method: [(status, json), ...]}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json or {}))
        status, body = self.responses[method].pop(0)

        class R:
            status_code = status

            def json(self_inner):
                return body
        return R()


def fake_tg(monkeypatch, responses):
    import requests
    tg = FakeTelegram(responses)
    monkeypatch.setattr(requests, "post", tg.post)
    return tg


def test_telegram_check_ok(appdir, capsys, monkeypatch):
    fake_tg(monkeypatch, {"getMe": [(200, {"ok": True, "result": {"username": "my_bot"}})]})
    assert run_with_stdin(monkeypatch, ["telegram-check"], "1:abc") == 0
    assert events(capsys)[-1] == {"event": "ok", "bot": "my_bot"}


def test_telegram_check_bad_token(appdir, capsys, monkeypatch):
    fake_tg(monkeypatch, {"getMe": [(401, {"ok": False, "description": "Unauthorized"})]})
    run_with_stdin(monkeypatch, ["telegram-check"], "1:abc")
    assert events(capsys)[-1]["code"] == "invalid_token"


def test_telegram_network_error_hides_token(appdir, capsys, monkeypatch):
    import requests

    def boom(url, json=None, timeout=None):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(requests, "post", boom)
    run_with_stdin(monkeypatch, ["telegram-check"], "1:secret")
    ev = events(capsys)[-1]
    assert ev["code"] == "no_network"
    assert "1:secret" not in ev["message"]


def test_telegram_connect_waits_for_start(appdir, capsys, monkeypatch):
    """Принимаем только /start с одноразовым кодом из ссылки мастера: чужой
    /start из очереди бота (бот уже кем-то запущен) чат не привязывает."""
    start = {"update_id": 7, "message": {"text": "/start k3y9", "chat": {"id": 555}}}
    stranger = {"update_id": 5, "message": {"text": "/start", "chat": {"id": 2}}}
    other = {"update_id": 6, "message": {"text": "привет", "chat": {"id": 1}}}
    tg = fake_tg(monkeypatch, {
        "getUpdates": [(200, {"ok": True, "result": []}),
                       (200, {"ok": True, "result": [stranger, other, start]}),
                       (200, {"ok": True, "result": []})],
        "sendMessage": [(200, {"ok": True, "result": {}})],
    })
    assert run_with_stdin(monkeypatch, ["telegram-connect"], "1:abc", "k3y9") == 0
    assert events(capsys)[-1] == {"event": "ok", "chat_id": 555}
    ack = [p for m, p in tg.calls if m == "getUpdates"][-1]
    assert ack["offset"] == 8
    assert ("sendMessage", {"chat_id": 555, "text": setup.TELEGRAM_HELLO}) in tg.calls
    text = (appdir / "config.env").read_text()
    assert "TELEGRAM_BOT_TOKEN=1:abc" in text and "TELEGRAM_CHAT_ID=555" in text


def test_telegram_connect_timeout(appdir, capsys, monkeypatch):
    import itertools
    # deadline, проверка цикла, расчёт wait — 0; дальше время вышло.
    clock = itertools.chain([0, 0, 0], itertools.repeat(200))
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(clock))
    fake_tg(monkeypatch, {"getUpdates": [(200, {"ok": True, "result": []})]})
    run_with_stdin(monkeypatch, ["telegram-connect"], "1:abc", "k3y9")
    assert events(capsys)[-1]["code"] == "timeout"


def test_telegram_connect_bot_busy(appdir, capsys, monkeypatch):
    fake_tg(monkeypatch, {"getUpdates": [(409, {"ok": False, "description": "Conflict: webhook"})]})
    run_with_stdin(monkeypatch, ["telegram-connect"], "1:abc", "k3y9")
    assert events(capsys)[-1]["code"] == "bot_busy"


def test_telegram_connect_requires_code(appdir, capsys, monkeypatch):
    run_with_stdin(monkeypatch, ["telegram-connect"], "1:abc")
    assert events(capsys)[-1]["event"] == "error"


def fake_hub(monkeypatch, tmp_path, sizes, cached=()):
    """Подмена huggingface_hub: model_info с размерами, hf_hub_download, который
    двигает переданный tqdm_class шагами по 10 байт."""
    import huggingface_hub
    from huggingface_hub import constants
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "hub"))

    class Sibling:
        def __init__(self, name, size):
            self.rfilename, self.size = name, size

    class Info:
        siblings = [Sibling(n, s) for n, s in sizes.items()]

    class Api:
        def model_info(self, repo, files_metadata=False):
            assert files_metadata
            return Info()

    downloaded = []

    def download(repo, filename, tqdm_class=None):
        bar = tqdm_class(total=sizes[filename])
        for _ in range(sizes[filename] // 10):
            bar.update(10)
        bar.close()
        downloaded.append(filename)
        return str(tmp_path / filename)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    monkeypatch.setattr(setup, "_cached_file", lambda repo, name: name in cached)
    return downloaded


def test_install_model_reports_progress(appdir, capsys, monkeypatch, tmp_path):
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(ticks) * 0.3)
    downloaded = fake_hub(monkeypatch, tmp_path, {"config.json": 10, "weights.safetensors": 100},
                          cached={"config.json"})
    assert setup.main(["install-model"]) == 0
    evs = events(capsys)
    progress = [e for e in evs if e["event"] == "progress"]
    assert downloaded == ["weights.safetensors"]          # закэшированное не качаем
    assert all(e["total"] == 110 and e["stage"] == "model" for e in progress)
    assert progress[0]["done"] >= 10                      # config.json уже учтён
    assert progress[-1]["done"] == 110                    # финал всегда отправлен
    assert len(progress) < 10                             # троттлинг 0,5 с
    assert evs[-1] == {"event": "ok", "model": setup.MODEL_REPO}


def test_install_model_removes_stale_incomplete(appdir, capsys, monkeypatch, tmp_path):
    """Старые *.incomplete (процесс убит) — удаляем; свежие не трогаем: их может
    прямо сейчас писать другая загрузка."""
    import os
    import time as _time
    fake_hub(monkeypatch, tmp_path, {"weights.safetensors": 10})
    blobs = tmp_path / "hub" / "models--mlx-community--whisper-large-v3-turbo" / "blobs"
    blobs.mkdir(parents=True)
    stale = blobs / "abc.123.incomplete"
    stale.write_bytes(b"x" * 5)
    old = _time.time() - setup.STALE_PARTIAL_SEC - 60
    os.utime(stale, (old, old))
    fresh = blobs / "abc.456.incomplete"
    fresh.write_bytes(b"z")
    (blobs / "done-blob").write_bytes(b"y")
    setup.main(["install-model"])
    assert not stale.exists()
    assert fresh.exists()
    assert (blobs / "done-blob").exists()


def test_install_model_disk_full_before_start(appdir, capsys, monkeypatch, tmp_path):
    fake_hub(monkeypatch, tmp_path, {"weights.safetensors": 10 * 2**30})
    monkeypatch.setattr(setup, "_free_bytes", lambda path: 2**30)
    assert setup.main(["install-model"]) == 1
    ev = events(capsys)[-1]
    assert ev["code"] == "disk_full" and "ГБ" in ev["message"]


def test_install_model_no_network(appdir, capsys, monkeypatch, tmp_path):
    import huggingface_hub
    import httpx
    from huggingface_hub import constants
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "hub"))

    class Api:
        def model_info(self, repo, files_metadata=False):
            raise httpx.ConnectError("nodename nor servname provided")
    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    setup.main(["install-model"])
    assert events(capsys)[-1]["code"] == "no_network"


def test_link_ffmpeg(appdir, capsys, monkeypatch, tmp_path):
    import imageio_ffmpeg
    exe = tmp_path / "ffmpeg-macos-aarch64"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(exe))
    assert setup.main(["link-ffmpeg"]) == 0
    link = appdir / "runtime" / "bin" / "ffmpeg"
    assert link.is_symlink() and link.resolve() == exe.resolve()
    assert setup.main(["link-ffmpeg"]) == 0  # повтор не падает
    assert events(capsys)[-1] == {"event": "ok", "ffmpeg": str(link)}


@pytest.mark.slow
def test_imageio_ffmpeg_has_what_daemon_uses():
    """stages.py/transcriber.py/reconcile.py: flac, channelsplit, -ignore_length."""
    import subprocess

    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()

    def out(*args):
        return subprocess.run([exe, "-hide_banner", *args], capture_output=True, text=True).stdout

    assert " flac " in out("-encoders")
    assert "channelsplit" in out("-filters")
    assert "ignore_length" in out("-h", "demuxer=wav")


def run(name, stdin_text, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = setup.main([name])
    return code, events(capsys)[-1]


def test_set_model_writes_config(appdir, capsys, monkeypatch):
    code, ev = run("set-model", "claude-sonnet-5-5\n", capsys, monkeypatch)
    assert code == 0 and ev == {"event": "ok", "model": "claude-sonnet-5-5"}
    assert "ANTHROPIC_MODEL=claude-sonnet-5-5" in (appdir / "config.env").read_text()
    setup.main(["status"])
    assert events(capsys)[-1]["summary_model"] == "claude-sonnet-5-5"


def test_set_model_rejects_unknown(appdir, capsys, monkeypatch):
    code, ev = run("set-model", "gpt-5\n", capsys, monkeypatch)
    assert code == 1 and ev["code"] == "bad_model"
    assert not (appdir / "config.env").exists()


def test_set_instructions_keeps_multiline_text(appdir, capsys, monkeypatch):
    text = "Первая строка.\n\nВторая строка."
    code, ev = run("set-instructions", text + "\n", capsys, monkeypatch)
    assert code == 0 and ev == {"event": "ok", "custom": True}
    assert summarizer.custom_instructions() == text
    setup.main(["status"])
    assert events(capsys)[-1]["summary_custom"] is True


def test_set_instructions_default_or_blank_removes_file(appdir, capsys, monkeypatch):
    run("set-instructions", "Своя.\n", capsys, monkeypatch)
    code, ev = run("set-instructions", summarizer.DEFAULT_INSTRUCTIONS + "\n", capsys, monkeypatch)
    assert ev == {"event": "ok", "custom": False}
    assert not summarizer.instructions_path().exists()
    run("set-instructions", "Своя.\n", capsys, monkeypatch)
    code, ev = run("set-instructions", "  \n", capsys, monkeypatch)
    assert ev == {"event": "ok", "custom": False}
    assert not summarizer.instructions_path().exists()


def test_set_instructions_too_long(appdir, capsys, monkeypatch):
    code, ev = run("set-instructions", "я" * (setup.MAX_INSTRUCTIONS + 1), capsys, monkeypatch)
    assert code == 1 and ev["code"] == "too_long"
    assert not summarizer.instructions_path().exists()


def test_summary_settings(appdir, capsys, monkeypatch):
    run("set-instructions", "Своя.\n", capsys, monkeypatch)
    setup.main(["summary-settings"])
    ev = events(capsys)[-1]
    assert ev["model"] == "claude-opus-5-5"
    assert ev["instructions"] == "Своя." and ev["custom"] is True
    assert ev["default_instructions"] == summarizer.DEFAULT_INSTRUCTIONS


def test_summary_models_match_swift():
    swift = Path(__file__).parent.parent / "app/Sources/MeetMind/RuntimeLogic.swift"
    ids = set(re.findall(r'id: "(claude-[a-z0-9-]+)"', swift.read_text(encoding="utf-8")))
    assert ids == set(setup.SUMMARY_MODELS)
    assert config.DEFAULT_ANTHROPIC_MODEL in setup.SUMMARY_MODELS


# ───── MCP: подключение к Claude Desktop ──────────────────────────────────────


def desktop_config() -> Path:
    return Path(os.environ["MEETMIND_CLAUDE_DESKTOP_CONFIG"])


def write_desktop(data) -> Path:
    path = desktop_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_claude_connect_creates_config(appdir, capsys):
    assert setup.main(["claude-connect"]) == 0
    path = desktop_config()
    assert events(capsys)[-1] == {"event": "ok", "config": str(path)}
    entry = json.loads(path.read_text())["mcpServers"]["beedott"]
    assert entry == setup.mcp_launch()
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-P", "-m", "daemon.mcp_server"]
    assert (Path(entry["env"]["PYTHONPATH"]) / "daemon" / "mcp_server.py").exists()


def test_claude_connect_keeps_other_settings_and_backs_up(appdir, capsys):
    path = write_desktop({"preferences": {"x": 1}, "mcpServers": {"trino": {"command": "t"}}})
    setup.main(["claude-connect"])
    data = json.loads(path.read_text())
    assert data["preferences"] == {"x": 1}
    assert data["mcpServers"]["trino"] == {"command": "t"}
    assert "beedott" in data["mcpServers"]
    backup = json.loads(path.with_name(path.name + ".beedott.bak").read_text())
    assert backup["mcpServers"] == {"trino": {"command": "t"}}


def test_claude_connect_is_idempotent(appdir, capsys):
    setup.main(["claude-connect"])
    first = desktop_config().read_text()
    setup.main(["claude-connect"])
    assert desktop_config().read_text() == first


def test_claude_connect_empty_file_is_empty_config(appdir, capsys):
    path = write_desktop("")
    assert setup.main(["claude-connect"]) == 0
    assert "beedott" in json.loads(path.read_text())["mcpServers"]


def test_claude_connect_keeps_symlink(appdir, capsys, tmp_path):
    real = tmp_path / "real-config.json"
    real.write_text("{}")
    path = desktop_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(real)
    setup.main(["claude-connect"])
    assert path.is_symlink()
    assert "beedott" in json.loads(real.read_text())["mcpServers"]


@pytest.mark.parametrize("raw", ["{oops", "[1, 2]", '{"mcpServers": []}'])
def test_claude_connect_bad_config_untouched(appdir, capsys, raw):
    path = write_desktop(raw)
    assert setup.main(["claude-connect"]) == 1
    assert events(capsys)[-1]["code"] == "bad_config"
    assert path.read_text() == raw


def test_claude_disconnect_removes_only_beedott(appdir, capsys):
    path = write_desktop({"mcpServers": {"trino": {"command": "t"}}})
    setup.main(["claude-connect"])
    assert setup.main(["claude-disconnect"]) == 0
    assert json.loads(path.read_text())["mcpServers"] == {"trino": {"command": "t"}}


def test_claude_disconnect_without_file_is_ok(appdir, capsys):
    assert setup.main(["claude-disconnect"]) == 0
    assert not desktop_config().exists()


def test_status_claude_states(appdir, capsys):
    def claude():
        setup.main(["status"])
        return events(capsys)[-1]["claude"]

    assert claude() is None
    setup.main(["claude-connect"])
    assert claude() == "connected"
    path = desktop_config()
    data = json.loads(path.read_text())
    data["mcpServers"]["beedott"]["command"] = "/old/python"
    path.write_text(json.dumps(data))
    assert claude() == "stale"
    path.write_text("{oops")
    assert claude() == "bad_config"


def test_launch_env_carries_pycache_prefix(appdir, monkeypatch):
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/tmp/pyc")
    assert setup.mcp_launch()["env"]["PYTHONPYCACHEPREFIX"] == "/tmp/pyc"


def test_launch_env_carries_tz(appdir, monkeypatch):
    monkeypatch.setenv("MEETMIND_TZ", "Asia/Tashkent")
    assert setup.mcp_launch()["env"]["MEETMIND_TZ"] == "Asia/Tashkent"


def test_mcp_launch_not_shadowed_by_project_package(appdir, tmp_path):
    """Claude Code запускает MCP-сервер в папке проекта пользователя. Без -P
    cwd встаёт первым в sys.path, и свой daemon/-пакет в проекте перекрыл бы
    наш — кладём такой фейковый пакет и проверяем, что `python -P -m
    daemon.mcp_server` всё равно импортирует настоящий модуль из репозитория."""
    project = tmp_path / "project"
    (project / "daemon").mkdir(parents=True)
    (project / "daemon" / "__init__.py").write_text("raise RuntimeError('чужой daemon')\n")
    entry = setup.mcp_launch()
    assert entry["args"][0] == "-P"
    code = "import daemon.mcp_server, sys; print(daemon.mcp_server.__file__)"
    r = subprocess.run([entry["command"], "-P", "-c", code], cwd=project,
                       env=entry["env"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    repo = Path(__file__).resolve().parent.parent
    assert Path(r.stdout.strip()).resolve().is_relative_to(repo)


def test_claude_connect_and_status_see_same_environment(appdir, monkeypatch):
    """В проде claude-connect и status — разные процессы `python -m
    daemon.setup <команда>`: без общего `import config` каждый видел бы своё
    окружение. Если в config.env лежит PYTHONPYCACHEPREFIX, status (который
    уже импортировал config через _setting()) всегда пересчитывал бы want с
    этим ключом, а connect писал бы запись без него — stale навсегда.
    Чистим окружение от PYTHONPYCACHEPREFIX/MEETMIND_TZ, чтобы тест не зависел
    от того, что уже экспортировано в шелле, где запущен pytest."""
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    monkeypatch.delenv("MEETMIND_TZ", raising=False)
    write_config(appdir, "PYTHONPYCACHEPREFIX=/tmp/somewhere\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
           "MEETMIND_APPDIR": str(appdir),
           "MEETMIND_CLAUDE_DESKTOP_CONFIG": os.environ["MEETMIND_CLAUDE_DESKTOP_CONFIG"]}

    def run(cmd):
        r = subprocess.run([sys.executable, "-m", "daemon.setup", cmd],
                           capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stdout + r.stderr
        return json.loads(r.stdout.strip().splitlines()[-1])

    run("claude-connect")
    assert run("status")["claude"] == "connected"


# ───── MCP: аккуратная запись конфига Claude Desktop ──────────────────────────


def test_claude_connect_keeps_file_mode(appdir, capsys):
    import stat
    path = write_desktop({"mcpServers": {}})
    path.chmod(0o600)
    assert setup.main(["claude-connect"]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_claude_connect_readonly_target_untouched(appdir, capsys):
    path = write_desktop({"mcpServers": {"trino": {"command": "t"}}})
    original = path.read_bytes()
    path.chmod(0o444)
    try:
        assert setup.main(["claude-connect"]) == 1
        ev = events(capsys)[-1]
        assert ev["code"] == "internal"
        assert path.read_bytes() == original
        assert not path.with_name(path.name + ".beedott.bak").exists()
        assert not path.with_name(path.name + ".beedott.tmp").exists()
    finally:
        path.chmod(0o644)


def test_claude_connect_backup_not_blocked_by_readonly_bak(appdir, capsys):
    """Старый .beedott.bak может остаться read-only (например, от бэкапа
    когда-то read-only файла) — это не должно мешать снять новый бэкап."""
    path = write_desktop({"mcpServers": {"trino": {"command": "t"}}})
    bak = path.with_name(path.name + ".beedott.bak")
    bak.write_text("stale")
    bak.chmod(0o444)
    try:
        assert setup.main(["claude-connect"]) == 0
        assert json.loads(bak.read_text())["mcpServers"] == {"trino": {"command": "t"}}
    finally:
        bak.chmod(0o644)


def test_claude_connect_non_finite_number_fails_cleanly(appdir, capsys):
    raw = '{"x": 1e400}'
    path = write_desktop(raw)
    assert setup.main(["claude-connect"]) == 1
    assert path.read_text() == raw
    assert not any(p.name.endswith(".beedott.tmp") for p in path.parent.iterdir())


def test_claude_connect_second_call_is_noop_and_keeps_first_backup(appdir, capsys):
    original = {"mcpServers": {"trino": {"command": "t"}}}
    path = write_desktop(original)
    assert setup.main(["claude-connect"]) == 0
    bak = path.with_name(path.name + ".beedott.bak")
    assert json.loads(bak.read_text()) == original
    after_first = path.read_text()
    assert setup.main(["claude-connect"]) == 0
    assert json.loads(bak.read_text()) == original
    assert path.read_text() == after_first


def test_status_claude_state_never_raises(appdir, capsys, monkeypatch):
    def boom(path):
        raise RecursionError("слишком вложенный JSON")
    monkeypatch.setattr(setup, "_read_desktop_config", boom)
    assert setup.main(["status"]) == 0
    assert events(capsys)[-1]["claude"] == "bad_config"


# ───── MCP: Claude Code ───────────────────────────────────────────────────────


def fake_claude(tmp_path, monkeypatch, exit_code=0) -> Path:
    """Фейковый claude CLI: пишет argv в файл, выходит с exit_code. exit_code —
    либо число (одно и то же для всех вызовов), либо dict {argv[1:3]: код} с
    кодом по умолчанию 0 для не перечисленных подкоманд."""
    log = tmp_path / "claude-argv.jsonl"
    script = tmp_path / "claude"
    codes = exit_code if isinstance(exit_code, dict) else {}
    default = exit_code if isinstance(exit_code, int) else 0
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"codes = {codes!r}\n"
        f"sys.exit(codes.get(tuple(sys.argv[1:3]), {default}))\n")
    script.chmod(0o755)
    from daemon import claude_auth
    monkeypatch.setattr(claude_auth, "claude_bin", lambda: str(script))
    return log


def claude_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_claude_code_connect_registers_same_launch(appdir, capsys, monkeypatch, tmp_path):
    log = fake_claude(tmp_path, monkeypatch)
    assert setup.main(["claude-code-connect"]) == 0
    result = events(capsys)[-1]
    assert result["claude_code"] == "added"
    remove, add = claude_calls(log)
    assert remove == ["mcp", "remove", "--scope", "user", "beedott"]
    assert add[:5] == ["mcp", "add-json", "--scope", "user", "beedott"]
    assert json.loads(add[5]) == {"type": "stdio", **setup.mcp_launch()}
    assert shlex.split(result["command"]) == ["claude", *add]


def test_claude_code_connect_without_cli(appdir, capsys):
    assert setup.main(["claude-code-connect"]) == 0
    result = events(capsys)[-1]
    assert result["claude_code"] == "absent"
    assert result["command"].startswith("claude mcp add-json --scope user beedott ")


def test_claude_code_connect_cli_fails(appdir, capsys, monkeypatch, tmp_path):
    fake_claude(tmp_path, monkeypatch, exit_code=1)
    assert setup.main(["claude-code-connect"]) == 0
    assert events(capsys)[-1]["claude_code"] == "failed"


def test_claude_code_disconnect(appdir, capsys, monkeypatch, tmp_path):
    log = fake_claude(tmp_path, monkeypatch)
    setup.main(["claude-code-disconnect"])
    assert events(capsys)[-1] == {"event": "ok", "claude_code": "removed"}
    assert claude_calls(log) == [["mcp", "remove", "--scope", "user", "beedott"]]


def test_claude_code_disconnect_without_cli(appdir, capsys):
    setup.main(["claude-code-disconnect"])
    assert events(capsys)[-1] == {"event": "ok", "claude_code": "absent"}


def test_claude_code_disconnect_recovers_when_already_absent(appdir, capsys, monkeypatch, tmp_path):
    """remove падает, потому что beedott и не был зарегистрирован — желаемое
    состояние (не зарегистрирован) уже достигнуто, проверяем через `mcp get`."""
    log = fake_claude(tmp_path, monkeypatch, exit_code={("mcp", "remove"): 1, ("mcp", "get"): 1})
    assert setup.main(["claude-code-disconnect"]) == 0
    assert events(capsys)[-1] == {"event": "ok", "claude_code": "removed"}
    assert claude_calls(log) == [["mcp", "remove", "--scope", "user", "beedott"],
                                 ["mcp", "get", "beedott"]]


def test_claude_code_disconnect_fails_when_still_registered(appdir, capsys, monkeypatch, tmp_path):
    fake_claude(tmp_path, monkeypatch, exit_code={("mcp", "remove"): 1, ("mcp", "get"): 0})
    assert setup.main(["claude-code-disconnect"]) == 0
    assert events(capsys)[-1]["claude_code"] == "failed"
