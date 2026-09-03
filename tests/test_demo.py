import threading
import unittest

from unhog.demo import DEMO_ROOT, demo_scan, demo_tree
from unhog.scanner import Node


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


class DemoScanTests(unittest.TestCase):
    def test_matches_scan_signature(self):
        calls = []
        tree = demo_scan(DEMO_ROOT, lambda seen, local, nbytes: calls.append((seen, local, nbytes)),
                         threading.Event())
        self.assertEqual(calls, [(tree.total_files, tree.file_count, tree.size)])

    def test_defaults(self):
        self.assertEqual(demo_scan().path, DEMO_ROOT)
        self.assertEqual(demo_scan("").path, DEMO_ROOT)


if __name__ == "__main__":
    unittest.main()
