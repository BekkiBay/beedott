"""HTTP-слой поверх Ollama: health-check, autostart, чат с JSON-ответом.

Вынесено из daemon/summarizer.py (PLAN_v3 Task 2.5) — summarizer.py дёргает
эти функции сам (в начале summarize_ollama), а не ходит в requests напрямую,
поэтому health-check относится к реальному боевому вызову, а не к отдельной
незасчитываемой проверке.

SPEC_v3 §10, кейс «Ollama не запущена»: демон пытается поднять сервис сам
(`ollama serve`), не получилось за ≤20 сек — штатный failed_retry оркестратора
с понятным текстом ошибки. Автопереключения на Claude НЕТ.
"""
import logging
import subprocess
import time

import requests

logger = logging.getLogger(__name__)

# Сколько ждём, пока `ollama serve` поднимется после autostart (SPEC_v3 §10 /
# PLAN_v3 Task 2.5: "ожидание ≤20 сек").
OLLAMA_AUTOSTART_TIMEOUT_S = 20
OLLAMA_AUTOSTART_POLL_INTERVAL_S = 0.5
OLLAMA_TAGS_TIMEOUT_S = 5


class OllamaDown(RuntimeError):
    """Ollama не отвечает на /api/tags и не поднялась автостартом."""


class OllamaModelMissing(RuntimeError):
    """Ollama жива, но нужной модели нет среди /api/tags."""


def _get_tags(url: str, *, timeout: float = OLLAMA_TAGS_TIMEOUT_S) -> dict:
    """GET {url}/api/tags. Кидает requests.ConnectionError/Timeout при недоступности."""
    r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
    r.raise_for_status()
    return r.json()


def _is_up(url: str) -> bool:
    try:
        _get_tags(url)
        return True
    except (requests.ConnectionError, requests.Timeout):
        return False


def ensure_running(url: str, autostart: bool = True) -> None:
    """Убедиться, что Ollama отвечает на {url}/api/tags.

    connection refused/timeout → (если autostart) поднять `ollama serve` в
    фоне и поллить /api/tags до OLLAMA_AUTOSTART_TIMEOUT_S секунд. Не
    поднялась (или autostart выключен) → OllamaDown с понятным текстом.
    """
    if _is_up(url):
        return
    if not autostart:
        raise OllamaDown(f"Ollama не запущена ({url}), autostart выключен")

    logger.warning(
        "ollama_client: Ollama не отвечает на %s, пробую поднять `ollama serve`", url)
    try:
        subprocess.Popen(
            ["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, OSError) as e:
        raise OllamaDown(
            f"Ollama не запущена — не удалось запустить `ollama serve`: {e}") from e

    deadline = time.monotonic() + OLLAMA_AUTOSTART_TIMEOUT_S
    while time.monotonic() < deadline:
        if _is_up(url):
            logger.info("ollama_client: Ollama поднялась после autostart")
            return
        time.sleep(OLLAMA_AUTOSTART_POLL_INTERVAL_S)

    raise OllamaDown(
        "Ollama не запущена (autostart не поднял сервис за "
        f"{OLLAMA_AUTOSTART_TIMEOUT_S} сек) — запусти `ollama serve` вручную")


def ensure_model(url: str, model: str) -> None:
    """Проверить, что `model` есть среди /api/tags — иначе OllamaModelMissing
    с подсказкой `ollama pull <model>` (SPEC_v3 §10 / PLAN_v3 Task 2.5)."""
    try:
        data = _get_tags(url)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise OllamaDown(f"Ollama не отвечает на {url}: {e}") from e

    names = set()
    for m in data.get("models", []) or []:
        n = m.get("name") or m.get("model")
        if n:
            names.add(n)

    if model in names:
        return
    # мягкое сопоставление без учёта тега (например "vikhr-nemo" == "vikhr-nemo:12b")
    base = model.split(":")[0]
    if any(n.split(":")[0] == base for n in names):
        return

    raise OllamaModelMissing(f"модель {model} не найдена — выполни: ollama pull {model}")


def chat_json(
    messages: list[dict], *, model: str, url: str, num_ctx: int, timeout: float,
) -> str:
    """Один POST {url}/api/chat с format=json. Возвращает сырой content (строка).
    Кидает requests.RequestException при сетевой ошибке/не-2xx."""
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": messages,
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }
    r = requests.post(f"{url.rstrip('/')}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return (data.get("message") or {}).get("content", "") or ""
