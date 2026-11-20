from __future__ import annotations

import argparse
import asyncio
import logging

from . import config
from .notifier import Notifier
from .scanner import scan
from .store import Store
from .worker import Hunter


def main() -> None:
    ap = argparse.ArgumentParser(prog="seedhunter")
    ap.add_argument("command", choices=["hunt", "scan", "bot", "stats"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("-d", "--duration", type=int, default=0)
    ap.add_argument("-s", "--shard", type=int)
    ap.add_argument("-f", "--family")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = config.load()
    if a.shard is not None:
        s.shard = a.shard
    if a.family:
        s.family = a.family
    store = Store(s.db_path)
    if a.command == "stats":
        print(store.count())
        for r in store.recent(20):
            print(r)
        return
    if a.command == "scan":
        if not a.target:
            raise SystemExit("usage: scan <address>")
        print("\n".join(asyncio.run(scan(s, a.target))))
        return
    if a.command == "bot":
        from .bot import build
        build(s).run_polling()
        return
    h = Hunter(s, store, Notifier(s.tg_token, s.tg_chat))
    try:
        asyncio.run(h.run(a.duration or None))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()