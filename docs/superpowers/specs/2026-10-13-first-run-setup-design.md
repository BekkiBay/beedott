# Первичная настройка BeeDott на чистом Mac — дизайн

Дата: 2026-10-13. Статус: согласован с пользователем (схема и шаги).

## Цель

Коллега получает `BeeDott.app` (zip), открывает его и без терминала, Python,
Homebrew и репозитория доходит до рабочего состояния: запись → расшифровка
Whisper → саммари Claude → (по желанию) Telegram.

Критерий приёмки: новый пользователь macOS на этом же Mac (чистый `~`) проходит
мастер и получает фоллоу-ап по тестовой записи; ни одной команды в терминале,
кроме разрешения Gatekeeper в Системных настройках.

## Решения

- **Подпись:** пока ad-hoc, без Developer ID и нотаризации. Коллеге даём
  инструкцию «Системные настройки → Конфиденциальность и безопасность → Всё
  равно открыть». Нотаризация — позже, отдельным шагом сборки.
- **Демон на чистом Mac:** установка через `uv` при первом запуске (не
  PyInstaller, не переписывание на Swift). Демон работает тем же кодом, что в
  dev-режиме.
- **Саммари:** Anthropic API по ключу пользователя. Путь `claude -p` остаётся
  для dev-режима, в мастере его нет.
- **Доставка:** фоллоу-ап всегда в Библиотеке и в Markdown (`~/MeetMind`);
  Telegram — опциональный шаг мастера со своим ботом пользователя.

## Раскладка

```
BeeDott.app/Contents/Resources/
  backend/     daemon/**/*.py, config.py, main.py, pyproject.toml, uv.lock
  bin/uv       uv (standalone arm64-бинарь, ~47 МБ)

~/Library/Application Support/MeetMind/       (app_dir(), уже существует)
  runtime/python/     Python 3.11 от uv           UV_PYTHON_INSTALL_DIR
  runtime/venv/       пакеты                      UV_PROJECT_ENVIRONMENT
  runtime/uv-cache/   кэш uv                      UV_CACHE_DIR
  runtime/pycache/    байткод                     PYTHONPYCACHEPREFIX
  runtime/bin/uv      копия uv из бандла, без карантина
  runtime/bin/ffmpeg  симлинк на ffmpeg из imageio-ffmpeg
  runtime/lock.sha    sha256 uv.lock, с которым ставили
  config.env          секреты (0600), уже есть
```

Модель Whisper — в стандартном кэше Hugging Face (`~/.cache/huggingface`), как
сейчас: dev-режим и встроенный режим делят одну копию.

## Два режима запуска демона

`RuntimeManager` (Swift) выбирает режим:

1. **dev** — найден репозиторий (UserDefaults `repoPath` → env `MEETMIND_REPO` →
   рядом со сборкой, как сейчас) и `uv` в системе. Команда как сейчас:
   `uv run python main.py`, cwd = репо.
2. **bundled** — иначе. Команда: `runtime/venv/bin/python
   <Resources/backend>/main.py`, cwd = `runtime/` (config.py делает
   `mkdir("sessions")` относительно cwd — внутри `.app` писать нельзя). env:
   `PYTHONPYCACHEPREFIX`, `PATH` = `runtime/bin:` + системный.

Демон дополнительно добавляет `app_dir()/runtime/bin` в начало PATH
(`_ensure_tool_path`), чтобы mlx-whisper и reconcile находили ffmpeg.

Запуск `daemon.setup` (`python -m` кладёт в sys.path cwd, а не каталог скрипта):

- **dev:** `uv run python -m daemon.setup <команда>`, cwd = репо.
- **bundled:** `runtime/venv/bin/python -m daemon.setup <команда>`, cwd =
  `runtime/`, env `PYTHONPATH=<Resources/backend>`, `PYTHONPYCACHEPREFIX`,
  `PATH` как у демона. Так `import config` делает `mkdir("sessions")` в
  `runtime/`, а не внутри `.app`.

## Установка рантайма (bundled)

`uv sync --frozen --no-dev --python 3.11 --project <Resources/backend>` с env
`UV_PROJECT_ENVIRONMENT`, `UV_PYTHON_INSTALL_DIR`, `UV_CACHE_DIR`,
`UV_PYTHON_PREFERENCE=only-managed`. После успеха — `python -m daemon.setup
link-ffmpeg`, затем запись `lock.sha`.

