# DataCatalog

A tool for organizing data on external drives: checksums for every file, finding duplicates
(including whole folders), safe removal through a quarantine, and detection of silent file
corruption. Runs on Windows and Linux (including Raspberry Pi). The UI opens in a web browser.

Requires only **Python 3.10+**, with no `pip install` needed.

## Status

**Stage 1 (done):** scanning, duplicate analysis, quarantine, integrity verification, web UI.
**Stage 2 (planned):** comparing and synchronizing the main drive with its backup (A ↔ B).
**Stage 3 (planned):** drive health (SMART via `smartmontools`).

## Running

**Windows**
1. Install Python from python.org and tick "Add python.exe to PATH" in the installer.
2. In the project folder run: `python run.py`. The browser opens automatically.

**Raspberry Pi / Linux**
```bash
# mount the NTFS drive with the fast kernel driver (kernel 5.15+)
sudo mkdir -p /mnt/drive
sudo mount -t ntfs3 -o uid=$(id -u),gid=$(id -g) /dev/sda1 /mnt/drive

python3 run.py --host 0.0.0.0 --password YOUR_PASSWORD --no-browser
```
Then open `http://<pi-address>:8765` on another computer (user `admin`, the password above).

Options: `--port 8765`, `--open E:\` (open a drive right away), `--no-browser`.

## Workflow

1. **Drives**: pick a drive and click "Initialize". You give it a name and a role (Main / Backup).
   A hidden `.datacatalog` folder with the database is created on the drive. Your files are not touched.
2. **Scan**: the first scan computes checksums of all files. For 1 TB over USB this takes several
   hours. It can be stopped and resumed later, even on a different computer, because the catalog lives
   on the drive. Later scans only hash new and changed files.
3. **Duplicate tree**: folders that contain duplicates. "Has a copy outside the folder" shows how much
   of a folder exists elsewhere too. A folder marked "100% copy" can be removed as a whole without
   losing data. Select folders or files and click "Check and move to quarantine".
4. **Duplicate groups / Identical folders**: other views of the same data, with a
   "keep the first copy, select the rest" shortcut.
5. **Quarantine**: moved files can be restored. Permanent deletion requires typing `DELETE`.
6. **Verify**: from time to time, re-reads every file and compares checksums. A file with the same
   size and modification time but different content is listed under **Problems**. If a good copy
   exists on the drive, it can be repaired with one click.

## Safety rules

- A file is moved to quarantine only if at least one of its copies stays on the drive and is not
  selected. Selecting every copy is blocked.
- Right before moving, the app checks that the kept copy still exists and has not changed, and that
  the file being moved has not changed since the scan.
- A file with suspected corruption is never treated as a "good copy".
- The quarantine is on the same drive, so moves are instant and reversible.
- Every operation is recorded in the journal (`journal` table). Stage 2 synchronization will use it to
  tell deliberate deletions apart from files that vanished.
- By default the server only listens on this computer (127.0.0.1). Network access requires a password.

## Design notes

- The catalog (`.datacatalog/catalog.db`, SQLite) is stored on the drive itself, so it travels with
  the drive between Windows and the Pi. Paths are stored relative to the drive root with `/`.
- Drives are identified by a `volume_id` in `.datacatalog/volume.json`, not by drive letter or mount point.
- Checksums: BLAKE2b-256 (Python standard library). Over USB the bottleneck is disk reads, not the CPU.
- Skipped: symlinks, `System Volume Information`, `$RECYCLE.BIN` and `.datacatalog` itself.

## Project layout

```
run.py              entry point (CLI options, starts the server)
dc/catalog.py       SQLite schema and catalog access
dc/drives.py        drive detection (Windows: ctypes, Linux: /proc/mounts + lsblk)
dc/scanner.py       incremental scan, hashing, verification
dc/analysis.py      per-folder duplicate statistics, identical folders
dc/quarantine.py    safe move/restore/purge, repair from a good copy
dc/server.py        HTTP JSON API + static files
dc/static/          web UI (plain HTML/CSS/JS, no build step)
tests/              pytest tests
```

## Tests

```
python -m pytest tests
```
