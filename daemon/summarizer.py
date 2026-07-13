"""
Гибридный summarizer: три backend'а работают параллельно.

    1) Ollama (локально, бесплатно, оффлайн) — Vikhr-Nemo / Qwen / T-pro
    2) Claude Code CLI (через Max-подписку, без отдельного API-биллинга)  ← рекомендую
    3) Anthropic API напрямую (если нет Max, нужен API ключ)

Каждый backend выключаемый через config. Можно включить любую комбинацию.
Для типичного юзкейса с Max: USE_OLLAMA=true + USE_CLAUDE_CODE=true,
USE_ANTHROPIC=false (чтобы не дублировать оплату).
"""
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import requests

from daemon.session import Session

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Ты — продакт-менеджер, который слушает встречи и пишет краткие, "
    "конкретные фоллоу-апы на русском языке. Никакой воды, никаких общих фраз. "
    "Только факты, договорённости, дедлайны, ответственные."
)

USER_PROMPT_TEMPLATE = """Проанализируй запись встречи и создай структурированный фоллоу-ап.

Встреча: {title}
Дата: {date_str}, {start_time}-{end_time} ({duration} минут)
Участники: {attendees}

Полная запись встречи:
---
{transcript}
---

Создай фоллоу-ап СТРОГО в этом формате (без отступления):

**{title}** · {date_str}, {start_time}-{end_time} ({duration} мин)
👥 {attendees}

**Контекст**
[2-3 предложения: о чём встреча, какая проблема обсуждалась, к чему пришли]

**Что обсуждалось**
• [пункт 1 — кто что сказал/предложил]
• [пункт 2]
• [пункт 3]

**Договорённости**
• [конкретное решение 1]
• [конкретное решение 2]

**Action items**
📅 [дата дедлайна или "asap"] — [что сделать] — [кто отвечает]
📅 [...] — [...] — [...]

**Открытые вопросы**
• [что осталось нерешённым]

Если каких-то пунктов в записи нет — пиши "нет" в соответствующей секции, не выдумывай.
Отвечай ТОЛЬКО фоллоу-апом, без преамбулы вроде "Вот ваш фоллоу-ап:" и без postscript.
"""


@dataclass
class FollowupResult:
    """Результат суммаризации — содержит варианты фоллоу-апа от разных движков.

    Поле .local — от Ollama. Поле .cloud — от Claude (CLI или API, что включено).
    """

    local: Optional[str] = None
    cloud: Optional[str] = None
    local_engine: str = ""
    cloud_engine: str = ""
    local_duration_s: float = 0.0
    cloud_duration_s: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.local or self.cloud)

    @property
    def primary(self) -> Optional[str]:
        return self.cloud or self.local