- **Карантин.** Скачанный zip помечает всё содержимое `com.apple.quarantine`.
  Бандл не трогаем: перед первым `uv sync` приложение копирует
  `Resources/bin/uv` в `runtime/bin/uv` и снимает атрибут с копии
  (`removexattr`); копирует заново, если копия отличается от бандловой (после
  обновления приложения). `.py`-файлы из бандла только читаются, карантин им не
  мешает; Python и пакеты качает uv — они без карантина. Проверяется на чистом
  аккаунте.
- **App Translocation.** Если приложение запущено прямо из Загрузок, macOS
  запускает его из случайного read-only пути (`/AppTranslocation/` в
  `Bundle.main.bundlePath`); автозапуск указывал бы на временный путь. В этом
  случае вместо мастера — окно «Перенеси BeeDott в папку Программы и открой
  снова» с кнопкой «Открыть Загрузки» (не «Показать в Finder»: `bundlePath`
  указывает на read-only копию) и выходом; рантайм не ставится.
- **Обновление.** На старте, если `sha256(Resources/backend/uv.lock) !=
  runtime/lock.sha`, супервизор до запуска демона выполняет тот же `uv sync`
  (быстро, кэш на месте); в меню — «Обновляю компоненты…». Ошибка — в меню
  «Компоненты не обновились» + лог, демон не стартует.
- **ffmpeg.** Новая зависимость `imageio-ffmpeg` (есть колесо
  `macosx_11_0_arm64`, ~20 МБ, статический ffmpeg). `link-ffmpeg` делает
  `runtime/bin/ffmpeg → imageio_ffmpeg.get_ffmpeg_exe()`.

## Python: `daemon/setup.py`

CLI `python -m daemon.setup <команда>`. Вывод — JSON-строки в stdout, по одной
на событие:

```
{"event": "progress", "stage": "model", "done": 671088640, "total": 1610612736}
{"event": "info", "message": "..."}
{"event": "ok", ...поля результата}
{"event": "error", "code": "no_network", "message": "Нет связи с huggingface.co"}
```

Код выхода 0 при `ok`, 1 при `error`. Секреты (ключ, токен) — только через
stdin, никогда в argv (видно в `ps`).

