import unittest

from unhog.scanner import Node
from unhog.treemap import hit_test, layout, local_size, open_aggregate, squarify, total_size


def area(r):
    return r[2] * r[3]


def inside(r, outer, tol=1e-6):
    x, y, w, h = r
    ox, oy, ow, oh = outer
    return x >= ox - tol and y >= oy - tol and x + w <= ox + ow + tol and y + h <= oy + oh + tol


class SquarifyTests(unittest.TestCase):
    def test_single_item_fills_rect(self):
        self.assertEqual(squarify([5], 10, 20, 100, 50), [(10, 20, 100, 50)])

    def test_areas_proportional_and_conserved(self):
        weights = [60, 30, 20, 10, 10, 5, 3, 1]
        outer = (0, 0, 400, 300)
        rects = squarify(weights, *outer)
        self.assertEqual(len(rects), len(weights))
        total = sum(area(r) for r in rects)
        self.assertAlmostEqual(total, 400 * 300, places=3)
        scale = 400 * 300 / sum(weights)
        for w, r in zip(weights, rects):
            self.assertAlmostEqual(area(r), w * scale, places=3)
            self.assertTrue(inside(r, outer), (r, outer))

    def test_rects_do_not_overlap(self):
        rects = squarify([9, 8, 7, 6, 5, 4, 3, 2, 1], 0, 0, 300, 200)
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                ax, ay, aw, ah = a
                bx, by, bw, bh = b
                overlap_w = min(ax + aw, bx + bw) - max(ax, bx)
                overlap_h = min(ay + ah, by + bh) - max(ay, by)
                self.assertFalse(overlap_w > 1e-6 and overlap_h > 1e-6, (a, b))

    def test_zero_weights(self):
        rects = squarify([10, 0, 0], 0, 0, 100, 100)
        self.assertEqual(rects[0], (0, 0, 100, 100))
        self.assertEqual(area(rects[1]), 0)
        self.assertEqual(area(rects[2]), 0)
        self.assertEqual(squarify([], 0, 0, 10, 10), [])
        self.assertEqual(area(squarify([0], 0, 0, 10, 10)[0]), 0)


def make_tree():
    root = Node("root", r"C:\root", True)
    a = Node("a", r"C:\root\a", True, parent=root)
    f1 = Node("f1.txt", r"C:\root\a\f1.txt", False, size=300, file_count=1, parent=a)
    f2 = Node("f2.txt", r"C:\root\a\f2.txt", False, size=100, file_count=1, parent=a)
    a.children = [f1, f2]
    a.size, a.file_count = 400, 2
    b = Node("b.bin", r"C:\root\b.bin", False, size=200, file_count=1, parent=root)
    root.children = [a, b]
    root.size, root.file_count = 600, 3
    return root, a, b, f1, f2


class LayoutTests(unittest.TestCase):
    def test_parents_before_children_and_nested(self):
        root, a, b, f1, f2 = make_tree()
        items = layout(root, (0, 0, 600, 400), title_h=18, pad=2)
        nodes = [it.node for it in items]
        self.assertEqual(nodes[0], root)
        self.assertLess(nodes.index(a), nodes.index(f1))
        self.assertLess(nodes.index(a), nodes.index(f2))
        by_node = {it.node: it for it in items}
        self.assertTrue(inside(by_node[a].rect, by_node[root].rect))
        self.assertTrue(inside(by_node[f1].rect, by_node[a].rect))
        self.assertTrue(inside(by_node[b].rect, by_node[root].rect))
        self.assertGreater(by_node[root].title_h, 0)
        self.assertEqual(by_node[b].title_h, 0)
        self.assertEqual(by_node[f1].depth, 2)

    def test_per_folder_title_height(self):
        root, a, b, f1, f2 = make_tree()
        items = layout(root, (0, 0, 600, 400), title_h=lambda n: 40.0 if n is root else 12.0)
        by_node = {it.node: it for it in items}
        self.assertEqual(by_node[root].title_h, 40.0)
        self.assertEqual(by_node[a].title_h, 12.0)
        # Children start below the taller root title strip.
        self.assertGreaterEqual(by_node[a].rect[1], 40.0)

    def test_per_folder_padding(self):
        root, a, b, f1, f2 = make_tree()
        items = layout(root, (0, 0, 600, 400), title_h=0.0,
                       pad=lambda n: 20.0 if n is root else 4.0)
        by_node = {it.node: it for it in items}
        # Root's children start 20 px in; a's children start 4 px inside a.
        self.assertEqual(min(by_node[a].rect[0], by_node[b].rect[0]), 20.0)
        self.assertEqual(by_node[f1].rect[0], by_node[a].rect[0] + 4.0)

    def test_tiny_rect_stops_recursion(self):
        root, *_ = make_tree()
        items = layout(root, (0, 0, 3, 3), min_px=4)
        self.assertEqual(items, [])
        items = layout(root, (0, 0, 10, 10), min_px=4)
        self.assertEqual([it.node for it in items][0], root)


