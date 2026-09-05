"""Dear PyGui front-end: SpaceMonger-style treemap of local-storage files."""

from __future__ import annotations

import datetime
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any, Callable, Optional

import dearpygui.dearpygui as dpg

from . import __version__
from .scanner import (Node, ProgressCallback, ScanCancelled, apply_filter, default_root, detach,
                      format_size, refresh_upwards, scan, scan_into)
from .treemap import Item, hit_test, layout, local_size, open_aggregate, total_size
from .win_dialogs import pick_folder, show_properties, ui_scale

# Sizes below are designed for 96 DPI and multiplied by the display scale, so
# the app looks the same size at any scaling but is rendered crisply (the call
# also makes the process DPI-aware; it has to happen before the viewport exists).
UI_SCALE = ui_scale()


def px(design_px: float) -> int:
    """Design pixels (96 DPI) to real pixels at the display scale."""
    return int(round(design_px * UI_SCALE))


FONT_SIZE = px(15)      # UI widgets and unscaled treemap labels
FONT_MIN, FONT_MAX = px(10), px(34)  # label size range when "Scale by size" is on
# How strongly folder padding shrinks with folder size ("Padding scaling"):
# the fraction of the chosen padding that the smallest folders still get.
PAD_SCALING_CHOICES: dict[str, float] = {
    "Off (100%)": 1.0, "Very mild (80%)": 0.8, "Mild (60%)": 0.6, "Moderate (45%)": 0.45,
    "Medium (30%)": 0.3, "Firm (20%)": 0.2, "Strong (10%)": 0.1, "Extreme (5%)": 0.05,
}
DEFAULT_PAD_SCALING = "Firm (20%)"
DRAW_FONT_PX = px(36)   # native size of the drawlist font; labels are scaled down from it
TITLE_PAD = px(5)       # title strip height = label font size + TITLE_PAD
# Inner margin of a folder around its children (the parent shows as a frame).
PAD_CHOICES: dict[str, int] = {f"{p} px": px(p) for p in (1, 2, 4, 6, 9, 12, 18, 24, 32)}
DEFAULT_PAD = "12 px"
MIN_PX = px(3)
RESIZE_SETTLE_S = 0.12
LIVE_REDRAW_S = 0.5     # how often the treemap is redrawn while a scan is running, at most
LIVE_REDRAW_BUDGET = 4  # after a live redraw, pause at least this many times its duration

KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3
MIN_SIZE_CHOICES: dict[str, int] = {
    "Off": 0, "100 KB": 100 * KB, "1 MB": MB, "10 MB": 10 * MB, "50 MB": 50 * MB,
    "100 MB": 100 * MB, "500 MB": 500 * MB, "1 GB": GB, "5 GB": 5 * GB, "10 GB": 10 * GB,
    "50 GB": 50 * GB,
}
DEFAULT_MIN_SIZE = "1 MB"
MAX_HISTORY = 100       # views remembered for Back
APP_NAME = "Unhog"
AUTHOR = "Andras Lasso"
WEBSITE = "https://github.com/lassoan/unhog"
WINDOW_TITLE = f"{APP_NAME} {__version__}"

DAY = 86400.0
# "Modified" filter: label -> (mode, age in seconds). Files are compared against
# now - age; "older" keeps files modified before that, "newer" those since.
MODIFIED_CHOICES: dict[str, Optional[tuple[str, float]]] = {"Any time": None}
for _label, _days in (("1 month", 30), ("3 months", 91), ("6 months", 182), ("1 year", 365),
                      ("2 years", 730), ("5 years", 1826)):
    MODIFIED_CHOICES[f"Older than {_label}"] = ("older", _days * DAY)
for _label, _days in (("1 week", 7), ("1 month", 30), ("3 months", 91), ("6 months", 182),
                      ("1 year", 365)):
    MODIFIED_CHOICES[f"Newer than {_label}"] = ("newer", _days * DAY)
DEFAULT_MODIFIED = "Any time"

# Colors ---------------------------------------------------------------------

CATEGORY_COLORS = {
    "video": (220, 70, 70),
    "image": (240, 160, 40),
    "audio": (166, 88, 224),
    "document": (64, 136, 236),
    "archive": (168, 118, 60),
    "code": (64, 184, 104),
    "disk": (126, 126, 180),
    "other": (140, 146, 156),
}
EXT_CATEGORY: dict[str, str] = {}
for _cat, _exts in {
    "video": "mp4 mov avi mkv wmv m4v mpg mpeg webm 3gp mts",
    "image": "jpg jpeg png gif heic heif bmp tif tiff raw cr2 nef arw dng webp svg psd",
    "audio": "mp3 wav flac m4a wma aac ogg opus aiff",
    "document": "doc docx pdf xls xlsx ppt pptx txt md odt ods odp rtf csv one pub vsd vsdx epub",
    "archive": "zip rar 7z tar gz bz2 xz cab",
    "code": "py js ts tsx jsx c cpp h hpp cs java json xml html css yaml yml toml ini bat ps1 sh sql ipynb",
    "disk": "iso vhd vhdx vmdk img bak pst ost",
}.items():
    for _e in _exts.split():
        EXT_CATEGORY["." + _e] = _cat

# Folder frame colors by nesting depth (cycled). Muted, clearly different hues so
# nesting reads at a glance while file-type colors still stand out.
FOLDER_PALETTE = [
    (34, 82, 148),   # 0 blue  (the folder currently shown)
    (24, 128, 116),  # 1 teal
    (146, 112, 34),  # 2 olive
    (138, 52, 124),  # 3 plum
    (48, 134, 64),   # 4 green
    (168, 74, 48),   # 5 rust
    (76, 76, 170),   # 6 indigo
    (130, 130, 40),  # 7 khaki
]
FOLDER_BORDER = (24, 26, 30)
FOLDER_PASSTHROUGH = (96, 98, 104)  # folder failing the Modified filter, shown only as a parent
TEXT = (235, 235, 235)
TEXT_DIM = (200, 200, 205)
LINK = (110, 170, 255)
HOVER = (255, 255, 120)
HOVER_THICKNESS = px(4)
BACKGROUND = (20, 21, 24)
TOOLTIP_BG = (36, 46, 68)     # blue-tinted, so tooltips do not look like the (gray) right-click menu
AGGREGATE_FILL = (72, 72, 78)
TOOLTIP_BORDER = (128, 150, 200)
FREE_FILL = (40, 43, 49)      # "Show free space" tile: dark, hatched, clearly not a file
FREE_HATCH = (66, 70, 78)
FREE_HATCH_STEP = px(14)


