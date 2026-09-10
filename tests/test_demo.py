import threading
import time
import unittest

from unhog.demo import DEMO_ROOT, demo_disk_usage, demo_rescan, demo_scan, demo_tree
from unhog.scanner import Node, ScanCancelled


def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


class DemoTreeTests(unittest.TestCase):
    def setUp(self):
        self.tree = demo_tree(now=1_800_000_000.0)

    def test_root(self):
        self.assertEqual(self.tree.path, DEMO_ROOT)
        self.assertEqual(self.tree.name, "OneDrive")
        self.assertTrue(self.tree.is_dir)
        self.assertIsNone(self.tree.parent)

    def test_folder_totals_match_children(self):
        for node in walk(self.tree):
            if not node.is_dir:
                continue
            self.assertTrue(node.children, node.path)
            self.assertEqual(node.size, sum(c.size for c in node.children), node.path)
            self.assertEqual(node.total_size, sum(c.total_size for c in node.children), node.path)
            self.assertEqual(node.file_count, sum(c.file_count for c in node.children), node.path)
            self.assertEqual(node.total_files, sum(c.total_files for c in node.children), node.path)
            self.assertEqual(node.mtime, max(c.mtime for c in node.children), node.path)
            self.assertEqual(node.local, node.size > 0, node.path)
            sizes = [c.total_size for c in node.children]
            self.assertEqual(sizes, sorted(sizes, reverse=True), node.path)

    def test_paths_and_parents(self):
        for node in walk(self.tree):
            for child in node.children:
                self.assertIs(child.parent, node)
                self.assertEqual(child.path, node.path + "\\" + child.name)

    def test_paths_are_unique(self):
        # As on a real disk: rescans and Free up space find nodes by path.
        paths = [n.path for n in walk(self.tree)]
        self.assertEqual(len(paths), len(set(paths)))

    def test_files_are_consistent(self):
        files = [n for n in walk(self.tree) if not n.is_dir]
        self.assertGreater(len(files), 1000)
        for f in files:
            self.assertFalse(f.children)
            self.assertGreater(f.total_size, 0)
            self.assertEqual(f.total_files, 1)
            if f.local:
                self.assertEqual(f.size, f.total_size)
                self.assertEqual(f.file_count, 1)
            else:
                self.assertEqual(f.size, 0)
                self.assertEqual(f.file_count, 0)
            self.assertGreater(f.mtime, 0)

    def test_mix_of_local_and_online_only(self):
        files = [n for n in walk(self.tree) if not n.is_dir]
        local = sum(1 for f in files if f.local)
        self.assertGreater(local, len(files) * 0.3)
        self.assertLess(local, len(files) * 0.95)
        self.assertLess(self.tree.size, self.tree.total_size)
        self.assertTrue(any(n.pinned for n in walk(self.tree)))

    def test_deterministic(self):
        a = [(n.path, n.total_size, n.local) for n in walk(demo_tree(now=1e9))]
        b = [(n.path, n.total_size, n.local) for n in walk(demo_tree(now=1e9))]
        self.assertEqual(a, b)
        c = [(n.path, n.total_size) for n in walk(demo_tree(seed=8, now=1e9))]
        self.assertNotEqual([x[:2] for x in a], c)

    def test_other_root(self):
        tree = demo_tree("D:\\Dropbox")
        self.assertEqual(tree.name, "Dropbox")
        self.assertTrue(all(n.path.startswith("D:\\Dropbox") for n in walk(tree)))


def snapshot(tree: Node) -> list[tuple]:
    """Everything but modification times, which depend on when the tree was generated."""
    return sorted((n.path, n.is_dir, n.total_size, n.size, n.file_count, n.total_files, n.local, n.pinned,
                   [c.total_size for c in n.children]) for n in walk(tree))


class DemoScanTests(unittest.TestCase):
    def test_replays_the_demo_tree(self):
        calls = []
        tree = demo_scan(DEMO_ROOT, lambda t, p: calls.append((t, p)), threading.Event(), duration=0)
        self.assertEqual(snapshot(tree), snapshot(demo_tree()))
        self.assertTrue(calls)
        self.assertTrue(all(c[0] is tree for c in calls))
        self.assertEqual(calls[-1][1], 1.0)
        for node in walk(tree):
            for child in node.children:
                self.assertIs(child.parent, node)

    def test_is_paced_and_reports_progress(self):
        seen = []
        estimates = []
        t0 = time.monotonic()
        tree = demo_scan(progress_cb=lambda n, p: (seen.append(n.total_files), estimates.append(p)),
                         duration=0.4)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 3.0)
        self.assertGreaterEqual(len(seen), 3)              # partial trees were reported along the way
        self.assertEqual(seen, sorted(seen))               # and the tree only grew
        self.assertEqual(seen[-1], tree.total_files)
        self.assertEqual(estimates, sorted(estimates))     # the progress estimate never goes back
        self.assertTrue(0.0 < estimates[len(estimates) // 2] < 1.0)
        self.assertEqual(estimates[-1], 1.0)

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(ScanCancelled):
            demo_scan(cancel=ev, duration=0)

    def test_defaults(self):
        self.assertEqual(demo_scan(duration=0).path, DEMO_ROOT)
        self.assertEqual(demo_scan("", duration=0).path, DEMO_ROOT)

    def test_rescan_replays_one_subtree(self):
        tree = demo_scan(duration=0)
        pictures = next(c for c in tree.children if c.name == "Pictures")
        before = snapshot(tree)
        parent = tree
        tree.children.remove(pictures)
        pictures.parent = None
        for attr in ("size", "total_size", "file_count", "total_files"):
            setattr(parent, attr, getattr(parent, attr) - getattr(pictures, attr))
        fresh = Node("Pictures", pictures.path, True, parent=parent, local=False)
        parent.children.append(fresh)
        estimates = []
        self.assertIs(demo_rescan(fresh, lambda t, p: estimates.append(p), duration=0), fresh)
        self.assertEqual(estimates[-1], 1.0)
        parent.children.sort(key=lambda n: n.total_size, reverse=True)
        self.assertEqual(snapshot(tree), before)  # same tree as before, rebuilt in place

    def test_disk_usage(self):
        usage = demo_disk_usage(DEMO_ROOT)
        self.assertEqual(usage.total, usage.used + usage.free)
        self.assertGreater(usage.free, 0)
        self.assertGreater(usage.total, demo_tree().total_size)  # the folder fits on the drive


if __name__ == "__main__":
    unittest.main()
