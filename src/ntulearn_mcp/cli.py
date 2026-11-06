"""Command line: `ntulearn login | status | courses | download ...`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import auth
from .client import BlackboardClient, NotAuthenticated
from .config import settings
from .courses import is_recent, list_courses, resolve_course
from .downloader import sync


def _print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # old Windows consoles
        print(msg.encode("ascii", "replace").decode(), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ntulearn", description="NTULearn (Blackboard) helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="sign in through NTU SSO in a browser window")
    p.add_argument("--timeout", type=int, default=300)

    p = sub.add_parser("set-cookie", help="save a session from a Cookie header copied from browser DevTools")
    p.add_argument("cookie", help='e.g. "BbRouter=...; JSESSIONID=..."')

    sub.add_parser("logout", help="delete the saved session")
    sub.add_parser("status", help="check the saved session")

    p = sub.add_parser("courses", help="list your courses")
    p.add_argument("--all", action="store_true", help="include past courses and organisations")

    p = sub.add_parser("download", help="bulk-download course materials")
    p.add_argument("courses", nargs="*", help="course codes / names / ids (default: all current courses)")
    p.add_argument("--dest", type=Path, default=settings.download_dir)
    p.add_argument("--include", action="append", help="filename glob to include, e.g. '*.pdf' (repeatable)")
    p.add_argument("--exclude", action="append", help="filename glob to skip, e.g. '*.mp4' (repeatable)")
    p.add_argument("--max-mb", type=float, help="skip files larger than this")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--dry-run", action="store_true", help="only list what would be downloaded")

    args = ap.parse_args(argv)
    client = BlackboardClient()

    try:
        if args.cmd == "login":
            r = auth.interactive_login(timeout_s=args.timeout)
            _print(f"Logged in as {r.get('name') or r.get('user')}. Session saved to {settings.cookie_file}")
        elif args.cmd == "set-cookie":
            n = auth.save_cookies(auth.parse_cookie_header(args.cookie))
            me = client.me()
            _print(f"Saved {n} cookies. Logged in as {me.get('userName')}.")
        elif args.cmd == "logout":
            auth.clear_cookies()
            _print("Saved session removed.")
        elif args.cmd == "status":
            me = client.me()
            _print(f"Logged in as {me.get('userName')} ({settings.base_url})")
        elif args.cmd == "courses":
            cs = list_courses(client)
            if not args.all:
                cs = [c for c in cs if is_recent(c) and not c["is_organization"]] or cs
            for c in cs:
                _print(f"{c['id']:<14} {c.get('code') or '':<28} {c.get('name')}")
        elif args.cmd == "download":
            if args.courses:
                targets = [resolve_course(client, q) for q in args.courses]
            else:
                targets = [c for c in list_courses(client) if is_recent(c) and not c["is_organization"]]
            _print(f"Downloading {len(targets)} course(s) into {args.dest}")
            rep = sync(client, targets, args.dest, args.include, args.exclude, args.max_mb,
                       args.dry_run, args.workers, log=_print)
            if args.dry_run:
                for f in rep.files:
                    size = f"{f['size_bytes'] / 1e6:.1f} MB" if f.get("size_bytes") else "? MB"
                    _print(f"  {size:>9}  {f['would_save_to']}")
            _print(json.dumps({k: v for k, v in rep.to_dict().items() if k != "files"}, indent=2))
            return 0 if rep.status == "done" else 1
    except NotAuthenticated as e:
        _print(str(e).replace("the `ntulearn_login` tool (or ", "").replace(") and sign in", " and sign in"))
        return 2
    except (ValueError, TimeoutError, RuntimeError) as e:
        _print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
