# Какую локальную LLM выбрать — гайд под MeetMind (май 2026)

Эта табличка отвечает на вопрос "какой ИИ ставить" для **твоего железа**
(MacBook Pro M5 Pro, 24GB unified memory) и **твоей задачи** (суммаризация
встреч на русском, параллельно работает Whisper large-v3).

## Память — суровая реальность

| Что | Сколько |
|---|---|
| Всего unified memory | 24 GB |
| macOS забирает себе | ~8 GB |
| Whisper large-v3 в памяти | ~3 GB |
| **Остаётся на LLM** | **~12-13 GB комфортно** |
| **После `sudo sysctl iogpu.wired_limit_mb=20480`** | до ~17 GB (но рискованно) |

⚠️ macOS по умолчанию даёт GPU только ~66% RAM (Metal `recommendedMaxWorkingSetSize`).
Чтобы поднять — `sudo sysctl iogpu.wired_limit_mb=18432` (выделит 18GB GPU из 24GB,
оставит 6GB на macOS — на грани). На каждом ребуте сбросится; для постоянства
см. секцию ниже.

## Рекомендации по моделям (май 2026)

### 🥇 Vikhr-Nemo 12B Instruct R — **дефолт MeetMind**

| Параметр | Значение |
|---|---|
| Размер Q4 | ~7.5 GB |
| Контекст | 128K токенов (буквально весь день встреч влезает) |
| База | Mistral Nemo 12B Instruct |
| Тюнинг | Русский, командой VikhrModels |
| Лицензия | Apache 2.0 |
| `OLLAMA_MODEL=` | `vikhr-nemo:12b-instruct-q4` |

**Почему:** российская команда специально тюнила Mistral Nemo на русском
корпусе (бизнес, разговорная речь, action items). Mistral Nemo базово сильна
на длинных контекстах — для часовых встреч с лёгкостью держит структуру. 7.5GB
оставляет огромный запас памяти под рост контекста и Whisper.

**Когда переключиться:** не справляется с очень сложным reasoning'ом (редко
для followup'ов).

### 🥈 Qwen3.5 14B Instruct — самый свежий мейнстрим

| Параметр | Значение |
|---|---|
| Размер Q4 | ~9 GB |
| Контекст | 32K натив, расширяется до 128K через YaRN |
| Лицензия | Apache 2.0 |
| Релиз | февраль 2026 |
| `OLLAMA_MODEL=` | `qwen3.5:14b` |

**Почему:** свежайшая итерация Qwen с 201 языком, активно поддерживается,
быстро интегрируется в Ollama/MLX/LM Studio. По reasoning чуть сильнее
Vikhr-Nemo, чуть слабее на чистом разговорном русском.

**Когда:** если хочешь самое свежее и не критичен русский колорит. Хорош если
встречи смешанные (русский + английский).

### 🥉 T-pro 2.0 32B (T-Bank) — **premium для русского**

| Параметр | Значение |
|---|---|
| Размер Q4 | ~18-20 GB |
| Контекст | 32K → 128K через RoPE |
| База | Qwen3-32B + Cyrillic-dense tokenizer + EAGLE speculative |
| Тюнинг | T-Wix 500K русских инструкций |
| Лицензия | permissive |
| `OLLAMA_MODEL=` | `t-tech/t-pro-it-2.0:Q4_K_M` |

**Почему:** русская промышленная модель из T-Bank, **Cyrillic-dense
токенизатор** означает 1 русское слово = 1.2-1.5 токена вместо 2-3 у обычных
моделей. То есть на русском получаешь больше эффективного контекста и быстрее
работу. EAGLE даёт 2× скорости. Hybrid reasoning — можно явно требовать
chain-of-thought или короткий ответ.

**Когда:** если ты готов
1. Сделать `sudo sysctl iogpu.wired_limit_mb=20480`
2. Принять, что macOS останется на пограничных 4GB
3. Возможно держать Whisper выключенным во время суммаризации

