# BeeDott в Claude через MCP — дизайн

Дата: 2026-10-14, ревизия 2026-10-15. Статус: согласован с пользователем.
Первая редакция (14.10) — только чтение, подключение вариантом A (BeeDott сам
прописывает себя в конфиг Claude Desktop). Ревизия 15.10 расширила объём:
Claude не только читает встречи, но и правит их и управляет записью.

## Цель

Любой пользователь BeeDott (не только автор) в мастере первичной настройки
одной кнопкой подключает свои встречи к Claude Desktop и Claude Code. Дальше в
чате Claude можно:

- спросить: «что решили на последней встрече?», «какие задачи на мне за
  неделю?», «где обсуждали минимальную корзину?»;
- поправить: «в задаче про отчёт исполнитель — Стас, а не Спикер 2, и
  перешли в Telegram», «переименуй вчерашнюю встречу», «пересобери саммари»;
- управлять: «начни запись „Синк по реакциям“», «останови запись»,
  «импортируй ~/Downloads/звонок.m4a».

Критерий приёмки: на Mac с BeeDott и Claude Desktop шаг мастера «Claude»
подключает сервер без терминала и ручной правки файлов; после перезапуска
Claude Desktop вопрос про последнюю встречу получает ответ с данными из
BeeDott, правка задачи меняет фоллоу-ап в Библиотеке, «начни запись»
запускает нативную запись приложения; прочие MCP-серверы и настройки Claude
Desktop не тронуты.

## Решения

- **Где работает.** Только локально: Claude запускает MCP-сервер как
  дочерний процесс (stdio), сервер ходит в демон BeeDott через существующий
  Unix-сокет `control.sock`. Сети и портов нет. claude.ai в браузере и на
  телефоне — вне рамок (нужен публичный сервер).
- **Архитектура — тонкий клиент сокета.** Все чтения и все изменения идут
  через методы демона: один writer на таблицу (SPEC_v3 §3), блокировки демона
  (`busy` во время записи и обработки) действуют и для Claude. В БД и файлы
  сессий MCP-сервер напрямую не лезет. Без запущенного BeeDott инструменты
  отвечают понятной ошибкой.
- **Объём (ревизия 15.10): чтение + правка + управление.** Отмена записи
  (`cancel_recording`) наружу не отдаётся: она стирает аудио, а стопа
  достаточно.
- **Запись запускает и останавливает приложение, не демон.** Нативная запись
  (Process Tap + микрофон, права TCC) живёт в Swift. Демон по запросу Claude
  только поднимает флаг в `status`; приложение, которое и так опрашивает
  `status` раз в 3 с, запускает или останавливает свой движок. Для стопа это
  уже существующий механизм `stop_requested` (предохранители T7).
- **Подключение — вариант A.** BeeDott сам пишет ключ `mcpServers.beedott` в
  `~/Library/Application Support/Claude/claude_desktop_config.json` и
  перезапускает Claude Desktop с подтверждения. Файл-расширение `.mcpb` и
  удалённый коннектор отклонены.
- **Claude Code — по возможности.** Если найден `claude` CLI, сервер
  регистрируется и там (`claude mcp add-json --scope user`). Это пишет в
  стандартный конфиг Claude Code; при нестандартном `CLAUDE_CONFIG_DIR`
  (у автора — `~/.claude-acc2`) приложение из Finder этой переменной не видит,
  поэтому мастер показывает готовую команду для терминала с кнопкой
  «Скопировать».
- **SDK.** Официальный Python-пакет `mcp`, зависимость `mcp>=2.3,<3`. API
  2.x: `MCPServer` из `mcp.server.mcpserver` (FastMCP из 1.x там переименован),
  `@server.tool(annotations=ToolAnnotations(...))`, `server.run("stdio")`;
  клиент для тестов — `mcp.client.stdio.stdio_client` + `ClientSession`. У
  коллег рантайм обновится сам при запуске новой версии (сравнение хэша
  `uv.lock` уже есть). Если при реализации API 2.3 окажется другим —
  сверить по исходникам установленного пакета, решения спека от этого не
  меняются.

## Python: MCP-сервер `daemon/mcp_server.py`

Запуск: `python -m daemon.mcp_server`. Ходит в демон через
`daemon.control.request()`.

**Импорты.** Модуль импортирует только `daemon.control` (и через него
`daemon.paths`), не `config` и не `daemon.db`: Claude Desktop запускает
процесс с произвольным cwd (часто `/`), а `import config` делает
`mkdir("sessions")` относительно cwd. Это закрепляется тестом. stdout
процесса — канал протокола MCP, поэтому логирование (если есть) — только в
stderr.

**Аннотации.** В MCP по умолчанию `destructive_hint=True` для любого
инструмента, который не `read_only`, поэтому аннотации ставятся явно:

