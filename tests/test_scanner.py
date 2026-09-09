import os
import tempfile
import threading
import unittest

from unhog.scanner import (
    FILE_ATTRIBUTE_PINNED,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_UNPINNED,
    Exclusions,
    Node,
    ScanCancelled,
    TreeBuilder,
    apply_filter,
    attach,
    default_root,
    detach,
    refresh_upwards,
    scan_into,
    format_size,
    is_local,
    is_pinned,
    is_unpinned,
    scan,
)


class AttributeTests(unittest.TestCase):
    def test_is_local(self):
        self.assertTrue(is_local(0x20))  # plain archive file
        self.assertTrue(is_local(FILE_ATTRIBUTE_PINNED | FILE_ATTRIBUTE_UNPINNED))
        self.assertFalse(is_local(FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))
        self.assertFalse(is_local(FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_UNPINNED))

    def test_pinned_flags(self):
        self.assertTrue(is_pinned(FILE_ATTRIBUTE_PINNED))
        self.assertFalse(is_pinned(0))
        self.assertTrue(is_unpinned(FILE_ATTRIBUTE_UNPINNED))
        self.assertFalse(is_unpinned(FILE_ATTRIBUTE_PINNED))


def write(path, nbytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * nbytes)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        write(os.path.join(self.root, "big.bin"), 1000)
        write(os.path.join(self.root, "sub", "a.txt"), 300)
        write(os.path.join(self.root, "sub", "deep", "b.txt"), 200)
        write(os.path.join(self.root, "empty_file.txt"), 0)
        os.makedirs(os.path.join(self.root, "empty_dir"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_sizes_counts_and_pruning(self):
        progress = []
        tree = scan(self.root, progress_cb=lambda t, p: progress.append((t, p)))
        self.assertEqual(tree.size, 1500)
        self.assertEqual(tree.file_count, 3)
        self.assertEqual(tree.total_files, 4)  # includes the zero-byte file
        names = [c.name for c in tree.children]
        self.assertEqual(names, ["big.bin", "sub"])  # sorted by size desc, empty_dir pruned
        sub = tree.children[1]
        self.assertEqual(sub.size, 500)
        self.assertEqual(sub.parent, tree)
        deep = next(c for c in sub.children if c.is_dir)
        self.assertEqual(deep.size, 200)
        self.assertEqual([n.name for n in deep.ancestors()], [tree.name, "sub", "deep"])
        # Progress reports the root of the growing tree and an estimate; the last
        # call sees the tree complete.
        self.assertTrue(progress)
        self.assertEqual(progress[-1][1], 1.0)
        self.assertIs(progress[-1][0], tree)
        self.assertEqual((tree.total_files, tree.file_count, tree.size), (4, 3, 1500))
        # Plain files on a local disk are local, so total sizes equal local sizes.
        self.assertEqual(tree.total_size, 1500)
        self.assertEqual(sub.total_size, 500)
        self.assertTrue(tree.local)
        self.assertTrue(all(c.local for c in tree.children))
        big = tree.children[0]
        self.assertEqual((big.size, big.total_size, big.file_count, big.total_files), (1000, 1000, 1, 1))

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(ScanCancelled):
            scan(self.root, cancel=ev)

    def test_rescan_one_folder_in_place(self):
        tree = scan(self.root)
        sub = next(c for c in tree.children if c.name == "sub")
        # The folder changes on disk: one file grows, a new one appears.
        write(os.path.join(self.root, "sub", "a.txt"), 900)
        write(os.path.join(self.root, "sub", "deep", "c.txt"), 50)
        parent = detach(sub)
        self.assertIs(parent, tree)
        self.assertIsNone(sub.parent)
        self.assertEqual((tree.size, tree.file_count, tree.total_files), (1000, 1, 2))
        self.assertEqual([c.name for c in tree.children], ["big.bin"])
        fresh = Node("sub", sub.path, True, parent=tree, local=False)
        tree.children.append(fresh)
        seen = []
        self.assertIs(scan_into(fresh, progress_cb=lambda t, p: seen.append(t)), fresh)
        self.assertTrue(all(t is fresh for t in seen))
        self.assertEqual((fresh.size, fresh.file_count, fresh.total_files), (1150, 3, 3))
        # The totals above were kept current while the folder was scanned into place.
        self.assertEqual((tree.size, tree.file_count, tree.total_files), (2150, 4, 5))
        tree.children.reverse()  # out of order on purpose
        refresh_upwards(tree)
        self.assertEqual([c.name for c in tree.children], ["sub", "big.bin"])
        self.assertEqual(tree.mtime, max(c.mtime for c in tree.children))
        self.assertTrue(tree.local)

    def test_attach_reverses_detach(self):
        tree = scan(self.root)
        sub = next(c for c in tree.children if c.name == "sub")
        before = (tree.size, tree.total_size, tree.file_count, tree.total_files, tree.mtime)
        parent = detach(sub)
        self.assertEqual((tree.size, tree.total_files), (1000, 2))
        attach(sub, parent)
        refresh_upwards(parent)
        self.assertIs(sub.parent, tree)
        self.assertIn(sub, tree.children)
        self.assertEqual((tree.size, tree.total_size, tree.file_count, tree.total_files, tree.mtime), before)
        self.assertEqual([c.name for c in tree.children], ["big.bin", "sub"])

    def test_excluded_folder_is_skipped(self):
        excl = Exclusions()
        excl.add(os.path.join(self.root, "sub"))
        tree = scan(self.root, exclusions=excl)
        self.assertEqual([c.name for c in tree.children], ["big.bin"])
        self.assertEqual((tree.size, tree.file_count, tree.total_files), (1000, 1, 2))
        self.assertTrue(tree.scanned)

    def test_folder_holding_only_an_excluded_folder_is_kept(self):
        # "sub" would be empty without "deep" and is normally dropped; keep it so
        # that "deep" has a place to return to when it is unhidden.
        os.remove(os.path.join(self.root, "sub", "a.txt"))
        excl = Exclusions()
        excl.add(os.path.join(self.root, "sub", "deep"))
        tree = scan(self.root, exclusions=excl)
        sub = next(c for c in tree.children if c.name == "sub")
        self.assertEqual((sub.total_size, sub.children), (0, []))
        self.assertTrue(sub.scanned)
        self.assertEqual(tree.size, 1000)

    def test_exclusion_added_mid_scan_abandons_the_folder(self):
        # root/a/{a1, a2}, root/b. Exclude "a" once the scan is inside it: the rest
        # of "a" is not read, "a" leaves the tree with its numbers, "b" is scanned.
        for name in ("a1", "a2"):
            write(os.path.join(self.root, "a", name, "f.bin"), 100)
        write(os.path.join(self.root, "b", "g.bin"), 10)
        write(os.path.join(self.root, "a", "top.bin"), 7)
        excl = Exclusions()
        seen = []
        root_holder = []

        def hide_a_when_inside():  # runs on the scan thread at each folder; re-queues until then
            root = root_holder[0]
            a = next((c for c in root.children if c.name == "a"), None)
            if a is not None and any(c.is_dir for c in a.children):
                excl.add(a.path)
                seen.append(a)
            else:
                excl.defer(hide_a_when_inside)

        excl.defer(hide_a_when_inside)
        root = Node(os.path.basename(self.root), self.root, True, local=False)
        root_holder.append(root)
        tree = scan_into(root, exclusions=excl)
        self.assertIs(tree, root)
        a = seen[0]
        self.assertIsNone(a.parent)  # detached by the scan thread
        self.assertFalse(a.scanned)  # abandoned part-way: not complete
        self.assertTrue(tree.scanned)
        self.assertLess(a.total_files, 3)  # top.bin (and at most the first subfolder's file)
        # The tree holds everything except "a", and its totals never included "a"'s files.
        self.assertEqual(sorted(c.name for c in tree.children), ["b", "big.bin", "sub"])
        self.assertEqual((tree.size, tree.file_count), (1510, 4))

    def test_missing_root(self):
        tree = scan(os.path.join(self.root, "does_not_exist"))
        self.assertEqual(tree.size, 0)
        self.assertEqual(tree.children, [])


class TreeBuilderTests(unittest.TestCase):
    def test_totals_are_kept_current_while_building(self):
        root = Node("root", r"C:\root", True, local=False)
        b = TreeBuilder(root)
        sub = b.add_dir(root, "sub", r"C:\root\sub")
        deep = b.add_dir(sub, "deep", r"C:\root\sub\deep")
        self.assertEqual([c.name for c in root.children], ["sub"])  # attached at once
        f = b.add_file(deep, "a.bin", r"C:\root\sub\deep\a.bin", 200, True, 50.0)
        self.assertIs(f.parent, deep)
        # Before any folder is finished, every ancestor already counts the file.
        for node in (deep, sub, root):
            self.assertEqual((node.size, node.total_size, node.file_count, node.total_files), (200, 200, 1, 1))
            self.assertEqual(node.mtime, 50.0)
            self.assertTrue(node.local)
        b.add_file(sub, "cloud.bin", r"C:\root\sub\cloud.bin", 1000, False, 80.0)
        self.assertEqual((sub.size, sub.total_size, sub.file_count, sub.total_files), (200, 1200, 1, 2))
        self.assertEqual((root.size, root.total_size, root.file_count, root.total_files), (200, 1200, 1, 2))
        self.assertEqual(root.mtime, 80.0)
        self.assertEqual(deep.mtime, 50.0)
        # Empty files are counted but not shown, and do not move folder times.
        self.assertIsNone(b.add_file(sub, "empty.txt", r"C:\root\sub\empty.txt", 0, True, 999.0))
        self.assertEqual((sub.total_files, root.total_files), (3, 3))
        self.assertEqual(root.mtime, 80.0)
        self.assertEqual(len(sub.children), 2)
        b.finish_dir(deep)
        b.finish_dir(sub)
        self.assertEqual([c.name for c in sub.children], ["cloud.bin", "deep"])  # sorted by total size
        self.assertIs(b.finish(), root)

    def test_progress_estimate_from_folders(self):
        root = Node("root", r"C:\root", True, local=False)
        b = TreeBuilder(root)
        self.assertEqual(b.progress(), 1.0)  # nothing open: finished (or not started)
        b.enter_dir(root, 4)                 # root has four subfolders
        self.assertEqual(b.progress(), 0.0)
        a = b.add_dir(root, "a", r"C:\root\a")
        b.enter_dir(a, 0)                    # a leaf folder: no idea how far into it we are
        self.assertEqual(b.progress(), 0.0)
        b.add_file(a, "f", r"C:\root\a\f", 1, True, 1.0)
        b.finish_dir(a)
        self.assertEqual(b.progress(), 0.25)  # one of four done
        c = b.add_dir(root, "c", r"C:\root\c")
        b.enter_dir(c, 2)
        c1 = b.add_dir(c, "c1", r"C:\root\c\c1")
        b.enter_dir(c1, 0)
        b.add_file(c1, "f", r"C:\root\c\c1\f", 1, True, 1.0)
        b.finish_dir(c1)
        self.assertAlmostEqual(b.progress(), 0.25 + 0.25 * 0.5)  # halfway through the second
        seen = []
        b.progress_cb = lambda t, p: seen.append(p)
        b.finish_dir(c)
        self.assertEqual(b.progress(), 0.5)
        b.finish()
        self.assertEqual(seen, [1.0])
        self.assertEqual(b.progress(), 1.0)

    def test_empty_folder_is_dropped_when_finished(self):
        root = Node("root", r"C:\root", True, local=False)
        b = TreeBuilder(root)
        empty = b.add_dir(root, "empty", r"C:\root\empty")
        b.add_file(empty, "zero.txt", r"C:\root\empty\zero.txt", 0, True, 1.0)
        b.finish_dir(empty)
        self.assertEqual(root.children, [])
        self.assertIsNone(empty.parent)
        self.assertEqual(root.total_files, 1)
        b.finish()
        self.assertFalse(root.local)

    def test_pinned_folder_and_cancel(self):
        root = Node("root", r"C:\root", True, local=False)
        ev = threading.Event()
        b = TreeBuilder(root, cancel=ev)
        b.add_file(root, "a", r"C:\root\a", 1, True, 1.0, pinned=True)
        b.add_file(root, "b", r"C:\root\b", 2, True, 1.0, pinned=True)
        b.finish()
        self.assertTrue(root.pinned)
        ev.set()
        with self.assertRaises(ScanCancelled):
            b.add_file(root, "c", r"C:\root\c", 3, True, 1.0)

    def test_progress_is_rate_limited(self):
        class Eager(TreeBuilder):
            PROGRESS_INTERVAL = 0.0  # report after every file

        class Patient(TreeBuilder):
            PROGRESS_INTERVAL = 3600.0

        root = Node("root", r"C:\root", True, local=False)
        seen = []
        b = Eager(root, progress_cb=lambda t, p: seen.append(t))
        for i in range(5):
            b.add_file(root, f"f{i}", rf"C:\root\f{i}", 1, True, 1.0)
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(n is root for n in seen))

        seen.clear()
        b = Patient(root, progress_cb=lambda t, p: seen.append((t, p)))
        b.add_file(root, "later", r"C:\root\later", 1, True, 1.0)
        self.assertEqual(seen, [])
        b.finish()
        self.assertEqual(seen, [(root, 1.0)])  # the finished tree is always reported


class FilterTests(unittest.TestCase):
    def test_apply_filter_recomputes_folder_sums(self):
        root = Node("root", r"C:\root", True)
        sub = Node("sub", r"C:\root\sub", True, parent=root)
        old = Node("old.bin", r"C:\root\sub\old.bin", False, size=100, file_count=1, total_files=1,
                   total_size=100, mtime=1000.0, parent=sub)
        new = Node("new.bin", r"C:\root\sub\new.bin", False, size=30, file_count=1, total_files=1,
                   total_size=30, mtime=5000.0, parent=sub)
        cloud = Node("cloud.bin", r"C:\root\cloud.bin", False, size=0, file_count=0, total_files=1,
                     total_size=7, local=False, mtime=5000.0, parent=root)
        stale = Node("stale", r"C:\root\stale", True, parent=root, mtime=1500.0)
        ancient = Node("ancient.bin", r"C:\root\stale\ancient.bin", False, size=40, file_count=1,
                       total_files=1, total_size=40, mtime=1500.0, parent=stale)
        stale.children = [ancient]
        stale.size, stale.total_size, stale.file_count, stale.total_files = 40, 40, 1, 1
        sub.children = [old, new]
        sub.size, sub.total_size, sub.file_count, sub.total_files = 130, 130, 2, 2
        sub.mtime = 5000.0  # a folder's time is the newest file inside it
        root.children = [sub, cloud, stale]
        root.size, root.total_size, root.file_count, root.total_files = 170, 177, 3, 4
        root.mtime = 5000.0

        # Unfiltered: the view_* accessors fall back to the raw numbers.
        self.assertEqual((root.view_size, root.view_total_size, root.view_file_count, root.view_total_files), (170, 177, 3, 4))

        apply_filter(root, lambda n: n.mtime >= 3000)  # "newer than": recent files in recent folders
        self.assertEqual((sub.view_size, sub.view_total_size, sub.view_file_count, sub.view_total_files), (30, 30, 1, 1))
        self.assertEqual((old.view_size, old.view_total_files), (0, 0))
        self.assertEqual(stale.view_size, 0)  # whole folder is old
        self.assertEqual((root.view_size, root.view_total_size, root.view_file_count, root.view_total_files), (30, 37, 1, 2))

        # "older than": the stale folder shows in full (its newest file is old),
        # and the mixed folder "sub" stays as a container showing just its old file.
        apply_filter(root, lambda n: n.mtime < 3000)
        self.assertEqual((sub.view_size, sub.view_total_files), (100, 1))
        self.assertEqual((stale.view_size, stale.view_total_files), (40, 1))
        self.assertEqual((root.view_size, root.view_total_size, root.view_file_count, root.view_total_files), (140, 140, 2, 2))
        self.assertFalse(sub.fmatch)   # container only: its newest file is recent
        self.assertTrue(stale.fmatch)  # genuinely old folder

        apply_filter(root, None)  # back to everything
        self.assertEqual((root.view_size, root.view_total_size, root.view_file_count, root.view_total_files), (170, 177, 3, 4))
        self.assertTrue(sub.fmatch)
        # Raw numbers never change.
        self.assertEqual((root.size, root.total_size), (170, 177))


    def test_scan_records_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.txt")
            write(path, 10)
            os.utime(path, (1_600_000_000, 1_600_000_000))
            tree = scan(tmp)
            self.assertAlmostEqual(tree.children[0].mtime, 1_600_000_000, delta=2)


class FormatTests(unittest.TestCase):
    def test_format_size(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(1023), "1023 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1536 * 1024), "1.5 MB")
        self.assertEqual(format_size(3 * 1024 ** 3), "3.0 GB")


class DefaultRootTests(unittest.TestCase):
    def test_default_root(self):
        root = default_root()
        self.assertTrue(os.path.isabs(root))
        if os.name == "nt":
            self.assertTrue(root.lower().endswith("onedrive") or os.path.isdir(root))
        else:
            self.assertTrue(os.path.isdir(root))
            home = os.path.expanduser("~")
            self.assertEqual(root, home if os.path.isdir(home) else os.path.abspath(os.sep))

    def test_default_root_without_home_falls_back_to_root(self):
        if os.name == "nt":
            self.skipTest("Windows always defaults to the OneDrive folder")
        old = {k: os.environ.get(k) for k in ("HOME",)}
        os.environ["HOME"] = "/nonexistent-unhog-home"
        try:
            self.assertEqual(default_root(), os.path.abspath(os.sep))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
