import os
import tempfile
import threading
import unittest

from unhog.scanner import (
    FILE_ATTRIBUTE_PINNED,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_UNPINNED,
    Node,
    ScanCancelled,
    apply_filter,
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
        tree = scan(self.root, progress_cb=lambda *a: progress.append(a))
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
        self.assertTrue(progress)
        self.assertEqual(progress[-1], (4, 3, 1500))
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

    def test_missing_root(self):
        tree = scan(os.path.join(self.root, "does_not_exist"))
        self.assertEqual(tree.size, 0)
        self.assertEqual(tree.children, [])


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


if __name__ == "__main__":
    unittest.main()