- чтение — `read_only_hint=True`;
- правка и управление — `read_only_hint=False, destructive_hint=False`;
- `delete_meeting` — `read_only_hint=False, destructive_hint=True`.

Подтверждение перед вызовом показывает сам клиент по своим правилам
(Claude Desktop и Claude Code по умолчанию спрашивают разрешение на
инструменты MCP); аннотации — подсказка клиенту, не замок. Замок для удаления
— на стороне BeeDott (см. `delete_meeting`).

Все ответы — Markdown-текст. `limit`, `days` и `from_minute` вне диапазона
прижимаются к границе, не ошибка.

### Чтение

| Инструмент | Параметры | Что возвращает |
|---|---|---|
| `list_meetings` | `limit: int = 20` (1…100) | Последние встречи, новые сверху: id, название, дата и время (локальное), длительность, статус («готово» / «обрабатывается» / «ошибка»), число задач |
| `search_meetings` | `query: str`, `limit: int = 10` (1…50) | Встречи с совпадениями в саммари или транскрипте, сгруппированные по встрече: id, название, дата, где нашлось, фрагмент. Даты — из одного вызова `sessions_list(limit=10000)`; встречи нет в списке — «дата неизвестна» |
| `get_meeting` | `meeting_id: str`, `include_transcript: bool = False`, `from_minute: int = 0` | Название, дата, длительность, участники (если есть), есть ли аудио и закреплено ли оно (`has_audio`, `keep_audio`), ревизия фоллоу-апа; фоллоу-ап: контекст, что обсуждали, решения, задачи (кто / что / срок), открытые вопросы. С флагом — транскрипт с минуты `from_minute` строками `[мм:сс] Спикер N: текст` (у старых моно-записей без `speaker` — `[мм:сс] текст`) |
| `action_items` | `days: int = 7` (1…90) | Задачи из встреч за последние N дней: задача, ответственный, срок, встреча (название, дата, id). Встречи без фоллоу-апа пропускаются. Список — `sessions_list(limit=10000)` (дефолт демона 200 обрезал бы окно) |
| `recording_status` | — | Идёт ли запись: название, с какого времени, сколько длится; сколько стадий в работе — сумма `n` строк `status.queue` со статусом `pending` / `queued` / `running` (как `RuntimeLogic.activeJobs`); сколько встреч с ошибкой — `status.sessions["FAILED"]` (подробности — `list_meetings`). `status.last_error` не используется: он не сбрасывается после успеха и показывал бы старые ошибки |

Детали чтения:

- Данные: `sessions_list`, `session_detail`, `search`, `status` —
  существующие методы демона (`daemon/app.py`). `action_items` — один
  `sessions_list` и `session_detail` только для встреч в окне, у которых
  счётчик `action_items` из списка больше нуля (`session_detail` каждый раз
  тащит весь транскрипт).
- Даты в сессиях — ISO UTC; в ответах — локальное время `ДД.ММ.ГГГГ ЧЧ:ММ`.
- Транскрипт с `from_minute` длиннее 40 000 символов (с префиксами строк)
  обрезается по границе строки с пометкой в конце: «показано с M1 по M2
  минуту из M; продолжение — `get_meeting(meeting_id, include_transcript=true,
  from_minute=M2)`». Предел — под лимит вывода MCP в Claude Code (~25 тыс.
  токенов на весь ответ, кириллица плотнее латиницы). Сверено 15.10 на
  реальной 81-минутной встрече: 29 000 символов одной страницей. Так Claude
  дочитывает длинную встречу страницами, а не теряет хвост.
- Встреча без фоллоу-апа (ещё обрабатывается или упала) в `get_meeting` —
  статус и, если есть, транскрипт по флагу.
- Форматирование — чистые функции (`format_meeting_list`, `format_meeting`,
  `format_transcript`, `format_search`, `format_action_items`,
  `format_status`), тестируются без сокета.

### Правка

| Инструмент | Параметры | Метод демона | Что делает |
|---|---|---|---|
| `rename_meeting` | `meeting_id`, `title: str` | `rename_session` (есть) | Переименовать. Поиск видит новое имя; отправленные Telegram и заметки не меняются |
| `resummarize_meeting` | `meeting_id` | `resummarize` (есть) | Заново собрать фоллоу-ап через Claude из того же транскрипта, как кнопка в карточке; новая версия уйдёт в Telegram ответом на старую |
| `update_followup` | `meeting_id`, `followup: Followup`, `resend: bool = False` | `update_followup` (новый) | Заменить фоллоу-ап целиком исправленной версией (см. ниже) |
| `set_keep_audio` | `meeting_id`, `keep: bool` | `set_keep_audio` (есть) | Хранить аудио — retention его не удалит |
| `delete_meeting` | `meeting_id`, `title: str` | `delete_session` (+ проверка названия) | Удалить встречу: аудио, транскрипт, саммари, поиск. Заметки в `~/MeetMind` и сообщения в Telegram остаются |

