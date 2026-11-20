from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, chain TEXT, address TEXT,
  seed TEXT, path TEXT, asset TEXT, amount TEXT, raw INTEGER, url TEXT,
  score INTEGER, verdict TEXT, UNIQUE(chain,address,asset));
CREATE TABLE IF NOT EXISTS sweeps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, chain TEXT, address TEXT,
  asset TEXT, amount TEXT, tx TEXT, ok INTEGER);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)
        self.con.commit()

    def add(self, **k) -> bool:
        try:
            self.con.execute(
                "INSERT OR IGNORE INTO hits(ts,chain,address,seed,path,asset,amount,raw,url)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (int(time.time()), k["chain"], k["address"], k["seed"],
                 k.get("path"), k["asset"], k["amount"], k["raw"], k.get("url")))
            self.con.commit()
            return True
        except sqlite3.Error:
            return False

    def annotate(self, chain, addr, asset, score, verdict):
        self.con.execute("UPDATE hits SET score=?,verdict=? WHERE chain=? AND address=? AND asset=?",
                         (score, verdict, chain, addr, asset))
        self.con.commit()

    def swept(self, chain, addr, asset, amount, tx, ok):
        self.con.execute("INSERT INTO sweeps(ts,chain,address,asset,amount,tx,ok)"
                         " VALUES(?,?,?,?,?,?,?)",
                         (int(time.time()), chain, addr, asset, amount, tx, int(ok)))
        self.con.commit()

    def count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM hits").fetchone()[0]

    def recent(self, n=10):
        return self.con.execute("SELECT ts,chain,asset,amount,address,verdict"
                                " FROM hits ORDER BY ts DESC LIMIT ?", (n,)).fetchall()