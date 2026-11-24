import os

import pytest

from datacatalog import catalog, quarantine, scanner, sync


def write(root, rel, data):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def read(root, rel):
    with open(os.path.join(root, *rel.split("/")), "rb") as f:
        return f.read()


def exists(root, rel):
    return os.path.exists(os.path.join(root, *rel.split("/")))


def corrupt_in_place(root, rel):
    p = os.path.join(root, *rel.split("/"))
    st = os.stat(p)
    with open(p, "r+b") as f:
        f.seek(5)
        f.write(b"\x00\xff\x00")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


@pytest.fixture
def drives(tmp_path):
    a, b = str(tmp_path / "A"), str(tmp_path / "B")
    os.makedirs(a)
    os.makedirs(b)
    write(a, "Photos/a.jpg", os.urandom(4000))
    write(a, "Photos/b.jpg", os.urandom(3000))
    write(a, "Docs/report.pdf", os.urandom(2000))
    write(a, "Docs/notes.txt", b"version 1")
    write(a, "Music/song.mp3", os.urandom(5000))
    write(a, "Dup/song_copy.mp3", read(a, "Music/song.mp3"))
    write(a, "Edit/both.txt", b"original")
    catalog.init_volume(a, "Main", "main")
    catalog.init_volume(b, "Backup", "backup")
    return catalog.Catalog(a), catalog.Catalog(b)


def compare(ca, cb):
    job = sync.SyncJob("compare", "A", "B")
    sync.run_compare(ca, cb, job)
    assert job.status == "done", job.message
    return {k["kind"]: k["count"] for k in sync.plan_summary(ca, cb)["kinds"]}


def run(ca, cb):
    job = sync.SyncJob("sync", "A", "B")
    sync.run_sync(ca, cb, job)
    assert job.status == "done", job.message
    return job


def items(ca, kind):
    return sync.plan_items(ca, kind)["items"]


def test_first_sync_copies_everything(drives):
    ca, cb = drives
    kinds = compare(ca, cb)
    assert kinds == {"copy_to_b": 7}
    job = run(ca, cb)
    assert job.errors == 0
    assert read(cb.root, "Docs/notes.txt") == b"version 1"
    assert cb.q1("SELECT COUNT(*) FROM files WHERE status='ok' AND hash IS NOT NULL")[0] == 7
    # mtime preserved -> the next scan of B does not need to re-hash anything
    j = scanner.Job("scan")
    scanner.Scanner(cb, j).run()
    assert j.files_hashed == 0
    assert compare(ca, cb) == {}
    assert ca.partner()["volume_id"] == cb.info["volume_id"]


def test_changes_are_classified_and_applied(drives):
    ca, cb = drives
    compare(ca, cb)
    run(ca, cb)

    # changes on the main drive
    write(ca.root, "Photos/new.jpg", os.urandom(1500))  # new
    write(ca.root, "Docs/notes.txt", b"version 2 - longer")  # edited
    os.makedirs(os.path.join(ca.root, "Archive"))
    os.rename(os.path.join(ca.root, "Docs", "report.pdf"), os.path.join(ca.root, "Archive", "report.pdf"))  # moved
    os.remove(os.path.join(ca.root, "Photos", "b.jpg"))  # vanished without the app
    write(ca.root, "Edit/both.txt", b"changed on main")  # conflict (also changed on B)
    # changes on the backup drive
    write(cb.root, "Edit/both.txt", b"changed on backup!")
    write(cb.root, "Extra/only_backup.txt", b"added directly to backup")

    j = scanner.Job("scan")
    scanner.Scanner(ca, j).run()
    # dedupe on main through the app
    assert quarantine.execute(ca, ["Dup"])["moved_files"] == 1
    # silent corruption on main, found by verify
    corrupt_in_place(ca.root, "Photos/a.jpg")
    v = scanner.Job("verify")
    scanner.Scanner(ca, v).run()
    assert v.corrupt == 1

    kinds = compare(ca, cb)
    assert kinds == {
        "copy_to_b": 1,
        "update_b": 1,
        "move_on_b": 1,
        "delete_on_b": 1,
        "repair_a": 1,
        "restore_to_a": 1,
        "only_on_b": 1,
        "conflict": 1,
    }
    assert items(ca, "move_on_b")[0]["src_path"] == "Docs/report.pdf"
    assert items(ca, "delete_on_b")[0]["path"] == "Dup/song_copy.mp3"
    assert items(ca, "conflict")[0]["choice"] == "skip"

    job = run(ca, cb)
    assert job.errors == 0
    assert exists(cb.root, "Photos/new.jpg")
    assert read(cb.root, "Docs/notes.txt") == b"version 2 - longer"
    assert exists(cb.root, "Archive/report.pdf") and not exists(cb.root, "Docs/report.pdf")
    assert not exists(cb.root, "Dup/song_copy.mp3")  # moved to backup's quarantine
    assert exists(ca.root, "Photos/b.jpg")  # restored from backup
    assert read(ca.root, "Photos/a.jpg") == read(cb.root, "Photos/a.jpg")  # repaired from backup
    assert ca.q1("SELECT corrupt FROM files WHERE path='Photos/a.jpg'")[0] == 0
    assert exists(ca.root, "Extra/only_backup.txt")  # copied to main by default
    assert read(cb.root, "Edit/both.txt") == b"changed on backup!"  # conflict skipped
    # the old version of notes.txt and the removed duplicate are in B's quarantine
    batch = cb.q1("SELECT id FROM q_batches WHERE id LIKE 'sync-%'")[0]
    kinds_in_batch = sorted(f["kind"] for f in quarantine.batch_files(cb, batch))
    assert kinds_in_batch == ["removed", "replaced"]

    # only the unresolved conflict remains
    assert compare(ca, cb) == {"conflict": 1}
    conflict = items(ca, "conflict")[0]
    assert "earlier sync" in conflict["reason"]
    # resolve it in favour of main
    assert sync.set_choice(ca, "a2b", ids=[conflict["id"]]) == 1
    run(ca, cb)
    assert read(cb.root, "Edit/both.txt") == b"changed on main"
    assert compare(ca, cb) == {}


def test_source_changed_after_compare_is_not_copied(drives):
    ca, cb = drives
    compare(ca, cb)
    # content changes but size and mtime stay the same - the copy must fail verification
    corrupt_in_place(ca.root, "Docs/report.pdf")
    job = sync.SyncJob("sync", "A", "B")
    sync.run_sync(ca, cb, job)
    assert job.errors == 1
    assert not exists(cb.root, "Docs/report.pdf")
    failed = ca.q1("SELECT result FROM sync_plan WHERE path='Docs/report.pdf'")[0]
    assert "checksum" in failed
    assert not any(n.endswith(".datacatalog-tmp") for _, _, fs in os.walk(cb.root) for n in fs)


def test_scope_follows_main_excludes(drives):
    ca, cb = drives
    ca.set_meta("excludes", ["Music"])
    write(cb.root, "Music/other.mp3", b"backup only, excluded on main")
    kinds = compare(ca, cb)
    assert kinds == {"copy_to_b": 6}  # Music excluded on both sides


def test_roles_pick_main_as_source(drives):
    ca, cb = drives
    assert sync.roles(cb, ca) == (ca, cb)