`Followup` — `TypedDict` с полями схемы SPEC_v3 §7 (`context: str`,
`discussed`, `decisions`, `open_questions: list[str]`, `action_items:
list[ActionItem]`, где `ActionItem` — `task`, `owner: str`, `deadline: str |
None`). SDK превращает его в JSON Schema инструмента — Claude видит
структуру. Окончательную проверку делает демон той же
`summarizer.validate_followup`, что проверяет ответ модели.

`delete_meeting` требует `title` — точное отображаемое название встречи
(Claude берёт его из `get_meeting` / `list_meetings`). Демон сверяет его с
`db.session_title(row)` (см. «Название встречи»); не совпало — `bad_params`
«название не совпадает: встреча <id> называется «…»», ничего не удалено.
Это страховка от удаления не той встречи по перепутанному id. В приложении
удаление идёт без `title`, как сейчас.

### Управление

| Инструмент | Параметры | Что делает |
|---|---|---|
| `start_recording` | `title: str \| None = None` | Попросить BeeDott начать запись (см. «Старт и стоп записи»). Ответ — после того как запись реально началась: id, название |
| `stop_recording` | — | Остановить текущую запись; ответ — после того как запись закрыта и ушла в обработку |
| `import_audio` | `path: str`, `title: str \| None = None` | Импортировать аудиофайл с этого Mac (`import_audio`, есть) — встреча появится в Библиотеке и пройдёт обычную обработку |

**`instructions` сервера** коротко объясняют Claude: это встречи
пользователя, записанные BeeDott; `meeting_id` берётся из `list_meetings` /
`search_meetings`; перед `update_followup` прочитай встречу через
`get_meeting` и пришли фоллоу-ап целиком, изменив только то, о чём просил
пользователь; `resend` — только если пользователь просил отправить в
Telegram; удаляй только по явной просьбе пользователя.

### Ошибки

Сейчас `control.request()` на любую беду бросает голый `RuntimeError`, а
тайм-аут и прочие `OSError` пропускает как есть. В `daemon/control.py`
появляются два подкласса `RuntimeError` (тексты сообщений прежние —
`daemon/cli.py` пропускает их наружу как есть, `tests/test_control.py` ждёт
`RuntimeError` и сверяет текст):

- `DaemonUnavailable` — нет сокета, отказ соединения, тайм-аут, любой
  `OSError` во время обмена (в том числе `BrokenPipeError` на `sendall`),
  демон закрыл соединение без ответа;
- `DaemonError` с полем `code` — демон ответил ошибкой.

MCP-сервер отображает: `DaemonUnavailable` → «BeeDott не запущен или не
отвечает — открой приложение BeeDott и повтори»; `DaemonError` с `code ==
"not_found"` → «Встреча <id> не найдена»; `busy` → «BeeDott: <сообщение>»
(текст демона уже человеческий: «идёт запись», «встреча сейчас
обрабатывается»); прочие `DaemonError` → «BeeDott: <сообщение>». Любое
другое исключение (например, `JSONDecodeError` на битом кадре) инструмент
тоже ловит и возвращает текстом «BeeDott: ошибка — <сообщение>»: `MCPServer`
сам заменяет непойманное исключение на безликое «Error executing tool» и
теряет текст. Трейсбэков в ответах нет. Ошибка возвращается как результат
инструмента с признаком ошибки (`is_error`), процесс MCP не падает.

## Python: демон

### Правка фоллоу-апа — модуль `daemon/followup_edit.py`

Логика в отдельном модуле (`daemon/app.py` и так 656 строк); хендлер
`update_followup` в `app.py` — тонкий: параметры, `self._lock`, вызов
`followup_edit.apply(con, sid, followup, resend=...)`, перевод исключений в
`ControlError`.

`apply`:

1. Сессии нет или она `CANCELLED` → `not_found`.
2. Состояние: правка разрешена только в `DELIVERED` или `FAILED` и только
   если есть `followup.json`. Иначе: `DETECTED` / `RECORDING` → `busy` «идёт
   запись этой встречи»; прочие состояния или job этой сессии в `running` →
   `busy` «встреча сейчас обрабатывается — попробуй, когда закончится»;
   `FAILED` без фоллоу-апа → `no_followup`: если `transcript.txt` есть —
   «фоллоу-апа ещё нет — сначала пересобери саммари», если нет — «встреча не
   обработалась — повтори обработку в BeeDott».
3. `summarizer.normalize_followup` + `validate_followup`; ошибка →
   `bad_params` с текстом валидатора, файлы не тронуты.
4. Новая версия: поля из запроса + `engine` из прежнего `followup.json`
   (как собрана исходная версия) + `revision` = прежняя + 1 (та же логика,
   что в `stages.summarize`).
