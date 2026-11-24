"""One-way mirror synchronization: main drive (A) -> backup drive (B).

Compare builds a plan stored in A's catalog (table sync_plan); nothing is changed until
the user reviews the plan and runs it.

How differences are classified (A = main, B = backup):

  only on A ............ copy_to_b      new on main -> copy to backup
  only on B
    same content now
    at another path on A move_on_b      file was moved/renamed on main -> rename on backup
    removed on A (journal,
    or still on A elsewhere) delete_on_b removed from main -> move to backup's quarantine
    missing on A (no journal) restore_to_a vanished from main -> copy back from backup
    otherwise ......... only_on_b      only on backup -> by default copied to main
  on both, different content
    corrupted on one side repair_a / repair_b
    changed on A since last sync update_b
    changed on B / both / unknown conflict (waits for a decision)
    first sync ........ differs        by default main wins

"Changed since last sync" is decided with the partner snapshot (table partner_files):
after every synchronization each drive stores the other drive's file list.

Safety:
  * every copied file is hashed while copying and must match the catalog checksum,
  * files are written to a temporary name, fsynced and then renamed into place,
  * a file that gets overwritten or deleted on the target goes to the target's
    quarantine (.datacatalog/quarantine/sync-...), nothing is deleted permanently,
  * before each action the source and target are checked against the catalog
    (size and modification time); anything that changed since compare is skipped.
"""

import hashlib
import os
import shutil
import sqlite3
import threading
import time

from . import catalog as catmod
from . import paths
from .scanner import CHUNK, Job, Scanner, same_stat

KINDS = {
    "copy_to_b": "New on main",
    "update_b": "Changed on main",
    "move_on_b": "Moved or renamed on main",
    "delete_on_b": "Removed from main",
    "repair_b": "Corrupted on backup",
    "repair_a": "Corrupted on main",
    "restore_to_a": "Missing on main",
    "only_on_b": "Only on backup",
    "conflict": "Conflict",
    "differs": "Different content (no sync history)",
    "unrecoverable": "Cannot be repaired",
    "unreadable": "No checksum",
}
# choice -> what happens
CHOICES = {
    "a2b": "copy main -> backup",
    "b2a": "copy backup -> main",
    "move_b": "rename on backup",
    "delete_b": "move to backup's quarantine",
    "skip": "skip",
}
DEFAULTS = {
    "copy_to_b": ("a2b", ["a2b", "skip"]),
    "update_b": ("a2b", ["a2b", "skip"]),
    "move_on_b": ("move_b", ["move_b", "skip"]),
    "delete_on_b": ("delete_b", ["delete_b", "skip"]),
    "repair_b": ("a2b", ["a2b", "skip"]),
    "repair_a": ("b2a", ["b2a", "skip"]),
    "restore_to_a": ("b2a", ["b2a", "delete_b", "skip"]),
    "only_on_b": ("b2a", ["b2a", "delete_b", "skip"]),
    "conflict": ("skip", ["a2b", "b2a", "skip"]),
    "differs": ("a2b", ["a2b", "b2a", "skip"]),
    "unrecoverable": ("skip", ["skip"]),
    "unreadable": ("skip", ["skip"]),
}
REMOVAL_OPS = ("quarantine", "purge", "forget_missing")

PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_plan (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,
    path      TEXT NOT NULL,         -- target path (same on both drives)
    src_path  TEXT,                  -- move_on_b: old path on B; repair_a: copy source on B
    size      INTEGER,
    hash_a    TEXT,
    hash_b    TEXT,
    reason    TEXT,
    choice    TEXT NOT NULL,
    options   TEXT NOT NULL,
    done      INTEGER NOT NULL DEFAULT 0,
    result    TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_kind ON sync_plan(kind);
"""


class SyncError(Exception):
    pass


class StopSync(Exception):
    """Raised to stop the whole run (e.g. target drive is full)."""


# ---------------------------------------------------------------- job state
class SyncJob:
    """Progress of a compare or sync run, in the same shape as scanner.Job.to_dict()."""

    def __init__(self, mode, name_a, name_b):
        self.mode = mode  # compare | sync
        self.name_a, self.name_b = name_a, name_b
        self.phase = "start"
        self.label = ""
        self.status = "running"
        self.message = ""
        self.summary = None
        self.cancel = threading.Event()
        self.started_at = time.time()
        self.finished_at = None
        self.sub = None  # scanner.Job while scanning
        self.files_total = self.files_done = 0
        self.bytes_total = self.bytes_done = 0
        self.errors = 0
        self.current = ""

    def scan_job(self, which):
        j = Job("scan")
        j.cancel = self.cancel
        self.sub = j
        self.phase = "scan"
        self.label = f"Scanning {self.name_a if which == 'a' else self.name_b}"
        return j

    def to_dict(self):
        if self.sub is not None and self.phase == "scan":
            d = self.sub.to_dict()
            label = {"walk": "walking folders", "hash": "computing checksums"}.get(d["phase"], d["phase"])
            d.update(mode=self.mode, label=f"{self.label}: {label}", status=self.status, summary=None)
            return d
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "mode": self.mode,
            "phase": "copy" if self.phase == "sync" else self.phase,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "summary": self.summary,
            "current": self.current,
            "to_hash_files": self.files_total,
            "files_hashed": self.files_done,
            "to_hash_bytes": self.bytes_total,
            "bytes_hashed": self.bytes_done,
            "errors": self.errors,
            "elapsed": elapsed,
            "rate": self.bytes_done / elapsed if elapsed > 0 else 0,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------- helpers
def ensure_plan_table(cat):
    with cat.lock:
        cat.conn.executescript(PLAN_SCHEMA)
        cat.conn.commit()


def roles(cat_a, cat_b):
    """Return (main, backup) catalogs. The drive with role 'main' is the source;
    when both have the same role, the drive opened first (cat_a) is the source."""
    ra, rb = cat_a.info.get("role"), cat_b.info.get("role")
    if ra != "main" and rb == "main":
        return cat_b, cat_a
    return cat_a, cat_b


def history(cat_a, cat_b):
    """Partner info for this exact pair, or None if they were never synced together."""
    pa, pb = cat_a.partner(), cat_b.partner()
    if pa and pb and pa.get("volume_id") == cat_b.info["volume_id"] and pb.get("volume_id") == cat_a.info["volume_id"]:
        return pa, pb
    return None


def scope_filter(cat_a):
    include = [p.strip("/").lower() for p in cat_a.get_meta("include", []) if p.strip()]
    exclude = [p.strip("/").lower() for p in cat_a.excludes() if p.strip()]

    def in_scope(path):
        low = path.lower()
        if include and not any(low == p or low.startswith(p + "/") for p in include):
            return 0
        if any(low == p or low.startswith(p + "/") for p in exclude):
            return 0
        return 1

    return in_scope


# ---------------------------------------------------------------- compare
def build_plan(cat_a, cat_b):
    """Compare two scanned catalogs and store the plan in cat_a (the main drive)."""
    ensure_plan_table(cat_a)
    hist = history(cat_a, cat_b)
    a_cursor = hist[0].get("journal_cursor_self", 0) if hist else 0
    conn = sqlite3.connect(cat_a.db_path, timeout=30)
    conn.create_function("in_scope", 1, scope_filter(cat_a))
    try:
        conn.execute("ATTACH DATABASE ? AS b", (cat_b.db_path,))
        rows = []

        def add(kind, path, size, hash_a, hash_b, reason, src_path=None):
            choice, options = DEFAULTS[kind]
            rows.append((kind, path, src_path, size, hash_a, hash_b, reason, choice, ",".join(options)))

        conn.executescript(
            """
            CREATE TEMP TABLE a_only AS
              SELECT a.path, a.size, a.hash, a.corrupt FROM main.files a
              WHERE a.status='ok' AND in_scope(a.path)
                AND NOT EXISTS (SELECT 1 FROM b.files x WHERE x.path=a.path AND x.status='ok');
            CREATE TEMP TABLE b_only AS
              SELECT bb.path, bb.size, bb.hash, bb.corrupt FROM b.files bb
              WHERE bb.status='ok' AND in_scope(bb.path)
                AND NOT EXISTS (SELECT 1 FROM main.files x WHERE x.path=bb.path AND x.status='ok');
            CREATE INDEX temp.ia ON a_only(hash);
            CREATE INDEX temp.ib ON b_only(hash);
            CREATE INDEX temp.ibp ON b_only(path);
            """
        )

        # 1. moves: the same content left one path on B and appeared at a new path on A
        moved_a, moved_b = set(), set()
        by_hash_a, by_hash_b = {}, {}
        for h, p in conn.execute(
            "SELECT hash, path FROM a_only WHERE corrupt=0 AND hash IN (SELECT hash FROM b_only WHERE corrupt=0) ORDER BY path"
        ):
            by_hash_a.setdefault(h, []).append(p)
        for h, p, s in conn.execute(
            "SELECT hash, path, size FROM b_only WHERE corrupt=0 AND hash IN (SELECT hash FROM a_only WHERE corrupt=0) ORDER BY path"
        ):
            by_hash_b.setdefault(h, []).append((p, s))
        for h, a_paths in by_hash_a.items():
            for new_path, (old_path, size) in zip(a_paths, by_hash_b.get(h, []), strict=False):
                add("move_on_b", new_path, size, h, h, f"was {old_path}", src_path=old_path)
                moved_a.add(new_path)
                moved_b.add(old_path)

        # 2. only on A
        for path, size, h, corrupt in conn.execute("SELECT path, size, hash, corrupt FROM a_only ORDER BY path"):
            if path in moved_a:
                continue
            if h is None:
                add("unreadable", path, size, None, None, "no checksum on main (read error?) - scan again")
            elif corrupt:
                good = conn.execute(
                    "SELECT path FROM b.files WHERE status='ok' AND corrupt=0 AND hash=? LIMIT 1", (h,)
                ).fetchone()
                if good:
                    add("repair_a", path, size, h, h, f"good copy on backup at {good[0]}", src_path=good[0])
                else:
                    add("unrecoverable", path, size, h, None, "corrupted on main, no good copy on backup")
            else:
                add("copy_to_b", path, size, h, None, "")

        # 3. only on B
        latest_op = {}
        for path, op in conn.execute(
            "SELECT j.path, j.op FROM main.journal j JOIN b_only bo ON bo.path=j.path WHERE j.id > ? ORDER BY j.id",
            (a_cursor,),
        ):
            latest_op[path] = op
        for path, size, h, corrupt in conn.execute("SELECT path, size, hash, corrupt FROM b_only ORDER BY path"):
            if path in moved_b:
                continue
            a_row = conn.execute("SELECT status FROM main.files WHERE path=?", (path,)).fetchone()
            a_status = a_row[0] if a_row else None
            if latest_op.get(path) in REMOVAL_OPS or a_status == "quarantined":
                add("delete_on_b", path, size, None, h, "removed from main by DataCatalog")
                continue
            if h is not None and not corrupt:
                elsewhere = conn.execute(
                    "SELECT path FROM main.files WHERE status='ok' AND corrupt=0 AND hash=? LIMIT 1", (h,)
                ).fetchone()
                if elsewhere:
                    add("delete_on_b", path, size, None, h, f"same content is still on main at {elsewhere[0]}")
                    continue
            if corrupt or h is None:
                add("unrecoverable", path, size, None, h, "only on backup and corrupted/unreadable there")
            elif a_status == "missing":
                add("restore_to_a", path, size, None, h, "disappeared from main without a recorded deletion")
            else:
                add("only_on_b", path, size, None, h, "exists only on backup")

        # 4. on both with different content or corruption flags
        for path, sa, ha, ca, sb, hb, cb, snap_a, snap_b in conn.execute(
            """SELECT a.path, a.size, a.hash, a.corrupt, bb.size, bb.hash, bb.corrupt,
                      (SELECT hash FROM b.partner_files WHERE path=a.path),
                      (SELECT hash FROM main.partner_files WHERE path=a.path)
               FROM main.files a JOIN b.files bb ON bb.path=a.path
               WHERE a.status='ok' AND bb.status='ok' AND in_scope(a.path)
                 AND (a.hash IS NOT bb.hash OR a.corrupt=1 OR bb.corrupt=1)
               ORDER BY a.path"""
        ):
            if ca and cb:
                add("unrecoverable", path, sa, ha, hb, "corrupted on both drives")
            elif ca:
                if hb == ha:
                    add("repair_a", path, sa, ha, hb, "corrupted on main, good copy on backup")
                else:
                    add("conflict", path, sb, ha, hb, "corrupted on main; backup has a different version")
            elif cb:
                add("repair_b", path, sa, ha, hb, "corrupted on backup")
            elif ha is None or hb is None:
                add("unreadable", path, sa, ha, hb, "missing checksum - scan again")
            elif not hist:
                add("differs", path, sa, ha, hb, "no sync history between these drives")
            else:
                a_changed = snap_a != ha
                b_changed = snap_b != hb
                if a_changed and not b_changed:
                    add("update_b", path, sa, ha, hb, "")
                elif a_changed and b_changed:
                    add("conflict", path, sa, ha, hb, "changed on both drives since last sync")
                elif b_changed:
                    add("conflict", path, sb, ha, hb, "changed only on backup since last sync")
                else:
                    add("conflict", path, sa, ha, hb, "different since an earlier sync (skipped before)")
    finally:
        conn.close()

    with cat_a.lock:
        cat_a.conn.execute("DELETE FROM sync_plan")
        cat_a.conn.executemany(
            "INSERT INTO sync_plan(kind,path,src_path,size,hash_a,hash_b,reason,choice,options) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
        cat_a.conn.commit()
    warnings = []
    pa = cat_a.partner()
    if pa and not hist:
        warnings.append(f"The main drive was last synced with another drive ({pa.get('name')}).")
    pb = cat_b.partner()
    if pb and not hist:
        warnings.append(f"The backup drive was last synced with another drive ({pb.get('name')}).")
    meta = {
        "created_at": time.time(),
        "main": {"volume_id": cat_a.info["volume_id"], "name": cat_a.info["name"], "root": cat_a.root},
        "backup": {"volume_id": cat_b.info["volume_id"], "name": cat_b.info["name"], "root": cat_b.root},
        "history": bool(hist),
        "last_sync": hist[0].get("synced_at") if hist else None,
        "warnings": warnings,
        "executed_at": None,
    }
    cat_a.set_meta("sync_plan", meta)
    return plan_summary(cat_a, cat_b)


def plan_summary(cat_a, cat_b=None):
    ensure_plan_table(cat_a)
    meta = cat_a.get_meta("sync_plan")
    if not meta:
        return None
    kinds = []
    for r in cat_a.q(
        """SELECT kind, COUNT(*) n, COALESCE(SUM(size),0) bytes,
                  SUM(CASE WHEN choice<>'skip' THEN 1 ELSE 0 END) active,
                  SUM(done) done, GROUP_CONCAT(DISTINCT choice) choices FROM sync_plan GROUP BY kind"""
    ):
        kinds.append(
            {
                "kind": r["kind"],
                "label": KINDS[r["kind"]],
                "count": r["n"],
                "bytes": r["bytes"],
                "active": r["active"],
                "choices": r["choices"].split(","),
                "done": r["done"],
            }
        )
    order = list(KINDS)
    kinds.sort(key=lambda k: order.index(k["kind"]))
    need = {
        c: n for c, n in cat_a.q("SELECT choice, COALESCE(SUM(size),0) FROM sync_plan WHERE done=0 GROUP BY choice")
    }
    to_b, to_a = need.get("a2b", 0), need.get("b2a", 0)
    free_a = shutil.disk_usage(cat_a.root).free if os.path.isdir(cat_a.root) else None
    free_b = None
    if cat_b is not None and os.path.isdir(cat_b.root):
        free_b = shutil.disk_usage(cat_b.root).free
    pending = cat_a.q1("SELECT COUNT(*) FROM sync_plan WHERE done=0 AND choice<>'skip'")[0]
    failed = cat_a.q1("SELECT COUNT(*) FROM sync_plan WHERE done=0 AND result IS NOT NULL")[0]
    return {
        **meta,
        "kinds": kinds,
        "bytes_to_backup": to_b,
        "bytes_to_main": to_a,
        "free_main": free_a,
        "free_backup": free_b,
        "pending": pending,
        "failed": failed,
        "space_ok": (free_b is None or to_b <= free_b) and (free_a is None or to_a <= free_a),
    }


def plan_items(cat_a, kind=None, offset=0, limit=200, failed=False):
    ensure_plan_table(cat_a)
    conds, args = [], []
    if kind:
        conds.append("kind=?")
        args.append(kind)
    if failed:
        conds.append("done=0 AND result IS NOT NULL")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    total = cat_a.q1(f"SELECT COUNT(*) FROM sync_plan {where}", args)[0]
    items = [
        dict(r)
        for r in cat_a.q(
            f"SELECT * FROM sync_plan {where} ORDER BY kind, path LIMIT ? OFFSET ?", args + [limit, offset]
        )
    ]
    for i in items:
        i["options"] = i["options"].split(",")
    return {"total": total, "items": items}


def set_choice(cat_a, choice, ids=None, kind=None):
    """Change the choice for selected plan items (only to an option they allow)."""
    if choice not in CHOICES:
        raise SyncError("Unknown choice.")
    with cat_a.lock:
        if ids:
            q = ",".join("?" * len(ids))
            cur = cat_a.conn.execute(
                f"UPDATE sync_plan SET choice=? WHERE done=0 AND id IN ({q}) AND (','||options||',') LIKE ?",
                [choice, *ids, f"%,{choice},%"],
            )
        elif kind:
            cur = cat_a.conn.execute(
                "UPDATE sync_plan SET choice=? WHERE done=0 AND kind=? AND (','||options||',') LIKE ?",
                (choice, kind, f"%,{choice},%"),
            )
        else:
            raise SyncError("Nothing selected.")
        cat_a.conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- execution
class TargetBatch:
    """Quarantine batch on one drive for files replaced or removed by a sync run."""

    def __init__(self, cat, prefix="sync"):
        self.cat = cat
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(catmod.catalog_dir(cat.root), "quarantine")
        self.id = f"{prefix}-{stamp}"
        n = 1
        while os.path.exists(os.path.join(base, self.id)) or cat.q1("SELECT 1 FROM q_batches WHERE id=?", (self.id,)):
            n += 1
            self.id = f"{prefix}-{stamp}-{n}"
        self.base = os.path.join(base, self.id)
        self.files = 0
        self.bytes = 0

    def move_in(self, rel, size):
        dst = paths.to_os(self.base, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(paths.to_os(self.cat.root, rel), dst)
        self.files += 1
        self.bytes += size

    def close(self, note):
        if not self.files:
            return
        with self.cat.lock:
            self.cat.conn.execute(
                "INSERT INTO q_batches(id,created_at,files,bytes,note,status) VALUES(?,?,?,?,?,'active')",
                (self.id, time.time(), self.files, self.bytes, note),
            )
            self.cat.conn.commit()


def _row(cat, rel):
    return cat.q1("SELECT * FROM files WHERE path=?", (rel,))


def _check_unchanged(cat, rel, label):
    """The file must exist and match its catalog entry (status ok)."""
    r = _row(cat, rel)
    if not r or r["status"] != "ok":
        raise SyncError(f"{label}: not in the catalog any more")
    try:
        st = os.stat(paths.to_os(cat.root, rel))
    except OSError as e:
        raise SyncError(f"{label}: cannot read ({e})") from e
    if not same_stat(st.st_size, st.st_mtime_ns, r["size"], r["mtime_ns"]):
        raise SyncError(f"{label}: changed since compare - compare again")
    return r


def _remove_empty_parents(root, rel_dir):
    while rel_dir:
        try:
            os.rmdir(paths.to_os(root, rel_dir))
        except OSError:
            return
        rel_dir = paths.parent_of(rel_dir)


class Runner:
    def __init__(self, cat_a, cat_b, job: SyncJob, quarantine_on_backup=True):
        self.a, self.b, self.job = cat_a, cat_b, job
        self.batch_a = TargetBatch(cat_a)
        self.batch_b = TargetBatch(cat_b)
        self.quarantine_on_backup = quarantine_on_backup

    def copy(self, src_cat, src_rel, expected, dst_cat, dst_rel, dst_batch):
        """Verified copy src -> dst. An existing target goes to dst_batch (quarantine)."""
        _check_unchanged(src_cat, src_rel, "source")
        dst_os = paths.to_os(dst_cat.root, dst_rel)
        existing = os.path.lexists(dst_os)
        old = None
        if existing:
            old = _check_unchanged(dst_cat, dst_rel, "target")
        src_os = paths.to_os(src_cat.root, src_rel)
        tmp = dst_os + ".datacatalog-tmp"
        os.makedirs(os.path.dirname(dst_os), exist_ok=True)
        h = hashlib.blake2b(digest_size=32)
        buf = bytearray(CHUNK)
        mv = memoryview(buf)
        try:
            with open(src_os, "rb", buffering=0) as fi, open(tmp, "wb") as fo:
                while True:
                    n = fi.readinto(buf)
                    if not n:
                        break
                    h.update(mv[:n])
                    fo.write(mv[:n])
                    self.job.bytes_done += n
                    if self.job.cancel.is_set():
                        raise StopSync("stopped")
                fo.flush()
                os.fsync(fo.fileno())
            if h.hexdigest() != expected:
                raise SyncError("source content does not match its checksum - run Verify on the source drive")
            shutil.copystat(src_os, tmp)
        except OSError as e:
            _silent_remove(tmp)
            if getattr(e, "errno", None) == 28:  # ENOSPC
                raise StopSync(f"no space left on {dst_cat.info['name']}") from e
            raise SyncError(f"copy failed: {e}") from e
        except BaseException:
            _silent_remove(tmp)
            raise
        if existing:
            dst_batch.move_in(dst_rel, old["size"])
            dst_cat.journal(
                "replaced",
                dst_rel,
                old["hash"],
                old["size"],
                dst_batch.id,
                f"older version replaced by sync from {src_cat.info['name']}",
            )
        os.replace(tmp, dst_os)
        st = os.stat(dst_os)
        dst_cat.upsert_file(dst_rel, st.st_size, st.st_mtime_ns, expected)
        with dst_cat.lock:
            dst_cat.conn.execute("UPDATE integrity_issues SET resolved=1 WHERE path=? AND resolved=0", (dst_rel,))
        dst_cat.journal("sync_copy", dst_rel, expected, st.st_size, None, f"from {src_cat.info['name']}:{src_rel}")

    def move_on_b(self, old_rel, new_rel):
        r = _check_unchanged(self.b, old_rel, "backup file")
        dst = paths.to_os(self.b.root, new_rel)
        if os.path.lexists(dst):
            raise SyncError("target path already exists on backup")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(paths.to_os(self.b.root, old_rel), dst)
        with self.b.lock:
            self.b.conn.execute(
                "UPDATE files SET path=?, dir=?, name=? WHERE id=?",
                (new_rel, paths.parent_of(new_rel), paths.name_of(new_rel), r["id"]),
            )
        self.b.journal("sync_move", new_rel, r["hash"], r["size"], None, f"from {old_rel}")
        _remove_empty_parents(self.b.root, paths.parent_of(old_rel))

    def delete_on_b(self, rel):
        r = _check_unchanged(self.b, rel, "backup file")
        if self.quarantine_on_backup:
            self.batch_b.move_in(rel, r["size"])
            with self.b.lock:
                self.b.conn.execute(
                    "UPDATE files SET status='quarantined', q_batch=? WHERE id=?", (self.batch_b.id, r["id"])
                )
            self.b.journal("quarantine", rel, r["hash"], r["size"], self.batch_b.id, "removed by sync")
        else:
            os.remove(paths.to_os(self.b.root, rel))
            with self.b.lock:
                self.b.conn.execute("DELETE FROM files WHERE id=?", (r["id"],))
            self.b.journal("purge", rel, r["hash"], r["size"], None, "deleted by sync")
        _remove_empty_parents(self.b.root, paths.parent_of(rel))

    def run_item(self, it):
        c = it["choice"]
        if c == "a2b":
            self.copy(self.a, it["path"], it["hash_a"], self.b, it["path"], self.batch_b)
        elif c == "b2a":
            src = it["src_path"] or it["path"]
            expected = it["hash_b"] if it["hash_b"] else it["hash_a"]
            self.copy(self.b, src, expected, self.a, it["path"], self.batch_a)
        elif c == "move_b":
            self.move_on_b(it["src_path"], it["path"])
        elif c == "delete_b":
            self.delete_on_b(it["path"])

    def run(self):
        j = self.job
        ensure_plan_table(self.a)
        order = "CASE choice WHEN 'move_b' THEN 0 WHEN 'delete_b' THEN 1 WHEN 'a2b' THEN 2 ELSE 3 END, path"
        items = [dict(r) for r in self.a.q(f"SELECT * FROM sync_plan WHERE done=0 AND choice<>'skip' ORDER BY {order}")]
        j.phase = "sync"
        j.label = f"Synchronizing {self.a.info['name']} → {self.b.info['name']}"
        j.files_total = len(items)
        j.bytes_total = sum(it["size"] or 0 for it in items if it["choice"] in ("a2b", "b2a"))
        stopped = False
        try:
            for it in items:
                if j.cancel.is_set():
                    stopped = True
                    break
                j.current = it["path"]
                try:
                    self.run_item(it)
                    result, done = "ok", 1
                except SyncError as e:
                    result, done = str(e), 0
                    j.errors += 1
                except StopSync as e:
                    with self.a.lock:
                        self.a.conn.execute("UPDATE sync_plan SET result=? WHERE id=?", (str(e), it["id"]))
                    j.message = str(e)
                    stopped = True
                    break
                j.files_done += 1
                with self.a.lock:
                    self.a.conn.execute("UPDATE sync_plan SET done=?, result=? WHERE id=?", (done, result, it["id"]))
                    if j.files_done % 50 == 0:
                        self.a.conn.commit()
                        self.b.conn.commit()
        finally:
            self.batch_a.close("replaced during sync")
            self.batch_b.close("removed or replaced during sync")
            for c in (self.a, self.b):
                with c.lock:
                    c.conn.commit()
            take_snapshots(self.a, self.b)
            meta = self.a.get_meta("sync_plan") or {}
            meta["executed_at"] = time.time()
            self.a.set_meta("sync_plan", meta)
        return not stopped


def take_snapshots(cat_a, cat_b):
    """Store each drive's current file list in the other drive's catalog."""
    now = time.time()
    for src, dst in ((cat_a, cat_b), (cat_b, cat_a)):
        rows = src.q("SELECT path, size, mtime_ns, hash FROM files WHERE status='ok'")
        with dst.lock:
            dst.conn.execute("DELETE FROM partner_files")
            dst.conn.executemany(
                "INSERT INTO partner_files(path,size,mtime_ns,hash) VALUES(?,?,?,?)", [tuple(r) for r in rows]
            )
            dst.conn.commit()
    for me, other in ((cat_a, cat_b), (cat_b, cat_a)):
        me.set_meta(
            "partner",
            {
                "volume_id": other.info["volume_id"],
                "name": other.info["name"],
                "role": other.info.get("role"),
                "synced_at": now,
                "journal_cursor_self": me.journal_max_id(),
                "journal_cursor_partner": other.journal_max_id(),
            },
        )


def _silent_remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------- entry points
def run_compare(cat_a, cat_b, job: SyncJob):
    """Incrementally scan both drives, then build the plan."""
    try:
        for which, cat in (("a", cat_a), ("b", cat_b)):
            sj = job.scan_job(which)
            Scanner(cat, sj).run()
            if sj.status != "done":
                job.status = "stopped" if sj.status == "stopped" else "failed"
                job.message = sj.message or f"scan of {cat.info['name']} {sj.status}"
                return
        job.sub = None
        job.phase = "compare"
        job.label = "Comparing drives"
        s = build_plan(cat_a, cat_b)
        n = sum(k["count"] for k in s["kinds"])
        job.summary = f"Compare finished: {n} difference(s)." if n else "Compare finished: the drives are in sync."
        job.status = "done"
    except Exception as e:
        job.status = "failed"
        job.message = f"{type(e).__name__}: {e}"
    finally:
        job.sub = None
        job.phase = "done"
        job.finished_at = time.time()


def run_sync(cat_a, cat_b, job: SyncJob, quarantine_on_backup=True):
    try:
        ok = Runner(cat_a, cat_b, job, quarantine_on_backup).run()
        job.status = "done" if ok else "stopped"
        job.summary = (
            f"Sync {'finished' if ok else 'stopped'}: {job.files_done - job.errors} done, {job.errors} failed."
        )
    except Exception as e:
        job.status = "failed"
        job.message = f"{type(e).__name__}: {e}"
    finally:
        job.phase = "done"
        job.finished_at = time.time()
