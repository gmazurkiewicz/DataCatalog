"""Safe removal: moving files to a quarantine folder on the same drive.

Files go to .datacatalog/quarantine/<batch>/<original path>.
A move within one drive is only a rename - it is instant.
Every operation is written to the journal table.

Safety rules:
  * a file can be moved only if at least one of its copies stays on the drive
    and is NOT selected for removal,
  * right before moving, the kept copy is checked to still exist with the size
    and modification time recorded in the catalog,
  * the file being moved must not have changed since the scan.
"""
import os
import shutil
import time

from . import catalog as catmod
from . import paths
from .scanner import same_stat


def qroot(root):
    return os.path.join(catmod.catalog_dir(root), "quarantine")


def expand_selection(cat, sel_paths):
    """Expand selected files and directories into file rows (status ok)."""
    rows = {}
    for p in sel_paths:
        p = p.strip("/")
        if p == "":
            for r in cat.q("SELECT id,path,dir,size,mtime_ns,hash,corrupt FROM files WHERE status='ok'"):
                rows[r["id"]] = r
            continue
        r = cat.q1("SELECT id,path,dir,size,mtime_ns,hash,corrupt FROM files WHERE status='ok' AND path=?", (p,))
        if r:
            rows[r["id"]] = r
        lo, hi = paths.prefix_range(p)
        for r in cat.q(
            "SELECT id,path,dir,size,mtime_ns,hash,corrupt FROM files WHERE status='ok' AND path>=? AND path<?",
            (lo, hi),
        ):
            rows[r["id"]] = r
    return list(rows.values())


def plan(cat, sel_paths):
    """Split a selection into movable files and blocked files (with a reason)."""
    rows = expand_selection(cat, sel_paths)
    selected_ids = {r["id"] for r in rows}
    movable, blocked = [], []
    keepers = {}
    for r in rows:
        if r["hash"] is None:
            blocked.append((r, "no checksum yet - wait for the scan to finish"))
            continue
        if r["corrupt"]:
            blocked.append((r, "suspected corruption - see the Problems tab"))
            continue
        if r["size"] == 0:
            movable.append(r)  # empty file - nothing is lost
            continue
        h = r["hash"]
        if h not in keepers:
            keepers[h] = next(
                (k for k in cat.q(
                    "SELECT id,path,size,mtime_ns FROM files WHERE status='ok' AND corrupt=0 AND hash=? ORDER BY path",
                    (h,))
                 if k["id"] not in selected_ids),
                None,
            )
        if keepers[h] is None:
            total = cat.q1("SELECT COUNT(*) FROM files WHERE status='ok' AND corrupt=0 AND hash=?", (h,))[0]
            blocked.append((r, "the only copy on this drive" if total < 2
                            else "all copies of this file are selected - unselect one"))
        else:
            movable.append(r)
    return movable, blocked, keepers


def summarize_plan(movable, blocked, keepers, limit=200):
    return {
        "movable_files": len(movable),
        "movable_bytes": sum(r["size"] for r in movable),
        "blocked_files": len(blocked),
        "blocked_bytes": sum(r["size"] for r, _ in blocked),
        "blocked": [
            {"path": r["path"], "size": r["size"], "reason": why} for r, why in blocked[:limit]
        ],
        "examples": [
            {"path": r["path"], "size": r["size"],
             "kept_copy": keepers[r["hash"]]["path"] if keepers.get(r["hash"]) else None}
            for r in movable[:20]
        ],
    }


def _remove_empty_parents(root, rel_dir):
    while rel_dir:
        try:
            os.rmdir(paths.to_os(root, rel_dir))
        except OSError:
            return
        rel_dir = paths.parent_of(rel_dir)


