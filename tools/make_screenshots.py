"""Render the documentation screenshots from the built-in demo folder tree.

Usage (from the repo root):

    python tools/make_screenshots.py [output-dir]

Writes PNG files to docs/images by default. The app is driven off-screen
through the same code path as normal use, just with ``demo.demo_scan`` in
place of the disk scanner, so screenshots never contain real files. Windows
only (Dear PyGui's frame-buffer capture needs its Direct3D backend), and the
Segoe UI font must be present for the text to look like it does for users.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dearpygui.dearpygui as dpg  # noqa: E402

from unhog.app import RESIZE_SETTLE_S, UnhogApp  # noqa: E402
from unhog.demo import DEMO_ROOT, demo_scan  # noqa: E402
from unhog.scanner import Node  # noqa: E402

WIDTH, HEIGHT = 1280, 800  # viewport client size; the PNGs come out this big


def find(tree: Node, rel_path: str) -> Node:
    """Return the node at ``rel_path`` (backslash-separated, relative to the root)."""
    node = tree
    for part in rel_path.split("\\"):
        node = next(c for c in node.children if c.name == part)
    return node


class ScreenshotApp(UnhogApp):
    def __init__(self, out_dir: str):
        super().__init__(DEMO_ROOT, scanner=demo_scan)
        self.out_dir = out_dir

    def frames(self, n: int) -> None:
        for _ in range(n):
            self._drain_messages()
            self._track_size()
            dpg.render_dearpygui_frame()

    def settle(self) -> None:
        """Let the scan result arrive and the resize debounce run out."""
        self.frames(3)
        time.sleep(RESIZE_SETTLE_S + 0.05)
        self.frames(3)

    def hover(self, node: Node) -> None:
        """Draw the hover outline and tooltip for ``node`` as if the mouse were over it."""
        item = next(i for i in self.items if i.node is node)
        x, y, w, h = item.rect
        self.hovered = item
        self._draw_overlay(x + w * 0.5, y + min(h * 0.5, 40))

    def save(self, name: str) -> str:
        path = os.path.join(self.out_dir, name + ".png")
        self.frames(2)
        dpg.output_frame_buffer(path)
        self.frames(3)  # the capture is written on a later frame
        return path

    def set_modified(self, choice: str) -> None:
        dpg.set_value(self.modified_combo, choice)
        self._on_modified(None, choice)

    def set_local_only(self, value: bool) -> None:
        dpg.set_value(self.local_only_check, value)
        self._on_local_only(None, value)

    def shoot(self) -> list[str]:
        self.build()
        dpg.set_viewport_width(WIDTH)
        dpg.set_viewport_height(HEIGHT)
        self.start_scan(self.root_path)
        self.settle()
        assert self.tree is not None, "demo scan did not finish"
        out = []

        # 1. The whole OneDrive folder, default settings.
        self.hover(find(self.tree, "Virtual Machines\\Ubuntu 22.04 dev.vhdx"))
        out.append(self.save("home"))

        # 2. Drilled into a folder, hovering a file.
        self.set_view(find(self.tree, "Pictures\\Camera Roll"))
        self.frames(2)
        self.hover(find(self.tree, "Pictures\\Camera Roll\\2020"))
        out.append(self.save("folder"))

        # 3. Only files older than a year, back at the root.
        self.set_view(self.tree)
        self.set_modified("Older than 1 year")
        self.frames(2)
        self.hover(find(self.tree, "Backups"))
        out.append(self.save("older-than"))

        # 4. All files including online-only ones (dimmed).
        self.set_modified("Any time")
        self.set_local_only(False)
        self.frames(2)
        self.hover(find(self.tree, "Virtual Machines\\Windows 10 test.vhdx"))
        out.append(self.save("all-files"))

        self.cancel.set()
        dpg.destroy_context()
        return out


def main(argv: list[str]) -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.abspath(argv[0]) if argv else os.path.join(root, "docs", "images")
    os.makedirs(out_dir, exist_ok=True)
    for path in ScreenshotApp(out_dir).shoot():
        if not os.path.isfile(path):
            print(f"missing: {path}", file=sys.stderr)
            return 1
        print(f"{path}  ({os.path.getsize(path) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