| Команда | Что делает | `ok` |
|---|---|---|
| `status` | модель скачана (офлайн: `snapshot_download(local_files_only=True)`, сеть не трогает)? способ саммари настроен? Telegram настроен? | `{"model": bool, "summary": "api"\|"claude_cli"\|null, "telegram": bool}` |
| `install-model` | качает `transcriber.MODEL_REPO` пофайлово через `hf_hub_download(tqdm_class=…)`; total — сумма размеров из `HfApi.model_info(files_metadata=True)`, done — сумма уже скачанных файлов + `n` текущего бара; событие не чаще раза в 0,5 с. Класс бара — подкласс обычного `tqdm.tqdm`
(не `huggingface_hub.utils.tqdm`: тот без терминала ставит `disable=None`, и `n`
не растёт); stderr в JSON-поток не попадает (xet рисует свой бар туда). Перед стартом удаляет осиротевшие `*.incomplete` в `blobs/` этого репо. Докачки нет (huggingface_hub 1.23 с hf-xet начинает файл заново): прерванная загрузка начинается с нуля, уже скачанные файлы не перекачиваются | `{"model": repo}` |
| `link-ffmpeg` | симлинк `runtime/bin/ffmpeg` | `{"ffmpeg": path}` |
| `check-key` | stdin: ключ. `anthropic.Anthropic(api_key=…).models.list(limit=1)`; успех → `update_config_env` для `ANTHROPIC_API_KEY` и `SUMMARY_BACKEND=api` | `{}` |
| `telegram-check` | stdin: токен. `getMe` | `{"bot": "username"}` |
| `telegram-connect` | stdin: токен. Цикл `getUpdates(timeout=50)` до общего лимита 120 с, ждёт сообщение `/start`, берёт `chat.id`, подтверждает `getUpdates(offset=last+1)`, шлёт «BeeDott подключён ✓», пишет `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. 409 (у бота webhook или другой поллер) → `bot_busy` | `{"chat_id": int}` |

`summary` в `status`: `"api"`, если `SUMMARY_BACKEND=api` и ключ непустой;
`"claude_cli"`, если `SUMMARY_BACKEND` = `claude_cli` или не задан (дефолт кода)
и `claude_auth.claude_bin()` находит бинарь (PATH у процесса из `.app`
минимальный — простого поиска в PATH мало); иначе `null`. Конфиг читается заново из `config.env` (не из уже импортированного
`config`).

Коды ошибок: `no_network`, `disk_full`, `invalid_key`, `invalid_token`,
`bot_busy`, `timeout` (не дождались /start), `internal` (всё прочее, сообщение — последняя
строка исключения). Тексты для человека — на стороне Swift по коду.

## Swift

- **`RuntimeManager.swift`** — режим (dev/bundled), пути, `needsSync`
  (сравнение хэша), `sync(progress:)` (Process с `uv`), `runSetup(command,
  stdin:, onEvent:)` (Process с python, построчный разбор JSON), снятие
  карантина, `daemonLaunch` (исполняемый файл, аргументы, cwd, env). Чистые
  части — разбор JSON-строки события, сравнение хэшей, выбор режима — без
  AppKit, покрываются `app/Tests`.
- **`DaemonSupervisor`** — команду и cwd берёт у `RuntimeManager`. Пока рантайм
  не установлен — не спавнит, состояние `.notInstalled`. Новое `restart()` для
  применения настроек; если демон запущен снаружи (`weSpawned == false`),
  перезапустить его нельзя — мастер пишет «Перезапусти демон, чтобы применить».
  Из `.notInstalled` супервизор выходит по вызову `runtimeReady()`: его делает
  мастер после успешного `uv sync` + `link-ffmpeg` (до скачивания модели —
  демону модель нужна только к первой расшифровке).
- **Дочерние процессы.** macOS не убивает детей вместе с родителем: запущенные
  `uv`/`python -m daemon.setup` приложение хранит и завершает в
  `applicationWillTerminate`.
- **`SetupWizard.swift`** — `SetupModel` (шаги, состояние каждого шага) +
  SwiftUI-вью + `SetupWindow.show()` по образцу `LibraryWindow`. Стиль — из
  `design_handoff_meetmind/README.md` §3 и `Theme.swift`.
- **`AppModel.start()`** — после старта супервизора спрашивает `status`; мастер
  открывается сам, если нет рантайма или модели, либо если
  `UserDefaults.setupCompleted == false` и саммари не настроено. Если всё уже
  есть (твой Mac) — `setupCompleted = true` молча, мастер не показывается.
- **Дропдаун** — пункт «Настройка…» в футере открывает мастер. Шаги, которые
  уже готовы по `status`, отмечены выполненными: шаг 1 (рантайм + модель), шаг 2
  при `summary != null` — показывает текущий способ («Anthropic API» или
  «claude CLI (Max)») и кнопку «Заменить ключ»; без неё `check-key` не
  вызывается и `SUMMARY_BACKEND` не меняется.
- **Применение настроек.** После сохранения ключа или Telegram демон
  перезапускается, чтобы перечитать `config.env`. Если идёт запись или
  обработка — перезапуск откладывается до простоя, в мастере: «Применится после
  текущей встречи».

## Шаги мастера

Окно ~520×460, «Шаг N из 4», точки прогресса, кнопки справа.

1. **Распознавание речи.** Что скачается (~1 ГБ пакетов + модель 1,5 ГБ),
   «всё работает на этом Mac, аудио никуда не уходит». Проверка свободного
   места ≥ 4 ГБ. «Установить» → строка «Python и пакеты» (спиннер + текущая
   строка uv) → строка «Модель Whisper — 640 из 1 500 МБ» (полоса). Ошибка →
   текст по коду + «Повторить». «Далее» — только когда всё готово. Закрыли окно
   или вышли — при следующем запуске мастер продолжит с того, что не готово
   (прерванный файл модели качается заново).
2. **Саммари.** Поле `sk-ant-…` (SecureField), ссылка «Где взять ключ» →
   console.anthropic.com/settings/keys. «Проверить» → `check-key` → «Ключ
   работает» / «Ключ не подходит» / «Нет связи». Строка: «В Anthropic уходит
   только текст расшифровки, аудио — никогда». Пропустить нельзя: без ключа
   стадия саммари падает в `failed_retry` (дефолт `claude_cli`, на чистом Mac
   `claude` нет). «Далее» — после «Ключ работает».
3. **Telegram (по желанию).** Инструкция «@BotFather → /newbot → скопируй
   токен», поле токена → `telegram-check` → «@имя_бота». «Открыть бота в
   Telegram» (`tg://resolve?domain=…`, фолбэк `https://t.me/…`) + «Отправь ему
   /start» → `telegram-connect` (спиннер «Жду /start…») → «Бот прислал тестовое
   сообщение». «Пропустить» / «Далее».
