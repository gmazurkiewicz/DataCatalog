# Manual test plan

Automated tests (`uv run pytest`) cover the logic on temporary folders. This checklist covers what
only real hardware can show: Windows drive letters, NTFS on the Raspberry Pi, USB enclosures,
SMART and long runs. Tick the boxes as you go.

> Before you start: the app never deletes anything without asking, but test on data you have a
> copy of. A good idea is to start with a small test folder on the real drive (Settings →
> "Scan only these folders"), then widen the scope.

## 1. Setup

- [ ] `uv sync` and `uv run pytest` pass on Windows.
- [ ] `uv run datacatalog` opens the browser at http://127.0.0.1:8765.
- [ ] `python run.py` works too (without uv).

## 2. Drives and first scan (main drive, Windows)

- [ ] The external drive appears on the Drives tab with letter, label, NTFS, size and free space.
- [ ] Initialize it as **Main**. Only a hidden `.datacatalog` folder appears on the drive.
- [ ] Settings → limit the scope to one test folder → Scan. Progress, speed and ETA look plausible.
- [ ] Stop the scan halfway, start it again: it continues, already hashed files are not re-read.
- [ ] Unplug the drive during a scan: the app shows an error but keeps running; after re-plugging,
      Scan continues.
- [ ] Clear the scope (whole drive) and run the full first scan. Note the time: ______

## 3. Duplicates

- [ ] Duplicate tree: folders you know are copies show **100% copy**.
- [ ] Expand a folder, click "N copies" on a file: the list of copies is right.
- [ ] Select a folder that also contains unique files: the plan says they will stay in place.
- [ ] Select both copies of something: the app refuses to move all of them.
- [ ] Move a folder to quarantine; it disappears from Explorer and shows on the Quarantine tab.
- [ ] Restore the batch: files are back with the same dates.
- [ ] Move again and "Delete permanently" (type DELETE) on something you really want gone.
- [ ] Duplicate groups and Identical folders show sensible results; "Keep first" selects the rest.

## 4. Integrity

- [ ] Run **Verify** on a small scope; it finishes with 0 corrupted.
- [ ] Simulate corruption of a test file that has a duplicate: in PowerShell save its time with
      `$t = (Get-Item f.jpg).LastWriteTime`, change one byte with a hex editor, then put the time back
      with `(Get-Item f.jpg).LastWriteTime = $t`. Verify reports it under Problems; "Repair from copy"
      restores it from the duplicate.
- [ ] "I changed it" accepts an intentional change.
- [ ] Delete a file in Explorer, Scan: it appears under Missing files.

## 5. Synchronization (main + backup)

- [ ] Connect the backup drive. Sync tab → pick it ("Initialize and use", role Backup).
- [ ] Compare scans the backup (first time: hours) and builds a plan. Note the time: ______
- [ ] Review "Only on backup": these are probably files deleted from main over the years.
      Decide per type or per file (copy to main / remove from backup).
- [ ] Run the sync. Check a few copied files open fine on the backup.
- [ ] Compare again: "The drives are in sync".
- [ ] On main: rename a folder, edit a file, add a file, quarantine a duplicate. Compare: rename shows
      as "Moved or renamed" (no copying), the others as expected. Run.
- [ ] Edit the same file on both drives: it shows as a conflict and is skipped until you choose.
- [ ] Open the backup drive as the current drive: its Quarantine tab shows `sync-...` batches with
      removed files and replaced older versions.

## 6. Raspberry Pi

- [ ] Mount the drive with `ntfs3` (README), run `uv run datacatalog --host 0.0.0.0 --password ...`.
- [ ] Open the UI from the PC, log in as `admin`.
- [ ] The drive is recognized as the same drive (name, role, last scan) as on Windows.
- [ ] Scan on the Pi: no re-hashing of unchanged files (catalog reused across systems).
- [ ] Compare/sync with both drives on the Pi.
- [ ] Problems → Name collisions: create `test.txt` and `Test.txt` in one folder on the Pi, Scan,
      the collision is listed. Remove one of them.

## 7. Health (SMART)

- [ ] Windows: install smartmontools, run as administrator → Health → Check now shows model, serial,
      temperature, power-on hours and attributes.
- [ ] Pi: with the sudoers rule, Check now works without running the app as root.
- [ ] Check now twice: the history table grows.
- [ ] Start a short self-test, wait ~2 minutes, Check now: the last self-test result is shown.
- [ ] If an enclosure does not pass SMART, the message says so (note the enclosure model): ______

## Report

For anything that fails, note: what you did, what you expected, what happened, the message shown
and the console output of `uv run datacatalog`.
