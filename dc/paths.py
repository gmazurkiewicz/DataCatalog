"""OS-independent path handling.

All paths stored in the catalog are relative to the drive root and use '/'.
"""
import os
import sys

IS_WINDOWS = sys.platform.startswith("win")

CATALOG_DIR = ".datacatalog"

# Directories skipped at the drive root (compared case-insensitively).
ROOT_EXCLUDED = {
    "system volume information",
    "$recycle.bin",
    ".trash-1000",
    ".trashes",
    ".spotlight-v100",
    ".fseventsd",
    "lost+found",
}
# Directories skipped at any depth.
ALWAYS_EXCLUDED = {CATALOG_DIR, "system volume information", "$recycle.bin"}


def to_os(root, rel=""):
    """Turn a relative catalog path into an absolute OS path.

    On Windows adds the \\\\?\\ prefix so paths longer than 260 characters work.
    """
    p = os.path.join(root, *rel.split("/")) if rel else root
    if IS_WINDOWS:
        p = os.path.abspath(p)
        if not p.startswith("\\\\?\\"):
            if p.startswith("\\\\"):
                p = "\\\\?\\UNC\\" + p[2:]
            else:
                p = "\\\\?\\" + p
    return p


def parent_of(rel):
    i = rel.rfind("/")
    return rel[:i] if i >= 0 else ""


def name_of(rel):
    return rel[rel.rfind("/") + 1:]


def join(a, b):
    return f"{a}/{b}" if a else b


def ancestors(rel_dir):
    """Return the directory itself and all its ancestors up to the root ('')."""
    out = [rel_dir]
    while rel_dir:
        rel_dir = parent_of(rel_dir)
        out.append(rel_dir)
    return out


def prefix_range(rel_dir):
    """(lo, hi) for a `path >= lo AND path < hi` query matching every file
    inside a directory. '0' is the ASCII character right after '/'."""
    return rel_dir + "/", rel_dir + "0"