5. `followup.json` и `followup.md` (`render.render_followup`, как в
   `stages.summarize`) пишутся атомарно: `.part` + `replace`.
6. `db.index_note` — поиск видит новую версию. Второй писатель `notes_fts`
   кроме стадии summarize (уже есть прецедент — `rename_session`); пишет
   демон под своим локом, гонки со стадией нет: шаг 2 не пускает правку во
   время обработки.
7. Доставки: `note` и `obsidian` → `pending` (заметка запишется заново
   новой версией; NoteSink называет файл `HHMM-<slug>.md`, поэтому после
   переименования встречи появится новый файл рядом со старым — как и при
   пересборке саммари). При `resend=true` ещё и `telegram`: перед сбросом — та же
   подготовка, что в `resummarize` (первое отправленное сообщение →
   `telegram_reply_to`, `telegram_progress.json` удаляется). Эта подготовка
   выносится из `DaemonApp.resummarize` в общую функцию, оба места зовут её.
   Без `resend` строка `telegram` не трогается: если фоллоу-ап уже был
   доставлен — повторно не уйдёт; если доставка так и не удалась (`FAILED`
   на deliver) — уйдёт исправленная версия. То же после упавшей пересборки
   саммари: `resummarize` уже сбросил все доставки, включая `telegram`, в
   `pending` — новая версия всё равно предназначалась Telegram, и правка уйдёт
   туда ответом на прежнее сообщение.
8. Новая функция `db.redeliver(con, sid)` по образцу `db.resummarize`, одной
   транзакцией (один `commit` в конце): состояние → `SUMMARIZED`; job
   `deliver` → `status='queued', attempts=0, next_retry_at=NULL,
   last_error=NULL` (строки нет — вставить). `db.enqueue_job` не подходит: он
   не сбрасывает `attempts`, и после `FAILED` на deliver (`attempts=8`) первая
   же неудача новой доставки стала бы окончательной. Если job `summarize` в
   `failed` (упала пересборка саммари, а прежний `followup.json` остался) —
   он помечается `done` с `last_error=NULL`: фоллоу-ап теперь есть, и карточка
   встречи не должна показывать шаг «саммари» упавшим. Переход состояния
   проверяется `assert_transition` до записи. Оркестратор прогоняет deliver:
   `SUMMARIZED → DELIVERING → DELIVERED`. В `daemon/states.py` добавляется
   переход `DELIVERED → SUMMARIZED` (сейчас из `DELIVERED` разрешён только
   `TRANSCRIBED`); `FAILED → SUMMARIZED` уже есть.
9. Ответ: `{"session_id", "revision", "resend"}`.

### Старт и стоп записи

Новые поля и методы `DaemonApp`:

- `_start_request: dict | None` — `{"title": str | None, "expires": monotonic}`.
- `request_start({"title"})`: идёт запись → `busy`; иначе ставит флаг со
  сроком 8 с и отвечает `{"requested": true}`. Повторный запрос перезаписывает
  флаг.
- `status` получает поле `start_requested`: `{"title": …}` или `null`;
  протухший флаг при чтении сбрасывается и отдаётся `null`.
- `start_recording` (любой owner) сбрасывает `_start_request` в самом начале,
  до любых проверок: неудачный старт не повторяется на следующем опросе.
- `request_stop({})`: записи нет → `no_active_session`; `owner == "daemon"`
  → обычный `stop_recording`; `owner == "app"` → `_stop_request = "mcp"`,
  если он ещё не стоит (предохранитель мог поставить свой).

MCP `start_recording`: `request_start`, затем опрос `status` каждые 0,5 с до
12 с, пока не появится `recording`. Срок флага (8 с) короче ожидания (12 с):
если приложение прочитало флаг в последний момент, запись успеет начаться
до того, как MCP сдастся, и ответ обычно не соврёт. Исключение — медленный
старт (например, первый запрос прав TCC), поэтому текст тайм-аута
осторожный: «BeeDott не начал запись за 12 с — приложение не запущено или
занято; проверь `recording_status`». Ответ при успехе — id и название из
`status`.

MCP `stop_recording`: запоминает id активной записи из `status`,
`request_stop`, опрос `status` до 12 с, пока `recording` не станет другим.
Затем `session_detail(id)`: состояние `CANCELLED` → «запись была пустой или
слишком короткой — BeeDott её отменил»; иначе — «запись остановлена, встреча
ушла в обработку». Не дождались → «запись ещё останавливается — проверь
`recording_status`».

### Название встречи

У встреч без события в Календаре `sessions.title` — `NULL`, и сейчас у них
три разных имени: «Без названия» в Библиотеке, «Встреча ЧЧ:ММ» в Telegram,
заметке и поиске (`stages._title`), `null` в `sessions_list`. Для Claude
нужно одно имя:

- `stages._title` переезжает в `daemon/db.py` как `session_title(row)`
  (stages зовёт её оттуда, поведение то же: название или «Встреча ЧЧ:ММ» по
  локальному времени начала); `tests/test_localtime.py` зовёт
  `stages._title` напрямую — тест переводится на `db.session_title`;
- `sessions_list` и `session_detail` добавляют поле `display_title` =
  `session_title(row)`; поле `title` остаётся как есть (Swift на него
  опирается);
- `status` добавляет `recording_display_title` = `session_title(rec)` для
  активной записи (`recording_title` остаётся сырым);
- MCP-сервер везде показывает `display_title` / `recording_display_title`
  (в том числе `recording_status` и ответ `start_recording`);
- `delete_session` сверяет переданный `title` с `session_title(row)`.

### Прочее

- `delete_session` принимает необязательный `title`: передан и не совпадает
  с `session_title(row)` → `bad_params`, ничего не удалено.
- `session_detail` добавляет `has_audio` — так же, как `sessions_list`
  (`audio_path` задан и файл существует).
- `daemon/cli.py` не меняется.

## Python: подключение в `daemon/setup.py`

Четыре новые команды и одно новое поле в `status`. Протокол прежний:
JSON-строки в stdout, `ok` / `error`, код выхода 0 / 1. Claude Desktop и
Claude Code — разные команды: конфиг Claude Desktop пишется за доли секунды,
и Claude можно открыть снова, не дожидаясь медленного `claude` CLI.

**Команда запуска сервера** считается из процесса, в котором выполняется
`daemon.setup`:

- `command` — `sys.executable` (у коллег `runtime/venv/bin/python`, у автора —
  `.venv/bin/python` репозитория);
- `args` — `["-P", "-m", "daemon.mcp_server"]` (`-P`: Claude Code запускает
  сервер в папке проекта, и без него чужой пакет `daemon/` или `mcp/` в этой
  папке перекрыл бы код BeeDott);
- `env` — `PYTHONPATH` = каталог, где лежит пакет `daemon`
  (`Path(daemon.__file__).resolve().parent.parent`: у коллег
  `BeeDott.app/Contents/Resources/backend`, у автора — репозиторий); плюс
  `PYTHONPYCACHEPREFIX` и `MEETMIND_APPDIR`, если они заданы в окружении
  процесса (`PYTHONPYCACHEPREFIX` ставит приложение в bundled-режиме,
  `MEETMIND_APPDIR` — тесты и ручной override; в `config.env` их нет).
  `PYTHONPYCACHEPREFIX` обязателен у коллег: иначе Python напишет
  `__pycache__` внутрь `.app` и сломает подпись.

`status` и команды подключения считают эту команду одной функцией, поэтому
всегда сравнивают одно и то же.

**Путь к конфигу:** `~/Library/Application Support/Claude/claude_desktop_config.json`,
переопределяется env `MEETMIND_CLAUDE_DESKTOP_CONFIG` (тесты). Чтение: пустой
файл (0 байт) — как `{}`; не JSON-объект или `mcpServers` не объект —
`bad_config`. Запись: копия в `claude_desktop_config.json.beedott.bak`, затем
временный файл в том же каталоге + `os.replace`; если конфиг — симлинк,
пишем в файл, на который он указывает (`Path.resolve()`), симлинк остаётся.

| Команда | Что делает | `ok` |
|---|---|---|
| `claude-connect` | Пишет `mcpServers.beedott` = команда запуска. Остальные ключи файла (`preferences`, чужие серверы и всё прочее) сохраняются как есть. Нет файла или каталога — создаёт | `{"config": путь}` |
| `claude-disconnect` | Убирает `mcpServers.beedott`. Файла или ключа нет — успех без записи | `{"config": путь}` |
| `claude-code-connect` | Если найден `claude` CLI: `claude mcp remove --scope user beedott` (ошибку игнорируем), затем `claude mcp add-json --scope user beedott '<json>'` с той же командой и env, что у Claude Desktop. Таймаут 30 с на вызов. CLI ищется так же, как в `status` (`claude_auth.claude_bin()` после загрузки `config.env`, чтобы учитывался `MEETMIND_CLAUDE_BIN`) | `{"claude_code": "added" \| "failed" \| "absent", "command": "claude mcp add-json --scope user beedott '<json>'"}` |
| `claude-code-disconnect` | Если найден CLI — `claude mcp remove --scope user beedott` | `{"claude_code": "removed" \| "failed" \| "absent"}` |

`claude-code-*` не завершаются ошибкой из-за CLI: неудача — `"failed"` в `ok`.
`command` в ответе `claude-code-connect` — готовая строка для терминала
(JSON экранирован `shlex.quote`) для всех трёх исходов: её мастер даёт
скопировать тем, у кого нестандартный `CLAUDE_CONFIG_DIR` или CLI не найден.

