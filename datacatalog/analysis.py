"""Duplicate analysis: per-directory statistics and identical directories.

For every directory D (recursively) we compute:
  files/bytes            - all files in D,
  dup_files/dup_bytes    - files that have a copy anywhere on the drive,
  out_files/out_bytes    - files that have a copy OUTSIDE directory D,
  unhashed               - files without a checksum yet.
A directory is a "100% copy" when out_files == files and unhashed == 0 - it can
then be removed as a whole without losing any data.

A directory fingerprint (used to find identical directories) is the sum of its
files' checksum values modulo 2^128 plus file count and size - it does not
depend on file names or order.
"""

import sqlite3
import threading
import time
from collections import defaultdict

from . import paths

MOD = 1 << 128


class DirStat:
    __slots__ = ("files", "bytes", "dup_files", "dup_bytes", "out_files", "out_bytes", "unhashed", "corrupt", "fp")

    def __init__(self):
        self.files = self.bytes = self.dup_files = self.dup_bytes = 0
        self.out_files = self.out_bytes = self.unhashed = self.corrupt = 0
        self.fp = 0

    def to_dict(self):
        return {
            "files": self.files,
            "bytes": self.bytes,
            "dup_files": self.dup_files,
            "dup_bytes": self.dup_bytes,
            "out_files": self.out_files,
            "out_bytes": self.out_bytes,
            "unhashed": self.unhashed - self.corrupt,
            "corrupt": self.corrupt,
            "fully_redundant": self.files > 0 and self.unhashed == 0 and self.out_files == self.files,
        }


class Analysis:
    def __init__(self):
        self.dirs = {}  # rel_dir -> DirStat
        self.children = defaultdict(set)
        self.hash_count = {}  # hash -> number of files (only > 1)
        self.groups_total = 0
        self.wasted_bytes = 0
        self.identical = []  # groups of identical directories
        self.built_at = None
        self.build_seconds = 0

    def dir_stat(self, d):
        return self.dirs.get(d)


def build(cat):
    """Build the analysis on a separate connection so a running scan is not blocked."""
    conn = sqlite3.connect(cat.db_path, timeout=30)
    try:
        return _build(conn)
    finally:
        conn.close()


def _build(conn):
    t0 = time.time()
    a = Analysis()
    hc = {}
    wasted = 0
    groups = 0
    for h, n, size in conn.execute(
        """SELECT hash, COUNT(*) n, MAX(size) s FROM files
           WHERE status='ok' AND hash IS NOT NULL AND size > 0 AND corrupt=0
           GROUP BY hash HAVING COUNT(*) > 1"""
    ):
        hc[h] = n
        wasted += (n - 1) * size
        groups += 1
    a.hash_count = hc
    a.groups_total = groups
    a.wasted_bytes = wasted

    dirs = a.dirs
    children = a.children
    dup_files = []  # (dir, hash, size)

    def get(d):
        s = dirs.get(d)
        if s is None:
            s = dirs[d] = DirStat()
            if d:
                children[paths.parent_of(d)].add(d)
                get(paths.parent_of(d))
        return s

    get("")
    cur = conn.execute(
        "SELECT dir, size, CASE WHEN corrupt=1 THEN NULL ELSE hash END, corrupt FROM files WHERE status='ok'"
    )
    rows = cur.fetchmany(20000)
    while rows:
        for d, size, h, bad in rows:
            get(d)
            anc = paths.ancestors(d)
            is_dup = h is not None and h in hc
            hv = int(h[:32], 16) if h else 0
            for x in anc:
                s = dirs[x]
                s.files += 1
                s.bytes += size
                if h is None:
                    s.unhashed += 1
                    if bad:
                        s.corrupt += 1
                else:
                    s.fp = (s.fp + hv) % MOD
                if is_dup:
                    s.dup_files += 1
                    s.dup_bytes += size
            if h is not None and size == 0:
                # an empty file carries no data - treat it as "covered"
                for x in anc:
                    dirs[x].out_files += 1
            if is_dup:
                dup_files.append((d, h, size))
        rows = cur.fetchmany(20000)

    # How many copies of each hash live inside each ancestor.
    inside = defaultdict(int)
    for d, h, _ in dup_files:
        for x in paths.ancestors(d):
            inside[(x, h)] += 1
    for d, h, size in dup_files:
        total = hc[h]
        for x in paths.ancestors(d):
            if total > inside[(x, h)]:
                s = dirs[x]
                s.out_files += 1
                s.out_bytes += size
    del inside

    a.identical = _identical(a)
    a.built_at = time.time()
    a.build_seconds = a.built_at - t0
    return a


def _identical(a):
    by_fp = defaultdict(list)
    for d, s in a.dirs.items():
        if d and s.files > 0 and s.unhashed == 0 and s.bytes > 0:
            by_fp[(s.fp, s.files, s.bytes)].append(d)
    key_of = {}
    for k, ds in by_fp.items():
        if len(ds) > 1:
            for d in ds:
                key_of[d] = k
    out = []
    for k, ds in by_fp.items():
        if len(ds) < 2:
            continue
        # A directory holding only one subdirectory has the same fingerprint -
        # keep the topmost one.
        members = set(ds)
        ds = [d for d in ds if not any(x in members for x in paths.ancestors(d)[1:])]
        if len(ds) < 2:
            continue
        # Skip groups whose members all sit in directories that are themselves
        # identical (only the topmost level is shown).
        parents = [paths.parent_of(d) for d in ds]
        pk = {key_of.get(p) for p in parents}
        if len(pk) == 1 and None not in pk and len(set(parents)) == len(parents):
            continue
        out.append({"dirs": sorted(ds), "files": k[1], "bytes": k[2], "wasted": k[2] * (len(ds) - 1)})
    out.sort(key=lambda g: -g["wasted"])
    return out


class AnalysisCache:
    """Holds the latest analysis and rebuilds it in the background after changes."""

    def __init__(self):
        self.current = None
        self.building = False
        self.error = None
        self._lock = threading.Lock()
        self._dirty = False

    def invalidate_and_rebuild(self, cat):
        with self._lock:
            if self.building:
                self._dirty = True
                return
            self.building = True
        threading.Thread(target=self._run, args=(cat,), daemon=True).start()

    def _run(self, cat):
        while True:
            try:
                self.current = build(cat)
                self.error = None
            except Exception as e:
                self.error = str(e)
            with self._lock:
                if self._dirty:
                    self._dirty = False
                    continue
                self.building = False
                return

    def get_or_build(self, cat):
        if self.current is None:
            with self._lock:
                if self.building:
                    return None
                self.building = True
            self._run(cat)
        return self.current
