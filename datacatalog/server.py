"""Local HTTP server: JSON API + static UI (standard library only)."""

import base64
import hmac
import json
import mimetypes
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, analysis, drives, health, paths, quarantine, schema, sync
from . import catalog as catmod
from .scanner import Job, Scanner

STATIC = os.path.join(os.path.dirname(__file__), "static")


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class App:
    def __init__(self):
        self.cat = None
        self.job = None
        self.thread = None
        self.cache = analysis.AnalysisCache()
        self.partner = None  # second drive, used for synchronization
        self.lock = threading.RLock()

    # --- helpers ---
    def need_cat(self):
        if self.cat is None:
            raise ApiError("No drive selected.", 409)
        return self.cat

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def need_idle(self):
        if self.busy():
            raise ApiError("A scan is running - wait for it to finish or stop it.", 409)

    def get_analysis(self):
        cat = self.need_cat()
        a = self.cache.current
        if a is None:
            a = self.cache.get_or_build(cat)
        if a is None:
            raise ApiError("The analysis is being computed, try again in a moment.", 503)
        return a

    # --- drives ---
    def api_drives(self, q):
        return {"drives": drives.list_drives(), "platform": "windows" if paths.IS_WINDOWS else "linux"}

    def _open(self, root):
        with self.lock:
            self.need_idle()
            if self.cat is not None:
                self.cat.close()
                self.cat = None
            self.cache = analysis.AnalysisCache()
            self.job = None
            if self.partner is not None and os.path.realpath(self.partner.root) == os.path.realpath(root):
                self.partner.close()
                self.partner = None
            try:
                self.cat = catmod.Catalog(root)
            except schema.IncompatibleCatalog as e:
                raise ApiError(str(e), 409) from e
            self.cache.invalidate_and_rebuild(self.cat)

    def api_open(self, body):
        root = body.get("root")
        if not root or not os.path.isdir(root):
            raise ApiError("The given path does not exist.")
        if catmod.read_volume_info(root) is None:
            raise ApiError("This drive has no catalog yet - initialize it.", 409)
        self._open(root)
        return self.api_status({})

    def api_init(self, body):
        root = body.get("root")
        name = (body.get("name") or "").strip()
        role = body.get("role") or "main"
        if not root or not os.path.isdir(root):
            raise ApiError("The given path does not exist.")
        if not name:
            raise ApiError("Enter a drive name.")
        if role not in ("main", "backup", "other"):
            raise ApiError("Unknown role.")
        try:
            catmod.init_volume(root, name, role)
        except OSError as e:
            raise ApiError(f"Could not create the catalog on the drive: {e}") from e
        self._open(root)
        if body.get("excludes"):
            self.cat.set_meta("excludes", [e for e in body["excludes"] if e.strip()])
        return self.api_status({})

    def api_close(self, body):
        with self.lock:
            self.need_idle()
            if self.cat:
                self.cat.close()
            self.cat = None
            self.cache = analysis.AnalysisCache()
        return {"ok": True}

    def api_settings(self, body):
        cat = self.need_cat()
        if "excludes" in body:
            cat.set_meta("excludes", [e.strip().strip("/") for e in body["excludes"] if e.strip()])
        if "include" in body:
            cat.set_meta("include", [e.strip().strip("/") for e in body["include"] if e.strip()])
        if "include_empty" in body:
            cat.set_meta("include_empty", bool(body["include_empty"]))
            self.cache.invalidate_and_rebuild(cat)
        if "name" in body or "role" in body:
            catmod.init_volume(cat.root, body.get("name", cat.info["name"]), body.get("role", cat.info["role"]))
            cat.info = catmod.read_volume_info(cat.root)
        return self.api_status({})

    def api_identity(self, body):
        cat = self.need_cat()
        with self.lock:
            self.need_idle()
            if body.get("action") == "adopt":
                cat.adopt_identity()
            elif body.get("action") == "new":
                cat.new_identity()
            else:
                raise ApiError("Unknown action.")
        return self.api_status({})

    # --- status and scanning ---
    def api_status(self, q):
        out = {
            "version": __version__,
            "catalog": None,
            "job": None,
            "analysis": None,
            "platform": "windows" if paths.IS_WINDOWS else "linux",
        }
        if self.cat:
            out["catalog"] = self.cat.summary()
            out["catalog"]["include"] = self.cat.get_meta("include", [])
            out["catalog"]["partner"] = self.cat.partner()
            h = health.latest(self.cat)
            out["catalog"]["health"] = {"verdict": h["verdict"], "ts": h["ts"]} if h else None
            a = self.cache.current
            out["analysis"] = {
                "building": self.cache.building,
                "error": self.cache.error,
                "built_at": a.built_at if a else None,
                "seconds": a.build_seconds if a else None,
                "groups": a.groups_total if a else None,
                "wasted_bytes": a.wasted_bytes if a else None,
                "identical_groups": len(a.identical) if a else None,
            }
        if self.partner:
            out["partner"] = {"volume": self.partner.info, "root": self.partner.root}
        if self.job:
            out["job"] = self.job.to_dict()
            out["job"]["running"] = self.busy()
        return out

    def api_scan(self, body):
        cat = self.need_cat()
        mode = body.get("mode", "scan")
        if mode not in ("scan", "verify"):
            raise ApiError("Unknown mode.")
        with self.lock:
            self.need_idle()
            job = Job(mode)
            self.job = job

            def run():
                Scanner(cat, job).run()
                self.cache.invalidate_and_rebuild(cat)

            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()
        return self.api_status({})

    def api_stop(self, body):
        if self.job:
            self.job.cancel.set()
        return {"ok": True}

    def api_reanalyze(self, body):
        self.cache.invalidate_and_rebuild(self.need_cat())
        return {"ok": True}

    # --- duplicate views ---
    def api_tree(self, q):
        cat = self.need_cat()
        a = self.get_analysis()
        path = q.get("path", "").strip("/")
        only = q.get("only_dups", "1") == "1"
        sort = q.get("sort", "waste")
        st = a.dir_stat(path)
        if st is None:
            raise ApiError("No such directory in the drive catalog.", 404)
        dirs = []
        for d in a.children.get(path, ()):
            s = a.dirs[d]
            if only and s.dup_files == 0:
                continue
            dirs.append({"name": paths.name_of(d), "path": d, "type": "dir", **s.to_dict()})
        files = []
        limit = int(q.get("limit", 3000))
        rows = cat.q(
            "SELECT path,name,size,mtime_ns,hash,corrupt FROM files WHERE status='ok' AND dir=? ORDER BY name",
            (path,),
        )
        for r in rows:
            copies = a.hash_count.get(r["hash"], 1) if r["hash"] and not r["corrupt"] else 0
            if only and copies < 2 and not r["corrupt"]:
                continue
            files.append(
                {
                    "name": r["name"],
                    "path": r["path"],
                    "type": "file",
                    "size": r["size"],
                    "mtime": r["mtime_ns"] / 1e9,
                    "hash": r["hash"],
                    "copies": copies,
                    "corrupt": bool(r["corrupt"]),
                }
            )
        key = {
            "waste": lambda x: -(x.get("dup_bytes", x.get("size", 0)) if x["type"] == "dir" else x["size"]),
            "size": lambda x: -(x.get("bytes") if x["type"] == "dir" else x["size"]),
            "name": lambda x: x["name"].lower(),
        }.get(sort, lambda x: x["name"].lower())
        dirs.sort(key=key)
        files.sort(key=key)
        return {
            "path": path,
            "stat": st.to_dict(),
            "dirs": dirs,
            "files": files[:limit],
            "files_total": len(files),
            "built_at": a.built_at,
        }

    def api_copies(self, q):
        cat = self.need_cat()
        h = q.get("hash")
        if not h:
            raise ApiError("Missing hash parameter.")
        rows = cat.q(
            "SELECT path,size,mtime_ns FROM files WHERE status='ok' AND corrupt=0 AND hash=? ORDER BY path", (h,)
        )
        return {
            "hash": h,
            "copies": [{"path": r["path"], "size": r["size"], "mtime": r["mtime_ns"] / 1e9} for r in rows],
        }

    def api_groups(self, q):
        cat = self.need_cat()
        offset = int(q.get("offset", 0))
        limit = min(int(q.get("limit", 100)), 500)
        min_size = int(q.get("min_size", 1))
        text = q.get("q", "").strip()
        args = [min_size]
        where = ""
        if text:
            where = " AND hash IN (SELECT hash FROM files WHERE status='ok' AND path LIKE ? AND hash IS NOT NULL)"
            args.append(f"%{text}%")
        base = f"""FROM files WHERE status='ok' AND corrupt=0 AND hash IS NOT NULL AND size >= ?{where}
                   GROUP BY hash HAVING COUNT(*) > 1"""
        total = cat.q1(f"SELECT COUNT(*) FROM (SELECT hash {base})", args)[0]
        groups = []
        for r in cat.q(
            f"SELECT hash, COUNT(*) n, MAX(size) size {base} ORDER BY (COUNT(*)-1)*MAX(size) DESC, hash LIMIT ? OFFSET ?",
            args + [limit, offset],
        ):
            files = cat.q(
                "SELECT path,mtime_ns FROM files WHERE status='ok' AND corrupt=0 AND hash=? ORDER BY path", (r["hash"],)
            )
            groups.append(
                {
                    "hash": r["hash"],
                    "count": r["n"],
                    "size": r["size"],
                    "wasted": (r["n"] - 1) * r["size"],
                    "files": [{"path": f["path"], "mtime": f["mtime_ns"] / 1e9} for f in files],
                }
            )
        return {"total": total, "offset": offset, "groups": groups}

    DUPFILE_SORT = {
        "path": "f.path",
        "folder": "f.dir",
        "name": "f.name",
        "size": "f.size",
        "mtime": "f.mtime_ns",
        "copies": "g.n",
        "group": "g.wasted",
    }

    def api_dupfiles(self, q):
        """Flat list of every file that has a duplicate, sortable by folder, name, size..."""
        cat = self.need_cat()
        offset = int(q.get("offset", 0))
        limit = min(int(q.get("limit", 200)), 1000)
        min_size = int(q.get("min_size", 1))
        sort = self.DUPFILE_SORT.get(q.get("sort", "folder"), "f.dir")
        order = "DESC" if q.get("order") == "desc" else "ASC"
        conds, args = [], [min_size]
        if q.get("q"):
            conds.append("f.path LIKE ?")
            args.append(f"%{q['q']}%")
        if q.get("hash"):
            conds.append("f.hash = ?")
            args.append(q["hash"])
        if q.get("folder") is not None and q.get("folder") != "":
            conds.append("f.dir = ?")
            args.append(q["folder"])
        where = "".join(" AND " + c for c in conds)
        base = f"""FROM files f JOIN (
                     SELECT hash, COUNT(*) n, (COUNT(*)-1)*MAX(size) wasted FROM files
                     WHERE status='ok' AND corrupt=0 AND hash IS NOT NULL AND size >= ?
                     GROUP BY hash HAVING COUNT(*) > 1) g ON g.hash = f.hash
                   WHERE f.status='ok' AND f.corrupt=0{where}"""
        total = cat.q1(f"SELECT COUNT(*) {base}", args)[0]
        rows = cat.q(
            f"""SELECT f.path, f.dir, f.name, f.size, f.mtime_ns, f.hash, g.n copies, g.wasted {base}
                ORDER BY {sort} {order}, f.path LIMIT ? OFFSET ?""",
            args + [limit, offset],
        )
        files = []
        for r in rows:
            others = cat.q(
                "SELECT dir FROM files WHERE status='ok' AND corrupt=0 AND hash=? AND path<>? ORDER BY path LIMIT 3",
                (r["hash"], r["path"]),
            )
            files.append(
                {
                    "path": r["path"],
                    "folder": r["dir"],
                    "name": r["name"],
                    "size": r["size"],
                    "mtime": r["mtime_ns"] / 1e9,
                    "hash": r["hash"],
                    "copies": r["copies"],
                    "group_wasted": r["wasted"],
                    "other_folders": [o["dir"] for o in others],
                }
            )
        return {"total": total, "offset": offset, "files": files}

    def api_identical(self, q):
        a = self.get_analysis()
        offset = int(q.get("offset", 0))
        limit = min(int(q.get("limit", 100)), 500)
        return {"total": len(a.identical), "groups": a.identical[offset : offset + limit]}

    # --- quarantine ---
    def api_plan(self, body):
        cat = self.need_cat()
        sel = body.get("paths") or []
        if not sel:
            raise ApiError("Nothing selected.")
        m, b, k = quarantine.plan(cat, sel)
        return quarantine.summarize_plan(m, b, k)

    def api_quarantine(self, body):
        cat = self.need_cat()
        with self.lock:
            self.need_idle()
            sel = body.get("paths") or []
            if not sel:
                raise ApiError("Nothing selected.")
            res = quarantine.execute(cat, sel, body.get("note", ""))
        self.cache.invalidate_and_rebuild(cat)
        return res

    def api_batches(self, q):
        cat = self.need_cat()
        return {"batches": quarantine.list_batches(cat)}

    def api_batch(self, q):
        cat = self.need_cat()
        return {"files": quarantine.batch_files(cat, q.get("id", ""))}

    def api_restore(self, body):
        cat = self.need_cat()
        with self.lock:
            self.need_idle()
            res = quarantine.restore(cat, body.get("batch", ""))
        self.cache.invalidate_and_rebuild(cat)
        return res

    def api_purge(self, body):
        cat = self.need_cat()
        if body.get("confirm") != "DELETE":
            raise ApiError("Permanent deletion was not confirmed.")
        with self.lock:
            self.need_idle()
            return quarantine.purge(cat, body.get("batch", ""))

    # --- synchronization ---
    def pair(self):
        """(main, backup) catalogs of the two open drives."""
        cat = self.need_cat()
        if self.partner is None:
            raise ApiError("Open the second drive first.", 409)
        return sync.roles(cat, self.partner)

    def _open_partner(self, root):
        cat = self.need_cat()
        info = catmod.read_volume_info(root)
        if info is None:
            raise ApiError("This drive has no catalog yet - initialize it.", 409)
        if info["volume_id"] == cat.info["volume_id"]:
            if os.path.realpath(root) == os.path.realpath(cat.root):
                raise ApiError("This is the drive that is already open.")
            raise ApiError(
                "This drive has the same catalog identity as the open drive - it looks like a copy made with "
                "another tool, .datacatalog folder included. Open it as the current drive and choose "
                "'This is a different drive' on the Drives tab to give it its own identity."
            )
        with self.lock:
            self.need_idle()
            if self.partner is not None:
                self.partner.close()
            try:
                self.partner = catmod.Catalog(root)
            except schema.IncompatibleCatalog as e:
                self.partner = None
                raise ApiError(str(e), 409) from e

    def api_partner_open(self, body):
        root = body.get("root")
        if not root or not os.path.isdir(root):
            raise ApiError("The given path does not exist.")
        self._open_partner(root)
        return self.api_sync_status({})

    def api_partner_init(self, body):
        root = body.get("root")
        name = (body.get("name") or "").strip()
        role = body.get("role") or "backup"
        if not root or not os.path.isdir(root):
            raise ApiError("The given path does not exist.")
        if not name:
            raise ApiError("Enter a drive name.")
        if role not in ("main", "backup", "other"):
            raise ApiError("Unknown role.")
        if catmod.read_volume_info(root) is None:
            try:
                catmod.init_volume(root, name, role)
            except OSError as e:
                raise ApiError(f"Could not create the catalog on the drive: {e}") from e
        self._open_partner(root)
        return self.api_sync_status({})

    def api_partner_close(self, body):
        with self.lock:
            self.need_idle()
            if self.partner is not None:
                self.partner.close()
            self.partner = None
        return {"ok": True}

    def api_sync_status(self, q):
        cat = self.need_cat()
        out = {"partner": None, "plan": None, "main": None, "backup": None, "same_role": False}
        if self.partner is None:
            return out
        main, backup = sync.roles(cat, self.partner)
        out["partner"] = {"volume": self.partner.info, "root": self.partner.root, "summary": self.partner.summary()}
        out["main"] = {"name": main.info["name"], "root": main.root, "role": main.info.get("role")}
        out["backup"] = {"name": backup.info["name"], "root": backup.root, "role": backup.info.get("role")}
        out["same_role"] = cat.info.get("role") == self.partner.info.get("role")
        plan = sync.plan_summary(main, backup)
        if (
            plan
            and plan["main"]["volume_id"] == main.info["volume_id"]
            and plan["backup"]["volume_id"] == backup.info["volume_id"]
        ):
            out["plan"] = plan
        out["history"] = sync.history(main, backup) is not None
        out["last_sync"] = (main.partner() or {}).get("synced_at") if out["history"] else None
        return out

    def _start_job(self, job, target):
        with self.lock:
            self.need_idle()
            self.job = job

            def run():
                try:
                    target()
                finally:
                    self.cache.invalidate_and_rebuild(self.cat)

            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()

    def api_sync_compare(self, body):
        main, backup = self.pair()
        job = sync.SyncJob("compare", main.info["name"], backup.info["name"])
        self._start_job(job, lambda: sync.run_compare(main, backup, job))
        return self.api_status({})

    def api_sync_plan(self, q):
        main, _ = self.pair()
        return sync.plan_items(
            main,
            q.get("kind") or None,
            int(q.get("offset", 0)),
            min(int(q.get("limit", 200)), 1000),
            failed=q.get("failed") == "1",
        )

    def api_sync_choose(self, body):
        main, _ = self.pair()
        self.need_idle()
        try:
            n = sync.set_choice(main, body.get("choice"), ids=body.get("ids"), kind=body.get("kind"))
        except sync.SyncError as e:
            raise ApiError(str(e)) from e
        return {"changed": n}

    def api_sync_run(self, body):
        main, backup = self.pair()
        st = self.api_sync_status({})
        plan = st["plan"]
        if not plan:
            raise ApiError("Compare the drives first.", 409)
        if not plan["pending"]:
            raise ApiError("Nothing to do - the plan has no pending actions.")
        if not plan["space_ok"] and not body.get("force"):
            raise ApiError("Not enough free space on the target drive for the selected actions.")
        job = sync.SyncJob("sync", main.info["name"], backup.info["name"])
        quarantine_b = body.get("quarantine_on_backup", True)
        self._start_job(job, lambda: sync.run_sync(main, backup, job, quarantine_b))
        return self.api_status({})

    # --- drive health ---
    def _health_cat(self, which):
        if which == "partner":
            if self.partner is None:
                raise ApiError("The second drive is not open.", 409)
            return self.partner
        return self.need_cat()

    def api_health(self, q):
        cat = self._health_cat(q.get("which", "main"))
        out = {
            "drive": {"name": cat.info["name"], "root": cat.root, "role": cat.info.get("role")},
            "smartctl": health.find_smartctl(),
            "platform": "windows" if paths.IS_WINDOWS else "linux",
            "device": None,
            "device_error": None,
            "latest": health.latest(cat),
            "history": health.history(cat),
            "partner_open": self.partner is not None,
        }
        try:
            out["device"] = health.device_for(cat.root)
        except health.HealthError as e:
            out["device_error"] = str(e)
        return out

    def api_health_check(self, body):
        cat = self._health_cat(body.get("which", "main"))
        try:
            health.check(cat)
        except health.HealthError as e:
            raise ApiError(str(e)) from e
        return self.api_health({"which": body.get("which", "main")})

    def api_health_selftest(self, body):
        cat = self._health_cat(body.get("which", "main"))
        try:
            return health.start_self_test(cat, body.get("kind", "short"))
        except health.HealthError as e:
            raise ApiError(str(e)) from e

    # --- problems ---
    def api_issues(self, q):
        cat = self.need_cat()
        issues = [
            dict(r)
            for r in cat.q("SELECT * FROM integrity_issues WHERE resolved=0 ORDER BY detected_at DESC LIMIT 1000")
        ]
        missing = [
            dict(r) for r in cat.q("SELECT path,size,hash FROM files WHERE status='missing' ORDER BY path LIMIT 1000")
        ]
        missing_total = cat.q1("SELECT COUNT(*) FROM files WHERE status='missing'")[0]
        last = cat.q1("SELECT id FROM scans ORDER BY id DESC LIMIT 1")
        errors = [
            dict(r)
            for r in cat.q(
                "SELECT path,error,ts FROM scan_errors WHERE scan_id=? ORDER BY id LIMIT 1000",
                (last[0] if last else -1,),
            )
        ]
        a = self.cache.current
        key = (cat.root, a.built_at if a else None)
        if getattr(self, "_collisions_key", None) != key:
            self._collisions = analysis.name_collisions(cat)
            self._collisions_key = key
        for i in issues:
            copy = cat.q1(
                "SELECT path FROM files WHERE status='ok' AND corrupt=0 AND hash=? AND path<>? LIMIT 1",
                (i["expected"], i["path"]),
            )
            i["good_copy"] = copy["path"] if copy else None
        return {
            "integrity": issues,
            "missing": missing,
            "missing_total": missing_total,
            "errors": errors,
            "collisions": self._collisions,
        }

    def api_issue_resolve(self, body):
        cat = self.need_cat()
        iid = body.get("id")
        action = body.get("action")
        r = cat.q1("SELECT * FROM integrity_issues WHERE id=?", (iid,))
        if not r:
            raise ApiError("No such issue.", 404)
        if action == "repair":
            with self.lock:
                self.need_idle()
                try:
                    res = quarantine.repair_from_copy(cat, r)
                except (ValueError, OSError) as e:
                    raise ApiError(str(e)) from e
            self.cache.invalidate_and_rebuild(cat)
            return res
        if action != "accept":
            raise ApiError("Unknown action.")
        with cat.lock:
            # The user accepts the current content as correct (an intentional edit).
            cat.conn.execute(
                "UPDATE files SET hash=?, hashed_at=?, corrupt=0 WHERE path=?", (r["actual"], time.time(), r["path"])
            )
            cat.conn.execute("UPDATE integrity_issues SET resolved=1 WHERE id=?", (iid,))
            cat.conn.commit()
        self.cache.invalidate_and_rebuild(cat)
        return {"ok": True}

    def api_forget_missing(self, body):
        """Remove catalog entries of missing files (e.g. deliberately deleted outside the app)."""
        cat = self.need_cat()
        with cat.lock:
            rows = cat.conn.execute("SELECT path,hash,size FROM files WHERE status='missing'").fetchall()
            for r in rows:
                cat.conn.execute(
                    "INSERT INTO journal(ts,host,op,path,hash,size,detail) VALUES(?,?,?,?,?,?,?)",
                    (
                        time.time(),
                        catmod.hostname(),
                        "forget_missing",
                        r["path"],
                        r["hash"],
                        r["size"],
                        "confirmed by the user",
                    ),
                )
            cat.conn.execute("DELETE FROM files WHERE status='missing'")
            cat.conn.commit()
        return {"forgotten": len(rows)}


