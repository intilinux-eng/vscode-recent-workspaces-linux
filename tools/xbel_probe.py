#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reproducible probe for the proposed Linux implementation of Electron's
app.addRecentDocument() / app.clearRecentDocuments().

It mirrors what the native code would do:
  1. resolve the app's .desktop file from its desktop ID (CHROME_DESKTOP)
     with GDesktopAppInfo and read its Exec command line;
  2. register the path in $XDG_DATA_HOME/recently-used.xbel with GBookmarkFile,
     using application name = desktop ID without ".desktop" and exec = Exec;
  3. (KDE) check which agent kactivitymanagerd assigned to the event, i.e. whether
     Plasma's task manager will list it under the app's "Recent" section.

Examples:
  xbel_probe.py ~/Projects/foo                       # correct exec (from .desktop)
  xbel_probe.py ~/Projects/bar --exec "/usr/share/code/code %u"   # mismatching exec
  xbel_probe.py ~/Projects/foo --remove              # clearRecentDocuments semantics
"""
import argparse, os, sqlite3, time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
try:  # GLib >= 2.86 moved GDesktopAppInfo to GioUnix
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

XBEL = os.path.join(GLib.get_user_data_dir(), "recently-used.xbel")
KAMD_DB = os.path.join(GLib.get_user_data_dir(), "kactivitymanagerd/resources/database")


def load():
    bf = GLib.BookmarkFile()
    if os.path.exists(XBEL):
        bf.load_from_file(XBEL)
    return bf


def mime_for(path):
    if os.path.isdir(path):
        return "inode/directory"
    ctype, _ = Gio.content_type_guess(path, None)
    return Gio.content_type_get_mime_type(ctype) or "application/octet-stream"


def kde_agents(path):
    if not os.path.exists(KAMD_DB):
        return None
    c = sqlite3.connect(f"file:{KAMD_DB}?mode=ro", uri=True)
    rows = c.execute(
        "select initiatingAgent, datetime(start,'unixepoch','localtime') from ResourceEvent "
        "where targettedResource = ? order by start desc limit 5", (path,)).fetchall()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    # Explicit default on purpose: when run from a terminal hosted by another Electron
    # app, CHROME_DESKTOP belongs to *that* app, not to VS Code.
    ap.add_argument("--desktop-id", default="com.microsoft.VSCode.desktop")
    ap.add_argument("--exec", dest="exec_", help="override Exec (to reproduce a mismatch)")
    ap.add_argument("--remove", action="store_true", help="remove only this app's registration")
    a = ap.parse_args()

    path = os.path.abspath(os.path.expanduser(a.path))
    uri = GLib.filename_to_uri(path, None)
    app_id = a.desktop_id[:-8] if a.desktop_id.endswith(".desktop") else a.desktop_id
    info = DesktopAppInfo.new(a.desktop_id if a.desktop_id.endswith(".desktop") else a.desktop_id + ".desktop")
    if info is None and not a.exec_:
        raise SystemExit(f"desktop file not found for {a.desktop_id}")
    exec_ = a.exec_ or info.get_commandline()

    bf = load()
    if a.remove:
        try:
            bf.remove_application(uri, app_id)
            if not bf.get_applications(uri):
                bf.remove_item(uri)
            print(f"removed registration of {app_id} for {path}")
        except GLib.Error as e:
            print("nothing to remove:", e.message)
    else:
        bf.set_mime_type(uri, mime_for(path))
        bf.set_title(uri, os.path.basename(path))
        bf.add_application(uri, app_id, exec_)
        print(f"registered {path}\n  name={app_id}\n  exec={exec_}")
    bf.to_file(XBEL)

    if not a.remove:
        time.sleep(4)  # kactivitymanagerd picks up the change via KDirWatch
        rows = kde_agents(path)
        if rows is None:
            print("kactivitymanagerd database not found (not KDE?)")
        else:
            print("KDE agents recorded for this path (newest first):")
            for agent, when in rows:
                mark = "  <-- matches app" if agent == app_id else ""
                print(f"  {when}  {agent}{mark}")


if __name__ == "__main__":
    main()
