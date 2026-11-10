"""Executes the actions attached to items. Every function here runs in a worker thread
and returns (ok: bool, message: str)."""
import os
import re
import shutil

from gi.repository import Gio

from .util import is_safe_user_path, run, run_full


def rpm_preview(pkgs):
    """Ask dnf what *would* be removed (nothing is changed). Returns (names, freed_text, error)."""
    code, out, err = run_full(["dnf", "remove", "--assumeno", *pkgs], timeout=120)
    text = out + "\n" + err
    names, section = [], None
    for line in text.splitlines():
        if re.match(r"^Removing( dependent packages| unused dependencies)?:", line):
            section = line.rstrip(":")
            continue
        if section and line.startswith(" ") and not line.startswith("  ") and line.strip():
            names.append((section, line.split()[0]))
        elif section and not line.startswith(" "):
            section = None
    m = re.search(r"After this operation, (.+?) will be freed", text)
    if not names:
        err_line = next((l for l in text.splitlines() if "protected" in l.lower() or "error" in l.lower() or "No packages" in l), "")
        return [], "", err_line or "dnf could not plan this removal."
    return names, m.group(1) if m else "", ""


def _trash(paths):
    done, failed = 0, []
    for p in paths:
        if not p or not os.path.lexists(p):
            continue
        if not is_safe_user_path(p):
            failed.append(p)
            continue
        try:
            Gio.File.new_for_path(p).trash(None)
            done += 1
        except Exception:  # noqa: BLE001 - trash unsupported -> report
            failed.append(p)
    return done, failed


def _delete(paths, contents_only):
    freed_fail = []
    for p in paths:
        if not os.path.lexists(p):
            continue
        if not is_safe_user_path(p):
            freed_fail.append(p)
            continue
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                if contents_only:
                    for child in os.listdir(p):
                        c = os.path.join(p, child)
                        if os.path.isdir(c) and not os.path.islink(c):
                            shutil.rmtree(c, ignore_errors=True)
                        else:
                            os.unlink(c)
                else:
                    shutil.rmtree(p, ignore_errors=True)
            else:
                os.unlink(p)
        except OSError:
            freed_fail.append(p)
    return freed_fail


def execute(action):
    t = action["type"]
    if t == "rpm-remove":
        code, out, err = run_full(["pkexec", "dnf", "remove", "-y", *action["pkgs"]])
        if code == 126 or code == 127:
            return False, "Cancelled (password not entered)."
        if code != 0:
            last = (err or out).strip().splitlines()[-1:] or ["unknown error"]
            return False, "dnf failed: " + last[0]
        if action.get("also_trash"):
            _trash(action["also_trash"])
        return True, "Uninstalled."
    if t == "cmd":
        argv = action["argv"]
        code, out, err = run_full(argv)
        if argv[0] == "pkexec" and code in (126, 127):
            return False, "Cancelled (password not entered)."
        if code != 0:
            msg = (err or out).strip().splitlines()
            return False, (msg[-1] if msg else f"{argv[0]} failed")
        if action.get("also_trash"):
            _trash(action["also_trash"])
        return True, "Done."
    if t == "trash":
        done, failed = _trash(action["paths"])
        if failed:
            return False, f"Couldn't move {len(failed)} item(s) to Trash."
        return True, "Moved to Trash. Empty the Trash to free the space."
    if t == "delete":
        failed = _delete(action["paths"], action.get("contents_only", True))
        if failed:
            return False, f"Couldn't delete {len(failed)} item(s)."
        return True, "Cleared."
    if t == "uri":
        ok = run(["xdg-open", action["uri"]]) is not None
        return ok, "Opened Steam — confirm the uninstall there."
    return False, f"Unknown action {t}"