def file_color(node: Node, depth: int):
    r, g, b = CATEGORY_COLORS[EXT_CATEGORY.get(node.extension, "other")]
    f = max(0.7, 1.0 - 0.04 * depth)
    if not node.local:
        f *= 0.45  # online-only placeholder: draw dimmed
    return (int(r * f), int(g * f), int(b * f), 255)


def folder_color(node: Node, depth: int):
    """One color per folder for both its title strip and the frame around its children.

    A folder that does not itself match the active Modified filter (its newest
    file is on the wrong side of the cutoff) is drawn gray: it is only there
    because something inside it matches.
    """
    if not node.fmatch:
        return FOLDER_PASSTHROUGH + (255,)
    return FOLDER_PALETTE[depth % len(FOLDER_PALETTE)] + (255,)


# App -----------------------------------------------------------------------

# Anything that builds a tree the way ``scanner.scan`` does (see also ``demo.demo_scan``).
Scanner = Callable[[str, Optional[ProgressCallback], Optional[threading.Event]], Node]
# Anything that fills an empty folder node the way ``scanner.scan_into`` does (``demo.demo_rescan``).
Rescanner = Callable[[Node, Optional[ProgressCallback], Optional[threading.Event]], Node]
# ``shutil.disk_usage`` or a stand-in: path -> object with ``total`` and ``free`` bytes.
DiskUsage = Callable[[str], Any]


