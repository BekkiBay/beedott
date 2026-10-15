"""MCP по-настоящему: `python -m daemon.mcp_server` подпроцессом с cwd /,
клиент из пакета mcp (stdio), демон — ControlServer с фейковыми хендлерами на
сокете во временной папке приложения."""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from daemon.control import ControlServer

REPO = Path(__file__).resolve().parent.parent
SESSION = {"id": "aaa111", "title": "Синк", "display_title": "Синк",
           "started_at": "2026-10-14T05:00:00+00:00", "duration_min": 30.0,
           "status": "delivered", "action_items": 1}


@pytest.fixture()
def appdir(monkeypatch):
    # НЕ pytest tmp_path: путь unix-сокета ограничен ~104 байтами на macOS
    d = tempfile.mkdtemp(prefix="mm", dir="/tmp")
    monkeypatch.setenv("MEETMIND_APPDIR", d)
    ControlServer({
        "sessions_list": lambda p: {"sessions": [SESSION]},
        "search": lambda p: {"results": [{"session_id": "aaa111", "title": "Синк",
                                          "source": "summary", "snippet": "про [[отчёт]]"}]},
    }).start()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def run(appdir: str, scenario):
    # stdio_client передаёт ребёнку только HOME, LOGNAME, PATH, SHELL, TERM, USER —
    # PYTHONPATH и MEETMIND_APPDIR явно.
    params = StdioServerParameters(
        command=sys.executable, args=["-P", "-m", "daemon.mcp_server"], cwd="/",
        env={"PYTHONPATH": str(REPO), "MEETMIND_APPDIR": appdir, "MEETMIND_TZ": "Asia/Tashkent"})

    async def main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await scenario(session)

    # Сервер, который стартовал, но молчит, иначе подвесил бы весь pytest:
    # плагина тайм-аутов в проекте нет.
    return asyncio.run(asyncio.wait_for(main(), timeout=30))


def test_client_lists_tools_and_searches(appdir):
    async def scenario(session):
        listed = await session.list_tools()
        found = await session.call_tool("search_meetings", {"query": "отчёт"})
        missing = await session.call_tool("get_meeting", {"meeting_id": "x"})
        return listed, found, missing

    listed, found, missing = run(appdir, scenario)
    assert {t.name for t in listed.tools} == {
        "list_meetings", "search_meetings", "get_meeting", "action_items", "recording_status"}
    assert not found.is_error
    assert "### Синк — 14.10.2026 10:00" in found.content[0].text
    assert "**отчёт**" in found.content[0].text
    # у фейкового демона нет session_detail → unknown_method → понятная ошибка
    assert missing.is_error
    assert "BeeDott: " in missing.content[0].text
