import os
import re
import subprocess

from gi.repository import GLib

HOME = os.path.expanduser("~")
ENV = dict(os.environ, LANG="C", LC_ALL="C")


def run(argv, timeout=120):
    """Run a command and return stdout ('' on any failure)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENV)
        return p.stdout
    except Exception:
        return ""


def run_full(argv, timeout=1800):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENV)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def du_children(path):
    """Disk usage of `path` and its direct children: {path: bytes}."""
    out = {}
    if not os.path.isdir(path):
        return out
    for line in run(["du", "-x", "-B1", "-d1", path], timeout=600).splitlines():
        size, _, p = line.partition("\t")
        if size.isdigit():
            out[p] = int(size)
    return out


def du_paths(paths):
    paths = [p for p in paths if os.path.exists(p)]
    out = {}
    if not paths:
        return out
    for line in run(["du", "-x", "-s", "-B1", "--", *paths], timeout=600).splitlines():
        size, _, p = line.partition("\t")
        if size.isdigit():
            out[p] = int(size)
    return out


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def fmt(n):
    if not n:
        return "0 B"
    return GLib.format_size(int(n))


_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4}


def parse_size(s):
    m = re.match(r"\s*([\d.,]+)\s*([kmgt]?i?b)", (s or "").replace("\xa0", " ").lower())
    if not m:
        return 0
    try:
        return int(float(m.group(1).replace(",", ".")) * _UNITS.get(m.group(2), 1))
    except ValueError:
        return 0


def ago(ts):
    if not ts:
        return ""
    import time
    d = time.time() - ts
    if d < 0:
        return "just now"
    for unit, secs in (("year", 31536000), ("month", 2592000), ("week", 604800),
                       ("day", 86400), ("hour", 3600)):
        if d >= secs:
            n = int(d // secs)
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "today"


PROTECTED_ROOTS = {HOME, os.path.join(HOME, ".config"), os.path.join(HOME, ".local"),
                   os.path.join(HOME, ".local/share"), os.path.join(HOME, ".cache"),
                   os.path.join(HOME, ".var"), os.path.join(HOME, ".var/app")}


def is_safe_user_path(path):
    """Only allow touching things strictly inside $HOME (never key folders themselves)."""
    rp = os.path.realpath(path)
    ap = os.path.abspath(path)
    for p in (rp, ap):
        if p in PROTECTED_ROOTS:
            return False
        for d in ("Documents", "Pictures", "Music", "Videos", "Desktop", "Downloads"):
            if p == os.path.join(HOME, d):
                return False
    return ap.startswith(HOME + os.sep)
