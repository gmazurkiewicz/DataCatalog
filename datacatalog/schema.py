"""Catalog format versioning and migrations.

Each catalog lives on its drive for years and may be opened by different versions of
DataCatalog (e.g. an updated app on Windows and an older one on the Raspberry Pi).

Two numbers describe a catalog:

  schema version  PRAGMA user_version - which migrations have been applied.
  compat level    meta 'schema_compat' (also in volume.json) - the oldest schema version
                  whose apps may still open and write this catalog safely.

An app with schema version S opens a catalog when compat <= S:
  * catalog version < S  -> it is migrated (after a backup copy is made),
  * catalog version == S -> normal case,
  * catalog version > S  -> it was touched by a newer app, but only additively; the older
                            app works with the parts it knows and never migrates down.
When compat > S the app refuses to open the catalog and asks to be updated.

Rules for future changes (keep them, so old catalogs and old apps keep working):
  1. Prefer additive changes: new tables, new columns with defaults, new meta keys.
     These keep the compat level unchanged.
  2. Never change the meaning or format of an existing column, table or meta key.
     If that is unavoidable, write a migration with `breaking=True` - it raises the
     compat level so older apps refuse the catalog instead of misreading it.
  3. Every migration runs once, inside one transaction, and must be idempotent
     (check before ALTER, use IF NOT EXISTS).
  4. Add a test that migrates tests/fixtures/catalog_v1 (and later fixtures) and
     checks that the data survived.

Stable references for future features: files.id is an INTEGER PRIMARY KEY and survives
renames done by the app - other tables should reference files(id). files.extra is a JSON
text column reserved for small per-file attributes that do not need their own table.
"""

import os
import sqlite3
import time

from . import __version__

# History of schema versions:
#   1  stage 1-3 (catalogs created before versioning; user_version 0)
#   2  versioning: schema_compat, hash_algorithm, last_writer, files.extra,
#      scans.app_version, sync_plan and smart_readings created up front
SCHEMA_VERSION = 2
HASH_ALGORITHM = "blake2b-256"


class IncompatibleCatalog(Exception):
    pass


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, column, ddl):
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _meta_get(conn, key):
    import json

    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def _meta_set(conn, key, value):
    import json

    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def _exec_script(conn, script):
    """Run several statements inside the current transaction (executescript would commit)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            conn.execute(buf)
            buf = ""
    if buf.strip():
        conn.execute(buf)


# ---------------------------------------------------------------- migrations
def _to_2(conn):
    from .health import SCHEMA as SMART_SCHEMA
    from .sync import PLAN_SCHEMA

    _add_column(conn, "files", "corrupt", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "files", "extra", "TEXT")
    _add_column(conn, "scans", "app_version", "TEXT")
    for script in (PLAN_SCHEMA, SMART_SCHEMA):
        _exec_script(conn, script)
    if _meta_get(conn, "hash_algorithm") is None:
        _meta_set(conn, "hash_algorithm", HASH_ALGORITHM)


# version -> (function, breaking)
MIGRATIONS = {
    2: (_to_2, False),
}


def compat_level(conn):
    v = _meta_get(conn, "schema_compat")
    return int(v) if v is not None else 1


def backup(conn, db_path, version):
    """Consistent copy of the database (works with WAL) next to it."""
    base = os.path.join(os.path.dirname(db_path), f"catalog-v{version}.bak.db")
    dst_path = base
    if os.path.exists(dst_path):
        dst_path = base.replace(".bak.db", time.strftime("-%Y%m%d-%H%M%S.bak.db"))
    dst = sqlite3.connect(dst_path)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    return dst_path


def prepare(conn, db_path, base_schema, volume_info):
    """Create or migrate the catalog. Returns a dict describing what happened."""
    vinfo_compat = int(volume_info.get("schema_compat") or 1)
    if vinfo_compat > SCHEMA_VERSION:
        raise IncompatibleCatalog(_newer_message(volume_info.get("app_version")))
    fresh = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'").fetchone() is None
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # The base (v1) tables use IF NOT EXISTS, so this also completes catalogs created
    # by the earliest versions, which did not have every v1 table yet.
    conn.executescript(base_schema)
    if fresh or version == 0:
        version = 1  # new catalog, or one from before versioning
    if not fresh:
        compat = compat_level(conn)
        if compat > SCHEMA_VERSION:
            writer = _meta_get(conn, "last_writer") or {}
            raise IncompatibleCatalog(_newer_message(writer.get("app_version")))
    result = {"from": version, "to": max(version, SCHEMA_VERSION), "backup": None, "newer": version > SCHEMA_VERSION}
    if version < SCHEMA_VERSION:
        if not fresh:
            result["backup"] = backup(conn, db_path, version)
        compat = compat_level(conn) if not fresh else 1
        conn.commit()
        old_isolation = conn.isolation_level
        conn.isolation_level = None  # manual transactions below
        try:
            _run_migrations(conn, version, compat, fresh)
        finally:
            conn.isolation_level = old_isolation
    if not result["newer"]:
        _meta_set(
            conn,
            "last_writer",
            {"app_version": __version__, "schema": SCHEMA_VERSION, "host": _host(), "ts": time.time()},
        )
        conn.commit()
    return result


def _run_migrations(conn, version, compat, fresh):
    for target in range(version + 1, SCHEMA_VERSION + 1):
        fn, breaking = MIGRATIONS[target]
        conn.execute("BEGIN")
        try:
            fn(conn)
            if breaking:
                compat = target
            _meta_set(conn, "schema_compat", compat)
            conn.execute(f"PRAGMA user_version = {target}")
            if not fresh:
                conn.execute(
                    "INSERT INTO journal(ts,host,op,path,detail) VALUES(?,?,?,?,?)",
                    (time.time(), _host(), "schema_migration", "", f"v{target - 1} -> v{target} by {__version__}"),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _newer_message(app_version):
    by = f" (DataCatalog {app_version})" if app_version else ""
    return (
        f"This drive's catalog was updated by a newer version of DataCatalog{by} in a way this version "
        "cannot handle safely. Update the app (git pull, then uv sync) and open the drive again."
    )


def _host():
    from .catalog import hostname

    return hostname()
