"""Catalog format compatibility.

tests/fixtures/catalog_v1 is a real catalog written by DataCatalog before versioning
(schema v1): a synced drive with a quarantined duplicate, a missing file, a corrupted
file found by Verify, a SMART reading and a pending sync plan. Keep this fixture forever;
every future version must still open and migrate it. When the format changes again,
add a fixture for the previous version next to it.
"""

import json
import os
import shutil
import sqlite3

import pytest

from datacatalog import analysis, catalog, health, scanner, schema, sync

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def drive_from_fixture(tmp_path, name="catalog_v1"):
    root = tmp_path / "drive"
    shutil.copytree(os.path.join(FIXTURES, name), root / ".datacatalog")
    return str(root)


def user_version(root):
    c = sqlite3.connect(os.path.join(root, ".datacatalog", "catalog.db"))
    try:
        return c.execute("PRAGMA user_version").fetchone()[0]
    finally:
        c.close()


def test_v1_catalog_is_migrated_without_data_loss(tmp_path):
    root = drive_from_fixture(tmp_path)
    assert user_version(root) == 0
    cat = catalog.Catalog(root)
    m = cat.migration
    assert (m["from"], m["to"]) == (1, schema.SCHEMA_VERSION)
    assert m["backup"] and os.path.exists(m["backup"])
    assert user_version(root) == schema.SCHEMA_VERSION

    # data written by v1 is all there
    status = {r["path"]: (r["status"], r["corrupt"]) for r in cat.q("SELECT path, status, corrupt FROM files")}
    assert status["Old/a_copy.jpg"] == ("quarantined", 0)
    assert status["gone.txt"] == ("missing", 0)
    assert status["Docs/r.pdf"] == ("ok", 1)
    assert cat.q1("SELECT COUNT(*) FROM integrity_issues WHERE resolved=0")[0] == 1
    assert cat.q1("SELECT COUNT(*) FROM q_batches")[0] == 1
    assert cat.q1("SELECT COUNT(*) FROM partner_files")[0] == 5
    assert cat.partner()["name"] == "Fixture-B"
    assert cat.excludes() == ["Tmp"]
    assert health.latest(cat)["summary"]["model"] == "Fixture HDD"
    assert sync.plan_summary(cat)["kinds"]

    # new things exist
    assert {"extra", "corrupt"} <= {r[1] for r in cat.q("PRAGMA table_info(files)")}
    assert "app_version" in {r[1] for r in cat.q("PRAGMA table_info(scans)")}
    assert cat.get_meta("hash_algorithm") == schema.HASH_ALGORITHM
    assert cat.get_meta("schema_compat") == 1
    assert cat.get_meta("last_writer")["schema"] == schema.SCHEMA_VERSION
    ops = [r["op"] for r in cat.q("SELECT op FROM journal ORDER BY id")]
    assert ops[0] == "quarantine" and ops[-1] == "schema_migration"
    info = json.load(open(os.path.join(root, ".datacatalog", "volume.json")))
    assert info["schema"] == schema.SCHEMA_VERSION and info["schema_compat"] == 1
    assert info["volume_id"] == "a0043014-fa8e-48c0-8265-0190eccea2fb"

    # the migrated catalog works: analysis and a scan of the (now empty) drive
    analysis.build(cat)
    j = scanner.Job("scan")
    scanner.Scanner(cat, j).run()
    assert j.status == "done"
    cat.close()

    # opening again does not migrate or back up again
    cat2 = catalog.Catalog(root)
    assert cat2.migration["backup"] is None and cat2.migration["from"] == schema.SCHEMA_VERSION


def test_fresh_catalog_gets_current_schema(tmp_path):
    root = str(tmp_path)
    catalog.init_volume(root, "New", "main")
    cat = catalog.Catalog(root)
    assert cat.migration["backup"] is None
    assert user_version(root) == schema.SCHEMA_VERSION
    assert cat.get_meta("hash_algorithm") == schema.HASH_ALGORITHM
    # no migration entry in the journal of a brand new catalog
    assert cat.q1("SELECT COUNT(*) FROM journal")[0] == 0


