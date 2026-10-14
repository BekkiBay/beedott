# BeeDott в Claude Desktop через MCP — дизайн

Дата: 2026-10-14. Статус: согласован с пользователем (вариант A — BeeDott сам
прописывает себя в конфиг Claude Desktop).

## Цель

Любой пользователь BeeDott (не только автор) в мастере первичной настройки
одной кнопкой подключает свои встречи к Claude Desktop. Дальше в чате Claude
можно спросить «что решили на последней встрече?», «какие задачи на мне за
неделю?», «где обсуждали минимальную корзину?» — Claude сам вызывает
инструменты BeeDott.

Критерий приёмки: на Mac с BeeDott и Claude Desktop шаг мастера «Claude»
подключает сервер без терминала и ручной правки файлов; после перезапуска
Claude Desktop вопрос про последнюю встречу получает ответ с данными из
BeeDott; твои прочие MCP-серверы и настройки Claude Desktop не тронуты.

## Решения

- **Где работает.** Только локально: Claude Desktop запускает MCP-сервер как
  дочерний процесс (stdio), сервер ходит в демон BeeDott через существующий
  Unix-сокет `control.sock`. Сети и портов нет. claude.ai в браузере и на
  телефоне — вне рамок (нужен публичный сервер).
- **Только чтение.** Поиск, список, карточка встречи, задачи. Удаление,
  переименование, пересборка саммари и управление записью наружу не отдаются.
- **Подключение — вариант A.** BeeDott сам пишет ключ `mcpServers.beedott` в
  `~/Library/Application Support/Claude/claude_desktop_config.json` и
  перезапускает Claude Desktop с подтверждения. Файл-расширение `.mcpb` и
  удалённый коннектор отклонены.
- **Claude Code — по возможности.** Если найден `claude` CLI, сервер
  регистрируется и там (`claude mcp add-json --scope user`). Это пишет в
  стандартный конфиг Claude Code; при нестандартном `CLAUDE_CONFIG_DIR`
  регистрацию придётся повторить руками — в мастере это не решаем.
- **SDK.** Официальный Python-пакет `mcp`, зависимость `mcp>=2.3,<3`. API
  2.x: `MCPServer` из `mcp.server.mcpserver` (FastMCP из 1.x там переименован),
  `@server.tool(annotations=ToolAnnotations(read_only_hint=True))`,
  `server.run("stdio")`; клиент для тестов — `mcp.client.stdio.stdio_client` +
  `ClientSession`. У коллег рантайм обновится сам при запуске новой версии
  (сравнение хэша `uv.lock` уже есть).

## Python: MCP-сервер `daemon/mcp_server.py`

Запуск: `python -m daemon.mcp_server`. Ходит в демон через
`daemon.control.request()`; в БД и файлы сессий напрямую не лезет.

**Импорты.** Модуль импортирует только `daemon.control` (и через него
`daemon.paths`), не `config` и не `daemon.db`: Claude Desktop запускает
процесс с произвольным cwd (часто `/`), а `import config` делает
`mkdir("sessions")` относительно cwd. Это закрепляется тестом.

**Инструменты** (все с аннотацией `read_only_hint=True`, ответ — Markdown-текст;
`limit` и `days` вне диапазона прижимаются к границе, не ошибка):

| Инструмент | Параметры | Что возвращает |
|---|---|---|
| `list_meetings` | `limit: int = 20` (1…100) | Последние встречи, новые сверху: id, название, дата и время (локальное), длительность, статус («готово» / «обрабатывается» / «ошибка»), число задач |
| `search_meetings` | `query: str`, `limit: int = 10` (1…50) | Встречи с совпадениями в саммари или транскрипте, сгруппированные по встрече: id, название, дата, где нашлось, фрагмент. Даты — из одного вызова `sessions_list(limit=10000)`; встречи нет в списке — «дата неизвестна» |
| `get_meeting` | `meeting_id: str`, `include_transcript: bool = False` | Название, дата, длительность, участники (если есть); фоллоу-ап: контекст, что обсуждали, решения, задачи (кто / что / срок), открытые вопросы. С флагом — транскрипт строками `[мм:сс] Спикер N: текст` (у старых моно-записей без `speaker` — `[мм:сс] текст`) |
| `action_items` | `days: int = 7` (1…90) | Задачи из встреч за последние N дней: задача, ответственный, срок, встреча (название, дата, id). Встречи без фоллоу-апа пропускаются. Список — `sessions_list(limit=10000)` (дефолт демона 200 обрезал бы окно) |

