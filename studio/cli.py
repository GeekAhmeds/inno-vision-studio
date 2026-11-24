"""`studio` command. `studio start` runs the local app and opens the browser."""

from __future__ import annotations

import argparse
import threading
import webbrowser

from . import __version__, projects
from .config import workspace_dir


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

        url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
        print(f"Inno Vision Studio {__version__}\n  workspace: {workspace_dir()}\n  open:      {url}")
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        uvicorn.run("studio.api:app", host=args.host, port=args.port, log_level="warning")
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
