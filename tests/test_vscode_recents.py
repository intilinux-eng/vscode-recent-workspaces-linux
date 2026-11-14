# SPDX-License-Identifier: MIT
"""Unit tests. Everything runs in a temporary XDG environment: the real
recently-used.xbel, VS Code storage and KDE activity manager are never touched."""
import json
import re
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Must be set before GLib is imported: it caches the XDG directories.
TMP = tempfile.TemporaryDirectory()
ROOT = Path(TMP.name)
os.environ["XDG_DATA_HOME"] = str(ROOT / "data")
os.environ["XDG_STATE_HOME"] = str(ROOT / "state")
os.environ["XDG_DATA_DIRS"] = str(ROOT / "sysdata")
(ROOT / "data" / "applications").mkdir(parents=True)
# GLib ignores .desktop files whose Exec program does not exist, so use a real one
FAKE_CODE = ROOT / "bin" / "code"
FAKE_CODE.parent.mkdir()
FAKE_CODE.write_text("#!/bin/sh\n")
FAKE_CODE.chmod(0o755)
EXEC = f"{FAKE_CODE} %F"
(ROOT / "data" / "applications" / "test-code.desktop").write_text(
    f"[Desktop Entry]\nType=Application\nName=Test Code\nExec={EXEC}\n")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vscode_recents as vr  # noqa: E402

from gi.repository import GLib  # noqa: E402

vr.WRITE_SPACING_S = 0
vr.kde_forget = lambda paths, app: None  # never talk to the real session bus


def make_storage(entries):
    db = ROOT / f"state-{len(list(ROOT.glob('state-*.vscdb')))}.vscdb"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    con.execute("INSERT INTO ItemTable VALUES (?, ?)", (vr.MRU_KEY, json.dumps({"entries": entries})))
    con.commit()
    con.close()
    return db


def set_mru(db, entries):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO ItemTable VALUES (?, ?)", (vr.MRU_KEY, json.dumps({"entries": entries})))
    con.commit()
    con.close()


def folder(name):
    p = ROOT / "projects" / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def bookmarks():
    bf = GLib.BookmarkFile()
    if vr.xbel_path().exists():
        bf.load_from_file(str(vr.xbel_path()))
    return bf


def uri(p):
    return GLib.filename_to_uri(p, None)


class NewlyOpened(unittest.TestCase):
    def test_first_run_adds_everything(self):
        self.assertEqual(vr.newly_opened(["a", "b"], []), ["a", "b"])

    def test_new_entry_at_front(self):
        self.assertEqual(vr.newly_opened(["c", "a", "b"], ["a", "b"]), ["c"])

    def test_reopened_entry_moves_to_front(self):
        self.assertEqual(vr.newly_opened(["b", "a", "c"], ["a", "b", "c"]), ["b"])

    def test_no_change(self):
        self.assertEqual(vr.newly_opened(["a", "b"], ["a", "b"]), [])


class ReadMru(unittest.TestCase):
    def test_filters_and_order(self):
        a, b, ws = folder("alpha"), folder("beta"), folder("ws")
        wsfile = os.path.join(ws, "x.code-workspace")
        Path(wsfile).write_text("{}")
        db = make_storage([
            {"folderUri": uri(a)},
            {"folderUri": "vscode-remote://ssh-remote+box/home/u/p", "remoteAuthority": "ssh-remote+box"},
            {"folderUri": uri(str(ROOT / "deleted"))},
            {"workspace": {"id": "1", "configPath": uri(wsfile)}},
            {"fileUri": uri(wsfile)},          # files are not published
            {"folderUri": uri(b)},
            {"folderUri": uri(a)},             # duplicate
        ])
        self.assertEqual(vr.read_mru(db), [a, wsfile, b])


class Desktop(unittest.TestCase):
    def test_resolves_exec_from_desktop_file(self):
        self.assertEqual(vr.resolve_desktop("test-code"), ("test-code", EXEC))

    def test_missing_desktop_file(self):
        with self.assertRaises(SystemExit):
            vr.resolve_desktop("does-not-exist")