Детали:

- Данные: `sessions_list`, `session_detail`, `search` — существующие методы
  демона (`daemon/app.py`). `action_items` — один `sessions_list` и
  `session_detail` только для встреч в окне, у которых счётчик `action_items`
  из списка больше нуля (`session_detail` каждый раз тащит весь транскрипт).
- Даты в сессиях — ISO UTC; в ответах — локальное время `ДД.ММ.ГГГГ ЧЧ:ММ`.
- Транскрипт длиннее 60 000 символов (с префиксами строк) обрезается с явной
  пометкой в конце («показано N из M минут»). Предел — под лимит вывода MCP в
  Claude Code (~25 тыс. токенов, кириллица плотнее латиницы); часовая встреча —
  около 54 000 символов. При реализации сверить на реальной длинной встрече.
- Встреча без фоллоу-апа (ещё обрабатывается или упала) в `get_meeting` —
  статус и, если есть, транскрипт по флагу.
- Форматирование — чистые функции (`format_meeting_list`, `format_meeting`,
  `format_search`, `format_action_items`), тестируются без сокета.
- `instructions` сервера коротко объясняют Claude, что это встречи
  пользователя, записанные BeeDott, и что `meeting_id` берётся из
  `list_meetings` / `search_meetings`.

**Ошибки.** Сейчас `control.request()` на любую беду бросает голый
`RuntimeError`, а тайм-аут и прочие `OSError` пропускает как есть. В
`daemon/control.py` появляются два подкласса `RuntimeError` (тексты сообщений
прежние — `daemon/cli.py` и `tests/test_control.py` ловят `RuntimeError` и
сверяют текст):

- `DaemonUnavailable` — нет сокета, отказ соединения, тайм-аут, любой
  `OSError` во время обмена (в том числе `BrokenPipeError` на `sendall`),
  демон закрыл соединение без ответа;
- `DaemonError` с полем `code` — демон ответил ошибкой.

MCP-сервер отображает: `DaemonUnavailable` → «BeeDott не запущен или не
отвечает — открой приложение BeeDott и повтори вопрос»; `DaemonError` с
`code == "not_found"` → «Встреча <id> не найдена»; прочие `DaemonError` →
«BeeDott: <сообщение>». Любое другое исключение (например, `JSONDecodeError`
на битом кадре) инструмент тоже ловит и возвращает текстом «BeeDott: ошибка —
<сообщение>»: `MCPServer` сам заменяет непойманное исключение на безликое
«Error executing tool» и теряет текст. Трейсбэков в ответах нет.

## Python: подключение в `daemon/setup.py`

Четыре новые команды и одно новое поле в `status`. Протокол прежний:
JSON-строки в stdout, `ok` / `error`, код выхода 0 / 1. Claude Desktop и
Claude Code — разные команды: конфиг Claude Desktop пишется за доли секунды,
и Claude можно открыть снова, не дожидаясь медленного `claude` CLI.

**Команда запуска сервера** считается из процесса, в котором выполняется
`daemon.setup`:

- `command` — `sys.executable` (у коллег `runtime/venv/bin/python`, у автора —
  `.venv/bin/python` репозитория);
- `args` — `["-m", "daemon.mcp_server"]`;
- `env` — `PYTHONPATH` = каталог, где лежит пакет `daemon`
  (`Path(daemon.__file__).resolve().parent.parent`: у коллег
  `BeeDott.app/Contents/Resources/backend`, у автора — репозиторий); плюс
  `PYTHONPYCACHEPREFIX` и `MEETMIND_APPDIR`, если они заданы в окружении
  процесса (их ставит приложение, в `config.env` их нет).
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
| `claude-code-connect` | Если найден `claude` CLI: `claude mcp remove --scope user beedott` (ошибку игнорируем), затем `claude mcp add-json --scope user beedott '<json>'` с той же командой и env, что у Claude Desktop. Таймаут 30 с на вызов. CLI ищется так же, как в `status` (`claude_auth.claude_bin()` после загрузки `config.env`, чтобы учитывался `MEETMIND_CLAUDE_BIN`) | `{"claude_code": "added" \| "failed" \| "absent"}` |
| `claude-code-disconnect` | Если найден CLI — `claude mcp remove --scope user beedott` | `{"claude_code": "removed" \| "failed" \| "absent"}` |