GET_ROUTES = {
    "/api/status": "api_status",
    "/api/drives": "api_drives",
    "/api/tree": "api_tree",
    "/api/copies": "api_copies",
    "/api/groups": "api_groups",
    "/api/dupfiles": "api_dupfiles",
    "/api/identical": "api_identical",
    "/api/batches": "api_batches",
    "/api/batch": "api_batch",
    "/api/issues": "api_issues",
    "/api/sync/status": "api_sync_status",
    "/api/health": "api_health",
    "/api/sync/plan": "api_sync_plan",
}
POST_ROUTES = {
    "/api/open": "api_open",
    "/api/init": "api_init",
    "/api/close": "api_close",
    "/api/settings": "api_settings",
    "/api/identity": "api_identity",
    "/api/scan": "api_scan",
    "/api/stop": "api_stop",
    "/api/reanalyze": "api_reanalyze",
    "/api/plan": "api_plan",
    "/api/quarantine": "api_quarantine",
    "/api/restore": "api_restore",
    "/api/purge": "api_purge",
    "/api/issue/resolve": "api_issue_resolve",
    "/api/forget_missing": "api_forget_missing",
    "/api/partner/open": "api_partner_open",
    "/api/partner/init": "api_partner_init",
    "/api/partner/close": "api_partner_close",
    "/api/sync/compare": "api_sync_compare",
    "/api/sync/choose": "api_sync_choose",
    "/api/sync/run": "api_sync_run",
    "/api/health/check": "api_health_check",
    "/api/health/selftest": "api_health_selftest",
}