4. **Готово.** Что настроено; где фоллоу-апы (меню → Библиотека, Telegram,
   `~/MeetMind`); «При первой записи macOS спросит доступ к микрофону и звуку —
   нажми Разрешить». «Начать» → `setupCompleted = true`, окно закрывается.

## Сборка и раздача

- `app/build.sh` копирует backend-файлы в `Contents/Resources/backend` (без
  `__pycache__`, тестов, `.env`) и `uv` в `Contents/Resources/bin/uv`.
  Источник `uv` — standalone-бинарь (`~/.local/bin/uv` или переменная `UV_BIN`);
  сборка падает, если `uv` не arm64 или линкуется с чем-то кроме системных
  библиотек. `Resources/bin/uv` подписывается ad-hoc до подписи бандла.
- `pyproject.toml`: прямые зависимости `imageio-ffmpeg` и `huggingface_hub`
  (setup.py импортирует их сам; сейчас huggingface_hub приходит только через
  mlx-whisper). ffmpeg из imageio-ffmpeg проверяется тестом на то, что
  использует код: `-ignore_length`, фильтр `channelsplit`, кодек `flac`.
- `app/package.sh` — сборка + `ditto -c -k --keepParent` → `app/build/BeeDott.zip`.
- `docs/INSTALL.md` — инструкция для коллеги: распаковать, перенести в
  Программы, открыть, разрешить в Настройках, пройти мастер.

## Ошибки

| Ситуация | Поведение |
|---|---|
| Нет интернета на шаге 1 | `no_network` → «Нет интернета» + «Повторить» |
| Мало места | проверка до старта; `disk_full` во время → «Не хватает места: нужно ещё N ГБ» |
| `uv sync` упал | последние строки вывода uv в мастере (свёрнуто) + «Повторить»; полный лог в `~/Library/Logs/MeetMind/setup.log` |
| Выход посреди установки | процессы uv/python убиваются вместе с приложением; повтор идемпотентен |
| Неверный ключ / токен | inline-ошибка под полем, остальное не трогаем |
| Не дождались /start | `timeout` → «Бот не получил /start» + «Ещё раз» |
| У бота webhook / другой поллер | `bot_busy` → «Этот бот уже подключён к другому сервису — создай нового» |
| Запуск из Загрузок (App Translocation) | окно «Перенеси в Программы», рантайм не ставится |
| Настройки изменены во время записи | перезапуск демона отложен до простоя |

## Тесты

- **pytest** `tests/test_setup.py`: `status` (комбинации config.env; мок
  `claude_auth.claude_bin()`, а не PATH; модель — офлайн-проверка на фейковом
  кэше); `check-key` (мок anthropic: успех пишет оба ключа с правами 0600,
  401 → `invalid_key`, сетевая ошибка → `no_network`); `telegram-*` (мок
  requests: getMe, getUpdates с /start и подтверждением offset, sendMessage,
  409 → `bot_busy`, таймаут); `install-model` (мок `model_info` и
  `hf_hub_download`, который двигает переданный `tqdm_class`: done/total,
  троттлинг 0,5 с, удаление осиротевших `*.incomplete` до старта);
  `link-ffmpeg`; секреты не попадают в argv и вывод.
- **pytest -m slow**: ffmpeg из imageio-ffmpeg умеет `-ignore_length`,
  `channelsplit`, `flac`.
- **app/Tests**: разбор JSON-событий, сравнение хэша, выбор режима
  (dev/bundled) по набору путей, распознавание App Translocation по пути.
- **Ручная проверка встроенного режима без GUI:** собрать `.app`, выполнить тот
  же `uv sync` во временный `runtime/` из `Resources/backend`, прогнать
  `setup status`, `install-model` (модель уже в кэше — мгновенно), запустить
  демон с `MEETMIND_APPDIR` во временной папке и проверить `health`.
- **Приёмка:** новый пользователь macOS на этом Mac, zip через Загрузки
  (карантин настоящий), мастер целиком, тестовая запись → фоллоу-ап. Делает
  пользователь (нужен пароль администратора для создания аккаунта).

## Вне рамок

Developer ID и нотаризация; автообновление приложения; выбор модели Whisper;
`claude -p` в мастере; разрешения микрофона/звука/календаря в мастере (macOS
спросит при первой записи); раздача через GitHub Releases (репо приватное).
