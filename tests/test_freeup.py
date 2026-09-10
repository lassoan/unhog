import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

from unhog import freeup
from unhog.demo import demo_free_up, demo_tree
from unhog.freeup import (FILE_ATTRIBUTE_NORMAL, free_up_space, mark_online_only, unloaded_files,
                          unpinned_attributes, watch_unloading)
from unhog.scanner import (FILE_ATTRIBUTE_PINNED, FILE_ATTRIBUTE_UNPINNED, Node, find_node,
                           scan, unload)

WINDOWS = sys.platform == "win32"


def write(path, nbytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * nbytes)


class AttributeMathTests(unittest.TestCase):
    def test_unpinned_attributes(self):
        archive = 0x20
        self.assertEqual(unpinned_attributes(archive), archive | FILE_ATTRIBUTE_UNPINNED)
        self.assertEqual(unpinned_attributes(archive | FILE_ATTRIBUTE_PINNED), archive | FILE_ATTRIBUTE_UNPINNED)
        # NORMAL is only valid on its own: it goes once another bit is set.
        self.assertEqual(unpinned_attributes(FILE_ATTRIBUTE_NORMAL), FILE_ATTRIBUTE_UNPINNED)
        # Everything else is kept (readonly, hidden, directory, ...).
        keep = 0x1 | 0x2 | 0x10
        self.assertEqual(unpinned_attributes(keep | FILE_ATTRIBUTE_PINNED), keep | FILE_ATTRIBUTE_UNPINNED)