`claude-code-*` не завершаются ошибкой из-за CLI: неудача — `"failed"` в `ok`.

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

## Swift: шаг мастера «Claude»

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
   (`https://claude.ai/download`) и «Проверить снова». «Пропустить».
2. **Установлен, не подключён (`claude` = `null` или `"stale"`).** Пример
   вопросов к Claude; строка про данные: «Claude видит встречу, только когда
   сам её запрашивает; ответы идут в твой аккаунт Claude». Кнопка
   «Подключить» (для `stale` — «Переподключить»).
3. **Подключён (`"connected"`).** «Claude подключён ✓», подсказка «Открой
   Claude и спроси: что решили на последней встрече?», ссылка «Отключить».
   Сразу после подключения, если `claude-code-connect` вернул `added`, —
   строка «И в Claude Code» (при следующем открытии мастера её нет:
   `status` Claude Code не проверяет).
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
   открытом Claude, результат только для строки «И в Claude Code».
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
  менять не нужно: `daemon/mcp_server.py` попадает в `Resources/backend`
  вместе с остальным `daemon/`.
- `app/Tools/bundled_smoke.sh`: дополнительно проверить, что в установленном
  bundled-рантайме `python -m daemon.mcp_server` стартует и отвечает на
  `initialize` (или хотя бы что модуль импортируется с cwd `/`).
- `docs/INSTALL.md`: абзац про шаг «Claude» и вопросы-примеры.

## Тесты

- **pytest `tests/test_mcp_server.py`:** форматтеры на фикстурах (фоллоу-ап с
  задачами и без, встреча в обработке, обрезка длинного транскрипта, локальное
  время); инструменты с подменённым `control.request` (группировка поиска,
  окно `days` в `action_items`, границы `limit`); «демон не запущен» →
  понятный текст; импорт `daemon.mcp_server` в подпроцессе с `cwd="/"` не
  тянет `config`.
- **pytest e2e:** настоящий `ControlServer` с фейковыми хендлерами на сокете
  во временном `MEETMIND_APPDIR` (короткий путь — лимит длины Unix-сокета),
  `python -m daemon.mcp_server` подпроцессом, MCP-клиент из пакета `mcp`
  (stdio): `initialize`, `list_tools` — четыре инструмента, `call_tool
  search_meetings` — данные из фейка. `stdio_client` передаёт ребёнку только
  HOME, LOGNAME, PATH, SHELL, TERM, USER — `PYTHONPATH` и `MEETMIND_APPDIR`
  задаются в `StdioServerParameters.env` явно; признак ошибки в результате —
  `is_error`.
- **pytest `tests/test_control.py`:** нет сокета → `DaemonUnavailable`;
  ответ с ошибкой → `DaemonError` с `code`; оба — `RuntimeError` с прежним
  текстом.
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
  кодом → `failed`.
- **Вручную:** на Mac автора (dev-режим) пройти шаг через «Настройку…»,
  убедиться, что confluence/clickhouse/trino на месте, спросить Claude про
  последнюю встречу. Проверить, что `terminate()` закрывает Claude без запроса
  разрешения на автоматизацию (если запрос есть — сработает запасной путь
  «Закрой Claude вручную»).

## Вне рамок

claude.ai в браузере и на телефоне; управление записью и любые изменяющие
действия из Claude; файл-расширение `.mcpb`; пункт подключения в дропдауне (только
шаг мастера через «Настройку…»); автопочинка ключа при старте приложения
(для `stale` — кнопка «Переподключить»); нестандартный `CLAUDE_CONFIG_DIR`
у Claude Code.