`status` получает поле `"claude"`: `"connected"` — у ключа совпадают
`command`, `args` и `env` с текущей командой запуска (прочие поля, которые мог
добавить Claude Desktop, не сравниваются); `"stale"` — ключ есть, но команда другая
(переставили приложение, сменился режим dev/bundled); `"bad_config"` — файл
повреждён (подключение сразу покажет ошибку, не закрывая Claude); `null` —
ключа или файла нет. Регистрацию в Claude Code `status` не проверяет
(запуск `claude` CLI на каждый `status` — секунды). Чтение конфига Claude
никогда не валит `status`: любой `OSError` или `ValueError` (кодировка, JSON)
→ `"bad_config"`. Иначе `checkSetupOnLaunch` счёл бы упавший `status` за
«нужна настройка» и открывал мастер на каждом запуске.

Ошибки команд: `bad_config` — файл повреждён, не трогаем, сообщение с путём;
`internal` — прочее (нет прав на запись и т. п.).

## Swift

### Запись по запросу Claude (`MeetMindApp.refreshStatus`)

В опросе `status` (раз в 3 с) новая ветка: записи нет (`recording == nil`),
своего движка нет (`capture.engine == nil`), а `start_requested` — объект →
`performStartRecording(source: "mcp", title: <title из запроса>)`. У
`performStartRecording` появляется параметр `title: String? = nil`: если он
задан, он заменяет название из Календаря (участники из Календаря
по-прежнему передаются). Ошибка старта — в лог, как у остальных триггеров;
флаг демон снимает в начале `start_recording` (или по сроку, если до демона
вызов не дошёл), MCP ответит, что запись не началась. Разбор
`start_requested` — чистая функция в `RuntimeLogic.swift`, чтобы её покрыл
`app/Tests`.

Стоп по запросу Claude отдельной ветки не требует: `stop_requested = "mcp"`
ловит существующая ветка предохранителей (`sid == capture.sessionId` и есть
`stop_requested` → `performStopRecording`). Лог в ней становится нейтральным
(«демон просит остановить запись (<причина>)»).

### Шаг мастера «Claude»

`SetupModel.Step` получает `.claude` между `.telegram` и `.done`; заголовок
«Шаг N из 5». Шаг необязательный: «Пропустить» / «Далее». Из «Настройки…»
шаг доступен всегда, в том числе пользователю, у которого `setupCompleted`
уже `true`.

**Определение Claude Desktop** — по bundle id `com.anthropic.claudefordesktop`
(`NSWorkspace.urlForApplication(withBundleIdentifier:)`), запущен ли —
`NSRunningApplication.runningApplications(withBundleIdentifier:)`.

При входе на шаг мастер заново спрашивает `status` (на чистом Mac мастер
открылся до установки рантайма, `status` тогда ещё не было); это 1–2 с
(импорт huggingface_hub) — в шаге спиннер «Проверяю Claude…». Подключение и
отключение входят в `isBusy` мастера (окно не закрывается и шаги не
переключаются посреди записи конфига). Полностью настроенный пользователь,
открывший «Настройку…», попадает на шаг `.claude`, пока Claude не подключён;
после подключения — как сейчас.

Состояния шага:

1. **Claude Desktop не установлен.** Текст «Подключи BeeDott к Claude
   Desktop — и спрашивай Claude про свои встречи», кнопка «Скачать Claude»
   (`https://claude.ai/download`) и «Проверить снова». Ссылка «У меня только
   Claude Code» — запускает `claude-code-connect` и показывает тот же
   результат, что состояние 3 (строка «Подключено к Claude Code» или блок с
   командой). «Пропустить».
2. **Установлен, не подключён (`claude` = `null` или `"stale"`).** Примеры
   просьб к Claude (вопрос, правка, «начни запись»); строка про данные:
   «Claude видит встречу, только когда сам её запрашивает; ответы идут в твой
   аккаунт Claude. Менять встречи и запускать запись Claude будет с твоего
   разрешения в Claude». Кнопка «Подключить» (для `stale` —
   «Переподключить»).
3. **Подключён (`"connected"`).** «Claude подключён ✓», подсказка «Открой
   Claude и спроси: что решили на последней встрече?», ссылка «Отключить».
   Сразу после подключения: если `claude-code-connect` вернул `added` —
   строка «И в Claude Code»; при любом исходе — свёрнутый блок «Команда для
   Claude Code» с полем `command` и кнопкой «Скопировать» (для своего
   `CLAUDE_CONFIG_DIR` или если CLI не нашёлся). При следующем открытии
   мастера ни строки, ни блока нет: `status` Claude Code не проверяет.
