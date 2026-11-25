"""`studio` command. `studio start` runs the local app and opens the browser."""

from __future__ import annotations

import argparse
import os
import json
import socket
import urllib.request
import threading
import webbrowser

from . import __version__, projects
from .config import workspace_dir


def _port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1" if host == "localhost" else host, port))
        except OSError:
            return True
    return False


def _is_studio(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/meta", timeout=2) as res:
            return "workspace" in json.loads(res.read().decode("utf-8"))
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="studio", description="Inno Vision Studio")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="run the studio and open it in the browser")
    start.add_argument("--host", default="127.0.0.1",
                       help="use 0.0.0.0 to open the studio to other PCs on the network (no login yet)")
    start.add_argument("--port", type=int, default=8765)
    start.add_argument("--no-browser", action="store_true")

    sub.add_parser("projects", help="list projects")
    new = sub.add_parser("new", help="create a project")
    new.add_argument("name")
    new.add_argument("--product", default="")
    new.add_argument("--template", default="count_line", choices=sorted(projects.TEMPLATES))

    args = parser.parse_args(argv)
    if args.command == "start":
        import uvicorn

        shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
        port = args.port
        while _port_busy(args.host, port):
            if _is_studio(shown, port):
                url = f"http://{shown}:{port}"
                print(f"The studio is already running at {url} — opening it.\n"
                      f"البرنامج شغال بالفعل على {url} — هيتفتح في المتصفح.")
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
            print(f"Port {port} is used by another program; trying {port + 1}.")
            port += 1
            if port > args.port + 20:
                print("No free port found. Start with: studio start --port <number>")
                return 1
        url = f"http://{shown}:{port}"
        print(f"Inno Vision Studio {__version__}\n  workspace: {workspace_dir()}\n  open:      {url}\n"
              "  stop:      Ctrl+C")
        if not args.no_browser:
            # The browser inherits this; it silences GTK's harmless "atk-bridge" notice on Linux.
            os.environ.setdefault("NO_AT_BRIDGE", "1")
            threading.Timer(1.5, webbrowser.open, args=(url,)).start()
        uvicorn.run("studio.api:app", host=args.host, port=port, log_level="warning")
    elif args.command == "projects":
        for meta in projects.list_projects():
            s = meta["stats"]
            print(f"{meta['slug']:<28} {meta['name']}  | videos {s['videos']} ({s['video_hours']} h),"
                  f" images {s['images']}, human labels {s['labels_human']}, models {s['models']}")
    elif args.command == "new":
        meta = projects.create_project(args.name, args.product, args.template)
        print(f"created: {meta['slug']}  ->  {projects.project_dir(meta['slug'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