Это абсолютный peak русского качества из локальных моделей. Не дефолт только
из-за памяти. Если у тебя был бы M5 Pro 36GB или Max — однозначно T-pro.

### 🪶 Gemma 3 12B Instruct — fallback / эксперимент

| Параметр | Значение |
|---|---|
| Размер Q4 | ~7.5 GB |
| Контекст | 32K → 128K |
| Лицензия | Gemma terms (более ограничительная) |
| `OLLAMA_MODEL=` | `gemma3:12b` |

**Почему:** Google называет Gemma 3 чемпионом по reliability на длинных
контекстах (теряет менее 15% скорости от 8K до 64K). На русском чуть слабее
Vikhr и Qwen3.5.

**Когда:** если хочешь третью точку сравнения. Лицензия Gemma не самая
дружелюбная для коммерческого использования — для личного норм.

### ❌ Что НЕ стоит брать в твой случай

| Модель | Почему нет |
|---|---|
| **Qwen2.5 14B** (мой первый выбор) | Устарел, есть свежий Qwen3.5 |
| **Llama 4** Scout/Maverick | На русском заметно слабее Qwen/Vikhr |
| **DeepSeek V3.1 / V4** | Огромные MoE — не поместятся |
| **GLM-5 / Kimi K2.6** | Тоже монстры |
| **Mistral Nemo (без Vikhr)** | Vikhr-Nemo строго лучше для русского |
| **Mistral Medium 3.5** | Слабее свежего Qwen на reasoning + хуже на RU |
| **Qwen3.6 27B** | "Tight but doable" на 24GB — на практике падает |
| **Gemma 4 26B A4B** (новый MoE) | Тяжелее чем нужно, русский слабее Vikhr |

## Как поднять лимит GPU на 24GB Mac

Если выбрал T-pro 2.0 или Qwen3.5 14B и хочешь больше контекста:

```bash
# Разово (до перезагрузки)
sudo sysctl iogpu.wired_limit_mb=18432   # 18GB GPU, 6GB на macOS

# Постоянно — создать LaunchDaemon
sudo tee /Library/LaunchDaemons/com.local.iogpu.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.local.iogpu</string>
    <key>RunAtLoad</key><true/>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/sbin/sysctl</string>
        <string>-w</string>
        <string>iogpu.wired_limit_mb=18432</string>
    </array>
</dict>
</plist>
EOF
sudo launchctl load /Library/LaunchDaemons/com.local.iogpu.plist
```

## Как переключаться между моделями

В `.env`:

```env
OLLAMA_MODEL=vikhr-nemo:12b-instruct-q4   # дефолт
# OLLAMA_MODEL=qwen3.5:14b                # для свежака
# OLLAMA_MODEL=t-tech/t-pro-it-2.0:Q4_K_M # premium (после sysctl)
```

`USE_OLLAMA=true` + `USE_ANTHROPIC=true` → MeetMind будет гнать оба движка
параллельно и присылать оба фоллоу-апа. Так ты сравнишь у себя на 5-10
встречах и решишь, какая локальная лучше для **твоих** встреч.

## Скачать модели разом

```bash
ollama pull vikhr-nemo:12b-instruct-q4
ollama pull qwen3.5:14b
ollama pull gemma3:12b
# T-pro 2.0 (тяжёлый, ~18GB):
ollama pull t-tech/t-pro-it-2.0:Q4_K_M
```

Если в реестре Ollama имя другое — посмотри [ollama.com/library](https://ollama.com/library)
или соответствующие репозитории на HuggingFace (там лежат GGUF файлы для
любых моделей, импортируется в Ollama через Modelfile).

## TL;DR

- **Дефолт:** Vikhr-Nemo 12B (русский тюн, 128K context, влезает с комфортом)
- **Свежак:** Qwen3.5 14B (Feb 2026)
- **Premium:** T-pro 2.0 32B (peak русского качества, требует sysctl tweak)

Гибридный режим включён по умолчанию — оба варианта (локально + Claude)
приходят на каждую встречу, можно сравнивать. Через 5-10 встреч ты сам
поймёшь, что тебе ближе.