Пользователь только с Claude Code (без Desktop) при каждом открытии
«Настройки…» попадает на этот шаг, а «Готово» пишет «Claude: не подключён»:
`status` Claude Code не проверяет. Это принято сознательно, не баг.

4. **Конфиг повреждён (`"bad_config"`).** Текст ошибки (см. ниже), кнопки
   «Подключить» нет, «Проверить снова».

**Порядок подключения и отключения** (одна функция для обоих):

1. Если Claude Desktop запущен — подтверждение в шаге: «Claude перезапустится,
   открытые чаты сохранятся» → «Перезапустить и подключить» / «Отмена».
2. Закрыть Claude (`terminate()`), ждать завершения до 10 с. Не закрылся —
   «Закрой Claude вручную и нажми ещё раз»; конфиг не пишем, принудительно не
   убиваем.
3. `runSetup("claude-connect")` (или `claude-disconnect`), строка состояния —
   `PhaseRow` как в других шагах.
4. Если Claude был запущен до шага 1 — сразу открыть его снова
   (`NSWorkspace.openApplication`). Если не был — кнопка «Открыть Claude».
5. `runSetup("claude-code-connect")` (или `claude-code-disconnect`) — уже при
   открытом Claude, результат только для строки «И в Claude Code» и блока с
   командой.
6. Перечитать `status`.

Конфиг пишется только при закрытом Claude Desktop: он держит файл в памяти
(`preferences` там же) и при выходе может перезаписать его старой версией.

**Шаг «Готово»** — строка «Claude: подключён» / «Claude: не подключён (можно в
Настройке)».

**Ошибки** в шаге — текст по коду: `bad_config` → «Конфиг Claude повреждён —
открой Claude → Настройки → Developer и исправь файл» + путь; `internal` →
последняя строка + «Повторить».

## Сборка и раздача

- `pyproject.toml` + `uv.lock`: зависимость `mcp`. Бандл (`app/build.sh`)
  менять не нужно: `daemon/mcp_server.py` и `daemon/followup_edit.py`
  попадают в `Resources/backend` вместе с остальным `daemon/`.
- `app/Tools/bundled_smoke.sh`: дополнительно проверить, что в установленном
  bundled-рантайме `python -m daemon.mcp_server` стартует и отвечает на
  `initialize` (или хотя бы что модуль импортируется с cwd `/`).
- `docs/INSTALL.md` и `README.md`: абзац про шаг «Claude», примеры просьб и
  команда для Claude Code вручную.
- У автора (`CLAUDE_CONFIG_DIR=~/.claude-acc2`) регистрация в Claude Code при
  внедрении — руками той же командой из `claude-code-connect`.

## Тесты

- **pytest `tests/test_mcp_server.py`:** форматтеры на фикстурах (фоллоу-ап с
  задачами и без, встреча в обработке, обрезка длинного транскрипта и
  продолжение с `from_minute`, локальное время, `recording_status` с записью
  и без); инструменты с подменённым `control.request` (группировка поиска,
  окно `days` в `action_items`, границы `limit`, `delete_meeting` передаёт
  `title`, `update_followup` передаёт `resend`); `start_recording` и
  `stop_recording` с фейковым `status` — успех, тайм-аут, `busy`;
  аннотации: чтение — `read_only_hint`, правка — `destructive_hint=False`,
  `delete_meeting` — `destructive_hint=True`; «демон не запущен» → понятный
  текст; импорт `daemon.mcp_server` в подпроцессе с `cwd="/"` не тянет
  `config`.
- **pytest e2e:** настоящий `ControlServer` с фейковыми хендлерами на сокете
  во временном `MEETMIND_APPDIR` (короткий путь — лимит длины Unix-сокета),
  `python -m daemon.mcp_server` подпроцессом, MCP-клиент из пакета `mcp`
  (stdio): `initialize`, `list_tools` — тринадцать инструментов, `call_tool
  search_meetings` — данные из фейка, `call_tool delete_meeting` с неверным
  `title` — `is_error`. `stdio_client` передаёт ребёнку только HOME, LOGNAME,
  PATH, SHELL, TERM, USER — `PYTHONPATH` и `MEETMIND_APPDIR` задаются в
  `StdioServerParameters.env` явно.
- **pytest `tests/test_followup_edit.py`:** успешная правка — `revision+1`,
  `engine` сохранён, `.json` и `.md` переписаны, поиск находит новый текст,
  `note`/`obsidian` в `pending`, `telegram` не тронут, сессия `SUMMARIZED`,
  deliver в очереди с `attempts=0` (в том числе после `FAILED` на deliver с
  `attempts=8`); упавшая пересборка (`summarize` в `failed`, старый
  `followup.json` есть) — `summarize` становится `done`; `resend=true` —
  `telegram` в `pending`, `telegram_reply_to` записан, прогресс удалён;
  невалидная схема → `bad_params`, файлы не изменены; запись → `busy` «идёт
  запись», обработка или job `running` → `busy`; `FAILED` без фоллоу-апа →
  `no_followup` с разным текстом при наличии и отсутствии `transcript.txt`;
  нет сессии или `CANCELLED` → `not_found`.
