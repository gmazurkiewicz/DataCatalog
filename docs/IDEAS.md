# Ideas for the future

Not planned yet; collected so they are not forgotten.

## Catalog size housekeeping

The catalog grows with the **number of files**, not with the amount of data. Measured on a
generated catalog with realistic paths (see "Catalog size" in the README):

- about 0.4–0.6 KB per file for the file list with its indexes,
- about 0.7–1 KB per file once a partner snapshot exists (after the first sync).

For 1 TB this is ~10 MB (mostly movies, ~10k files), ~100–200 MB (photos and music, ~200k files)
or ~0.5–1 GB (many small files, ~1M files) - roughly 0.01–0.1 % of the data. Things that grow it
temporarily, and could be cleaned up:

- **Clear the sync plan after a successful run and compact the database.** The first sync puts
  every file into `sync_plan` (+30–50 %). SQLite reuses freed pages but never returns them to the
  file system without `VACUUM`. Idea: after a sync with no failed items, delete the plan rows and
  run `VACUUM` (or offer a "Compact database" button in Settings, with the current size shown).
- **Keep the raw smartctl output only for the last N readings.** Each SMART reading stores the full
  smartctl JSON (20–50 KB). The evaluated summary and the history are small and should be kept
  forever; the raw JSON is only useful for the most recent readings (e.g. the last 20).
- `catalog.db-wal` grows during long scans and is truncated automatically at checkpoints; nothing
  to do, but worth knowing when looking at the folder.
