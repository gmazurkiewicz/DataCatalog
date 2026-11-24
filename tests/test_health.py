import os

import pytest

from datacatalog import analysis, catalog, health, scanner


def ata(reallocated=0, pending=0, passed=True, temp=35, when_failed="", test_passed=True):
    return {
        "smartctl": {"version": [7, 4], "messages": []},
        "device": {"name": "/dev/sda", "protocol": "ATA"},
        "model_name": "WDC WD10EZEX",
        "serial_number": "WD-123",
        "user_capacity": {"bytes": 1_000_204_886_016},
        "smart_status": {"passed": passed},
        "temperature": {"current": temp},
        "power_on_time": {"hours": 12000},
        "power_cycle_count": 900,
        "ata_smart_attributes": {
            "table": [
                {
                    "id": 5,
                    "name": "Reallocated_Sector_Ct",
                    "value": 200,
                    "worst": 200,
                    "thresh": 140,
                    "when_failed": when_failed,
                    "raw": {"value": reallocated, "string": str(reallocated)},
                },
                {
                    "id": 197,
                    "name": "Current_Pending_Sector",
                    "value": 200,
                    "worst": 200,
                    "thresh": 0,
                    "when_failed": "",
                    "raw": {"value": pending, "string": str(pending)},
                },
                {
                    "id": 9,
                    "name": "Power_On_Hours",
                    "value": 84,
                    "worst": 84,
                    "thresh": 0,
                    "when_failed": "",
                    "raw": {"value": 12000, "string": "12000"},
                },
            ]
        },
        "ata_smart_self_test_log": {
            "standard": {
                "table": [
                    {
                        "type": {"string": "Short offline"},
                        "status": {
                            "passed": test_passed,
                            "string": "Completed without error" if test_passed else "Completed: read failure",
                        },
                        "lifetime_hours": 11990,
                    }
                ]
            }
        },
    }


NVME = {
    "smartctl": {"messages": []},
    "device": {"name": "/dev/nvme0", "protocol": "NVMe"},
    "model_name": "Samsung SSD 970",
    "serial_number": "S4",
    "smart_status": {"passed": True},
    "temperature": {"current": 40},
    "power_on_time": {"hours": 3000},
    "nvme_smart_health_information_log": {
        "critical_warning": 0,
        "media_errors": 2,
        "percentage_used": 4,
        "available_spare": 100,
        "available_spare_threshold": 10,
    },
}


def test_evaluate_healthy_drive():
    s = health.evaluate(ata())
    assert s["verdict"] == "good"
    assert s["values"]["attr_5"] == 0 and s["values"]["power_on_hours"] == 12000
    assert s["last_self_test"]["passed"] is True


def test_evaluate_warnings_and_growth():
    s = health.evaluate(ata(reallocated=8, temp=58))
    assert s["verdict"] == "warning"
    texts = " ".join(f["text"] for f in s["findings"])
    assert "Reallocated sectors: 8" in texts and "High temperature" in texts
    s2 = health.evaluate(ata(reallocated=24), previous=s["values"])
    assert any("increased since the last check: 8 → 24" in f["text"] for f in s2["findings"])


@pytest.mark.parametrize(
    "data",
    [ata(passed=False), ata(when_failed="now"), ata(test_passed=False)],
)
def test_evaluate_failing(data):
    assert health.evaluate(data)["verdict"] == "bad"


def test_evaluate_nvme():
    s = health.evaluate(NVME)
    assert s["verdict"] == "warning"
    assert s["values"]["media_errors"] == 2


def test_query_falls_back_to_sat_for_usb(monkeypatch):
    monkeypatch.setattr(health, "find_smartctl", lambda: "smartctl")
    monkeypatch.setattr(health.os, "geteuid", lambda: 0, raising=False)
    calls = []

    def runner(cmd):
        calls.append(cmd)
        if "sat" in cmd:
            return 0, ata(), ""
        return 2, {"smartctl": {"messages": [{"string": "/dev/sdb: Unknown USB bridge"}]}}, ""

    data, attempts = health.query("/dev/sdb", runner=runner)
    assert data["serial_number"] == "WD-123"
    assert len(attempts) == 2 and calls[1][-3:] == ["-d", "sat", "/dev/sdb"]


def test_query_permission_error(monkeypatch):
    monkeypatch.setattr(health, "find_smartctl", lambda: "smartctl")
    monkeypatch.setattr(health.os, "geteuid", lambda: 0, raising=False)

    def runner(cmd):
        return (
            2,
            {"smartctl": {"messages": [{"string": "Smartctl open device: /dev/sda failed: Permission denied"}]}},
            "",
        )

    with pytest.raises(health.HealthError, match="No permission"):
        health.query("/dev/sda", runner=runner)


def test_check_stores_history(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "find_smartctl", lambda: "smartctl")
    monkeypatch.setattr(health.os, "geteuid", lambda: 0, raising=False)
    root = str(tmp_path)
    catalog.init_volume(root, "T", "main")
    cat = catalog.Catalog(root)
    readings = iter([ata(reallocated=1), ata(reallocated=5)])
    runner = lambda cmd: (0, next(readings), "")  # noqa: E731
    health.check(cat, runner=runner, device="/dev/sda")
    r = health.check(cat, runner=runner, device="/dev/sda")
    assert r["verdict"] == "warning"
    assert any("1 → 5" in f["text"] for f in r["summary"]["findings"])
    assert [h["values"]["attr_5"] for h in health.history(cat)] == [5, 1]


def test_name_collisions(tmp_path):
    root = str(tmp_path)
    for rel in ("Photos/IMG.jpg", "Photos/img.jpg", "Docs/a.txt", "docs/b.txt"):
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(rel.encode())
    catalog.init_volume(root, "T", "main")
    cat = catalog.Catalog(root)
    j = scanner.Job("scan")
    scanner.Scanner(cat, j).run()
    found = {(c["type"], tuple(c["names"])) for c in analysis.name_collisions(cat)}
    assert ("file", ("IMG.jpg", "img.jpg")) in found
    assert ("folder", ("Docs", "docs")) in found
