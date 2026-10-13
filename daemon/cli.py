"""CLI MeetMind v3.

  python -m daemon.cli daemon                       # демон (foreground)
  python -m daemon.cli record start [--title T]
  python -m daemon.cli record stop | cancel
  python -m daemon.cli status
  python -m daemon.cli reprocess <session_id>
  python -m daemon.cli import <файл> [--title T]   # импорт готовой записи
  python -m daemon.cli health

Все команды кроме daemon — клиенты control-сокета (SPEC_v3 §6).
"""
import argparse
import json
import sys

from daemon.control import request


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="meetmind")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="запустить демон (foreground)")

    rec = sub.add_parser("record", help="управление записью")
    rec.add_argument("action", choices=["start", "stop", "cancel"])
    rec.add_argument("--title", default=None)

    sub.add_parser("status")
    rp = sub.add_parser("reprocess")
    rp.add_argument("session_id")
    sub.add_parser("health")
    imp = sub.add_parser("import", help="импортировать готовый аудиофайл")
    imp.add_argument("path")
    imp.add_argument("--title", default=None)

    args = p.parse_args(argv)

    if args.cmd == "daemon":
        from daemon.app import DaemonApp  # тяжёлые импорты только для демона
        DaemonApp().run()
        return

    if args.cmd == "record":
        method = f"{args.action}_recording"
        params = {"title": args.title} if args.action == "start" else {}
        out = request(method, params)
    elif args.cmd == "status":
        out = request("status")
    elif args.cmd == "import":
        import os
        out = request("import_audio", {"path": os.path.abspath(args.path), "title": args.title})
    elif args.cmd == "reprocess":
        out = request("reprocess", {"session_id": args.session_id})
    else:
        out = request("health")

    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