class Summarizer:
    """Гибридный суммаризатор: Ollama + Claude Code CLI + Claude API.

    Любой из трёх backend'ов можно выключить. Включённые работают параллельно.
    """

    def __init__(
        self,
        # Anthropic API (классический путь, нужен sk-ant-... ключ)
        anthropic_api_key: str = "",
        anthropic_model: str = "claude-sonnet-4-5",
        use_anthropic: bool = True,
        # Claude Code CLI (использует Max-подписку через CLI авторизацию)
        use_claude_code: bool = False,
        claude_code_model: str = "opus",  # opus / sonnet / haiku — алиасы Claude Code
        claude_code_binary: str = "claude",
        # Ollama (локально)
        ollama_url: str = "http://localhost:11434",
        ollama_model: str = "vikhr-nemo:12b-instruct-q4",
        use_ollama: bool = True,
    ):
        self.anthropic_model = anthropic_model
        self.use_anthropic = use_anthropic and bool(anthropic_api_key)

        self.claude_code_model = claude_code_model
        self.claude_code_binary = claude_code_binary
        self.use_claude_code = use_claude_code

        self.ollama_url = ollama_url.rstrip("/")
        self.ollama_model = ollama_model
        self.use_ollama = use_ollama

        # ── Anthropic API ──
        self._anthropic_client = None
        if self.use_anthropic:
            try:
                from anthropic import Anthropic

                self._anthropic_client = Anthropic(api_key=anthropic_api_key)
                logger.info(f"Anthropic API готов: {anthropic_model}")
            except Exception as e:
                logger.error(f"Anthropic API не поднялся: {e}")
                self.use_anthropic = False

        # ── Claude Code CLI ──
        if self.use_claude_code:
            if shutil.which(claude_code_binary):
                logger.info(
                    f"Claude Code CLI готов: «{claude_code_binary} --model {claude_code_model}»"
                )
            else:
                logger.warning(
                    f"Claude Code CLI «{claude_code_binary}» не найден в PATH. "
                    f"Установи: https://docs.claude.com/claude-code/setup"
                )
                self.use_claude_code = False

        # ── Ollama ──
        if self.use_ollama:
            if self._ping_ollama():
                logger.info(f"Ollama готова: {ollama_url}, модель {ollama_model}")
            else:
                logger.warning(f"Ollama не отвечает по {ollama_url} — выключаю")
                self.use_ollama = False

        if not (self.use_ollama or self.use_anthropic or self.use_claude_code):
            logger.error("Все три движка выключены — суммаризация не работает")

    # ───────────────────────────── публичный API ──────────────────────────────

    def summarize(
        self, enriched_transcript: list[dict], session: Session
    ) -> FollowupResult:
        """Запустить включённые backend'ы параллельно и вернуть результат."""
        prompt = self._build_user_prompt(enriched_transcript, session)

        # Какой движок занимает «cloud» слот? Если включены оба — Claude Code приоритетнее
        # (он бесплатный через Max), Anthropic API как запасной.
        cloud_label = ""
        if self.use_claude_code:
            cloud_label = f"claude-code:{self.claude_code_model}"
        elif self.use_anthropic:
            cloud_label = f"anthropic:{self.anthropic_model}"

        result = FollowupResult(
            local_engine=f"ollama:{self.ollama_model}" if self.use_ollama else "",
            cloud_engine=cloud_label,
        )

        tasks: list[tuple[str, callable, str]] = []
        if self.use_ollama:
            tasks.append(("local", self._summarize_ollama, prompt))
        if self.use_claude_code:
            tasks.append(("cloud", self._summarize_claude_code, prompt))
        elif self.use_anthropic:
            # Anthropic API только если Claude Code не включён — чтобы избежать
            # двойной оплаты тому же провайдеру за один и тот же вопрос
            tasks.append(("cloud", self._summarize_anthropic, prompt))

        if not tasks:
            result.errors.append("ни одного движка не включено")
            return result

        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {pool.submit(fn, prm): name for name, fn, prm in tasks}
            for fut in as_completed(futures):
                name = futures[fut]
                t0 = time.time()
                try:
                    text = fut.result()
                    elapsed = time.time() - t0
                    if name == "local":
                        result.local = text
                        result.local_duration_s = elapsed
                    else:
                        result.cloud = text
                        result.cloud_duration_s = elapsed
                except Exception as e:
                    err = f"{name}: {e}"
                    logger.error(f"Суммаризация {name} упала: {e}", exc_info=True)
                    result.errors.append(err)

        return result

    # ───────────────────────────── внутренности ───────────────────────────────

    def _ping_ollama(self) -> bool:
        try:
            r = requests.get(f"{self.ollama_url}/api/tags", timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    def _build_user_prompt(
        self, enriched_transcript: list[dict], session: Session
    ) -> str:
        transcript = self._format_transcript(enriched_transcript)
        max_chars = 200_000
        if len(transcript) > max_chars:
            logger.warning(
                f"Транскрипт {len(transcript)} символов, обрезаю до {max_chars}"
            )
            transcript = transcript[:max_chars] + "\n\n[...обрезано...]"

        start = session.start_time
        end = session.end_time or datetime.now()
        return USER_PROMPT_TEMPLATE.format(
            title=session.title or "Без названия",
            date_str=start.strftime("%d.%m.%Y"),
            start_time=start.strftime("%H:%M"),
            end_time=end.strftime("%H:%M"),
            duration=session.get_duration_minutes(),
            attendees=", ".join(session.attendees) if session.attendees else "—",
            transcript=transcript,
        )

    @staticmethod
    def _format_transcript(enriched_transcript: list[dict]) -> str:
        lines = []
        for item in enriched_transcript:
            speaker = item.get("speaker", "")
            text = (item.get("text") or "").strip()
            if not text:
                continue
            source = item.get("source", "audio")
            if source == "chat":
                lines.append(f"[чат] {speaker}: {text}" if speaker else f"[чат] {text}")
            else:
                lines.append(f"{speaker}: {text}" if speaker else text)
        return "\n".join(lines)

    # ─── Anthropic API backend ────────────────────────────────────────────────

    def _summarize_anthropic(self, user_prompt: str) -> str:
        logger.info(f"Anthropic API: → {self.anthropic_model}")
        message = self._anthropic_client.messages.create(
            model=self.anthropic_model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = message.content[0].text.strip()
        logger.info(f"Anthropic API: ← {len(text)} символов")
        return text

    # ─── Claude Code CLI backend (через Max-подписку) ─────────────────────────

    def _summarize_claude_code(self, user_prompt: str) -> str:
        """Запустить `claude` CLI в headless-режиме.

        Использует существующую CLI-авторизацию (через Max-подписку юзера).
        Промпт идёт через stdin, чтобы обойти лимит длины командной строки.
        """
        logger.info(f"Claude Code CLI: → model={self.claude_code_model}")
        cmd = [
            self.claude_code_binary,
            "--print",                          # headless mode, печатает ответ и выходит
            "--model", self.claude_code_model,  # opus / sonnet / haiku
            "--append-system-prompt", SYSTEM_PROMPT,
            "--output-format", "text",          # просто текст в stdout, без JSON-обёртки
        ]

        # Окружение: убедимся что CLI не пытается лезть в TTY
        env = os.environ.copy()
        env.setdefault("CI", "1")

        try:
            proc = subprocess.run(
                cmd,
                input=user_prompt,
                capture_output=True,
                text=True,
                timeout=600,  # Opus может думать минуту-две на длинных транскриптах
                env=env,
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"claude binary не найден: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Claude Code CLI таймаут (>{e.timeout}с)") from e

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:500]
            raise RuntimeError(
                f"Claude Code CLI вернул код {proc.returncode}: {stderr}"
            )

        text = (proc.stdout or "").strip()
        if not text:
            raise RuntimeError(
                f"Claude Code CLI вернул пустой ответ. stderr: {proc.stderr[:200]}"
            )
        logger.info(f"Claude Code CLI: ← {len(text)} символов")
        return text

    # ─── Ollama backend ───────────────────────────────────────────────────────

    def _summarize_ollama(self, user_prompt: str) -> str:
        logger.info(f"Ollama: → {self.ollama_model}")
        payload = {
            "model": self.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": 0.3,
                "num_ctx": 32768,
            },
        }
        r = requests.post(
            f"{self.ollama_url}/api/chat", json=payload, timeout=600
        )
        r.raise_for_status()
        data = r.json()
        text = data.get("message", {}).get("content", "").strip()
        if not text:
            raise RuntimeError(f"Ollama вернула пустой ответ: {data}")
        logger.info(f"Ollama: ← {len(text)} символов")
        return text
