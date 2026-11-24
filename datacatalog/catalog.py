"""Catalog database stored on the drive itself in .datacatalog/catalog.db."""

import json
import os
import sqlite3
import threading
import time
import uuid

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    dir           TEXT NOT NULL,
    name          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    hash          TEXT,
    hashed_at     REAL,
    verified_at   REAL,
    seen_scan     INTEGER,
    status        TEXT NOT NULL DEFAULT 'ok',   -- ok | missing | quarantined
    q_batch       TEXT,
    corrupt       INTEGER NOT NULL DEFAULT 0    -- 1 = suspected corruption
);
CREATE INDEX IF NOT EXISTS idx_files_dir    ON files(dir);
CREATE INDEX IF NOT EXISTS idx_files_hash   ON files(hash);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE INDEX IF NOT EXISTS idx_files_size   ON files(size);

CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY,
    mode          TEXT NOT NULL,                -- scan | verify
    host          TEXT,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    status        TEXT NOT NULL,                -- running | done | stopped | failed
    files_seen    INTEGER DEFAULT 0,
    bytes_seen    INTEGER DEFAULT 0,
    files_hashed  INTEGER DEFAULT 0,
    bytes_hashed  INTEGER DEFAULT 0,
    new_files     INTEGER DEFAULT 0,
    changed_files INTEGER DEFAULT 0,
    missing_files INTEGER DEFAULT 0,
    errors        INTEGER DEFAULT 0,
    corrupt       INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scan_errors (
    id       INTEGER PRIMARY KEY,
    scan_id  INTEGER,
    path     TEXT,
    error    TEXT,
    ts       REAL
);
CREATE TABLE IF NOT EXISTS integrity_issues (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL,
    expected   TEXT,
    actual     TEXT,
    size       INTEGER,
    detected_at REAL,
    scan_id    INTEGER,
    resolved   INTEGER DEFAULT 0
);
-- Operation journal: basis for later synchronization (tells deliberate
-- deletions apart from files that vanished without a trace).
CREATE TABLE IF NOT EXISTS journal (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    host      TEXT,
    op        TEXT NOT NULL,                    -- quarantine | restore | purge
    batch     TEXT,
    path      TEXT NOT NULL,
    hash      TEXT,
    size      INTEGER,
    detail    TEXT
);
CREATE TABLE IF NOT EXISTS q_batches (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    files       INTEGER,
    bytes       INTEGER,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'active'  -- active | restored | purged
);
"""

SCHEMA_VERSION = "1"


def hostname():
    try:
        import socket

        return socket.gethostname()
    except Exception:
        return "?"


def catalog_dir(root):
    return os.path.join(root, paths.CATALOG_DIR)


def volume_file(root):
    return os.path.join(catalog_dir(root), "volume.json")


def read_volume_info(root):
    """Return drive info from .datacatalog/volume.json, or None."""
    try:
        with open(volume_file(root), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def init_volume(root, name, role):
    os.makedirs(catalog_dir(root), exist_ok=True)
    info = read_volume_info(root) or {
        "volume_id": str(uuid.uuid4()),
        "created_at": time.time(),
    }
    info["name"] = name
    info["role"] = role
    info["schema"] = SCHEMA_VERSION
    tmp = volume_file(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    os.replace(tmp, volume_file(root))
    return info


class Catalog:
    """Connection to one drive's catalog. Thread-safe thanks to a lock."""

    def __init__(self, root):
        self.root = root
        info = read_volume_info(root)
        if info is None:
            raise ValueError("This drive has no catalog (.datacatalog) yet. Initialize it first.")
        self.info = info
        self.db_path = os.path.join(catalog_dir(root), "catalog.db")
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            # WAL can be problematic on some NTFS drivers under Linux;
            # if it fails we stay in the default journal mode.
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(files)")}
            if "corrupt" not in cols:
                self.conn.execute("ALTER TABLE files ADD COLUMN corrupt INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # --- meta ---
    def get_meta(self, key, default=None):
        with self.lock:
            r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self.conn.commit()

    def excludes(self):
        return self.get_meta("excludes", [])

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def summary(self):
        r = self.q1(
            """SELECT COUNT(*) n, COALESCE(SUM(size),0) b,
                      SUM(CASE WHEN hash IS NULL THEN 1 ELSE 0 END) unhashed
               FROM files WHERE status='ok'"""
        )
        last = self.q1("SELECT * FROM scans ORDER BY id DESC LIMIT 1")
        last_done = self.q1("SELECT * FROM scans WHERE status='done' AND mode='scan' ORDER BY id DESC LIMIT 1")
        last_verify = self.q1("SELECT * FROM scans WHERE status='done' AND mode='verify' ORDER BY id DESC LIMIT 1")
        issues = self.q1("SELECT COUNT(*) FROM integrity_issues WHERE resolved=0")[0]
        missing = self.q1("SELECT COUNT(*) FROM files WHERE status='missing'")[0]
        quarantined = self.q1("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='quarantined'")
        return {
            "volume": self.info,
            "root": self.root,
            "files": r["n"],
            "bytes": r["b"],
            "unhashed": r["unhashed"] or 0,
            "last_scan": dict(last) if last else None,
            "last_full_scan": dict(last_done) if last_done else None,
            "last_verify": dict(last_verify) if last_verify else None,
            "integrity_issues": issues,
            "missing": missing,
            "quarantined_files": quarantined[0],
            "quarantined_bytes": quarantined[1],
            "excludes": self.excludes(),
        }
