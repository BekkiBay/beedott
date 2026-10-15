"""ControlSocket: unix domain socket + JSON-RPC (SPEC_v3 §6).

Кадр — одна строка JSON:
  запрос:  {"id": 1, "method": "status", "params": {...}}
  ответ:   {"id": 1, "result": {...}} | {"id": 1, "error": {"code": ..., "message": ...}}
  notification демона (без id): {"method": "heartbeat"}

Сервер однопоточный (asyncio в отдельном thread), handlers — обычные функции,
предоставляет DaemonApp. Клиент — синхронный (для CLI).
"""
import asyncio
import json
import logging
import socket
import threading
from typing import Callable

from daemon.paths import socket_path

logger = logging.getLogger(__name__)

HEARTBEAT_SEC = 5


class ControlError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DaemonUnavailable(RuntimeError):
    """Демон не отвечает: нет сокета, отказ соединения, тайм-аут, обрыв."""


class DaemonError(RuntimeError):
    """Демон ответил ошибкой. code — код ControlError (busy, not_found, ...);
    текст исключения прежний — «code: message» (daemon/cli.py печатает его)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ControlServer:
    """handlers: {"method_name": fn(params: dict) -> dict}."""

    def __init__(self, handlers: dict[str, Callable[[dict], dict]]):
        self.handlers = handlers
        self._clients: set[asyncio.StreamWriter] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="control")
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("control server не поднялся за 10с")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            # напр. unix socket path > ~104 байт (лимит macOS) — bind падает
            logger.exception("control server упал при старте")

    async def _serve(self) -> None:
        sp = socket_path()
        sp.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._on_client, path=str(sp))
        sp.chmod(0o600)
        self._started.set()
        logger.info("control socket: %s", sp)
        async with server:
            heartbeat = asyncio.ensure_future(self._heartbeat())
            try:
                await server.serve_forever()
            finally:
                heartbeat.cancel()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            self._broadcast({"method": "heartbeat"})

    def _broadcast(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        for w in list(self._clients):
            try:
                w.write(data)
            except Exception:
                self._clients.discard(w)

    # --------------------------------------------------------------- clients

    async def _on_client(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
        self._clients.add(writer)
        try:
            while line := await reader.readline():
                resp = self._dispatch(line)
                if resp is not None:
                    writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()

    def _dispatch(self, line: bytes) -> dict | None:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return {"id": None, "error": {"code": "parse_error", "message": "не JSON"}}
        rid = req.get("id")
        method = req.get("method", "")
        fn = self.handlers.get(method)
        if fn is None:
            return {"id": rid, "error": {"code": "unknown_method", "message": method}}
        try:
            return {"id": rid, "result": fn(req.get("params") or {})}
        except ControlError as exc:
            return {"id": rid, "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001
            logger.exception("control: %s упал", method)
            return {"id": rid, "error": {"code": "internal", "message": str(exc)}}


def request(method: str, params: dict | None = None, timeout: float = 30) -> dict:
    """Синхронный клиент (CLI, MCP-сервер). Возвращает result. Демон ответил
    ошибкой — DaemonError; демон недоступен или оборвал обмен — DaemonUnavailable.
    Оба — RuntimeError с прежними текстами."""
    sp = socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            try:
                s.connect(str(sp))
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                raise DaemonUnavailable(
                    f"демон не запущен (нет сокета {sp}) — запусти: python main.py") from exc
            s.sendall((json.dumps({"id": 1, "method": method, "params": params or {}},
                                  ensure_ascii=False) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                # пропускаем notification-кадры (heartbeat) до нашего ответа
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    msg = json.loads(line)
                    if msg.get("id") == 1:
                        if "error" in msg:
                            raise DaemonError(msg["error"]["code"], msg["error"]["message"])
                        return msg.get("result", {})
                    buf = rest
    except OSError as exc:  # тайм-аут, BrokenPipe на sendall, сброс соединения
        raise DaemonUnavailable(f"демон не отвечает: {exc}") from exc
    raise DaemonUnavailable("демон закрыл соединение без ответа")
