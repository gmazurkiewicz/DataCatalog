# Catalog data format and compatibility

Every drive carries its own catalog in `.datacatalog/` on the drive itself. A catalog may live
for years and be opened by different versions of DataCatalog - for example an updated app on
Windows and an older one on the Raspberry Pi. This document describes the format and the rules
that keep old catalogs and old apps working.

## Files in `.datacatalog/`

| File | Purpose |
|---|---|
| `volume.json` | Drive identity and a copy of the format version (readable without SQLite) |
| `catalog.db` | SQLite database (WAL mode; `catalog.db-wal` / `-shm` are temporary) |
| `catalog-vN.bak.db` | Copy made automatically before upgrading from format version N |
| `quarantine/<batch>/...` | Files moved to quarantine, with their original relative paths |

### volume.json

| Key | Meaning |
|---|---|
| `volume_id` | Random UUID created at initialization - the drive's identity (not the drive letter or mount point) |
| `name`, `role` | User-given name; `main`, `backup` or `other` |
| `serial` | File system serial / UUID seen when the catalog was created; used to notice a catalog copied to another drive |
| `schema`, `schema_compat` | Format version and compatibility level (see below), duplicated from the database |
| `app_version` | Version of DataCatalog that last upgraded or wrote the catalog |

## Versioning

| Value | Where | Meaning |
|---|---|---|
| schema version | `PRAGMA user_version` | Which migrations have been applied (0 = catalog from before versioning = v1) |
| compat level | meta `schema_compat`, `volume.json` | The oldest schema version whose apps may still open and write this catalog |

An app with schema version S opens a catalog when `compat <= S`:

- catalog version **< S**: a backup copy is made, then the missing migrations run one by one,
  each in its own transaction (a failed migration leaves the catalog untouched);
- catalog version **= S**: normal case;
- catalog version **> S**: it was changed by a newer app, but only additively - the older app
  works with the parts it knows and never migrates down; the UI suggests updating.

When `compat > S` the app refuses to open the drive and asks to be updated - this protects old
catalogs from being misread or overwritten by an old app.

Version history:

| Version | Changes | Breaking |
|---|---|---|
| 1 | Stages 1-3 (catalogs created before versioning) | - |
| 2 | `schema_compat`, `hash_algorithm`, `last_writer` meta; `files.extra`; `scans.app_version`; `sync_plan` and `smart_readings` created up front | no |

## Rules for future changes

1. **Prefer additive changes**: new tables, new columns with a default, new meta keys. They keep
   the compat level, so not-yet-updated apps keep working with the drive.
2. **Never change the meaning or format** of an existing column, table or meta key. If it is
   unavoidable, register the migration with `breaking=True`: it raises the compat level so older
   apps refuse the catalog instead of misreading it.
3. Migrations are idempotent (check before `ALTER`, use `IF NOT EXISTS`) and run inside one
   transaction; `executescript` must not be used inside them (it commits).
4. Keep `tests/fixtures/catalog_v1` forever. When the format changes, also add a fixture of the
   previous version and a test that migrates it and checks that the data survived. A test also
   checks that a fresh catalog and a migrated one end up with identical tables.

## Built in now so later changes stay non-invasive

- **Stable file ids**: `files.id` is an `INTEGER PRIMARY KEY` (kept by `VACUUM`) and survives
  renames done by the app. Future per-file data (tags, media metadata, ...) should go into new
  tables referencing `files(id)`.
- **`files.extra`**: a JSON text column for small per-file attributes that do not deserve a table.
- **`hash_algorithm`** meta (`blake2b-256`): a future app can switch algorithms for new catalogs
  without misreading old ones. Scanning refuses a catalog with an unknown algorithm, and sync
  refuses two drives with different algorithms.
- **`last_writer`** meta and `scans.app_version`: which app version wrote what, for diagnosis.
- **Drive identity** (`volume_id` + `serial`): a catalog copied to another drive (e.g. a clone
  made with another tool) is detected; the user chooses "same data, new drive" (keep identity)
  or "different drive" (new identity, sync history cleared).
- **Journal**: every change made by the app (quarantine, restore, purge, sync, repair,
  migration) is recorded; synchronization depends on it, so entries are only ever appended.
- **Paths** are stored relative to the drive root, with `/`, exactly as the file system returns
  them (no Unicode normalization). Times: `mtime_ns` integer nanoseconds from the file system,
  other timestamps are Unix seconds (REAL).

## Main tables (v2)

| Table | Content |
|---|---|
| `files` | One row per file: path, dir, name, size, mtime_ns, hash, status (`ok` / `missing` / `quarantined`), corrupt flag, quarantine batch, extra |
| `scans`, `scan_errors` | Scan and verify runs with counters; errors per path |
| `integrity_issues` | Suspected corruption: expected vs actual checksum |
| `journal` | Append-only log of operations |
| `q_batches` | Quarantine batches |
| `partner_files` | Snapshot of the partner drive's files from the last sync |
| `sync_plan` | The current compare result and the user's choices |
| `smart_readings` | SMART readings: verdict, evaluated summary, raw smartctl JSON |
| `meta` | Key/value settings and state (JSON values) |