class Registry(unittest.TestCase):
    def setUp(self):
        vr.xbel_path().unlink(missing_ok=True)

    def test_unregister_keeps_other_apps(self):
        p = folder("shared")
        bf = GLib.BookmarkFile()
        bf.set_mime_type(uri(p), "inode/directory")
        bf.add_application(uri(p), "org.kde.dolphin", "dolphin %u")
        vr.xbel_path().parent.mkdir(parents=True, exist_ok=True)
        bf.to_file(str(vr.xbel_path()))

        vr.register(p, "test-code", EXEC)
        self.assertTrue(bookmarks().has_application(uri(p), "test-code"))
        self.assertEqual(vr.unregister([p], "test-code"), 1)
        b = bookmarks()
        self.assertFalse(b.has_application(uri(p), "test-code"))
        self.assertTrue(b.has_application(uri(p), "org.kde.dolphin"))

    def test_item_removed_when_no_app_left(self):
        p = folder("solo")
        vr.register(p, "test-code", EXEC)
        vr.unregister([p], "test-code")
        self.assertFalse(bookmarks().has_item(uri(p)))

    def test_entry_metadata(self):
        p = folder("meta")
        vr.register(p, "test-code", EXEC)
        b = bookmarks()
        self.assertEqual(b.get_mime_type(uri(p)), "inode/directory")
        self.assertEqual(b.get_title(uri(p)), "meta")
        # kactivitymanagerd reads the raw attribute (GLib's getter expands %F),
        # strips one level of quotes and compares it with the .desktop Exec= line
        raw = vr.xbel_path().read_text()
        m = re.search(r'name="test-code" exec="([^"]*)"', raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).replace("&apos;", "'").strip("'"), EXEC)


class Sync(unittest.TestCase):
    def setUp(self):
        vr.xbel_path().unlink(missing_ok=True)
        vr.state_path().unlink(missing_ok=True)

    def run_sync(self, db, max_=10):
        return vr.cmd_sync(SimpleNamespace(desktop_id="test-code", storage=str(db), max=max_,
                                           dry_run=False, verbose=False))

    def counts(self, paths):
        b = bookmarks()
        return {os.path.basename(p): (b.get_application_info(uri(p), "test-code")[2]
                                      if b.has_item(uri(p)) and b.has_application(uri(p), "test-code") else 0)
                for p in paths}

    def test_incremental_and_pruning(self):
        a, b, c = folder("a"), folder("b"), folder("c")
        db = make_storage([{"folderUri": uri(a)}, {"folderUri": uri(b)}])
        self.run_sync(db)
        self.assertEqual(self.counts([a, b, c]), {"a": 1, "b": 1, "c": 0})

        # user opens c, then re-opens b: only those get a new "use"
        set_mru(db, [{"folderUri": uri(b)}, {"folderUri": uri(c)}, {"folderUri": uri(a)}])
        self.run_sync(db)
        self.assertEqual(self.counts([a, b, c]), {"a": 1, "b": 2, "c": 1})

        # nothing changed: no new uses recorded
        self.run_sync(db)
        self.assertEqual(self.counts([a, b, c]), {"a": 1, "b": 2, "c": 1})

        # a removed from VS Code's list -> our registration is dropped
        set_mru(db, [{"folderUri": uri(b)}, {"folderUri": uri(c)}])
        self.run_sync(db)
        self.assertEqual(self.counts([a, b, c]), {"a": 0, "b": 2, "c": 1})

    def test_max_limits_published_entries(self):
        paths = [folder(f"m{i}") for i in range(5)]
        db = make_storage([{"folderUri": uri(p)} for p in paths])
        self.run_sync(db, max_=3)
        self.assertEqual(sorted(vr.registered_paths("test-code")), sorted(paths[:3]))


if __name__ == "__main__":
    unittest.main()