class WeightTests(unittest.TestCase):
    def test_online_only_files_shown_only_with_total_size(self):
        root, a, b, f1, f2 = make_tree()
        cloud = Node("cloud.mp4", r"C:\root\cloud.mp4", False, size=0, file_count=0,
                     total_files=1, total_size=5000, local=False, parent=root)
        root.children.append(cloud)
        root.total_files, root.total_size = 4, 5600
        for n in (a, b, f1, f2):
            n.total_size = n.size

        local_items = layout(root, (0, 0, 600, 400), weight=local_size)
        self.assertNotIn(cloud, [it.node for it in local_items])

        all_items = layout(root, (0, 0, 600, 400), weight=total_size)
        by_node = {it.node: it for it in all_items}
        self.assertIn(cloud, by_node)
        inner = (600 - 4) * (400 - 18 - 4)  # root rect minus padding and title strip
        self.assertAlmostEqual(area(by_node[cloud].rect), inner * 5000 / 5600, delta=1.0)
        self.assertAlmostEqual(area(by_node[b].rect), inner * 200 / 5600, delta=1.0)


class MinWeightTests(unittest.TestCase):
    def test_small_children_fold_into_one_aggregate_tile(self):
        root = Node("root", r"C:\root", True)
        sizes = [1000, 500, 8, 5, 2]
        for i, sz in enumerate(sizes):
            root.children.append(Node(f"f{i}", rf"C:\root\f{i}", False, size=sz, file_count=1,
                                      total_files=1, total_size=sz, parent=root))
        root.size = root.total_size = sum(sizes)
        root.file_count = root.total_files = len(sizes)

        items = layout(root, (0, 0, 600, 400), min_weight=10)
        children = [it.node for it in items if it.depth == 1]
        names = [n.name for n in children]
        self.assertEqual(names, ["f0", "f1", "3 smaller items"])
        agg = children[-1]
        self.assertTrue(agg.aggregate)
        self.assertEqual((agg.size, agg.file_count, agg.total_files), (15, 3, 3))
        self.assertEqual(agg.path, root.path)
        self.assertIs(agg.parent, root)
        inner = (600 - 4) * (400 - 18 - 4)
        self.assertAlmostEqual(area({it.node: it for it in items}[agg].rect),
                               inner * 15 / sum(sizes), delta=1.0)

    def test_open_aggregate_shows_folded_items_with_smaller_limit(self):
        root = Node("root", r"C:\root", True)
        sizes = [1000, 8, 5, 2]
        for i, sz in enumerate(sizes):
            root.children.append(Node(f"f{i}", rf"C:\root\f{i}", False, size=sz, file_count=1,
                                      total_files=1, total_size=sz, parent=root))
        root.size = root.total_size = sum(sizes)
        items = layout(root, (0, 0, 600, 400), min_weight=10)
        agg = next(it.node for it in items if it.node.aggregate)
        self.assertEqual([c.name for c in agg.children], ["f1", "f2", "f3"])

        view = open_aggregate(agg)
        self.assertTrue(view.is_dir and view.aggregate)
        self.assertIs(view.parent, root)
        self.assertEqual([n.name for n in view.ancestors()], ["root", "3 smaller items"])
        # Limit scaled by the tile's share of the root: 10 * 15/1015 -> everything shows.
        inner = layout(view, (0, 0, 600, 400), min_weight=10 * agg.size / root.size)
        self.assertEqual([it.node.name for it in inner], ["3 smaller items", "f1", "f2", "f3"])
        # Folded items keep their real parent.
        self.assertTrue(all(c.parent is root for c in view.children))

    def test_no_limit_shows_everything(self):
        root, *_ = make_tree()
        self.assertFalse(any(it.node.aggregate for it in layout(root, (0, 0, 600, 400))))
        # Limit above every child: everything folds into a single tile.
        items = layout(root, (0, 0, 600, 400), min_weight=10_000)
        self.assertEqual([it.node.name for it in items], ["root", "2 smaller items"])


class HitTestTests(unittest.TestCase):
    def test_deepest_item_wins(self):
        root, a, b, f1, f2 = make_tree()
        items = layout(root, (0, 0, 600, 400))
        by_node = {it.node: it for it in items}
        x, y, w, h = by_node[f1].rect
        hit = hit_test(items, x + w / 2, y + h / 2)
        self.assertEqual(hit.node, f1)
        self.assertIsNone(hit_test(items, -1, -1))
        rx, ry, rw, rh = by_node[root].rect
        self.assertEqual(hit_test(items, rx + 1, ry + 1).node, root)


if __name__ == "__main__":
    unittest.main()