def test_fresh_and_migrated_schemas_are_identical(tmp_path):
    def tables(cat):
        return {
            r["name"]: sorted((c[1], c[2], c[3], c[4]) for c in cat.q(f"PRAGMA table_info({r['name']})"))
            for r in cat.q("SELECT name FROM sqlite_master WHERE type='table'")
        }

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    catalog.init_volume(str(fresh), "F", "main")
    migrated = drive_from_fixture(tmp_path)
    assert tables(catalog.Catalog(str(fresh))) == tables(catalog.Catalog(migrated))


def _bump(root, version=None, compat=None):
    c = sqlite3.connect(os.path.join(root, ".datacatalog", "catalog.db"))
    if version is not None:
        c.execute(f"PRAGMA user_version = {version}")
    if compat is not None:
        c.execute("UPDATE meta SET value=? WHERE key='schema_compat'", (json.dumps(compat),))
    c.commit()
    c.close()


def test_newer_compatible_catalog_opens_without_migrating(tmp_path):
    root = str(tmp_path)
    catalog.init_volume(root, "X", "main")
    catalog.Catalog(root).close()
    _bump(root, version=schema.SCHEMA_VERSION + 5)
    cat = catalog.Catalog(root)
    assert cat.migration["newer"] is True
    assert user_version(root) == schema.SCHEMA_VERSION + 5  # never migrated down


def test_newer_incompatible_catalog_is_refused(tmp_path):
    root = str(tmp_path)
    catalog.init_volume(root, "X", "main")
    catalog.Catalog(root).close()
    _bump(root, version=schema.SCHEMA_VERSION + 1, compat=schema.SCHEMA_VERSION + 1)
    with pytest.raises(schema.IncompatibleCatalog, match="newer version"):
        catalog.Catalog(root)
    # also refused early from volume.json alone
    info = catalog.read_volume_info(root)
    info["schema_compat"] = schema.SCHEMA_VERSION + 1
    catalog.write_volume_info(root, info)
    with pytest.raises(schema.IncompatibleCatalog):
        catalog.Catalog(root)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    root = drive_from_fixture(tmp_path)

    def boom(conn):
        conn.execute("ALTER TABLE files ADD COLUMN extra TEXT")
        raise RuntimeError("simulated failure")

    monkeypatch.setitem(schema.MIGRATIONS, 2, (boom, False))
    with pytest.raises(RuntimeError):
        catalog.Catalog(root)
    assert user_version(root) == 0
    c = sqlite3.connect(os.path.join(root, ".datacatalog", "catalog.db"))
    assert "extra" not in {r[1] for r in c.execute("PRAGMA table_info(files)")}
    c.close()


def test_other_checksum_algorithm_is_not_mixed(tmp_path):
    root = str(tmp_path)
    catalog.init_volume(root, "X", "main")
    cat = catalog.Catalog(root)
    cat.set_meta("hash_algorithm", "blake3-256")
    j = scanner.Job("scan")
    scanner.Scanner(cat, j).run()
    assert j.status == "failed" and "blake3-256" in j.message


def test_new_identity_clears_sync_history(tmp_path, monkeypatch):
    root = drive_from_fixture(tmp_path)
    cat = catalog.Catalog(root)
    old = cat.info["volume_id"]
    cat.new_identity()
    assert cat.info["volume_id"] != old
    assert cat.partner() is None
    assert cat.q1("SELECT COUNT(*) FROM partner_files")[0] == 0
    assert cat.q1("SELECT COUNT(*) FROM files")[0] == 5  # file list kept
    assert catalog.read_volume_info(root)["volume_id"] == cat.info["volume_id"]


def test_identity_mismatch_detected(tmp_path, monkeypatch):
    from datacatalog import drives

    root = str(tmp_path)
    catalog.init_volume(root, "X", "main")
    monkeypatch.setattr(drives, "volume_serial", lambda r: "AAAA1111")
    catalog.Catalog(root).close()
    assert catalog.read_volume_info(root)["serial"] == "AAAA1111"
    monkeypatch.setattr(drives, "volume_serial", lambda r: "BBBB2222")
    cat = catalog.Catalog(root)
    assert cat.identity_mismatch == {"stored": "AAAA1111", "current": "BBBB2222"}
    cat.adopt_identity()
    assert catalog.read_volume_info(root)["serial"] == "BBBB2222"
    assert catalog.Catalog(root).identity_mismatch is None
