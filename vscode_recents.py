#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Publish VS Code's recently opened folders/workspaces to the Linux desktop.

VS Code fills the Windows Jump List and the macOS dock menu with recent workspaces,
but on Linux it publishes nothing. This tool reads VS Code's own MRU list
("File > Open Recent") and registers the entries in the freedesktop.org
recently-used registry ($XDG_DATA_HOME/recently-used.xbel), attributed to VS Code's
.desktop file. KDE Plasma then lists them in the task manager context menu under
"Recent" for the VS Code icon.

This is exactly what a native implementation of Electron's app.addRecentDocument()
on Linux would do, so it doubles as a field test for that proposal.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

try:  # GLib >= 2.86 moved GDesktopAppInfo to GioUnix
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix  # noqa: E402

    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

MRU_KEY = "history.recentlyOpenedPathsList"
DEFAULT_DESKTOP_IDS = (
    "com.microsoft.VSCode.desktop",  # Microsoft .deb / .rpm
    "code.desktop",                  # older packages, Arch
    "code_code.desktop",             # Snap
    "com.visualstudio.code.desktop", # Flatpak
)
WRITE_SPACING_S = 3.0  # kactivitymanagerd batches nearby changes; keep events in separate seconds


def xbel_path() -> Path:
    return Path(GLib.get_user_data_dir()) / "recently-used.xbel"


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "vscode-recents" / "state.json"


def default_storage() -> Path:
    return Path.home() / ".vscode-shared" / "sharedStorage" / "state.vscdb"


# ---------------------------------------------------------------- VS Code MRU