@unittest.skipUnless(WINDOWS, "the Free up space attributes exist on Windows only")
class MarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        write(os.path.join(self.root, "a.bin"), 100)
        write(os.path.join(self.root, "empty.txt"), 0)
        write(os.path.join(self.root, "sub", "b.bin"), 50)
        write(os.path.join(self.root, "sub", "deep", "c.bin"), 25)
        os.makedirs(os.path.join(self.root, "sub", "hollow"))
        # One file "Always keep on this device", which Free up space undoes.
        attrs, _ = freeup.get_attributes(os.path.join(self.root, "a.bin"))
        freeup.set_attributes(os.path.join(self.root, "a.bin"), attrs | FILE_ATTRIBUTE_PINNED)

    def tearDown(self):
        self.tmp.cleanup()

    def attrs(self, *parts):
        return os.stat(os.path.join(self.root, *parts)).st_file_attributes

    def test_folder_is_marked_recursively_and_local_files_watched(self):
        self.assertTrue(self.attrs("a.bin") & FILE_ATTRIBUTE_PINNED)
        events = []
        watch, files, failed = mark_online_only(self.root, report=events.append)
        self.assertEqual((files, failed), (4, 0))
        for parts in (("a.bin",), ("empty.txt",), ("sub",), ("sub", "b.bin"), ("sub", "deep"),
                      ("sub", "deep", "c.bin"), ("sub", "hollow"), ()):
            a = self.attrs(*parts)
            self.assertTrue(a & FILE_ATTRIBUTE_UNPINNED, parts)
            self.assertFalse(a & FILE_ATTRIBUTE_PINNED, parts)
        # Files on local storage with something in them are watched, by folder.
        self.assertEqual(watch, {self.root: {"a.bin"}, os.path.join(self.root, "sub"): {"b.bin"},
                                 os.path.join(self.root, "sub", "deep"): {"c.bin"}})
        self.assertTrue(all(e.kind == "marking" for e in events))
        # Marking again finds nothing to change and reports the same.
        self.assertEqual(mark_online_only(self.root)[1:], (4, 0))

    def test_single_file(self):
        path = os.path.join(self.root, "a.bin")
        watch, files, failed = mark_online_only(path)
        self.assertEqual((files, failed), (1, 0))
        self.assertEqual(watch, {self.root: {"a.bin"}})
        self.assertTrue(self.attrs("a.bin") & FILE_ATTRIBUTE_UNPINNED)
        self.assertFalse(self.attrs("sub", "b.bin") & FILE_ATTRIBUTE_UNPINNED)
        self.assertEqual(mark_online_only(os.path.join(self.root, "empty.txt"))[0], {})

    def test_missing_path(self):
        with self.assertRaises(OSError):
            mark_online_only(os.path.join(self.root, "nope"))

    def test_cancel_stops_the_walk(self):
        ev = threading.Event()
        ev.set()
        watch, files, failed = mark_online_only(self.root, cancel=ev)
        self.assertEqual(watch, {})
        self.assertFalse(self.attrs("a.bin") & FILE_ATTRIBUTE_UNPINNED)

    def test_unloaded_files_notices_vanished_files(self):
        # Plain files cannot be turned into placeholders here; a file that is
        # gone counts as unloaded too, which exercises the same path.
        watch, _, _ = mark_online_only(self.root)
        self.assertEqual(unloaded_files(watch), [])
        os.remove(os.path.join(self.root, "a.bin"))
        os.remove(os.path.join(self.root, "sub", "deep", "c.bin"))
        self.assertEqual(unloaded_files(watch), [os.path.join(self.root, "a.bin"),
                                                 os.path.join(self.root, "sub", "deep", "c.bin")])
        self.assertEqual(watch, {os.path.join(self.root, "sub"): {"b.bin"}})
        self.assertEqual(unloaded_files(watch), [])

    def test_free_up_space_reports_marked_unloaded_done(self):
        # Pretend the sync client unloads a.bin on the first look and the rest never.
        looks = []

        def local_names(folder, names):
            looks.append(folder)
            return names - {"a.bin"}

        events = []
        with mock.patch.object(freeup, "_local_names", local_names):
            free_up_space(self.root, events.append, threading.Event(), poll_s=0.01, poll_max_s=0.02,
                          give_up_s=0.05)
        kinds = [e.kind for e in events]
        self.assertEqual(kinds[-1], "done")
        self.assertIn("marked", kinds)
        self.assertIn("unloaded", kinds)
        marked = events[kinds.index("marked")]
        self.assertEqual((marked.files, marked.failed, marked.remaining), (4, 0, 3))
        unloaded = [p for e in events if e.kind == "unloaded" for p in e.paths]
        self.assertEqual(unloaded, [os.path.join(self.root, "a.bin")])
        self.assertEqual(events[-1].remaining, 2)  # b.bin and c.bin were still local when it gave up
        self.assertGreater(len(looks), 3)


class WatchTests(unittest.TestCase):
    def test_watch_ends_when_everything_is_unloaded(self):
        watch = {r"C:\r": {"a", "b"}, r"C:\r\s": {"c"}}
        rounds = []

        def local_names(folder, names):
            rounds.append(folder)
            return set() if len(rounds) > 2 else names  # nothing on the first look, all gone on the next

        events = []
        with mock.patch.object(freeup, "_local_names", local_names):
            remaining = watch_unloading(watch, events.append, threading.Event(), poll_s=0.001,
                                        poll_max_s=0.002, give_up_s=10)
        self.assertEqual(remaining, 0)
        self.assertEqual(watch, {})
        self.assertEqual(sorted(p for e in events for p in e.paths),
                         [os.path.join(r"C:\r", "a"), os.path.join(r"C:\r", "b"), os.path.join(r"C:\r\s", "c")])

    def test_watch_gives_up_and_stops_on_cancel(self):
        with mock.patch.object(freeup, "_local_names", lambda folder, names: names):
            watch = {r"C:\r": {"a"}}
            self.assertEqual(watch_unloading(watch, lambda e: None, threading.Event(), poll_s=0.001,
                                             poll_max_s=0.002, give_up_s=0.01), 1)
            self.assertEqual(watch, {r"C:\r": {"a"}})
            cancel = threading.Event()
            cancel.set()
            self.assertEqual(watch_unloading(watch, lambda e: None, cancel, poll_s=0.001), 1)


