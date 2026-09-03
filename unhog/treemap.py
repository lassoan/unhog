"""Squarified treemap layout and hit-testing (pure Python, no GUI)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Union

from .scanner import Node

Rect = tuple[float, float, float, float]  # x, y, w, h
Weight = Callable[[Node], float]
TitleHeight = Union[float, Callable[[Node], float]]  # fixed, or per folder
Padding = Union[float, Callable[[Node], float]]      # fixed, or per folder


def local_size(node: Node) -> float:
    return node.view_size          # local bytes of files matching the active filter


def total_size(node: Node) -> float:
    return node.view_total_size    # all bytes of files matching the active filter


@dataclass(frozen=True)
class Item:
    node: Node
    rect: Rect
    depth: int
    title_h: float = 0.0  # height of the folder title strip (0 for files)

    def contains(self, x: float, y: float) -> bool:
        rx, ry, rw, rh = self.rect
        return rx <= x < rx + rw and ry <= y < ry + rh

    def in_title(self, x: float, y: float) -> bool:
        rx, ry, rw, _ = self.rect
        return self.title_h > 0 and rx <= x < rx + rw and ry <= y < ry + self.title_h


def _worst(row: Sequence[float], side: float) -> float:
    total = sum(row)
    if total <= 0 or side <= 0:
        return float("inf")
    rmax, rmin = max(row), min(row)
    s2 = total * total
    return max(side * side * rmax / s2, s2 / (side * side * rmin))


def squarify(weights: Sequence[float], x: float, y: float, w: float, h: float) -> list[Rect]:
    """Lay out ``weights`` (sorted descending) inside the rectangle.

    Returns one rect per weight, in the same order. Zero weights produce
    zero-size rects. Areas are proportional to the weights.
    """
    n = len(weights)
    if n == 0:
        return []
    total = float(sum(weights))
    if total <= 0 or w <= 0 or h <= 0:
        return [(x, y, 0.0, 0.0)] * n

    area_scale = (w * h) / total
    areas = [max(0.0, float(wt)) * area_scale for wt in weights]
    rects: list[Rect] = [(x, y, 0.0, 0.0)] * n

    cx, cy, cw, ch = x, y, w, h
    i = 0
    while i < n:
        if areas[i] <= 0:
            i += 1
            continue
        side = min(cw, ch)
        row = [areas[i]]
        j = i + 1
        while j < n and areas[j] > 0:
            if _worst(row + [areas[j]], side) <= _worst(row, side):
                row.append(areas[j])
                j += 1
            else:
                break
        row_area = sum(row)
        if cw >= ch:
            # Lay the row out vertically along the left edge.
            row_w = row_area / ch if ch > 0 else 0
            ry = cy
            for k, a in enumerate(row):
                rh = a / row_w if row_w > 0 else 0
                rects[i + k] = (cx, ry, row_w, rh)
                ry += rh
            cx += row_w
            cw -= row_w
        else:
            # Lay the row out horizontally along the top edge.
            row_h = row_area / cw if cw > 0 else 0
            rx = cx
            for k, a in enumerate(row):
                rw = a / row_h if row_h > 0 else 0
                rects[i + k] = (rx, cy, rw, row_h)
                rx += rw
            cy += row_h
            ch -= row_h
        i = j if j > i else i + 1
    return rects


def layout(root: Node, rect: Rect, min_px: float = 4.0, title_h: TitleHeight = 18.0,
           pad: Padding = 2.0, max_depth: int = 64, weight: Weight = local_size,
           min_weight: float = 0.0) -> list[Item]:
    """Recursively lay out ``root`` inside ``rect``.

    ``weight`` picks the number a node's area is proportional to (local
    bytes by default, or ``total_size`` to show every file). Nodes with a
    zero weight are omitted. Children whose weight is below ``min_weight``
    are not shown individually: per folder they are folded into one
    synthetic tile (``Node.aggregate`` set, name "N smaller items") that
    carries their combined size. Parents are emitted before their
    children so drawing in list order is correct. Folders reserve
    ``title_h`` pixels (a number, or a function of the folder node) for a
    title strip and lay their children out inside a padded inner rect.
    Recursion stops when an item is narrower than ``min_px``.
    """
    items: list[Item] = []
    _layout_into(root, rect, 0, items, min_px, title_h, pad, max_depth, weight, min_weight)
    return items


def make_aggregate(parent: Node, small: Sequence[Node]) -> Node:
    """One tile standing in for ``small`` (children of ``parent`` below the size limit)."""
    n = len(small)
    return Node(
        name=f"{n} smaller item{'s' if n != 1 else ''}",
        path=parent.path,
        is_dir=False,
        size=sum(c.size for c in small),
        file_count=sum(c.file_count for c in small),
        total_files=sum(c.total_files for c in small),
        pinned=all(c.pinned for c in small),
        parent=parent,
        total_size=sum(c.total_size for c in small),
        local=any(c.local for c in small),
        aggregate=True,
        children=list(small),  # kept so the tile can be opened; the items stay parented to ``parent``
        fsize=sum(c.view_size for c in small),
        ftotal_size=sum(c.view_total_size for c in small),
        ffile_count=sum(c.view_file_count for c in small),
        ftotal_files=sum(c.view_total_files for c in small),
        mtime=max((c.mtime for c in small), default=0.0),
    )


def open_aggregate(agg: Node) -> Node:
    """A folder-like view root holding the items folded into ``agg``.

    Its parent is the real folder, so going up from it returns there, and
    the folded items keep their real parent (they are not re-parented).
    """
    view = Node(
        name=agg.name, path=agg.path, is_dir=True, size=agg.size, file_count=agg.file_count,
        total_files=agg.total_files, pinned=agg.pinned, parent=agg.parent,
        total_size=agg.total_size, local=agg.local, aggregate=True, children=list(agg.children),
        fsize=agg.fsize, ftotal_size=agg.ftotal_size, ffile_count=agg.ffile_count,
        ftotal_files=agg.ftotal_files, mtime=agg.mtime,
    )
    return view


def _layout_into(node: Node, rect: Rect, depth: int, items: list[Item],
                 min_px: float, title_h: TitleHeight, pad: Padding, max_depth: int,
                 weight: Weight, min_weight: float) -> None:
    x, y, w, h = rect
    if w < min_px or h < min_px:
        return
    if not node.is_dir:
        items.append(Item(node, rect, depth))
        return

    want_th = title_h(node) if callable(title_h) else title_h
    th = want_th if h >= want_th * 2 and w >= min_px * 4 else 0.0
    items.append(Item(node, rect, depth, th))
    if depth >= max_depth:
        return

    p = pad(node) if callable(pad) else pad
    ix, iy = x + p, y + th + p
    iw, ih = w - 2 * p, h - th - 2 * p
    if iw < min_px or ih < min_px:
        return

    weighted = [(weight(c), c) for c in node.children if weight(c) > 0]
    if min_weight > 0:
        small = [c for wt, c in weighted if wt < min_weight]
        if small:
            weighted = [(wt, c) for wt, c in weighted if wt >= min_weight]
            agg = make_aggregate(node, small)
            weighted.append((weight(agg), agg))
    weighted.sort(key=lambda wc: wc[0], reverse=True)
    if not weighted:
        return
    rects = squarify([wt for wt, _ in weighted], ix, iy, iw, ih)
    for (_, child), crect in zip(weighted, rects):
        _layout_into(child, crect, depth + 1, items, min_px, title_h, pad, max_depth,
                     weight, min_weight)


def hit_test(items: Sequence[Item], x: float, y: float) -> Optional[Item]:
    """Return the deepest item containing the point, or None."""
    for item in reversed(items):
        if item.contains(x, y):
            return item
    return None
