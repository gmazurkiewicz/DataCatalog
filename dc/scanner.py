"""Scanner: incremental drive scan, checksum calculation and verification.

Scan phases:
  1. walk: record each file's path, size and modification time (fast),
  2. hash files that have no checksum yet (new or changed) - files that may be
     duplicates (same size as another file) go first, so the duplicate views
     become useful as early as possible.

Stopping a scan (Stop button, drive unplugged) loses no work: files already
hashed keep their checksum in the database and the next scan finishes the rest.

Verify mode re-reads every file and compares checksums. A file with the same
size and modification time but a different checksum has been silently corrupted.
"""
import hashlib
import os
import stat as stat_mod
import threading
import time

from . import catalog as catmod
from . import paths

CHUNK = 1024 * 1024
MTIME_TOLERANCE_NS = 1_000_000  # 1 ms - systems round timestamps differently


def hash_file(os_path, cancel=None, on_bytes=None):
    h = hashlib.blake2b(digest_size=32)
    buf = bytearray(CHUNK)
    mv = memoryview(buf)
    with open(os_path, "rb", buffering=0) as f:
        while True:
            n = f.readinto(buf)
            if not n:
                break
            h.update(mv[:n])
            if on_bytes:
                on_bytes(n)
            if cancel is not None and cancel.is_set():
                return None
    return h.hexdigest()


def same_stat(size_a, mtime_a, size_b, mtime_b):
    return size_a == size_b and abs(mtime_a - mtime_b) <= MTIME_TOLERANCE_NS


class Job:
    """Job state shown in the UI."""

    def __init__(self, mode):
        self.mode = mode
        self.phase = "start"
        self.started_at = time.time()
        self.finished_at = None
        self.status = "running"
        self.message = ""
        self.cancel = threading.Event()
        self.current = ""
        self.files_seen = 0
        self.bytes_seen = 0
        self.to_hash_files = 0
        self.to_hash_bytes = 0
        self.files_hashed = 0
        self.bytes_hashed = 0
        self.new_files = 0
        self.changed_files = 0
        self.missing_files = 0
        self.errors = 0
        self.corrupt = 0
        self.scan_id = None

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if k != "cancel"}
        elapsed = (self.finished_at or time.time()) - self.started_at
        d["elapsed"] = elapsed
        d["rate"] = self.bytes_hashed / elapsed if elapsed > 0 else 0
        return d


