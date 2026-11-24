import json
import os
import threading
import urllib.request

import pytest

from datacatalog import analysis, catalog, quarantine, scanner
from datacatalog.server import serve


def write(root, rel, data):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


@pytest.fixture
def disk(tmp_path):
    r = str(tmp_path / "disk")
    os.makedirs(r)
    A, B, C, U = os.urandom(3000), os.urandom(2000), os.urandom(1000), os.urandom(500)
    write(r, "Photos/2020/a.jpg", A)
    write(r, "Photos/2020/b.jpg", B)
    write(r, "Photos/2021/c.jpg", C)
    write(r, "Old/Photos/2020/a.jpg", A)
    write(r, "Old/Photos/2020/b.jpg", B)
    write(r, "Old/unique.txt", U)
    write(r, "Music/Album/1.mp3", A + B)
    write(r, "Copy/Music/Album/1.mp3", A + B)
    write(r, "Docs/x.pdf", C + C)
    write(r, "Docs/x_kopia.pdf", C + C)
    write(r, "System Volume Information/ignore.bin", A)
    catalog.init_volume(r, "Test", "main")
    return r


def scan(cat, mode="scan"):
    j = scanner.Job(mode)
    scanner.Scanner(cat, j).run()
    assert j.status == "done", j.message
    return j


def test_scan_and_incremental(disk):
    cat = catalog.Catalog(disk)
    j = scan(cat)
    assert j.files_seen == 10  # System Volume Information is skipped
    assert j.files_hashed == 10
    j2 = scan(cat)
    assert j2.files_hashed == 0 and j2.new_files == 0
    write(disk, "New/n.txt", b"abc")
    j3 = scan(cat)
    assert j3.new_files == 1 and j3.files_hashed == 1
    os.remove(os.path.join(disk, "New", "n.txt"))
    j4 = scan(cat)
    assert j4.missing_files == 1


def test_analysis(disk):
    cat = catalog.Catalog(disk)
    scan(cat)
    a = analysis.build(cat)
    assert a.groups_total == 4
    assert a.dirs["Old/Photos"].to_dict()["fully_redundant"]
    assert not a.dirs["Old"].to_dict()["fully_redundant"]  # unique.txt
    assert not a.dirs["Docs"].to_dict()["fully_redundant"]  # copies only inside
    assert a.dirs["Docs"].dup_files == 2
    ident = {tuple(g["dirs"]) for g in a.identical}
    # "Copy" holds only a copy of "Music" - the topmost level is shown, no nesting
    assert ident == {("Copy", "Music"), ("Old/Photos", "Photos/2020")}


def test_plan_blocks_unique_and_all_copies(disk):
    cat = catalog.Catalog(disk)
    scan(cat)
    m, b, _ = quarantine.plan(cat, ["Old"])
    assert {r["path"] for r in m} == {"Old/Photos/2020/a.jpg", "Old/Photos/2020/b.jpg"}
    assert [r["path"] for r, _ in b] == ["Old/unique.txt"]
    m, b, _ = quarantine.plan(cat, ["Music", "Copy"])
    assert not m and len(b) == 2


def test_quarantine_restore_purge(disk):
    cat = catalog.Catalog(disk)
    scan(cat)
    res = quarantine.execute(cat, ["Copy"])
    assert res["moved_files"] == 1
    assert not os.path.exists(os.path.join(disk, "Copy"))  # empty folders removed
    j = scan(cat)
    assert j.missing_files == 0  # quarantined files are not "missing"
    assert quarantine.restore(cat, res["batch"])["restored"] == 1
    assert os.path.exists(os.path.join(disk, "Copy", "Music", "Album", "1.mp3"))
    res = quarantine.execute(cat, ["Copy"])
    assert quarantine.purge(cat, res["batch"])["deleted"] == 1
    ops = [r["op"] for r in cat.q("SELECT op FROM journal ORDER BY id")]
    assert ops == ["quarantine", "restore", "quarantine", "purge"]


def test_changed_file_is_skipped(disk):
    cat = catalog.Catalog(disk)
    scan(cat)
    p = os.path.join(disk, "Copy", "Music", "Album", "1.mp3")
    with open(p, "ab") as f:
        f.write(b"zmiana")
    res = quarantine.execute(cat, ["Copy"])
    assert res["moved_files"] == 0 and res["skipped_count"] == 1
    assert os.path.exists(p)


def test_corruption_detected_and_protected(disk):
    cat = catalog.Catalog(disk)
    scan(cat)
    p = os.path.join(disk, "Docs", "x.pdf")
    st = os.stat(p)
    with open(p, "r+b") as f:
        f.seek(10)
        f.write(b"\x00\xff")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    j = scan(cat, "verify")
    assert j.corrupt == 1
    # a corrupted file must never be the kept copy
    m, b, _ = quarantine.plan(cat, ["Docs/x_kopia.pdf"])
    assert not m
    a = analysis.build(cat)
    assert a.dirs["Docs"].dup_files == 0
    issue = cat.q1("SELECT * FROM integrity_issues")
    res = quarantine.repair_from_copy(cat, issue)
    assert res["from"] == "Docs/x_kopia.pdf"
    j = scan(cat, "verify")
    assert j.corrupt == 0
    assert analysis.build(cat).dirs["Docs"].dup_files == 2


def test_stop_and_resume(disk):
    cat = catalog.Catalog(disk)
    for i in range(50):
        write(disk, f"Many/{i}.bin", os.urandom(200_000))
    j = scanner.Job("scan")
    s = scanner.Scanner(cat, j)
    orig = scanner.hash_file

    def slow(path, cancel=None, on_bytes=None):
        if j.files_hashed >= 5:
            j.cancel.set()
        return orig(path, cancel, on_bytes)

    scanner.hash_file = slow
    try:
        s.run()
    finally:
        scanner.hash_file = orig
    assert j.status == "stopped"
    done = cat.q1("SELECT COUNT(*) FROM files WHERE hash IS NOT NULL")[0]
    assert 0 < done < 60
    j2 = scan(cat)
    assert j2.files_hashed == 60 - done
    assert cat.q1("SELECT COUNT(*) FROM files WHERE hash IS NULL")[0] == 0


def test_excludes(disk):
    cat = catalog.Catalog(disk)
    cat.set_meta("excludes", ["Old"])
    j = scan(cat)
    assert j.files_seen == 7


def test_http_api(disk):
    app, httpd = serve("127.0.0.1", 0, None, disk)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    def post(p, b):
        r = urllib.request.Request(
            base + p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json", "X-DataCatalog": "1"}
        )
        return json.load(urllib.request.urlopen(r))

    def get(p):
        return json.load(urllib.request.urlopen(base + p))

    try:
        post("/api/scan", {"mode": "scan"})
        app.thread.join(10)
        import time

        for _ in range(50):
            if app.cache.current and not app.cache.building:
                break
            time.sleep(0.1)
        tree = get("/api/tree?path=")
        names = {d["name"] for d in tree["dirs"]}
        assert {"Photos", "Old", "Music", "Copy", "Docs"} <= names
        assert get("/api/groups")["total"] == 4
        assert get("/api/identical")["total"] == 2
        plan = post("/api/plan", {"paths": ["Old"]})
        assert plan["movable_files"] == 2 and plan["blocked_files"] == 1
        r = post("/api/quarantine", {"paths": ["Old"]})
        assert r["moved_files"] == 2
        assert get("/api/batches")["batches"][0]["files"] == 2
        assert urllib.request.urlopen(base + "/").status == 200
    finally:
        httpd.shutdown()