def make_handler(app, password=None):
    expected = None
    if password:
        expected = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "DataCatalog/" + __version__

        def log_message(self, fmt, *args):
            pass

        def _auth(self):
            if expected is None:
                return True
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), expected.encode()):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="DataCatalog"')
            self.end_headers()
            return False

        def _json(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _call(self, name, arg):
            try:
                self._json(200, getattr(app, name)(arg))
            except ApiError as e:
                self._json(e.code, {"error": str(e)})
            except Exception as e:
                traceback.print_exc()
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            if u.path in GET_ROUTES:
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                return self._call(GET_ROUTES[u.path], q)
            self._static(u.path)

        def do_POST(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            if u.path not in POST_ROUTES:
                return self._json(404, {"error": "Not found."})
            # Protection against calls from a foreign web page (CSRF).
            if self.headers.get("X-DataCatalog") != "1":
                return self._json(403, {"error": "Missing X-DataCatalog header."})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "Niepoprawny JSON."})
            self._call(POST_ROUTES[u.path], body)

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
            if not full.startswith(os.path.normpath(STATIC)) or not os.path.isfile(full):
                self.send_response(404)
                self.end_headers()
                return
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(host="127.0.0.1", port=8765, password=None, open_root=None):
    app = App()
    if open_root:
        app._open(open_root)
    httpd = ThreadingHTTPServer((host, port), make_handler(app, password))
    httpd.daemon_threads = True
    return app, httpd