class Scanner:
    def __init__(self, cat: catmod.Catalog, job: Job):
        self.cat = cat
        self.job = job
        self.excl = {e.strip().strip("/").lower() for e in cat.excludes() if e.strip()}
        self.only = [p.strip().strip("/") for p in cat.get_meta("include", []) if p.strip()]

    # --- helpers ---
    def _error(self, rel, err):
        self.job.errors += 1
        with self.cat.lock:
            self.cat.conn.execute(
                "INSERT INTO scan_errors(scan_id,path,error,ts) VALUES(?,?,?,?)",
                (self.job.scan_id, rel, str(err), time.time()),
            )

    def _excluded(self, rel, name):
        low = name.lower()
        if low in paths.ALWAYS_EXCLUDED or ("/" not in rel and low in paths.ROOT_EXCLUDED):
            return True
        return rel.lower() in self.excl

    def _start(self):
        with self.cat.lock:
            cur = self.cat.conn.execute(
                "INSERT INTO scans(mode,host,started_at,status) VALUES(?,?,?,'running')",
                (self.job.mode, catmod.hostname(), self.job.started_at),
            )
            self.cat.conn.commit()
        self.job.scan_id = cur.lastrowid

    def _finish(self, status):
        j = self.job
        j.status = status
        j.finished_at = time.time()
        j.phase = "done"
        with self.cat.lock:
            self.cat.conn.execute(
                """UPDATE scans SET finished_at=?, status=?, files_seen=?, bytes_seen=?,
                   files_hashed=?, bytes_hashed=?, new_files=?, changed_files=?,
                   missing_files=?, errors=?, corrupt=? WHERE id=?""",
                (j.finished_at, status, j.files_seen, j.bytes_seen, j.files_hashed,
                 j.bytes_hashed, j.new_files, j.changed_files, j.missing_files,
                 j.errors, j.corrupt, j.scan_id),
            )
            self.cat.conn.commit()

    # --- phase 1: walk directories ---
    def walk(self):
        j = self.job
        j.phase = "walk"
        scan_id = j.scan_id
        root = self.cat.root
        conn = self.cat.conn
        pending = 0
        stack = self.only[:] if self.only else [""]
        while stack:
            if j.cancel.is_set():
                return False
            rel_dir = stack.pop()
            j.current = rel_dir or "/"
            try:
                with os.scandir(paths.to_os(root, rel_dir)) as it:
                    entries = list(it)
            except OSError as e:
                self._error(rel_dir or "/", e)
                continue
            with self.cat.lock:
                known = {
                    r["name"]: r for r in conn.execute(
                        "SELECT id,name,size,mtime_ns,hash,status FROM files WHERE dir=?", (rel_dir,)
                    )
                }
            rows_new, rows_seen, rows_changed = [], [], []
            for e in entries:
                rel = paths.join(rel_dir, e.name)
                try:
                    if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if not self._excluded(rel, e.name):
                            stack.append(rel)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError as err:
                    self._error(rel, err)
                    continue
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
                j.files_seen += 1
                j.bytes_seen += st.st_size
                k = known.get(e.name)
                if k is None:
                    rows_new.append((rel, rel_dir, e.name, st.st_size, st.st_mtime_ns, scan_id))
                    j.new_files += 1
                elif k["status"] == "quarantined":
                    # a file with this name reappeared - treat it as new
                    rows_changed.append((st.st_size, st.st_mtime_ns, scan_id, k["id"]))
                    j.new_files += 1
                elif same_stat(k["size"], k["mtime_ns"], st.st_size, st.st_mtime_ns) and k["hash"] is not None:
                    rows_seen.append((scan_id, k["id"]))
                else:
                    rows_changed.append((st.st_size, st.st_mtime_ns, scan_id, k["id"]))
                    if k["hash"] is not None:
                        j.changed_files += 1
            with self.cat.lock:
                conn.executemany(
                    "INSERT INTO files(path,dir,name,size,mtime_ns,seen_scan,status) VALUES(?,?,?,?,?,?,'ok')",
                    rows_new,
                )
                conn.executemany("UPDATE files SET seen_scan=?, status='ok' WHERE id=?", rows_seen)
                conn.executemany(
                    """UPDATE files SET size=?, mtime_ns=?, hash=NULL, hashed_at=NULL,
                       seen_scan=?, status='ok', q_batch=NULL, corrupt=0 WHERE id=?""",
                    rows_changed,
                )
                pending += len(rows_new) + len(rows_seen) + len(rows_changed)
                if pending > 5000:
                    conn.commit()
                    pending = 0
        with self.cat.lock:
            # Files not found during a complete walk are marked as missing.
            if self.only:
                conds, args = [], [scan_id]
                for p in self.only:
                    lo, hi = paths.prefix_range(p)
                    conds.append("(path >= ? AND path < ?)")
                    args += [lo, hi]
                where = " AND (" + " OR ".join(conds) + ")"
            else:
                where, args = "", [scan_id]
            cur = conn.execute(
                f"UPDATE files SET status='missing' WHERE status='ok' AND seen_scan IS NOT ?{where}", args
            )
            j.missing_files = cur.rowcount
            conn.commit()
        return True

    # --- phase 2: hashing ---
    def hash_pending(self):
        j = self.job
        j.phase = "hash"
        conn = self.cat.conn
        with self.cat.lock:
            rows = conn.execute(
                """SELECT id, path, size, mtime_ns FROM files
                   WHERE status='ok' AND hash IS NULL
                   ORDER BY CASE WHEN size > 0 AND size IN (
                        SELECT size FROM files WHERE status='ok' GROUP BY size HAVING COUNT(*) > 1)
                        THEN 0 ELSE 1 END, path"""
            ).fetchall()
        j.to_hash_files = len(rows)
        j.to_hash_bytes = sum(r["size"] for r in rows)
        return self._hash_rows(rows, verify=False)

    def _hash_rows(self, rows, verify):
        j = self.job
        conn = self.cat.conn
        root = self.cat.root
        pending = 0

        def on_bytes(n):
            j.bytes_hashed += n

        for r in rows:
            if j.cancel.is_set():
                with self.cat.lock:
                    conn.commit()
                return False
            rel = r["path"]
            j.current = rel
            osp = paths.to_os(root, rel)
            try:
                st_before = os.stat(osp)
                digest = hash_file(osp, j.cancel, on_bytes)
                if digest is None:
                    continue
                st_after = os.stat(osp)
            except OSError as e:
                self._error(rel, e)
                continue
            j.files_hashed += 1
            now = time.time()
            unchanged_during = same_stat(st_before.st_size, st_before.st_mtime_ns,
                                         st_after.st_size, st_after.st_mtime_ns)
            with self.cat.lock:
                if not verify:
                    if unchanged_during:
                        conn.execute(
                            "UPDATE files SET hash=?, hashed_at=?, verified_at=?, size=?, mtime_ns=? WHERE id=?",
                            (digest, now, now, st_after.st_size, st_after.st_mtime_ns, r["id"]),
                        )
                else:
                    if not unchanged_during:
                        pass
                    elif not same_stat(r["size"], r["mtime_ns"], st_after.st_size, st_after.st_mtime_ns):
                        # regular change (different time/size) - update the checksum
                        j.changed_files += 1
                        conn.execute(
                            "UPDATE files SET hash=?, hashed_at=?, verified_at=?, size=?, mtime_ns=?, corrupt=0 WHERE id=?",
                            (digest, now, now, st_after.st_size, st_after.st_mtime_ns, r["id"]),
                        )
                    elif digest != r["hash"]:
                        # same size and time, different content -> suspected corruption
                        j.corrupt += 1
                        exists = conn.execute(
                            "SELECT 1 FROM integrity_issues WHERE path=? AND actual=? AND resolved=0",
                            (rel, digest),
                        ).fetchone()
                        if not exists:
                            conn.execute(
                                """INSERT INTO integrity_issues(path,expected,actual,size,detected_at,scan_id)
                                   VALUES(?,?,?,?,?,?)""",
                                (rel, r["hash"], digest, r["size"], now, j.scan_id),
                            )
                        conn.execute("UPDATE files SET corrupt=1 WHERE id=?", (r["id"],))
                    else:
                        conn.execute("UPDATE files SET verified_at=? WHERE id=?", (now, r["id"]))
                        if r["corrupt"]:
                            # content is correct again (e.g. repaired outside the app)
                            conn.execute("UPDATE files SET corrupt=0 WHERE id=?", (r["id"],))
                            conn.execute("UPDATE integrity_issues SET resolved=1 WHERE path=? AND resolved=0", (rel,))
                pending += 1
                if pending >= 200:
                    conn.commit()
                    pending = 0
        with self.cat.lock:
            conn.commit()
        return True

    # --- verification ---
    def verify(self):
        j = self.job
        j.phase = "verify"
        with self.cat.lock:
            rows = self.cat.conn.execute(
                "SELECT id, path, size, mtime_ns, hash, corrupt FROM files WHERE status='ok' AND hash IS NOT NULL ORDER BY path"
            ).fetchall()
        j.to_hash_files = len(rows)
        j.to_hash_bytes = sum(r["size"] for r in rows)
        return self._hash_rows(rows, verify=True)

    def run(self):
        self._start()
        try:
            if self.job.mode == "verify":
                ok = self.verify()
            else:
                ok = self.walk() and self.hash_pending()
            self._finish("done" if ok else "stopped")
        except Exception as e:  # e.g. drive unplugged mid-scan
            self.job.message = f"{type(e).__name__}: {e}"
            try:
                self._finish("failed")
            except Exception:
                self.job.status = "failed"
                self.job.finished_at = time.time()
        return self.job