def execute(cat, sel_paths, note=""):
    """Move selected files that have a copy outside the selection to quarantine."""
    movable, blocked, keepers = plan(cat, sel_paths)
    root = cat.root
    batch = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(qroot(root), batch)
    n = 1
    while os.path.exists(base) or cat.q1("SELECT 1 FROM q_batches WHERE id=?", (os.path.basename(base),)):
        n += 1
        base = os.path.join(qroot(root), f"{batch}-{n}")
    batch = os.path.basename(base)
    moved, skipped = [], []
    verified_keepers = {}
    host = catmod.hostname()
    touched_dirs = set()
    for r in movable:
        src = paths.to_os(root, r["path"])
        try:
            st = os.stat(src)
        except OSError as e:
            skipped.append({"path": r["path"], "reason": f"cannot read: {e}"})
            continue
        if not same_stat(st.st_size, st.st_mtime_ns, r["size"], r["mtime_ns"]):
            skipped.append({"path": r["path"], "reason": "file changed since the scan - scan again"})
            continue
        if r["size"] > 0:
            k = keepers[r["hash"]]
            if k["id"] not in verified_keepers:
                try:
                    kst = os.stat(paths.to_os(root, k["path"]))
                    verified_keepers[k["id"]] = same_stat(kst.st_size, kst.st_mtime_ns, k["size"], k["mtime_ns"])
                except OSError:
                    verified_keepers[k["id"]] = False
            if not verified_keepers[k["id"]]:
                skipped.append({"path": r["path"],
                                "reason": f"the kept copy {k['path']} is missing or has changed"})
                continue
        dst = paths.to_os(base, r["path"])
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
        except OSError as e:
            skipped.append({"path": r["path"], "reason": f"move failed: {e}"})
            continue
        moved.append(r)
        touched_dirs.add(r["dir"])
        with cat.lock:
            cat.conn.execute("UPDATE files SET status='quarantined', q_batch=? WHERE id=?", (batch, r["id"]))
            cat.conn.execute(
                "INSERT INTO journal(ts,host,op,batch,path,hash,size,detail) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), host, "quarantine", batch, r["path"], r["hash"], r["size"],
                 keepers[r["hash"]]["path"] if r["size"] > 0 and keepers.get(r["hash"]) else None),
            )
    for d in sorted(touched_dirs, key=len, reverse=True):
        _remove_empty_parents(root, d)
    with cat.lock:
        if moved:
            cat.conn.execute(
                "INSERT INTO q_batches(id,created_at,files,bytes,note,status) VALUES(?,?,?,?,?,'active')",
                (batch, time.time(), len(moved), sum(r["size"] for r in moved), note),
            )
        cat.conn.commit()
    if not moved:
        try:
            os.rmdir(base)
        except OSError:
            pass
    return {
        "batch": batch if moved else None,
        "moved_files": len(moved),
        "moved_bytes": sum(r["size"] for r in moved),
        "skipped": skipped[:200],
        "skipped_count": len(skipped),
        "blocked_count": len(blocked),
    }


def list_batches(cat):
    return [dict(r) for r in cat.q("SELECT * FROM q_batches ORDER BY created_at DESC")]


def batch_files(cat, batch, limit=1000):
    return [dict(r) for r in cat.q(
        "SELECT path,size,hash FROM files WHERE status='quarantined' AND q_batch=? ORDER BY path LIMIT ?",
        (batch, limit),
    )]


def restore(cat, batch):
    root = cat.root
    base = os.path.join(qroot(root), batch)
    restored, problems = 0, []
    host = catmod.hostname()
    rows = cat.q("SELECT id,path,hash,size FROM files WHERE status='quarantined' AND q_batch=?", (batch,))
    for r in rows:
        src = paths.to_os(base, r["path"])
        dst = paths.to_os(root, r["path"])
        if os.path.exists(dst):
            problems.append({"path": r["path"], "reason": "a file already exists at the destination"})
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
        except OSError as e:
            problems.append({"path": r["path"], "reason": str(e)})
            continue
        restored += 1
        with cat.lock:
            cat.conn.execute("UPDATE files SET status='ok', q_batch=NULL WHERE id=?", (r["id"],))
            cat.conn.execute(
                "INSERT INTO journal(ts,host,op,batch,path,hash,size) VALUES(?,?,?,?,?,?,?)",
                (time.time(), host, "restore", batch, r["path"], r["hash"], r["size"]),
            )
    with cat.lock:
        if not problems:
            cat.conn.execute("UPDATE q_batches SET status='restored' WHERE id=?", (batch,))
        cat.conn.commit()
    if not problems:
        shutil.rmtree(base, ignore_errors=True)
    return {"restored": restored, "problems": problems}


