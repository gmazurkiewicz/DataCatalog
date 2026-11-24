# DataCatalog

A tool for organizing data on external drives: checksums for every file, finding duplicates
(including whole folders), safe removal through a quarantine, and detection of silent file
corruption. Runs on Windows and Linux (including Raspberry Pi). The UI opens in a web browser.

The runtime uses the Python standard library only (Python 3.10+). Drive health additionally
needs the external `smartctl` program from [smartmontools](https://www.smartmontools.org). The project is managed with
[uv](https://docs.astral.sh/uv/), but plain Python works too.

## Status

- **Stage 1 (done):** scanning, duplicate analysis, quarantine, integrity verification, web UI.
- **Stage 2 (done):** comparing and synchronizing the main drive with its backup (one-way mirror A → B).
- **Stage 3 (done):** drive health (SMART via `smartmontools`), name-collision check, verification reminder.

## Running

**With uv (recommended)**: uv downloads a suitable Python by itself, nothing else to install.
```
uv run datacatalog                      # Windows: the browser opens automatically
uv run datacatalog --help
```
To install it as a command available everywhere (e.g. on the Pi): `uv tool install .`

**Without uv**: any Python 3.10+.
```
python run.py                           # or: python -m datacatalog
```

**Raspberry Pi / Linux**
```bash
# mount the NTFS drive with the fast kernel driver (kernel 5.15+)
sudo mkdir -p /mnt/drive
sudo mount -t ntfs3 -o uid=$(id -u),gid=$(id -g) /dev/sda1 /mnt/drive

uv run datacatalog --host 0.0.0.0 --password YOUR_PASSWORD --no-browser
```
Then open `http://<pi-address>:8765` on another computer (user `admin`, the password above).

Options: `--port 8765`, `--open E:\` (open a drive right away), `--no-browser`, `--version`.

## Workflow

1. **Drives**: pick a drive and click "Initialize". You give it a name and a role (Main / Backup).
   A hidden `.datacatalog` folder with the database is created on the drive. Your files are not touched.
2. **Scan**: the first scan computes checksums of all files. For 1 TB over USB this takes several
   hours. It can be stopped and resumed later, even on a different computer, because the catalog lives
   on the drive. Later scans only hash new and changed files.
3. **Duplicate tree**: folders that contain duplicates. "Has a copy outside the folder" shows how much
   of a folder exists elsewhere too. A folder marked "100% copy" can be removed as a whole without
   losing data. Select folders or files and click "Check and move to quarantine".
4. **Duplicate files**: a flat table of every file that has a duplicate, sortable by name, folder,
   size, modification time and number of copies; click a folder to filter by it, or the number of
   copies to see the whole group. **Duplicate groups / Identical folders**: other views of the same
   data, with a "keep the first copy, select the rest" shortcut.
   Empty (0-byte) files are not treated as duplicates by default - they carry no data. Tick
   "Include empty files" in the duplicate tree to count them too.
5. **Quarantine**: moved files can be restored. Permanent deletion requires typing `DELETE`.
6. **Verify**: from time to time, re-reads every file and compares checksums. A file with the same
   size and modification time but different content is listed under **Problems**. If a good copy
   exists on the drive, it can be repaired with one click.

## Synchronization (Sync tab)

The main drive is mirrored to the backup drive. Open the main drive, connect the backup and pick it
on the Sync tab (or initialize it there as "Backup"). **Compare** scans both drives incrementally and
builds a plan. Nothing changes until you review it and click **Run sync**.

| Difference | Default action |
|---|---|
| New on main | copy to backup |
| Changed on main (since the last sync) | copy to backup, the old version goes to the backup's quarantine |
| Moved or renamed on main | rename on backup (no copying) |
| Removed from main by DataCatalog, or still on main at another path | move to the backup's quarantine |
| Missing on main without a recorded deletion | copy back from backup |
| Only on backup | copy to main (or remove from backup) |
| Corrupted on one drive, good on the other | repair from the good copy |
| Changed on backup, or on both drives | conflict - skipped until you choose |
| Different content, no sync history | main wins |

Every action can be changed per difference type or per file. After a run each drive stores a
snapshot of the other drive's file list; this is how the next compare knows which side changed.
Every copied file is hashed while copying and must match its catalog checksum, it is written to a
temporary name, flushed to disk and only then renamed into place. Modification times are preserved.

## Drive health (Health tab)

Reads SMART data with `smartctl` and stores every reading in the drive's catalog, so the history
travels with the drive. The verdict (Good / Warning / Failing) is based on the overall SMART status,
attributes below their thresholds, reallocated / pending / uncorrectable sectors, NVMe media errors
and spare, temperature and the last self-test - and on **growth** of the bad counters since the
previous check, which is the most useful early warning. Short and extended self-tests can be started
from the tab.

Setup:
- **Windows:** install smartmontools from smartmontools.org and start DataCatalog as administrator.
  The drive is addressed by its letter (`E:`).
- **Raspberry Pi / Linux:** `sudo apt install smartmontools`. smartctl needs root: run DataCatalog as
  root, or allow it without a password (`sudo visudo -f /etc/sudoers.d/smartctl`, line
  `youruser ALL=(root) NOPASSWD: /usr/sbin/smartctl`); DataCatalog then uses `sudo -n smartctl`.
- USB enclosures often hide SMART; DataCatalog retries with `-d sat`. Some enclosures do not pass
  SMART through at all - then only Verify can tell you about the data.

The Problems tab also lists **name collisions**: names in one folder that differ only by letter case.
Linux can create them on NTFS, Windows cannot handle them.

## Safety rules

- A file is moved to quarantine only if at least one of its copies stays on the drive and is not
  selected. Selecting every copy is blocked.
- Right before moving, the app checks that the kept copy still exists and has not changed, and that
  the file being moved has not changed since the scan.
- A file with suspected corruption is never treated as a "good copy".
- The quarantine is on the same drive, so moves are instant and reversible.
- Every operation is recorded in the journal (`journal` table). Synchronization uses it to tell
  deliberate deletions apart from files that vanished.
- Synchronization never deletes permanently by default: removed and replaced files go to the target
  drive's quarantine. A file that changed after compare is skipped.
- By default the server only listens on this computer (127.0.0.1). Network access requires a password.

## Design notes

- The catalog (`.datacatalog/catalog.db`, SQLite) is stored on the drive itself, so it travels with
  the drive between Windows and the Pi. Paths are stored relative to the drive root with `/`.
- Drives are identified by a `volume_id` in `.datacatalog/volume.json`, not by drive letter or mount point.
- Checksums: BLAKE2b-256 (Python standard library). Over USB the bottleneck is disk reads, not the CPU.
- Skipped: symlinks, `System Volume Information`, `$RECYCLE.BIN` and `.datacatalog` itself.

## Project layout

```
pyproject.toml              project metadata, `datacatalog` command, dev tools (uv)
run.py                      run from a checkout without installing
datacatalog/cli.py          command line options, starts the server
datacatalog/catalog.py      SQLite schema and catalog access
datacatalog/drives.py       drive detection (Windows: ctypes, Linux: /proc/mounts + lsblk)
datacatalog/scanner.py      incremental scan, hashing, verification
datacatalog/analysis.py     per-folder duplicate statistics, identical folders
datacatalog/quarantine.py   safe move/restore/purge, repair from a good copy
datacatalog/sync.py         compare plan and verified one-way mirror to the backup drive
datacatalog/health.py       SMART readings via smartctl, evaluation and history
datacatalog/server.py       HTTP JSON API + static files
datacatalog/static/         web UI (plain HTML/CSS/JS, no build step)
tests/                      pytest tests
```

## Testing

Automated: `uv run pytest`. Manual test plan for real drives: [docs/TESTING.md](docs/TESTING.md).

## Development

```
uv sync                     # creates .venv with pytest and ruff (dev group)
uv run pytest               # tests
uv run ruff check .         # lint
uv run ruff format .        # format
```

Runtime dependencies must stay empty (`dependencies = []` in `pyproject.toml`), so the app keeps
running with nothing but Python. Development tools go to the `dev` dependency group.