def read_mru(storage: Path) -> list[str]:
    """Local folders/workspaces from VS Code's MRU, most recent first."""
    if not storage.exists():
        raise SystemExit(f"VS Code storage not found: {storage} (use --storage)")
    con = sqlite3.connect(f"file:{storage}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (MRU_KEY,)).fetchone()
    finally:
        con.close()
    if not row:
        return []
    entries = json.loads(row[0]).get("entries", [])
    paths: list[str] = []
    for e in entries:
        uri = e.get("folderUri") or (e.get("workspace") or {}).get("configPath")
        if not uri or e.get("remoteAuthority"):
            continue
        u = urlparse(uri)
        if u.scheme != "file":
            continue  # vscode-remote:// etc. cannot be opened through the .desktop file
        p = unquote(u.path)
        if os.path.exists(p) and p not in paths:
            paths.append(p)
    return paths


# ------------------------------------------------------------- desktop entry

def resolve_desktop(desktop_id: str | None) -> tuple[str, str]:
    """Return (application name, Exec command line) for VS Code's .desktop file.

    KDE's kactivitymanagerd attributes XBEL entries by matching `exec` exactly
    against the .desktop Exec= line, so we must use the real one."""
    ids = [desktop_id] if desktop_id else list(DEFAULT_DESKTOP_IDS)
    for did in ids:
        did = did if did.endswith(".desktop") else did + ".desktop"
        try:
            info = DesktopAppInfo.new(did)  # PyGObject raises instead of returning None
        except TypeError:
            info = None
        if info is not None:
            return did[: -len(".desktop")], info.get_commandline()
    raise SystemExit(f"no VS Code .desktop file found (tried {', '.join(ids)}); use --desktop-id")


# --------------------------------------------------------------- XBEL access

def _load() -> GLib.BookmarkFile:
    bf = GLib.BookmarkFile()
    path = xbel_path()
    if path.exists():
        bf.load_from_file(str(path))
    return bf


def _mime(path: str) -> str:
    if os.path.isdir(path):
        return "inode/directory"
    ctype, _ = Gio.content_type_guess(path, None)
    return Gio.content_type_get_mime_type(ctype) or "application/octet-stream"


def register(path: str, app: str, exec_: str) -> None:
    """Record one use of `path` by the app (same as gtk_recent_manager_add_full)."""
    bf = _load()  # reload right before writing: other apps share this file
    uri = GLib.filename_to_uri(path, None)
    bf.set_mime_type(uri, _mime(path))
    if not bf.has_item(uri) or not bf.get_title(uri):
        bf.set_title(uri, os.path.basename(path.rstrip("/")) or path)
    bf.add_application(uri, app, exec_)
    xbel_path().parent.mkdir(parents=True, exist_ok=True)
    bf.to_file(str(xbel_path()))


def unregister(paths: list[str], app: str) -> int:
    """Remove only *this app's* registration; never touch other apps' entries."""
    if not paths:
        return 0
    bf = _load()
    removed = 0
    for path in paths:
        uri = GLib.filename_to_uri(path, None)
        try:
            if not bf.has_application(uri, app):
                continue
            bf.remove_application(uri, app)
            if not bf.get_applications(uri):
                bf.remove_item(uri)
            removed += 1
        except GLib.Error:
            continue
    if removed:
        bf.to_file(str(xbel_path()))
    return removed


def registered_paths(app: str) -> list[str]:
    bf = _load()
    out = []
    for uri in bf.get_uris():
        try:
            if bf.has_application(uri, app) and uri.startswith("file://"):
                out.append(GLib.filename_from_uri(uri)[0])
        except GLib.Error:
            pass
    return out


# ------------------------------------------------------- KDE stats (optional)

def kde_forget(paths: list[str], app: str) -> None:
    """Drop Plasma's usage stats for paths VS Code no longer lists.

    kactivitymanagerd only imports *additions* from recently-used.xbel, so without
    this, entries removed from VS Code's MRU would linger in Plasma's menu."""
    if not paths:
        return
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        acts = bus.call_sync("org.kde.ActivityManager", "/ActivityManager/Activities",
                             "org.kde.ActivityManager.Activities", "ListActivities",
                             None, GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 2000, None)
        for act in acts.unpack()[0]:
            for p in paths:
                bus.call_sync("org.kde.ActivityManager", "/ActivityManager/Resources/Scoring",
                              "org.kde.ActivityManager.ResourcesScoring", "DeleteStatsForResource",
                              GLib.Variant("(sss)", (act, app, p)), None,
                              Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        pass  # not KDE, or the activity manager is not running


# --------------------------------------------------------------------- state

def load_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


def newly_opened(current: list[str], previous: list[str]) -> list[str]:
    """Entries opened since the last run, most recent first.

    VS Code moves an entry to the front when it is opened, so the new items are the
    prefix of `current` up to the previous head."""
    if not previous:
        return list(current)
    head = previous[0]
    out = []
    for p in current:
        if p == head:
            break
        out.append(p)
    return out


# ---------------------------------------------------------------------- main

def cmd_sync(a) -> int:
    app, exec_ = resolve_desktop(a.desktop_id)
    full = read_mru(Path(a.storage))
    top = full[: a.max]
    state = load_state()
    previous = state.get("mru", []) if state.get("app") == app else []

    added = newly_opened(top, previous)
    stale = [p for p in previous if p not in top]
    gone = [p for p in previous if p not in full]  # removed from VS Code's list entirely

    if a.dry_run:
        print(json.dumps({"app": app, "exec": exec_, "add": added, "unregister": stale,
                          "forget_kde": gone}, indent=2))
        return 0

    # oldest first, spaced, so Plasma ends up with the same order as VS Code
    for i, p in enumerate(reversed(added)):
        if i:
            time.sleep(WRITE_SPACING_S)
        register(p, app, exec_)
    n_unreg = unregister(stale, app)
    kde_forget(gone, app)
    save_state({"app": app, "exec": exec_, "mru": top})
    if a.verbose or added or n_unreg:
        print(f"{app}: +{len(added)} registered, -{n_unreg} unregistered, {len(gone)} forgotten in KDE")
    return 0


def cmd_clear(a) -> int:
    app, _ = resolve_desktop(a.desktop_id)
    paths = registered_paths(app)
    n = unregister(paths, app)
    kde_forget(paths, app)
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass
    print(f"{app}: removed {n} entries")
    return 0


def cmd_status(a) -> int:
    app, exec_ = resolve_desktop(a.desktop_id)
    print(f"desktop entry : {app}.desktop")
    print(f"exec          : {exec_}")
    print(f"VS Code MRU   : {a.storage}")
    for p in read_mru(Path(a.storage))[: a.max]:
        print(f"  {p}")
    print(f"registered in : {xbel_path()}")
    for p in registered_paths(app):
        print(f"  {p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", nargs="?", default="sync", choices=("sync", "clear", "status"))
    ap.add_argument("--desktop-id", help="VS Code .desktop file (default: autodetect)")
    ap.add_argument("--storage", default=str(default_storage()),
                    help="VS Code shared storage DB (default: %(default)s)")
    ap.add_argument("--max", type=int, default=10, help="entries to publish (default: 10)")
    ap.add_argument("--dry-run", action="store_true", help="show what sync would do")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    return {"sync": cmd_sync, "clear": cmd_clear, "status": cmd_status}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