- **pytest `tests/test_states.py`:** `DELIVERED → SUMMARIZED` разрешён.
- **pytest `tests/test_control.py`:** нет сокета → `DaemonUnavailable`;
  ответ с ошибкой → `DaemonError` с `code`; оба — `RuntimeError` с прежним
  текстом.
- **pytest (хендлеры демона, по образцу `tests/test_library_api.py`):**
  `request_start` ставит `start_requested` в `status`, флаг протухает через
  8 с (подменённые monotonic-часы), `start_recording` его снимает, при
  активной записи — `busy`; `request_stop` при `owner=app` ставит
  `stop_requested="mcp"`, при `owner=daemon` закрывает запись, без записи —
  `no_active_session`; `delete_session` с неверным `title` ничего не удаляет,
  у встречи без названия (`title = NULL`) удаление по «Встреча ЧЧ:ММ»
  проходит; `sessions_list` и `session_detail` отдают `display_title`,
  `session_detail` — `has_audio`. MCP `stop_recording` при `CANCELLED` после
  стопа говорит, что запись отменена.
- **pytest `tests/test_setup.py`:** фикстура `appdir` ставит
  `MEETMIND_CLAUDE_DESKTOP_CONFIG` во временный каталог (иначе тесты читают
  настоящий конфиг Claude) и, как сейчас, глушит `claude_auth.claude_bin`;
  `test_status_clean_mac` ждёт новое поле `claude: null`. `claude-connect`
  создаёт файл с нуля; дописывает к существующему, сохраняя `preferences` и
  чужие серверы; повторный вызов идемпотентен; делает `.bak`; пустой файл —
  как `{}`; симлинк остаётся симлинком; битый JSON → `bad_config`, файл не
  изменён; `claude-disconnect` убирает только свой ключ; `status.claude` —
  `connected` / `stale` / `bad_config` / `null`; env с `PYTHONPYCACHEPREFIX`;
  `claude-code-*` — фейковый `claude`-скрипт пишет argv в файл: `remove` +
  `add-json` с той же командой и env; CLI нет → `absent`; CLI с ненулевым
  кодом → `failed`; `command` в ответе есть при всех исходах и разбирается
  `shlex.split` обратно в тот же JSON.
- **Swift `app/Tests`:** разбор `start_requested` (объект с названием, без
  названия, `null`, поля нет у старого демона).
- **Вручную:** на Mac автора (dev-режим) пройти шаг через «Настройку…»,
  убедиться, что confluence/clickhouse/trino на месте; зарегистрировать
  сервер в Claude Code (`~/.claude-acc2`) командой из мастера. В Claude
  спросить про последнюю встречу; поправить исполнителя задачи без `resend` и
  с `resend`; «начни запись» → проверить, что пишет нативный движок
  (`audio_owner=app`), «останови запись» → встреча ушла в обработку.
  Проверить, что `terminate()` закрывает Claude без запроса разрешения на
  автоматизацию (если запрос есть — сработает запасной путь «Закрой Claude
  вручную»). Не делать ничего из этого во время настоящей встречи.

## Порядок реализации

Одна фича, но план делится на три этапа, каждый — рабочий результат с
коммитами и тестами:

1. **Чтение и подключение.** `control.py` (типизированные ошибки),
   `display_title` / `has_audio`, `mcp_server.py` с пятью инструментами
   чтения, команды `claude-*` в `setup.py`, шаг мастера «Claude». После этого
   этапа Claude уже отвечает про встречи.
2. **Правка.** `states.py`, `db.redeliver`, `followup_edit.py`, общая
   подготовка Telegram-ответа, `update_followup`, `delete_session(title)` и
   пять инструментов правки.
3. **Управление записью.** `request_start` / `request_stop`, поле
   `start_requested`, ветка в `refreshStatus`, параметр `title` у
   `performStartRecording`, три инструмента управления.

Тексты, которые описывают возможности (примеры просьб и строка про данные в
шаге мастера, `instructions` сервера), на каждом этапе называют только то,
что уже есть. Ожидания e2e-теста (число инструментов, кейс `delete_meeting`)
растут вместе с этапами: 5 → 10 → 13.

## Вне рамок

claude.ai в браузере и на телефоне; отмена записи из Claude; файл-расширение
`.mcpb`; пункт подключения в дропдауне (только шаг мастера через
«Настройку…»); автопочинка ключа при старте приложения (для `stale` — кнопка
«Переподключить»); автоматическая регистрация в Claude Code при нестандартном
`CLAUDE_CONFIG_DIR` (только команда для копирования); MCP-resources и
prompts.