class UnhogApp:
    def __init__(self, root_path: Optional[str] = None, scanner: Scanner = scan,
                 disk_usage: DiskUsage = shutil.disk_usage, rescanner: Rescanner = scan_into):
        self.root_path = root_path or default_root()
        self.scanner = scanner
        self.rescanner = rescanner
        self.disk_usage = disk_usage
        self.rescan_parent: Optional[Node] = None  # set while one folder is being scanned again
        self.rescan_node: Optional[Node] = None    # the (initially empty) node that scan fills
        self.tree: Optional[Node] = None           # the tree being shown; grows while scanning
        self.tree_filtered = False                 # ``apply_filter`` has run with a real filter
        self.scanning = False
        self.progress_estimate = 0.0               # from the scanner: share of folders done
        self.next_live_redraw = 0.0
        self.disk: Any = None                      # disk usage of the scanned folder's drive
        self.free_node: Optional[Node] = None      # synthetic tile for the drive's free space
        self.show_free = False
        self.view: Optional[Node] = None
        self.items: list[Item] = []
        self.hovered: Optional[Item] = None
        self.hover_dirty = False                   # mouse moved: refresh the tooltip next frame
        self.context_node: Optional[Node] = None
        self.msgs: "queue.Queue[tuple]" = queue.Queue()
        self.cancel = threading.Event()
        self.scan_thread: Optional[threading.Thread] = None
        self.drawlist_size = (0, 0)
        self.size_changed_at = 0.0
        self.needs_layout = False
        self.text_widths: dict[str, float] = {}   # width at the draw font's native size
        self.draw_font = None
        self.draw_font_px = 13.0                  # DPG default font size, if Segoe UI is missing
        self.local_only = True
        self.scale_fonts = True
        self.min_size = MIN_SIZE_CHOICES[DEFAULT_MIN_SIZE]
        self.pad = PAD_CHOICES[DEFAULT_PAD]
        self.pad_scale_min = PAD_SCALING_CHOICES[DEFAULT_PAD_SCALING]
        self.history: list[Node] = []             # previous views, for Back
        self.modified_choice = DEFAULT_MODIFIED

    # -- setup ---------------------------------------------------------------

    def build(self) -> None:
        dpg.create_context()
        # Callbacks are run by ``frame`` on the UI thread instead of DPG's own
        # callback thread, so a click can never redraw the treemap while a live
        # redraw is halfway through (that could deadlock inside DPG).
        dpg.configure_app(manual_callback_management=True)
        self._setup_font()
        self._setup_theme()

        with dpg.window(tag="main") as self.main:
            with dpg.group(horizontal=True):
                dpg.add_text("Folder:")
                self.path_input = dpg.add_input_text(default_value=self.root_path, width=px(340),
                                                     on_enter=True, callback=self._on_path_enter)
                dpg.add_button(label="Browse...", callback=self._on_browse)
                dpg.add_button(label="Rescan", callback=self._on_rescan)
                dpg.add_spacer(width=px(12))
                self.local_only_check = dpg.add_checkbox(label="Local files only", default_value=True,
                                                         callback=self._on_local_only)
                dpg.add_spacer(width=px(12))
                dpg.add_text("Min size:")
                dpg.add_combo(items=list(MIN_SIZE_CHOICES), default_value=DEFAULT_MIN_SIZE,
                              width=px(90), callback=self._on_min_size)
                self.min_size_label = dpg.add_text("", color=TEXT_DIM)
                dpg.add_spacer(width=px(12))
                dpg.add_text("Modified:")
                self.modified_combo = dpg.add_combo(items=list(MODIFIED_CHOICES), default_value=DEFAULT_MODIFIED,
                                                    width=px(170), callback=self._on_modified)
                dpg.add_spacer(width=px(12))
                dpg.add_button(label="Settings...", callback=self._show_settings)
            with dpg.group(horizontal=True):  # navigation + breadcrumb
                dpg.add_button(label="Back", callback=self._on_back)
                dpg.add_button(label="Zoom out", callback=self._on_up)
                dpg.add_button(label="Zoom full", callback=self._on_home)
                dpg.add_spacer(width=px(12))
                dpg.add_text("Current folder:")
                with dpg.group(horizontal=True) as self.breadcrumb:
                    dpg.add_text("")
            with dpg.group(horizontal=True):
                self.progress_bar = dpg.add_progress_bar(default_value=0.0, width=px(220), show=False)
                self.status = dpg.add_text("Ready.")
            self.drawlist = dpg.add_drawlist(width=100, height=100)
            if self.draw_font is not None:
                dpg.bind_item_font(self.drawlist, self.draw_font)
            self.main_layer = dpg.add_draw_layer(parent=self.drawlist)
            self.overlay_layer = dpg.add_draw_layer(parent=self.drawlist)

        with dpg.window(label="Settings", modal=True, show=False, autosize=True,
                        no_collapse=True, no_saved_settings=True) as self.settings_window:
            dpg.add_checkbox(label="Scale fonts and padding by folder size",
                             default_value=self.scale_fonts, callback=self._on_scale_fonts)
            dpg.add_spacer(height=px(4))
            with dpg.group(horizontal=True):
                dpg.add_text("Padding:")
                dpg.add_combo(items=list(PAD_CHOICES), default_value=DEFAULT_PAD, width=px(110),
                              callback=self._on_pad)
            dpg.add_text("Frame width a folder draws around its children.", color=TEXT_DIM)
            dpg.add_spacer(height=px(4))
            with dpg.group(horizontal=True):
                dpg.add_text("Padding scaling:")
                dpg.add_combo(items=list(PAD_SCALING_CHOICES), default_value=DEFAULT_PAD_SCALING,
                              width=px(160), callback=self._on_pad_scaling)
            dpg.add_text("Share of the padding that the smallest folders keep.", color=TEXT_DIM)
            dpg.add_spacer(height=px(8))
            self.show_free_check = dpg.add_checkbox(label="Show free space on the drive",
                                                    default_value=self.show_free, callback=self._on_show_free)
            dpg.add_text("A hatched tile beside the folder, on the same scale.", color=TEXT_DIM)
            dpg.add_spacer(height=px(8))
            dpg.add_separator()
            dpg.add_spacer(height=px(4))
            dpg.add_text("About")
            with dpg.group() as about:  # single-spaced lines
                dpg.add_text(f"{APP_NAME} {__version__}", color=TEXT_DIM)
                dpg.add_text(f"Author: {AUTHOR}", color=TEXT_DIM)
                with dpg.group(horizontal=True):
                    dpg.add_text("Website:", color=TEXT_DIM)
                    # Plain text (so it lines up with the label) made clickable below.
                    self.website_link = dpg.add_text(WEBSITE, color=LINK)
            dpg.bind_item_theme(about, self.tight_theme)
            dpg.add_spacer(height=px(8))
            dpg.add_button(label="Close", width=px(100),
                           callback=lambda: dpg.configure_item(self.settings_window, show=False))

        with dpg.window(popup=True, no_title_bar=True, show=False, autosize=True,
                        no_move=True) as self.context_menu:
            self.ctx_back = dpg.add_selectable(label="Back", callback=self._ctx_back)
            self.ctx_zoom = dpg.add_selectable(label="Zoom in", callback=self._ctx_zoom)
            self.ctx_nav_sep = dpg.add_separator()
            self.ctx_open = dpg.add_selectable(label="Open in Explorer", callback=self._ctx_open)
            # For a file: its folder. For a folder: the parent, with the folder selected.
            self.ctx_folder = dpg.add_selectable(label="Open folder in Explorer", callback=self._ctx_reveal)
            self.ctx_copy = dpg.add_selectable(label="Copy path", callback=self._ctx_copy)
            self.ctx_props = dpg.add_selectable(label="Open Properties", callback=self._ctx_properties)
            self.ctx_rescan_sep = dpg.add_separator()
            self.ctx_rescan = dpg.add_selectable(label="Rescan", callback=self._ctx_rescan)

        self.file_dialog = dpg.add_file_dialog(directory_selector=True, show=False, modal=True,
                                               width=px(760), height=px(460), callback=self._on_dir_chosen,
                                               default_path=self.root_path)

        with dpg.item_handler_registry() as link_handlers:
            dpg.add_item_clicked_handler(callback=lambda: webbrowser.open(WEBSITE))
        dpg.bind_item_handler_registry(self.website_link, link_handlers)

        with dpg.handler_registry():
            dpg.add_mouse_move_handler(callback=self._on_mouse_move)
            dpg.add_mouse_double_click_handler(button=dpg.mvMouseButton_Left, callback=self._on_double_click)
            dpg.add_mouse_click_handler(button=dpg.mvMouseButton_Right, callback=self._on_right_click)
            dpg.add_key_press_handler(key=dpg.mvKey_Escape, callback=self._on_escape)

        dpg.create_viewport(title=WINDOW_TITLE, width=px(1280), height=px(820))
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window(self.main, True)

    def _setup_font(self) -> None:
        font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf")
        if not os.path.isfile(font_path):
            return
        with dpg.font_registry():
            font = dpg.add_font(font_path, FONT_SIZE)  # character ranges are automatic in DPG 2.x
            # Treemap labels are drawn at many sizes; scaling down from a large
            # atlas stays crisp, scaling up would blur.
            self.draw_font = dpg.add_font(font_path, DRAW_FONT_PX)
            self.draw_font_px = float(DRAW_FONT_PX)
        dpg.bind_font(font)

    def _setup_theme(self) -> None:
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, BACKGROUND)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, px(8), px(6))
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, px(6), px(3))
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, px(6), px(6))
        dpg.bind_theme(theme)
        with dpg.theme() as self.tight_theme:  # single-spaced lines of text
            with dpg.theme_component(dpg.mvAll):
                # Text items are laid out at frame height, so the vertical frame
                # padding has to go too, not just the spacing between items.
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, px(6), px(2))
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, px(6), 0)

    # -- main loop -----------------------------------------------------------

    def run(self) -> None:
        self.build()
        self.start_scan(self.root_path)
        while dpg.is_dearpygui_running():
            self.frame()
        self.cancel.set()
        dpg.destroy_context()

    def frame(self) -> None:
        """One round of the UI loop: run queued callbacks, apply scan results, draw."""
        jobs = dpg.get_callback_queue()
        if jobs:
            dpg.run_callbacks(jobs)
        self._drain_messages()
        self._track_size()
        self._live_redraw()
        self._update_hover()
        dpg.render_dearpygui_frame()

    def _track_size(self) -> None:
        vw, vh = dpg.get_viewport_client_width(), dpg.get_viewport_client_height()
        x0, y0 = dpg.get_item_rect_min(self.drawlist)
        w = max(50, vw - x0 - px(8))
        h = max(50, vh - y0 - px(8))
        if (w, h) != self.drawlist_size:
            self.drawlist_size = (w, h)
            dpg.configure_item(self.drawlist, width=w, height=h)
            self.size_changed_at = time.monotonic()
            self.needs_layout = True
        if self.needs_layout and time.monotonic() - self.size_changed_at >= RESIZE_SETTLE_S:
            self.needs_layout = False
            self.redraw()

    def _drain_messages(self) -> None:
        """Handle what the scan thread posted. Messages carry the cancel event of
        their scan as a token, so leftovers from a superseded scan are ignored."""
        try:
            while True:
                msg = self.msgs.get_nowait()
                kind, token = msg[0], msg[1]
                if token is not self.cancel:
                    continue
                if kind == "progress":
                    _, _, t, self.progress_estimate = msg  # t: the root of what is being scanned
                    if self.rescan_parent is None:
                        self._adopt_tree(t)
                    self._update_progress()
                    verb = "Scanning" if self.rescan_parent is None else "Rescanning"
                    dpg.set_value(self.status, f"{verb} {t.path} ... {t.total_files:,} files seen, "
                                               f"{t.file_count:,} local ({format_size(t.size)}) so far.")
                elif kind == "done":
                    if self.rescan_parent is None:
                        self._adopt_tree(msg[2])
                    self._end_scan()
                    self._refresh_view()
                    self._update_status()
                elif kind == "disk":
                    self.disk = msg[2]
                    self._make_free_node()
                    self._update_progress()
                    self.redraw()
                elif kind == "error":
                    self._end_scan()
                    dpg.set_value(self.status, f"Scan failed: {msg[2]}")
                elif kind == "cancelled":
                    self._end_scan()
        except queue.Empty:
            pass

    def _adopt_tree(self, tree: Node) -> None:
        """Start showing ``tree`` (the scan's root, possibly still growing)."""
        if self.tree is tree:
            return
        self.tree = tree
        self.tree_filtered = False
        self._apply_modified_filter()
        self.set_view(tree)
        self.next_live_redraw = time.monotonic() + LIVE_REDRAW_S

    def _live_redraw(self) -> None:
        """While a scan runs, redraw the growing tree now and then.

        Redraws are at least ``LIVE_REDRAW_S`` apart, and after a slow one
        (huge trees) the pause grows to ``LIVE_REDRAW_BUDGET`` times its
        duration, so the UI thread never spends most of its time redrawing.
        """
        if not self.scanning or self.tree is None:
            return
        now = time.monotonic()
        if now < self.next_live_redraw:
            return
        self._refresh_view()
        took = time.monotonic() - now
        self.next_live_redraw = now + took + max(LIVE_REDRAW_S, LIVE_REDRAW_BUDGET * took)

    def _end_scan(self) -> None:
        self.scanning = False
        if self.rescan_parent is not None:
            refresh_upwards(self.rescan_parent)  # child order, newest file, pinned
            self.rescan_parent = self.rescan_node = None
        self._update_progress()

    def _update_progress(self) -> None:
        """Show scan progress: bytes for a whole drive, finished folders otherwise.

        For a drive root the drive's used bytes are known up front, so progress
        is the local bytes found so far against them. For a folder nothing
        says how big it is, so the scanner's folder-based estimate is used.
        """
        if not self.scanning:
            dpg.configure_item(self.progress_bar, show=False)
            return
        used = int(self.disk.used) if self.disk is not None else 0
        if self.rescan_parent is None and self._scanning_whole_drive() and used > 0 and self.tree is not None:
            frac = min(1.0, self.tree.size / used)
            label = f"{frac:.0%}  {format_size(self.tree.size)} of {format_size(used)}"
        else:
            frac = self.progress_estimate
            label = f"{frac:.0%}"
        dpg.configure_item(self.progress_bar, show=True, overlay=label)
        dpg.set_value(self.progress_bar, frac)

    def _scanning_whole_drive(self) -> bool:
        r"""True when the scanned folder is the root of its drive, e.g. ``C:\``."""
        drive, rest = os.path.splitdrive(os.path.abspath(self.root_path))
        return bool(drive) and rest.strip("\\/") == ""

    def _refresh_view(self) -> None:
        """Recompute the filter for the tree as it is now and redraw the current folder."""
        self._apply_modified_filter()
        self._climb_to_visible_view()
        self.redraw()

    # -- scanning ------------------------------------------------------------

    def start_scan(self, path: str) -> None:
        """Scan ``path`` from scratch, replacing whatever is shown."""
        self._stop_scan()
        self.root_path = path
        self.tree = None
        self.disk = None
        self.free_node = None
        self.rescan_parent = None
        self.set_view(None)
        dpg.set_value(self.path_input, path)
        cancel = self._begin_scan(f"Scanning {path} ...")

        def worker():
            try:
                self.msgs.put(("disk", cancel, self.disk_usage(path)))
            except OSError:
                pass
            self._run_scan(lambda progress: self.scanner(path, progress, cancel), cancel)

        self._launch(worker)

    def rescan_folder(self, folder: Node) -> None:
        """Scan just ``folder`` again, in place; the rest of the tree is kept.

        The old subtree's numbers are taken out of the folders above, and an
        empty node is attached in its place and filled by the scan, so the
        treemap shows the folder filling in live and the totals stay right.
        """
        if folder is self.tree or folder.parent is None:
            self.start_scan(self.root_path)
            return
        self._stop_scan()
        parent = detach(folder)
        fresh = Node(folder.name, folder.path, True, parent=parent, local=False)
        parent.children.append(fresh)
        self._redirect_views(folder, fresh)
        self.rescan_parent = parent
        self.rescan_node = fresh
        cancel = self._begin_scan(f"Rescanning {folder.path} ...")
        self._launch(lambda: self._run_scan(lambda progress: self.rescanner(fresh, progress, cancel), cancel))
        self._refresh_view()

    def _redirect_views(self, old: Node, new: Node) -> None:
        """Point the current view and the Back history at ``new`` where they were inside ``old``."""
        def redirect(node: Node) -> Node:
            return new if old in node.ancestors() else node
        if self.view is not None:
            self.view = redirect(self.view)
            self._rebuild_breadcrumb()
        history = [redirect(n) for n in self.history]
        self.history = [n for i, n in enumerate(history) if i == 0 or n is not history[i - 1]]

    def _stop_scan(self) -> None:
        if self.scan_thread is not None and self.scan_thread.is_alive():
            self.cancel.set()
            self.scan_thread.join(timeout=5)

    def _begin_scan(self, status: str) -> threading.Event:
        """Reset the per-scan state; returns the new cancel event, which also tags the scan's messages."""
        self.cancel = threading.Event()
        self.scanning = True
        self.progress_estimate = 0.0
        self._update_progress()
        dpg.set_value(self.status, status)
        return self.cancel

    def _run_scan(self, run: Callable[[ProgressCallback], Node], cancel: threading.Event) -> None:
        """Worker-thread body: run the scan and post its outcome for ``_drain_messages``."""
        def progress(tree, estimate):
            self.msgs.put(("progress", cancel, tree, estimate))
        try:
            tree = run(progress)
        except ScanCancelled:
            self.msgs.put(("cancelled", cancel))
        except Exception as exc:  # noqa: BLE001
            self.msgs.put(("error", cancel, str(exc)))
        else:
            self.msgs.put(("done", cancel, tree))

    def _launch(self, worker: Callable[[], None]) -> None:
        self.scan_thread = threading.Thread(target=worker, name="scan", daemon=True)
        self.scan_thread.start()

    def _update_status(self) -> None:
        t = self.tree
        if t is None:
            return
        filt = "" if self.modified_choice == DEFAULT_MODIFIED else f" modified {self.modified_choice.lower()}"
        if self.local_only:
            summary = (f"Showing {t.view_file_count:,} local files{filt} using {format_size(t.view_size)} "
                       f"(of {t.total_files:,} files, {format_size(t.total_size)} total in {t.path}).")
        else:
            summary = (f"Showing all {t.view_total_files:,} files{filt}, {format_size(t.view_total_size)} "
                       f"(of {t.total_files:,} files, {format_size(t.total_size)} total in {t.path}); "
                       f"{t.view_file_count:,} of them use {format_size(t.view_size)} of local storage "
                       "(online-only files drawn dimmed).")
        if self.show_free and self.disk is not None:
            summary += (f" {format_size(self.disk.free)} of {format_size(self.disk.total)} free on "
                        f"{self._drive()}.")
        dpg.set_value(self.status, summary + " Double-click a folder to zoom in, "
                                   "double-click background to zoom out, right-click for options.")

    # -- free space tile -----------------------------------------------------

    def _drive(self) -> str:
        """The drive (or share) the scanned folder is on, e.g. "C:"."""
        return os.path.splitdrive(self.root_path)[0] or self.root_path

    def _make_free_node(self) -> None:
        """A file-like node standing for the drive's free space, laid out beside the root."""
        if self.disk is None:
            self.free_node = None
            return
        free = int(self.disk.free)
        self.free_node = Node(f"Free space on {self._drive()}", self._drive() + os.sep, False,
                              size=free, total_size=free, local=True)

    def _free_tile_shown(self) -> bool:
        """The tile is drawn only in the top-level view."""
        return (self.show_free and self.free_node is not None and self.tree is not None
                and self.view is self.tree)

    def _on_show_free(self, sender, app_data) -> None:
        self.show_free = bool(app_data)
        self._update_status()
        self.redraw()

    @staticmethod
    def _split_off(rect: tuple[float, float, float, float], keep: float, take: float):
        """Cut ``rect`` along its longer side into a part with weight ``keep`` and one with ``take``."""
        x, y, w, h = rect
        total = keep + take
        if total <= 0:
            return rect, None
        if w >= h:
            tw = w * take / total
            return (x, y, w - tw, h), (x + w - tw, y, tw, h)
        th = h * take / total
        return (x, y, w, h - th), (x, y + h - th, w, th)

    def _weight(self, node: Node) -> int:
        return node.view_size if self.local_only else node.view_total_size

    def _apply_modified_filter(self) -> None:
        """Recompute the filtered sizes for the current "Modified" choice."""
        if self.tree is None:
            return
        rule = MODIFIED_CHOICES.get(self.modified_choice)
        if rule is None:
            if self.tree_filtered:  # resetting is a pass over the whole tree: only when needed
                apply_filter(self.tree, None)
                self.tree_filtered = False
            return
        mode, age = rule
        cutoff = time.time() - age
        if mode == "older":
            apply_filter(self.tree, lambda n: n.mtime < cutoff)
        else:
            apply_filter(self.tree, lambda n: n.mtime >= cutoff)
        self.tree_filtered = True

    def _on_modified(self, sender, app_data) -> None:
        self.modified_choice = app_data if app_data in MODIFIED_CHOICES else DEFAULT_MODIFIED
        self._apply_modified_filter()
        self._climb_to_visible_view()
        self._update_status()
        self.redraw()

    def _climb_to_visible_view(self) -> None:
        """If the current folder has nothing to show, move up to one that has."""
        if self.scanning and self.view is self.rescan_node:
            return  # still empty only because its scan has just started: stay and watch it fill
        if self.view is not None and self._weight(self.view) <= 0:
            node = self.view
            while node.parent is not None and self._weight(node) <= 0:
                node = node.parent
            if node.parent is None and node is not self.tree and self.tree is not None:
                node = self.tree  # the folder turned out empty and was dropped from the tree
            self.view = node
            self._rebuild_breadcrumb()

    def _on_pad(self, sender, app_data) -> None:
        self.pad = PAD_CHOICES.get(app_data, PAD_CHOICES[DEFAULT_PAD])
        self.redraw()

    def _on_scale_fonts(self, sender, app_data) -> None:
        self.scale_fonts = bool(app_data)
        self.redraw()

    def _font_size_for(self, node: Node) -> int:
        """Label font size: fixed, or grown with the node's share of the current view."""
        if not self.scale_fonts or self.view is None:
            return FONT_SIZE
        total = self._weight(self.view)
        if self._free_tile_shown():
            total += self._weight(self.free_node)  # the free tile shares the area with the root
        frac = self._weight(node) / total if total > 0 else 0.0
        return int(round(FONT_MIN + (FONT_MAX - FONT_MIN) * math.sqrt(max(0.0, min(1.0, frac)))))

    def _title_h_for(self, node: Node) -> float:
        return self._font_size_for(node) + TITLE_PAD

    def _pad_for(self, node: Node) -> float:
        """Frame width: the chosen padding for the folder shown, thinner for smaller folders."""
        if not self.scale_fonts or self.view is None:
            return float(self.pad)
        total = self._weight(self.view)
        frac = self._weight(node) / total if total > 0 else 0.0
        lo = self.pad_scale_min
        scale = lo + (1.0 - lo) * math.sqrt(max(0.0, min(1.0, frac)))
        return max(1.0, round(self.pad * scale))

    def _show_settings(self) -> None:
        vw, vh = dpg.get_viewport_client_width(), dpg.get_viewport_client_height()
        dpg.configure_item(self.settings_window, show=True,
                           pos=(max(0, vw // 2 - px(200)), max(0, vh // 3)))

    def _on_pad_scaling(self, sender, app_data) -> None:
        self.pad_scale_min = PAD_SCALING_CHOICES.get(app_data, PAD_SCALING_CHOICES[DEFAULT_PAD_SCALING])
        self.redraw()

    def _effective_min_size(self) -> float:
        """The chosen limit applies to the scanned root; drilling in scales it down
        by the shown folder's share of the root, so detail stays consistent."""
        if self.min_size <= 0 or self.tree is None or self.view is None:
            return float(self.min_size)
        root_w = self._weight(self.tree)
        if root_w <= 0:
            return float(self.min_size)
        return self.min_size * self._weight(self.view) / root_w

    def _update_min_size_label(self) -> None:
        eff = self._effective_min_size()
        if self.min_size > 0 and self.view is not None and self.view is not self.tree:
            dpg.set_value(self.min_size_label, f"(here: {format_size(eff)})")
        else:
            dpg.set_value(self.min_size_label, "")

    def _on_min_size(self, sender, app_data) -> None:
        self.min_size = MIN_SIZE_CHOICES.get(app_data, 0)
        self.redraw()

    def _on_local_only(self, sender, app_data) -> None:
        self.local_only = bool(app_data)
        self._climb_to_visible_view()
        self._update_status()
        self.redraw()

    # -- navigation ----------------------------------------------------------

    def set_view(self, node: Optional[Node], record: bool = True) -> None:
        """Show ``node``. The previous view is remembered for Back unless ``record`` is False."""
        if node is None:
            self.history.clear()  # new scan: nothing to go back to
        elif record and self.view is not None and node is not self.view:
            self.history.append(self.view)
            del self.history[:-MAX_HISTORY]
        self.view = node
        self.hovered = None
        self._rebuild_breadcrumb()
        self.redraw()

    def go_back(self) -> None:
        while self.history:
            node = self.history.pop()
            if node is not self.view:
                self.set_view(node, record=False)
                return

    def _on_back(self) -> None:
        self.go_back()

    def _rebuild_breadcrumb(self) -> None:
        dpg.delete_item(self.breadcrumb, children_only=True)
        if self.view is None:
            dpg.add_text("", parent=self.breadcrumb)
            return
        chain = self.view.ancestors()
        for i, node in enumerate(chain):
            if i:
                dpg.add_text(">", parent=self.breadcrumb)
            dpg.add_button(label=node.name, small=True, parent=self.breadcrumb,
                           user_data=node, callback=self._on_breadcrumb)

    def go_up(self) -> None:
        if self.view is not None and self.view.parent is not None:
            self.set_view(self.view.parent)

    def _on_breadcrumb(self, sender, app_data, user_data) -> None:
        self.set_view(user_data)

    def _on_up(self) -> None:
        self.go_up()

    def _on_home(self) -> None:
        if self.tree is not None:
            self.set_view(self.tree)

    def _on_rescan(self) -> None:
        self.start_scan(dpg.get_value(self.path_input) or self.root_path)

    def _on_path_enter(self, sender, app_data) -> None:
        path = app_data.strip().strip('"')
        if os.path.isdir(path):
            self.start_scan(path)
        else:
            dpg.set_value(self.status, f"Not a folder: {path}")

    def _on_browse(self) -> None:
        """Native Windows folder picker; falls back to the built-in dialog if unavailable."""
        try:
            path = pick_folder(initial=self.root_path, title="Select folder to scan",
                               owner_title=WINDOW_TITLE)
        except OSError:
            dpg.show_item(self.file_dialog)
            return
        if path and os.path.isdir(path):
            self.start_scan(path)

    def _on_dir_chosen(self, sender, app_data) -> None:
        path = app_data.get("file_path_name") or app_data.get("current_path")
        if path and os.path.isdir(path):
            self.start_scan(path)

    # -- drawing -------------------------------------------------------------

    def redraw(self) -> None:
        dpg.delete_item(self.main_layer, children_only=True)
        dpg.delete_item(self.overlay_layer, children_only=True)
        w, h = self.drawlist_size
        dpg.draw_rectangle((0, 0), (w, h), fill=BACKGROUND + (255,), color=BACKGROUND + (255,),
                           parent=self.main_layer)
        self.items = []
        if self.view is None or w <= 0 or h <= 0:
            return
        rect, free_rect = (0.0, 0.0, float(w), float(h)), None
        if self._free_tile_shown():
            rect, free_rect = self._split_off(rect, self._weight(self.view), self._weight(self.free_node))
        self.items = layout(self.view, rect, min_px=MIN_PX, title_h=self._title_h_for,
                            pad=self._pad_for, weight=local_size if self.local_only else total_size,
                            min_weight=self._effective_min_size())
        if free_rect is not None and free_rect[2] >= MIN_PX and free_rect[3] >= MIN_PX:
            self.items.append(Item(self.free_node, free_rect, 0))
        self._update_min_size_label()
        for item in self.items:
            self._draw_item(item)
        self.hover_dirty = True  # the overlay was cleared: put the tooltip back next frame

    def _draw_item(self, item: Item) -> None:
        x, y, w, h = item.rect
        pmin, pmax = (x, y), (x + w, y + h)
        node = item.node
        fs = self._font_size_for(node)
        if node is self.free_node:
            dpg.draw_rectangle(pmin, pmax, fill=FREE_FILL + (255,), color=FOLDER_BORDER + (255,),
                               parent=self.main_layer)
            self._draw_hatch(item.rect)
            self._draw_label(node.name, x + px(4), y + px(2), w - px(8), TEXT, fs)
            if h >= 2 * fs + px(8):
                self._draw_label(format_size(self._weight(node)), x + px(4), y + px(3) + fs, w - px(8),
                                 TEXT_DIM, fs)
        elif node.is_dir:
            dpg.draw_rectangle(pmin, pmax, fill=folder_color(node, item.depth),
                               color=FOLDER_BORDER + (255,), parent=self.main_layer)
            if item.title_h > 0:
                label = f"{node.name}  ({format_size(self._weight(node))})"
                if self.scanning and node is self.tree:
                    label += "  \u2013 scanning..."
                self._draw_label(label, x + px(4), y + (item.title_h - fs) / 2 - 1, w - px(8), TEXT, fs)
        else:
            fill = AGGREGATE_FILL + (255,) if node.aggregate else file_color(node, item.depth)
            dpg.draw_rectangle(pmin, pmax, fill=fill, color=(0, 0, 0, 160), parent=self.main_layer)
            if w >= px(28) and h >= fs + px(4):
                lines = [node.name]
                if h >= 2 * fs + px(8):
                    lines.append(format_size(self._weight(node)))
                for i, line in enumerate(lines):
                    color = TEXT_DIM if node.aggregate or i > 0 else TEXT
                    self._draw_label(line, x + px(3), y + px(2) + i * (fs + 1), w - px(6), color, fs)

    def _draw_hatch(self, rect: tuple[float, float, float, float]) -> None:
        """Diagonal lines across ``rect`` (clipped to it), marking the free-space tile."""
        x, y, w, h = rect
        x1, y1 = x + w, y + h
        c = x + y + FREE_HATCH_STEP
        while c < x1 + y1:  # each line is x + y = c
            ax = max(x, c - y1)
            bx = min(x1, c - y)
            if bx > ax:
                dpg.draw_line((ax, c - ax), (bx, c - bx), color=FREE_HATCH + (255,), thickness=1,
                              parent=self.main_layer)
            c += FREE_HATCH_STEP

    def _text_width(self, text: str, font_size: float = FONT_SIZE) -> float:
        """Rendered width of ``text`` drawn at ``font_size`` with the drawlist font."""
        native = self.text_widths.get(text)
        if native is None:
            kwargs = {"font": self.draw_font} if self.draw_font is not None else {}
            size = dpg.get_text_size(text, **kwargs)
            native = size[0] if size and size[0] > 0 else len(text) * self.draw_font_px * 0.55
            self.text_widths[text] = native
        return native * font_size / self.draw_font_px

    def _fit_text(self, text: str, max_w: float, font_size: float = FONT_SIZE) -> str:
        if self._text_width(text, font_size) <= max_w:
            return text
        ell = "\u2026"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._text_width(text[:mid] + ell, font_size) <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return (text[:lo] + ell) if lo > 0 else ""

    def _draw_label(self, text: str, x: float, y: float, max_w: float, color,
                    font_size: float = FONT_SIZE) -> None:
        if max_w < font_size:
            return
        text = self._fit_text(text, max_w, font_size)
        if text:
            dpg.draw_text((x, y), text, color=color + (255,), size=font_size, parent=self.main_layer)

    def _draw_overlay(self, mx: float, my: float) -> None:
        """Hover outline plus a tooltip box, drawn on the overlay layer."""
        dpg.delete_item(self.overlay_layer, children_only=True)
        item = self.hovered
        if item is None:
            return
        x, y, w, h = item.rect
        dpg.draw_rectangle((x, y), (x + w, y + h), color=HOVER + (255,), thickness=HOVER_THICKNESS,
                           parent=self.overlay_layer)

        n = item.node
        lines = [n.path]
        filtered = self.modified_choice != DEFAULT_MODIFIED
        suffix = f" (modified {self.modified_choice.lower()})" if filtered else ""
        if n is self.free_node:
            lines = self._free_tooltip()
        elif n.aggregate:
            lines[0] = f"{n.name} below the size limit in {n.path}"
            lines.append(f"Local: {format_size(n.view_size)} in {n.view_file_count:,} files{suffix}")
            lines.append(f"Total: {format_size(n.view_total_size)} in {n.view_total_files:,} files{suffix}")
        elif n.is_dir:
            lines.append(f"Local: {format_size(n.view_size)} in {n.view_file_count:,} files{suffix}")
            lines.append(f"Total: {format_size(n.view_total_size)} in {n.view_total_files:,} files{suffix}")
            if filtered:
                lines.append(f"Unfiltered: {format_size(n.size)} local, {format_size(n.total_size)} total")
                if not n.fmatch:
                    lines.append("Shown only as a parent: the folder itself does not match the filter")
            lines.append(f"Items inside: {len(n.children):,}")
            if n.mtime:
                when = datetime.datetime.fromtimestamp(n.mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"Newest file modified: {when}")
        else:
            state = "on local storage" if n.local else "online-only (no local storage)"
            lines.append(f"Size: {format_size(n.total_size)} ({n.total_size:,} bytes), {state}")
            if n.mtime:
                when = datetime.datetime.fromtimestamp(n.mtime).strftime("%Y-%m-%d %H:%M")
                lines.append(f"Modified: {when}")
        if n.pinned:
            lines.append("Always keep on this device")
        line_h = FONT_SIZE + px(3)
        tw = max(self._text_width(t) for t in lines) + px(12)
        th = line_h * len(lines) + px(8)
        dw, dh = self.drawlist_size
        dx, dy = px(16), px(20)  # offset from the mouse, so the pointer does not cover the box
        tx = mx + dx if mx + dx + tw <= dw else max(0, mx - tw - px(4))
        ty = my + dy if my + dy + th <= dh else max(0, my - th - px(4))
        dpg.draw_rectangle((tx, ty), (tx + tw, ty + th), fill=TOOLTIP_BG + (240,),
                           color=TOOLTIP_BORDER + (255,), parent=self.overlay_layer)
        for i, t in enumerate(lines):
            dpg.draw_text((tx + px(6), ty + px(4) + i * line_h), t, size=FONT_SIZE,
                          color=(TEXT if i == 0 else TEXT_DIM) + (255,), parent=self.overlay_layer)

    def _free_tooltip(self) -> list[str]:
        total, free = int(self.disk.total), int(self.disk.free)
        lines = [f"Free space on {self._drive()}",
                 f"Free: {format_size(free)} of {format_size(total)}"
                 + (f" ({free / total:.0%})" if total > 0 else "")]
        shown = self._weight(self.tree) if self.tree is not None else 0
        if shown > 0 and free > 0:
            what = "The local files shown" if self.local_only else "The files shown"
            lines.append(f"{what} use {format_size(shown)}, {shown / free:.1f}x the free space")
        return lines

    # -- mouse ---------------------------------------------------------------

    def _mouse_in_drawing(self) -> Optional[tuple[float, float]]:
        if not dpg.is_item_hovered(self.drawlist):
            return None
        mx, my = dpg.get_drawing_mouse_pos()
        return float(mx), float(my)

    def _item_under_mouse(self) -> Optional[Item]:
        pos = self._mouse_in_drawing()
        if pos is None or not self.items:
            return None
        return hit_test(self.items, *pos)

    def _on_mouse_move(self) -> None:
        self.hover_dirty = True  # several moves per frame are common: handle them once, in ``frame``

    def _update_hover(self) -> None:
        """Outline and tooltip for the item under the mouse, refreshed at most once per frame."""
        if dpg.is_item_shown(self.context_menu):
            self.hover_dirty = True  # refresh as soon as the menu closes, even if the mouse is still
            return
        if not self.hover_dirty:
            return
        self.hover_dirty = False
        pos = self._mouse_in_drawing()
        item = hit_test(self.items, *pos) if pos is not None and self.items else None
        if item is None and self.hovered is None:
            return
        self.hovered = item
        self._draw_overlay(*(pos or (0.0, 0.0)))

    def _on_double_click(self) -> None:
        if not dpg.is_item_hovered(self.drawlist):
            return
        item = self._item_under_mouse()
        if item is None or item.node is self.view:
            self.go_up()
        else:
            self._zoom_into(item.node)

    def _zoom_into(self, node: Node) -> None:
        """Show only this folder, or the items folded into a "smaller items" tile."""
        if node.aggregate and not node.is_dir:
            node = open_aggregate(node)
        if node.is_dir:
            self.set_view(node)

    def _on_right_click(self) -> None:
        menu_was_open = dpg.is_item_shown(self.context_menu)
        pos = self._mouse_in_drawing()
        if pos is None and menu_was_open:
            # While the menu is up the drawlist does not count as hovered, so find
            # the spot by hand: the click that closes the menu also opens it anew.
            mx, my = dpg.get_mouse_pos(local=False)
            x0, y0 = dpg.get_item_rect_min(self.drawlist)
            w, h = self.drawlist_size
            if 0 <= mx - x0 < w and 0 <= my - y0 < h:
                pos = (float(mx - x0), float(my - y0))
        item = hit_test(self.items, *pos) if pos is not None and self.items else None
        if item is None or item.node is self.free_node:
            self._close_menu()
            return
        if menu_was_open:
            self._close_menu()  # toggle it, so that it reopens at the new position
            self.hovered = item  # and move the highlight to the item the menu is now about
            self._draw_overlay(*pos)
        self.context_node = item.node
        folder_like = item.node.is_dir or item.node.aggregate  # an aggregate's path is its folder
        path = item.node.path.rstrip("\\/")
        has_parent = bool(path) and os.path.dirname(path) not in ("", path)  # not a drive root
        back = bool(self.history)
        # Zoom in is what double-clicking does: show this folder (or the folded items).
        zoom_in = folder_like and item.node is not self.view
        dpg.configure_item(self.ctx_back, show=back)
        dpg.configure_item(self.ctx_zoom, show=zoom_in)
        dpg.configure_item(self.ctx_nav_sep, show=back or zoom_in)
        dpg.configure_item(self.ctx_open, show=folder_like)
        dpg.configure_item(self.ctx_folder, show=has_parent)
        dpg.configure_item(self.ctx_props, show=sys.platform == "win32")
        dpg.configure_item(self.ctx_rescan_sep, show=not self.scanning)
        dpg.configure_item(self.ctx_rescan, show=not self.scanning)
        for sel in (self.ctx_back, self.ctx_zoom, self.ctx_open, self.ctx_folder, self.ctx_copy,
                    self.ctx_props, self.ctx_rescan):
            dpg.set_value(sel, False)
        dpg.configure_item(self.context_menu, show=True, pos=dpg.get_mouse_pos(local=False))

    # -- context menu actions -------------------------------------------------

    def _close_menu(self) -> None:
        dpg.configure_item(self.context_menu, show=False)

    def _on_escape(self) -> None:
        """Escape closes the right-click menu or the Settings window, whichever is open."""
        if dpg.is_item_shown(self.context_menu):
            self._close_menu()
        elif dpg.is_item_shown(self.settings_window):
            dpg.configure_item(self.settings_window, show=False)

    def _ctx_open(self) -> None:
        self._close_menu()
        if self.context_node is not None:
            open_in_explorer(self.context_node.path)

    def _ctx_reveal(self) -> None:
        """Open the item's folder in Explorer with the item selected."""
        self._close_menu()
        if self.context_node is not None:
            reveal_in_explorer(self.context_node.path)

    def _ctx_properties(self) -> None:
        self._close_menu()
        if self.context_node is not None:
            try:
                show_properties(self.context_node.path, owner_title=WINDOW_TITLE)
            except OSError as exc:
                dpg.set_value(self.status, f"Could not open Properties: {exc}")

    def _ctx_zoom(self) -> None:
        self._close_menu()
        if self.context_node is not None:
            self._zoom_into(self.context_node)

    def _ctx_back(self) -> None:
        self._close_menu()
        self.go_back()

    def _ctx_rescan(self) -> None:
        """Scan the item's folder again: the folder itself, or a file's (or tile's) folder."""
        self._close_menu()
        node = self.context_node
        if node is None or self.scanning:
            return
        folder = node if node.is_dir and not node.aggregate else node.parent
        if folder is not None:
            self.rescan_folder(folder)

    def _ctx_copy(self) -> None:
        self._close_menu()
        if self.context_node is not None:
            dpg.set_clipboard_text(self.context_node.path)


def open_in_explorer(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_in_explorer(path: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--demo" in argv:
        # Made-up folder tree instead of a disk scan: for trying the UI and for screenshots.
        from .demo import DEMO_ROOT, demo_disk_usage, demo_rescan, demo_scan
        argv = [a for a in argv if a != "--demo"]
        UnhogApp(argv[0] if argv else DEMO_ROOT, scanner=demo_scan, disk_usage=demo_disk_usage,
                 rescanner=demo_rescan).run()
        return 0
    root = argv[0] if argv else None
    if root is not None and not os.path.isdir(root):
        print(f"Not a folder: {root}", file=sys.stderr)
        return 2
    UnhogApp(root).run()
    return 0
