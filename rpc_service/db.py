"""Isolated storage for the add-on.

Separate database file (default rpc_service/data/service.db). No table from the
existing seedhunter Store (data/hits.db) is touched or read.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('telegram','web')),
  tg_user_id TEXT UNIQUE,
  tg_username TEXT,
  label TEXT,
  auth_salt TEXT, auth_hash TEXT,
  pin_salt TEXT, pin_hash TEXT,
  pin_fails INTEGER NOT NULL DEFAULT 0,
  pin_locked_until INTEGER NOT NULL DEFAULT 0,
  is_public INTEGER NOT NULL DEFAULT 0,
  withdrawal_suspended INTEGER NOT NULL DEFAULT 0,
  tier TEXT NOT NULL DEFAULT 'free',
  premium_until INTEGER NOT NULL DEFAULT 0,
  session_version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  user_agent TEXT,
  revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wallets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  network TEXT NOT NULL,
  address TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'kms',
  key_ref TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  UNIQUE (account_id, network)
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_ref TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  currency TEXT NOT NULL,
  network TEXT NOT NULL,
  address TEXT NOT NULL,
  amount_raw TEXT NOT NULL,
  amount_usd INTEGER NOT NULL,
  price_usd REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  txid TEXT UNIQUE,
  confirmations INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  credited_at INTEGER,
  credited_until INTEGER,
  note TEXT
);
CREATE TABLE IF NOT EXISTS withdrawals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wd_ref TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  wallet_id INTEGER NOT NULL REFERENCES wallets(id),
  network TEXT NOT NULL,
  destination TEXT NOT NULL,
  amount_raw TEXT NOT NULL,
  fee_raw TEXT NOT NULL DEFAULT '0',
  status TEXT NOT NULL DEFAULT 'requested',
  idem_key TEXT,
  txid TEXT,
  failure_reason TEXT,
  created_ip TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (account_id, idem_key)
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  account_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT,
  ip TEXT
);
CREATE TABLE IF NOT EXISTS usage (
  account_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, day)
);
CREATE TABLE IF NOT EXISTS monitor_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rpc_health (
  network TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  ok INTEGER NOT NULL,
  latency_ms INTEGER,
  fail_count INTEGER NOT NULL DEFAULT 0,
  checked_at INTEGER NOT NULL,
  PRIMARY KEY (network, endpoint)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=8000")
    return con


def init(path: Path) -> None:
    with closing(connect(path)) as con:
        con.executescript(SCHEMA)
        con.commit()


def fetch_all(path: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with closing(connect(path)) as con:
        return con.execute(sql, args).fetchall()


def fetch_one(path: Path, sql: str, args: tuple = ()) -> sqlite3.Row | None:
    with closing(connect(path)) as con:
        return con.execute(sql, args).fetchone()


def execute(path: Path, sql: str, args: tuple = ()) -> int:
    with closing(connect(path)) as con:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid or cur.rowcount


def audit(path: Path, account_id: int | None, action: str, detail: dict | None = None,
          ip: str | None = None) -> None:
    execute(path, "INSERT INTO audit(ts,account_id,action,detail,ip) VALUES(?,?,?,?,?)",
            (int(time.time()), account_id, action,
             json.dumps(detail, separators=(",", ":")) if detail else None, ip))


def bump_usage(path: Path, account_id: int, day: str, amount: int = 1) -> None:
    execute(path,
            "INSERT INTO usage(account_id,day,requests) VALUES(?,?,?) "
            "ON CONFLICT(account_id,day) DO UPDATE SET requests=requests+excluded.requests",
            (account_id, day, amount))


def usage_today(path: Path, account_id: int, day: str) -> int:
    row = fetch_one(path, "SELECT requests FROM usage WHERE account_id=? AND day=?",
                    (account_id, day))
    return int(row["requests"]) if row else 0