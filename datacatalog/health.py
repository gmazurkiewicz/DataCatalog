"""Drive health through SMART, using smartctl from smartmontools (an external program).

  Windows:  install smartmontools from https://www.smartmontools.org and run DataCatalog
            as administrator. The drive is addressed by its letter ("E:").
  Linux/Pi: sudo apt install smartmontools. smartctl needs root: run DataCatalog as root,
            or allow it without a password (sudo visudo):
                youruser ALL=(root) NOPASSWD: /usr/sbin/smartctl
            DataCatalog then calls `sudo -n smartctl` automatically.

USB enclosures often hide SMART; when the first attempt fails, `-d sat` is tried.
Every reading is stored in the drive's own catalog (table smart_readings), so the
history travels with the drive and the app can tell when bad counters grow.
"""

import json
import os
import shutil
import subprocess
import time

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS smart_readings (
    id        INTEGER PRIMARY KEY,
    ts        REAL NOT NULL,
    host      TEXT,
    device    TEXT,
    model     TEXT,
    serial    TEXT,
    verdict   TEXT NOT NULL,          -- good | warning | bad | unknown
    summary   TEXT NOT NULL,          -- JSON produced by evaluate()
    raw       TEXT                    -- full smartctl JSON output
);
"""

# ATA attributes worth watching: id -> (label, severity when raw value > 0)
ATA_WATCH = {
    5: ("Reallocated sectors", "warning"),
    10: ("Spin retries", "warning"),
    184: ("End-to-end errors", "warning"),
    187: ("Reported uncorrectable errors", "warning"),
    188: ("Command timeouts", "info"),
    196: ("Reallocation events", "warning"),
    197: ("Pending sectors", "warning"),
    198: ("Offline uncorrectable sectors", "warning"),
    199: ("Interface CRC errors (cable/connector)", "info"),
}
HDD_TEMP_WARN = 55
NVME_TEMP_WARN = 70
WINDOWS_SMARTCTL = [
    r"C:\Program Files\smartmontools\bin\smartctl.exe",
    r"C:\Program Files (x86)\smartmontools\bin\smartctl.exe",
]


class HealthError(Exception):
    pass


def ensure_table(cat):
    with cat.lock:
        cat.conn.executescript(SCHEMA)
        cat.conn.commit()


def find_smartctl():
    p = shutil.which("smartctl")
    if p:
        return p
    if paths.IS_WINDOWS:
        for c in WINDOWS_SMARTCTL:
            if os.path.isfile(c):
                return c
    for c in ("/usr/sbin/smartctl", "/usr/local/sbin/smartctl", "/sbin/smartctl"):
        if os.path.isfile(c):
            return c
    return None


def device_for(root):
    """smartctl device name for the drive that holds `root`."""
    if paths.IS_WINDOWS:
        drive = os.path.splitdrive(os.path.abspath(root))[0]
        if not drive or not drive.endswith(":"):
            raise HealthError("SMART is only available for drives with a letter (e.g. E:).")
        return drive.upper()
    real = os.path.realpath(root)
    best, dev = "", None
    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2 or not parts[0].startswith("/dev/"):
                    continue
                mp = parts[1].replace("\\040", " ")
                if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
                    best, dev = mp, parts[0]
    except OSError as e:
        raise HealthError(f"Cannot read mount table: {e}") from e
    if not dev:
        raise HealthError("Cannot find the block device for this drive.")
    # partition -> whole disk (/dev/sda1 -> /dev/sda, /dev/nvme0n1p1 -> /dev/nvme0n1)
    try:
        r = subprocess.run(["lsblk", "-no", "PKNAME", dev], capture_output=True, text=True, timeout=10)
        parent = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
        if parent:
            return "/dev/" + parent
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return dev


def _run(cmd, timeout=90):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise HealthError("smartctl did not respond in time.") from e
    except OSError as e:
        raise HealthError(f"Cannot run smartctl: {e}") from e
    try:
        data = json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        data = {}
    return r.returncode, data, (r.stdout + r.stderr)


def _messages(data):
    return [m.get("string", "") for m in data.get("smartctl", {}).get("messages", [])]


def _has_smart(data):
    return "smart_status" in data or "ata_smart_attributes" in data or "nvme_smart_health_information_log" in data


def query(device, args=("-a",), runner=None):
    """Run smartctl -j <args> on a device, with USB bridge and sudo fallbacks.

    Returns (smartctl JSON, list of command attempts)."""
    exe = find_smartctl()
    if not exe and runner is None:
        raise HealthError("smartctl not found. Install smartmontools.")
    runner = runner or _run
    attempts = []
    variants = [[], ["-d", "sat"]]
    use_sudo = [False]
    if not paths.IS_WINDOWS and hasattr(os, "geteuid") and os.geteuid() != 0:
        use_sudo.append(True)
    last = None
    for sudo in use_sudo:
        for extra in variants:
            cmd = (["sudo", "-n"] if sudo else []) + [exe or "smartctl", "-j", *args, *extra, device]
            code, data, text = runner(cmd)
            attempts.append({"cmd": " ".join(cmd), "exit": code, "messages": _messages(data)})
            last = (code, data, text)
            # bit 1: device open failed; anything else still gives usable data
            if data and _has_smart(data) and not (code & 2):
                return data, attempts
            if data and args != ("-a",) and not (code & 3):
                return data, attempts  # e.g. starting a self-test
        if last and "a password is required" in last[2].lower():
            break
    msgs = " ".join(sum((a["messages"] for a in attempts), [])) or (last[2].strip() if last else "")
    low = msgs.lower()
    if "permission denied" in low or "operation not permitted" in low or "access is denied" in low:
        hint = (
            "Run DataCatalog as administrator."
            if paths.IS_WINDOWS
            else "smartctl needs root: run as root or allow 'sudo -n smartctl' (see the Health tab)."
        )
        raise HealthError(f"No permission to read SMART. {hint}")
    if "password is required" in low:
        raise HealthError(
            "smartctl needs root and sudo asks for a password - allow it without one (see the Health tab)."
        )
    raise HealthError(f"SMART data not available for this drive ({msgs[:300] or 'no output'}).")


def evaluate(data, previous=None):
    """Turn smartctl JSON into a verdict, key values and human readable findings."""
    findings = []  # (severity, text)
    values = {}
    nvme = "nvme_smart_health_information_log" in data
    status = data.get("smart_status", {})
    if status.get("passed") is False:
        findings.append(("bad", "The drive reports that it is FAILING (SMART overall-health check failed)."))
    temp = (data.get("temperature") or {}).get("current")
    if temp is not None:
        values["temperature"] = temp
        limit = NVME_TEMP_WARN if nvme else HDD_TEMP_WARN
        if temp >= limit:
            findings.append(("warning", f"High temperature: {temp} °C."))
    poh = (data.get("power_on_time") or {}).get("hours")
    if poh is not None:
        values["power_on_hours"] = poh
    if data.get("power_cycle_count") is not None:
        values["power_cycles"] = data["power_cycle_count"]

    attributes = []
    for a in (data.get("ata_smart_attributes") or {}).get("table", []):
        raw = (a.get("raw") or {}).get("value", 0)
        item = {
            "id": a.get("id"),
            "name": a.get("name"),
            "value": a.get("value"),
            "worst": a.get("worst"),
            "thresh": a.get("thresh"),
            "raw": raw,
            "raw_string": (a.get("raw") or {}).get("string"),
            "when_failed": a.get("when_failed") or "",
            "watched": a.get("id") in ATA_WATCH,
        }
        attributes.append(item)
        if item["when_failed"] == "now":
            findings.append(("bad", f"Attribute {item['name']} is below its failure threshold now."))
        elif item["when_failed"] == "past":
            findings.append(("warning", f"Attribute {item['name']} was below its failure threshold in the past."))
        if item["id"] in ATA_WATCH:
            label, sev = ATA_WATCH[item["id"]]
            values[f"attr_{item['id']}"] = raw
            if raw and raw > 0:
                findings.append((sev, f"{label}: {raw}."))

    if nvme:
        log = data["nvme_smart_health_information_log"]
        for k in (
            "critical_warning",
            "media_errors",
            "percentage_used",
            "available_spare",
            "available_spare_threshold",
            "unsafe_shutdowns",
            "num_err_log_entries",
        ):
            if k in log:
                values[k] = log[k]
        if log.get("critical_warning"):
            findings.append(("bad", f"NVMe critical warning flags: {log['critical_warning']}."))
        if log.get("media_errors"):
            findings.append(("warning", f"Media and data integrity errors: {log['media_errors']}."))
        if log.get("percentage_used", 0) >= 90:
            findings.append(("warning", f"Rated endurance used: {log['percentage_used']} %."))
        if log.get("available_spare") is not None and log.get("available_spare_threshold") is not None:
            if log["available_spare"] <= log["available_spare_threshold"]:
                findings.append(("bad", "Available spare is at or below the threshold."))

    tests = (data.get("ata_smart_self_test_log") or {}).get("standard", {}).get("table", [])
    last_test = None
    if tests:
        t = tests[0]
        st = t.get("status") or {}
        last_test = {
            "type": (t.get("type") or {}).get("string"),
            "passed": st.get("passed"),
            "status": st.get("string"),
            "hours": t.get("lifetime_hours"),
        }
        if st.get("passed") is False:
            findings.append(("bad", f"The last self-test failed: {st.get('string')}."))
    running = (data.get("ata_smart_data") or {}).get("self_test", {}).get("status", {})
    test_running = None
    if running.get("remaining_percent") is not None:
        test_running = f"Self-test in progress, {running['remaining_percent']} % remaining."

    errors = (data.get("ata_smart_error_log") or {}).get("summary", {}).get("count")
    if errors:
        values["error_log_count"] = errors
        findings.append(("info", f"The drive's error log has {errors} entries."))

    # growth since the previous reading is the most important early warning
    if previous:
        for key, label in [(f"attr_{i}", ATA_WATCH[i][0]) for i in (5, 187, 197, 198)] + [
            ("media_errors", "Media errors")
        ]:
            old, new = previous.get(key), values.get(key)
            if old is not None and new is not None and new > old:
                findings.append(("warning", f"{label} increased since the last check: {old} → {new}."))

    if not _has_smart(data):
        verdict = "unknown"
    elif any(s == "bad" for s, _ in findings):
        verdict = "bad"
    elif any(s == "warning" for s, _ in findings):
        verdict = "warning"
    else:
        verdict = "good"
    return {
        "verdict": verdict,
        "model": data.get("model_name") or data.get("model_family") or "",
        "family": data.get("model_family") or "",
        "serial": data.get("serial_number") or "",
        "firmware": data.get("firmware_version") or "",
        "capacity": (data.get("user_capacity") or {}).get("bytes"),
        "protocol": (data.get("device") or {}).get("protocol"),
        "rotation_rate": data.get("rotation_rate"),
        "values": values,
        "findings": [{"severity": s, "text": t} for s, t in findings],
        "attributes": attributes,
        "last_self_test": last_test,
        "self_test_running": test_running,
    }


def latest(cat):
    ensure_table(cat)
    r = cat.q1("SELECT * FROM smart_readings ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    d = dict(r)
    d["summary"] = json.loads(d["summary"])
    d.pop("raw", None)
    return d


def history(cat, limit=50):
    ensure_table(cat)
    out = []
    for r in cat.q("SELECT id, ts, verdict, summary FROM smart_readings ORDER BY id DESC LIMIT ?", (limit,)):
        s = json.loads(r["summary"])
        out.append(
            {
                "id": r["id"],
                "ts": r["ts"],
                "verdict": r["verdict"],
                "values": s.get("values", {}),
                "findings": len(s.get("findings", [])),
            }
        )
    return out


def check(cat, runner=None, device=None):
    """Read SMART for the drive of `cat`, store and return the evaluated reading."""
    from .catalog import hostname

    ensure_table(cat)
    device = device or device_for(cat.root)
    data, attempts = query(device, runner=runner)
    prev = latest(cat)
    summary = evaluate(data, prev["summary"]["values"] if prev else None)
    summary["device"] = device
    summary["attempts"] = attempts
    with cat.lock:
        cat.conn.execute(
            "INSERT INTO smart_readings(ts,host,device,model,serial,verdict,summary,raw) VALUES(?,?,?,?,?,?,?,?)",
            (
                time.time(),
                hostname(),
                device,
                summary["model"],
                summary["serial"],
                summary["verdict"],
                json.dumps(summary, ensure_ascii=False),
                json.dumps(data),
            ),
        )
        cat.conn.commit()
    return latest(cat)


def start_self_test(cat, kind="short", runner=None, device=None):
    if kind not in ("short", "long"):
        raise HealthError("Unknown self-test type.")
    device = device or device_for(cat.root)
    data, _ = query(device, args=("-t", kind), runner=runner)
    minutes = None
    st = data.get("ata_smart_data", {}).get("self_test", {}).get("polling_minutes", {})
    if st:
        minutes = st.get("short" if kind == "short" else "extended")
    return {"started": kind, "minutes": minutes, "messages": _messages(data)}
