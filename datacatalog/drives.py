"""Detect connected drives on Windows and Linux (standard library only)."""

import json
import os
import shutil
import subprocess

from . import catalog, paths

LINUX_SKIP_FS = {
    "proc",
    "sysfs",
    "devtmpfs",
    "devpts",
    "tmpfs",
    "cgroup",
    "cgroup2",
    "pstore",
    "securityfs",
    "debugfs",
    "tracefs",
    "configfs",
    "fusectl",
    "mqueue",
    "hugetlbfs",
    "bpf",
    "autofs",
    "overlay",
    "squashfs",
    "ramfs",
    "binfmt_misc",
    "efivarfs",
    "rpc_pipefs",
    "nsfs",
    "selinuxfs",
    "fuse.portal",
    "fuse.gvfsd-fuse",
}
LINUX_SKIP_MOUNTS = ("/proc", "/sys", "/dev", "/run/user", "/snap", "/boot", "/var/lib/docker")


def _usage(path):
    try:
        u = shutil.disk_usage(path)
        return u.total, u.free
    except OSError:
        return None, None


def _with_catalog(d):
    info = catalog.read_volume_info(d["path"]) if d.get("path") else None
    d["known"] = info is not None
    d["catalog"] = info
    return d


def list_windows():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    out = []
    mask = k32.GetLogicalDrives()
    types = {2: "removable", 3: "local", 4: "network", 5: "CD/DVD", 6: "RAM"}
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype in (5,):  # skip optical drives
            continue
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maxlen = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = k32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            label,
            261,
            ctypes.byref(serial),
            ctypes.byref(maxlen),
            ctypes.byref(flags),
            fs,
            261,
        )
        if not ok:
            continue
        total, free = _usage(root)
        out.append(
            _with_catalog(
                {
                    "path": root,
                    "label": label.value,
                    "fs": fs.value,
                    "serial": f"{serial.value:08X}",
                    "type": types.get(dtype, "?"),
                    "total": total,
                    "free": free,
                    "mounted": True,
                    "system": os.environ.get("SystemDrive", "C:").upper() + "\\" == root.upper(),
                }
            )
        )
    return out


def _lsblk():
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,PATH,FSTYPE,SIZE,MOUNTPOINT,LABEL,UUID,TYPE,TRAN,MODEL"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout).get("blockdevices", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def list_linux():
    out = []
    seen = set()
    by_dev = {}
    tree = _lsblk()
    if tree:

        def walk(nodes, parent=None):
            for n in nodes:
                n["_parent"] = parent
                if n.get("path"):
                    by_dev[n["path"]] = n
                walk(n.get("children") or [], n)

        walk(tree)

    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            mounts = [line.split() for line in f]
    except OSError:
        mounts = []
    for m in mounts:
        if len(m) < 3:
            continue
        dev, mp, fs = m[0], m[1].replace("\\040", " "), m[2]
        if fs in LINUX_SKIP_FS or mp.startswith(LINUX_SKIP_MOUNTS) or mp in seen:
            continue
        if not dev.startswith("/dev/"):
            continue
        seen.add(mp)
        total, free = _usage(mp)
        n = by_dev.get(dev, {})
        p = n.get("_parent") or {}
        out.append(
            _with_catalog(
                {
                    "path": mp,
                    "device": dev,
                    "label": n.get("label") or "",
                    "fs": n.get("fstype") or fs,
                    "serial": n.get("uuid") or "",
                    "type": "USB" if (p.get("tran") or n.get("tran")) == "usb" else "local",
                    "model": (p.get("model") or "").strip(),
                    "total": total,
                    "free": free,
                    "mounted": True,
                    "system": mp == "/",
                }
            )
        )
    # Partitions that are visible but not mounted - shown with a hint.
    for dev, n in by_dev.items():
        if n.get("mountpoint") or n.get("type") != "part" or not n.get("fstype"):
            continue
        if n["fstype"] in ("swap", "LVM2_member", "crypto_LUKS"):
            continue
        hint = (
            f"sudo mkdir -p /mnt/drive && sudo mount -t ntfs3 {dev} /mnt/drive"
            if n["fstype"] == "ntfs"
            else f"sudo mkdir -p /mnt/drive && sudo mount {dev} /mnt/drive"
        )
        p = n.get("_parent") or {}
        out.append(
            {
                "path": None,
                "device": dev,
                "label": n.get("label") or "",
                "fs": n["fstype"],
                "serial": n.get("uuid") or "",
                "type": "USB" if p.get("tran") == "usb" else "local",
                "model": (p.get("model") or "").strip(),
                "total": int(n["size"]) if n.get("size") else None,
                "free": None,
                "mounted": False,
                "mount_hint": hint,
                "known": False,
                "catalog": None,
                "system": False,
            }
        )
    return out


def list_drives():
    try:
        drives = list_windows() if paths.IS_WINDOWS else list_linux()
    except Exception as e:  # never break the app when detection fails
        return [{"error": str(e)}]
    # Known drives first, then external ones, the system drive last.
    drives.sort(key=lambda d: (not d.get("known"), d.get("system", False), not d.get("mounted"), d.get("path") or ""))
    return drives