def purge(cat, batch):
    """Permanently delete the files of a quarantine batch."""
    root = cat.root
    base = os.path.join(qroot(root), batch)
    host = catmod.hostname()
    rows = cat.q("SELECT id,path,hash,size FROM files WHERE status='quarantined' AND q_batch=?", (batch,))
    deleted, problems = 0, []
    for r in rows:
        p = paths.to_os(base, r["path"])
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError as e:
            problems.append({"path": r["path"], "reason": str(e)})
            continue
        deleted += 1
        with cat.lock:
            cat.conn.execute("DELETE FROM files WHERE id=?", (r["id"],))
            cat.conn.execute(
                "INSERT INTO journal(ts,host,op,batch,path,hash,size) VALUES(?,?,?,?,?,?,?)",
                (time.time(), host, "purge", batch, r["path"], r["hash"], r["size"]),
            )
    with cat.lock:
        if not problems:
            cat.conn.execute("UPDATE q_batches SET status='purged' WHERE id=?", (batch,))
        cat.conn.commit()
    if not problems:
        shutil.rmtree(base, ignore_errors=True)
    return {"deleted": deleted, "problems": problems}


def repair_from_copy(cat, issue):
    """Overwrite a corrupted file with a good copy from the same drive.

    The corrupted version is moved to quarantine (nothing is deleted)."""
    root = cat.root
    good = cat.q1(
        "SELECT path,size,mtime_ns FROM files WHERE status='ok' AND corrupt=0 AND hash=? AND path<>? LIMIT 1",
        (issue["expected"], issue["path"]),
    )
    if not good:
        raise ValueError("There is no good copy of this file on this drive.")
    from .scanner import hash_file
    src = paths.to_os(root, good["path"])
    if hash_file(src) != issue["expected"]:
        raise ValueError(f"The copy {good['path']} has changed too - it cannot be used.")
    dst = paths.to_os(root, issue["path"])
    batch = "repair-" + time.strftime("%Y%m%d-%H%M%S")
    qdst = paths.to_os(os.path.join(qroot(root), batch), issue["path"])
    os.makedirs(os.path.dirname(qdst), exist_ok=True)
    tmp = dst + ".datacatalog-tmp"
    shutil.copy2(src, tmp)
    if hash_file(tmp) != issue["expected"]:
        os.remove(tmp)
        raise ValueError("The copy failed verification after copying.")
    os.rename(dst, qdst)          # corrupted version goes to quarantine
    os.rename(tmp, dst)
    st = os.stat(dst)
    now = time.time()
    with cat.lock:
        cat.conn.execute(
            "UPDATE files SET hash=?, size=?, mtime_ns=?, hashed_at=?, verified_at=?, corrupt=0 WHERE path=?",
            (issue["expected"], st.st_size, st.st_mtime_ns, now, now, issue["path"]),
        )
        cat.conn.execute("UPDATE integrity_issues SET resolved=1 WHERE id=?", (issue["id"],))
        cat.conn.execute(
            "INSERT INTO journal(ts,host,op,batch,path,hash,size,detail) VALUES(?,?,?,?,?,?,?,?)",
            (now, catmod.hostname(), "repair", batch, issue["path"], issue["expected"], st.st_size,
             f"from copy {good['path']}; corrupted version in quarantine"),
        )
        cat.conn.commit()
    return {"repaired": issue["path"], "from": good["path"], "damaged_copy": f".datacatalog/quarantine/{batch}"}
