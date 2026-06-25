"""Command-line entry point: ``obc <command>``.

Commands
--------
  initdb              create the SQLite schema
  normalize           load data/raw/*.json into the catalog
  stats               print catalog counts
  serve               run the search UI (uvicorn)
  scrape ...          harvest the catalog (see obc.scrape)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines into os.environ."""
    f = Path(path)
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="obc")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb", help="create the SQLite schema")
    sub.add_parser("normalize", help="load data/raw/*.json into the catalog")
    sub.add_parser("stats", help="print catalog counts")
    sp = sub.add_parser("serve", help="run the search UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--reload", action="store_true")
    sub.add_parser("scrape", help="harvest the catalog (see obc.scrape --help)")
    lp = sub.add_parser("lists", help="update curated lists (bestsellers, prizes)")
    lp.add_argument("args", nargs="*",
                    help="optional 'update' action and/or specific list slugs")

    args, rest = p.parse_known_args(argv)

    if args.cmd == "initdb":
        from . import db
        conn = db.connect(); db.init_db(conn); conn.close()
        print("schema created at", db.DEFAULT_DB)
    elif args.cmd == "normalize":
        from .normalize import normalize
        normalize()
    elif args.cmd == "stats":
        from . import db
        conn = db.connect(); print(db.stats(conn)); conn.close()
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("obc.web.app:app", host=args.host, port=args.port, reload=args.reload)
    elif args.cmd == "scrape":
        from . import scrape
        return scrape.main(rest)
    elif args.cmd == "lists":
        from . import lists
        slugs = [a for a in args.args if a != "update"]  # 'update' is the implied action
        lists.update(slugs or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