class UnloadTests(unittest.TestCase):
    def test_unload_takes_the_bytes_out_of_the_folders_above(self):
        root = Node("root", r"C:\root", True, size=130, file_count=2, total_files=2, total_size=130,
                    local=True, pinned=True)
        sub = Node("sub", r"C:\root\sub", True, size=100, file_count=1, total_files=1, total_size=100,
                   parent=root, local=True, pinned=True)
        f = Node("f.bin", r"C:\root\sub\f.bin", False, size=100, file_count=1, total_files=1,
                 total_size=100, parent=sub, pinned=True)
        g = Node("g.bin", r"C:\root\g.bin", False, size=30, file_count=1, total_files=1, total_size=30,
                 parent=root)
        sub.children, root.children = [f], [sub, g]
        self.assertEqual(unload(f), 100)
        self.assertEqual((f.size, f.file_count, f.total_size, f.total_files, f.local, f.pinned),
                         (0, 0, 100, 1, False, False))
        self.assertEqual((sub.size, sub.file_count, sub.total_size, sub.local, sub.pinned), (0, 0, 100, False, False))
        self.assertEqual((root.size, root.file_count, root.total_size, root.local, root.pinned), (30, 1, 130, True, False))
        self.assertEqual(unload(f), 0)  # already online-only: nothing changes
        self.assertEqual(unload(sub), 0)  # folders are not unloaded as such
        self.assertEqual(root.size, 30)

    def test_find_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "a.bin"), 10)
            write(os.path.join(tmp, "sub", "deep", "c.bin"), 10)
            write(os.path.join(tmp, "sub2", "d.bin"), 10)  # "sub" is a prefix of "sub2"
            tree = scan(tmp)
            self.assertIs(find_node(tree, tmp), tree)
            c = find_node(tree, os.path.join(tmp, "sub", "deep", "c.bin"))
            self.assertEqual((c.name, c.parent.name, c.parent.parent.name), ("c.bin", "deep", "sub"))
            self.assertEqual(find_node(tree, os.path.join(tmp, "sub2", "d.bin")).name, "d.bin")
            self.assertEqual(find_node(tree, os.path.join(tmp, "sub2")).name, "sub2")
            self.assertIsNone(find_node(tree, os.path.join(tmp, "sub", "nope.bin")))
            self.assertIsNone(find_node(tree, os.path.join(tmp, "sub3")))
            self.assertIsNone(find_node(tree, os.path.dirname(tmp)))
            self.assertIsNone(find_node(tree, tmp + "x"))
            self.assertIsNone(find_node(c, os.path.join(tmp, "a.bin")))


class DemoFreeUpTests(unittest.TestCase):
    def test_demo_reports_every_local_file_under_the_node(self):
        tree = demo_tree(now=1_800_000_000.0)
        folder = next(c for c in tree.children if c.name == "Videos")
        local = {n.path for n in walk(folder) if not n.is_dir and n.local}
        self.assertTrue(local)
        events = []
        demo_free_up(folder, events.append, threading.Event(), duration=0)
        self.assertEqual(events[0].kind, "marked")
        self.assertEqual(events[0].remaining, len(local))
        self.assertEqual(events[0].files, sum(1 for n in walk(folder) if not n.is_dir))
        unloaded = [p for e in events if e.kind == "unloaded" for p in e.paths]
        self.assertEqual(sorted(unloaded), sorted(local))
        self.assertEqual((events[-1].kind, events[-1].remaining), ("done", 0))
        self.assertGreater(len(events), 3)  # in batches, not all at once

    def test_demo_cancel(self):
        tree = demo_tree(now=1_800_000_000.0)
        cancel = threading.Event()
        cancel.set()
        events = []
        demo_free_up(tree, events.append, cancel, duration=0)
        self.assertEqual([e.kind for e in events], ["marked", "done"])
        self.assertEqual(events[-1].remaining, events[0].remaining)


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


if __name__ == "__main__":
    unittest.main()
