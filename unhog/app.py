"""Dear PyGui front-end: SpaceMonger-style treemap of local-storage files."""

from __future__ import annotations

import datetime
import math
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Optional

import dearpygui.dearpygui as dpg

from .scanner import Node, ScanCancelled, apply_filter, default_root, format_size, scan
from .treemap import Item, hit_test, layout, local_size, open_aggregate, total_size
from .win_dialogs import pick_folder

FONT_SIZE = 15          # UI widgets and unscaled treemap labels
FONT_MIN, FONT_MAX = 10, 34  # label size range when "Scale by size" is on
# How strongly folder padding shrinks with folder size ("Padding scaling"):
# the fraction of the chosen padding that the smallest folders still get.
PAD_SCALING_CHOICES: dict[str, float] = {
    "Off (100%)": 1.0, "Very mild (80%)": 0.8, "Mild (60%)": 0.6, "Moderate (45%)": 0.45,
    "Medium (30%)": 0.3, "Firm (20%)": 0.2, "Strong (10%)": 0.1, "Extreme (5%)": 0.05,
}
DEFAULT_PAD_SCALING = "Firm (20%)"
DRAW_FONT_PX = 36       # native size of the drawlist font; labels are scaled down from it
TITLE_PAD = 5           # title strip height = label font size + TITLE_PAD
# Inner margin of a folder around its children (the parent shows as a frame).
PAD_CHOICES: dict[str, int] = {f"{px} px": px for px in (1, 2, 4, 6, 9, 12, 18, 24, 32)}
DEFAULT_PAD = "12 px"
MIN_PX = 3
RESIZE_SETTLE_S = 0.12

KB, MB, GB = 1024, 1024 ** 2, 1024 ** 3
MIN_SIZE_CHOICES: dict[str, int] = {
    "Off": 0, "100 KB": 100 * KB, "1 MB": MB, "10 MB": 10 * MB, "50 MB": 50 * MB,
    "100 MB": 100 * MB, "500 MB": 500 * MB, "1 GB": GB, "5 GB": 5 * GB, "10 GB": 10 * GB,
    "50 GB": 50 * GB,
}
DEFAULT_MIN_SIZE = "1 MB"
MAX_HISTORY = 100       # views remembered for Back
WINDOW_TITLE = "Unhog"

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
HOVER = (255, 255, 120)
HOVER_THICKNESS = 4
BACKGROUND = (20, 21, 24)
TOOLTIP_BG = (30, 32, 38)
AGGREGATE_FILL = (72, 72, 78)
TOOLTIP_BORDER = (120, 124, 132)


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


class UnhogApp:
    def __init__(self, root_path: Optional[str] = None):
        self.root_path = root_path or default_root()
        self.tree: Optional[Node] = None
        self.view: Optional[Node] = None
        self.items: list[Item] = []
        self.hovered: Optional[Item] = None
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
        self._setup_font()
        self._setup_theme()

        with dpg.window(tag="main") as self.main:
            with dpg.group(horizontal=True):
                dpg.add_text("Folder:")
                self.path_input = dpg.add_input_text(default_value=self.root_path, width=340,
                                                     on_enter=True, callback=self._on_path_enter)
                dpg.add_button(label="Browse...", callback=self._on_browse)
                dpg.add_button(label="Rescan", callback=self._on_rescan)
                dpg.add_spacer(width=12)
                dpg.add_checkbox(label="Local files only", default_value=True,
                                 callback=self._on_local_only)
                dpg.add_spacer(width=12)
                dpg.add_text("Min size:")
                dpg.add_combo(items=list(MIN_SIZE_CHOICES), default_value=DEFAULT_MIN_SIZE,
                              width=90, callback=self._on_min_size)
                self.min_size_label = dpg.add_text("", color=TEXT_DIM)
                dpg.add_spacer(width=12)
                dpg.add_text("Modified:")
                dpg.add_combo(items=list(MODIFIED_CHOICES), default_value=DEFAULT_MODIFIED,
                              width=170, callback=self._on_modified)
                dpg.add_spacer(width=12)
                dpg.add_button(label="Preferences...", callback=self._show_preferences)
            with dpg.group(horizontal=True):  # navigation + breadcrumb
                dpg.add_button(label="Back", callback=self._on_back)
                dpg.add_button(label="Up", callback=self._on_up)
                dpg.add_button(label="Home", callback=self._on_home)
                dpg.add_spacer(width=12)
                dpg.add_text("Current folder:")
                with dpg.group(horizontal=True) as self.breadcrumb:
                    dpg.add_text("")
            self.status = dpg.add_text("Ready.")
            self.drawlist = dpg.add_drawlist(width=100, height=100)
            if self.draw_font is not None:
                dpg.bind_item_font(self.drawlist, self.draw_font)
            self.main_layer = dpg.add_draw_layer(parent=self.drawlist)
            self.overlay_layer = dpg.add_draw_layer(parent=self.drawlist)

        with dpg.window(label="Preferences", modal=True, show=False, autosize=True,
                        no_collapse=True, no_saved_settings=True) as self.prefs_window:
            dpg.add_checkbox(label="Scale fonts and padding by folder size",
                             default_value=self.scale_fonts, callback=self._on_scale_fonts)
            dpg.add_spacer(height=4)
            with dpg.group(horizontal=True):
                dpg.add_text("Padding:")
                dpg.add_combo(items=list(PAD_CHOICES), default_value=DEFAULT_PAD, width=110,
                              callback=self._on_pad)
            dpg.add_text("Frame width a folder draws around its children.", color=TEXT_DIM)
            dpg.add_spacer(height=4)
            with dpg.group(horizontal=True):
                dpg.add_text("Padding scaling:")
                dpg.add_combo(items=list(PAD_SCALING_CHOICES), default_value=DEFAULT_PAD_SCALING,
                              width=160, callback=self._on_pad_scaling)
            dpg.add_text("Share of the padding that the smallest folders keep.", color=TEXT_DIM)
            dpg.add_spacer(height=8)
            dpg.add_button(label="Close", width=100,
                           callback=lambda: dpg.configure_item(self.prefs_window, show=False))

        with dpg.window(popup=True, no_title_bar=True, show=False, autosize=True,
                        no_move=True) as self.context_menu:
            self.ctx_open = dpg.add_selectable(label="Open in Explorer", callback=self._ctx_open)
            self.ctx_reveal = dpg.add_selectable(label="Open containing folder in Explorer",
                                                 callback=self._ctx_reveal)
            self.ctx_zoom = dpg.add_selectable(label="Show only this folder", callback=self._ctx_zoom)
            dpg.add_separator()
            self.ctx_back = dpg.add_selectable(label="Back", callback=self._ctx_back)
            self.ctx_copy = dpg.add_selectable(label="Copy path", callback=self._ctx_copy)

        self.file_dialog = dpg.add_file_dialog(directory_selector=True, show=False, modal=True,
                                               width=760, height=460, callback=self._on_dir_chosen,
                                               default_path=self.root_path)

        with dpg.handler_registry():
            dpg.add_mouse_move_handler(callback=self._on_mouse_move)
            dpg.add_mouse_double_click_handler(button=dpg.mvMouseButton_Left, callback=self._on_double_click)
            dpg.add_mouse_click_handler(button=dpg.mvMouseButton_Right, callback=self._on_right_click)

        dpg.create_viewport(title=WINDOW_TITLE, width=1280, height=820)
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
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 8, 6)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 3)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 6, 6)
        dpg.bind_theme(theme)

    # -- main loop -----------------------------------------------------------

    def run(self) -> None:
        self.build()
        self.start_scan(self.root_path)
        while dpg.is_dearpygui_running():
            self._drain_messages()
            self._track_size()
            dpg.render_dearpygui_frame()
        self.cancel.set()
        dpg.destroy_context()

    def _track_size(self) -> None:
        vw, vh = dpg.get_viewport_client_width(), dpg.get_viewport_client_height()
        x0, y0 = dpg.get_item_rect_min(self.drawlist)
        w = max(50, vw - x0 - 8)
        h = max(50, vh - y0 - 8)
        if (w, h) != self.drawlist_size:
            self.drawlist_size = (w, h)
            dpg.configure_item(self.drawlist, width=w, height=h)
            self.size_changed_at = time.monotonic()
            self.needs_layout = True
        if self.needs_layout and time.monotonic() - self.size_changed_at >= RESIZE_SETTLE_S:
            self.needs_layout = False
            self.redraw()

    def _drain_messages(self) -> None:
        try:
            while True:
                msg = self.msgs.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, seen, local, nbytes = msg
                    dpg.set_value(self.status, f"Scanning... {seen:,} files seen, "
                                               f"{local:,} local ({format_size(nbytes)})")
                elif kind == "done":
                    self.tree = msg[1]
                    self._apply_modified_filter()
                    self.set_view(self.tree)
                    self._update_status()
                elif kind == "error":
                    dpg.set_value(self.status, f"Scan failed: {msg[1]}")
                elif kind == "cancelled":
                    pass
        except queue.Empty:
            pass

    # -- scanning ------------------------------------------------------------

    def start_scan(self, path: str) -> None:
        if self.scan_thread is not None and self.scan_thread.is_alive():
            self.cancel.set()
            self.scan_thread.join(timeout=5)
        self.cancel = threading.Event()
        self.root_path = path
        self.tree = None
        self.set_view(None)
        dpg.set_value(self.path_input, path)
        dpg.set_value(self.status, f"Scanning {path} ...")
        cancel = self.cancel

        def worker():
            def progress(seen, local, nbytes):
                self.msgs.put(("progress", seen, local, nbytes))
            try:
                tree = scan(path, progress, cancel)
            except ScanCancelled:
                self.msgs.put(("cancelled",))
            except Exception as exc:  # noqa: BLE001
                self.msgs.put(("error", str(exc)))
            else:
                self.msgs.put(("done", tree))

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
        dpg.set_value(self.status, summary + " Double-click a folder to zoom in, "
                                   "double-click background to go up, right-click for options.")

    def _weight(self, node: Node) -> int:
        return node.view_size if self.local_only else node.view_total_size

    def _apply_modified_filter(self) -> None:
        """Recompute the filtered sizes for the current "Modified" choice."""
        if self.tree is None:
            return
        rule = MODIFIED_CHOICES.get(self.modified_choice)
        if rule is None:
            apply_filter(self.tree, None)
            return
        mode, age = rule
        cutoff = time.time() - age
        if mode == "older":
            apply_filter(self.tree, lambda n: n.mtime < cutoff)
        else:
            apply_filter(self.tree, lambda n: n.mtime >= cutoff)

    def _on_modified(self, sender, app_data) -> None:
        self.modified_choice = app_data if app_data in MODIFIED_CHOICES else DEFAULT_MODIFIED
        self._apply_modified_filter()
        self._climb_to_visible_view()
        self._update_status()
        self.redraw()

    def _climb_to_visible_view(self) -> None:
        """If the current folder has nothing to show, move up to one that has."""
        if self.view is not None and self._weight(self.view) <= 0:
            node = self.view
            while node.parent is not None and self._weight(node) <= 0:
                node = node.parent
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

    def _show_preferences(self) -> None:
        vw, vh = dpg.get_viewport_client_width(), dpg.get_viewport_client_height()
        dpg.configure_item(self.prefs_window, show=True, pos=(max(0, vw // 2 - 200), max(0, vh // 3)))

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
        self.items = layout(self.view, (0, 0, w, h), min_px=MIN_PX, title_h=self._title_h_for,
                            pad=self._pad_for, weight=local_size if self.local_only else total_size,
                            min_weight=self._effective_min_size())
        self._update_min_size_label()
        for item in self.items:
            self._draw_item(item)

    def _draw_item(self, item: Item) -> None:
        x, y, w, h = item.rect
        pmin, pmax = (x, y), (x + w, y + h)
        node = item.node
        fs = self._font_size_for(node)
        if node.is_dir:
            dpg.draw_rectangle(pmin, pmax, fill=folder_color(node, item.depth),
                               color=FOLDER_BORDER + (255,), parent=self.main_layer)
            if item.title_h > 0:
                label = f"{node.name}  ({format_size(self._weight(node))})"
                self._draw_label(label, x + 4, y + (item.title_h - fs) / 2 - 1, w - 8, TEXT, fs)
        else:
            fill = AGGREGATE_FILL + (255,) if node.aggregate else file_color(node, item.depth)
            dpg.draw_rectangle(pmin, pmax, fill=fill, color=(0, 0, 0, 160), parent=self.main_layer)
            if w >= 28 and h >= fs + 4:
                lines = [node.name]
                if h >= 2 * fs + 8:
                    lines.append(format_size(self._weight(node)))
                for i, line in enumerate(lines):
                    color = TEXT_DIM if node.aggregate or i > 0 else TEXT
                    self._draw_label(line, x + 3, y + 2 + i * (fs + 1), w - 6, color, fs)

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
        if n.aggregate:
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
        line_h = FONT_SIZE + 3
        tw = max(self._text_width(t) for t in lines) + 12
        th = line_h * len(lines) + 8
        dw, dh = self.drawlist_size
        tx = mx + 16 if mx + 16 + tw <= dw else max(0, mx - tw - 4)
        ty = my + 20 if my + 20 + th <= dh else max(0, my - th - 4)
        dpg.draw_rectangle((tx, ty), (tx + tw, ty + th), fill=TOOLTIP_BG + (240,),
                           color=TOOLTIP_BORDER + (255,), parent=self.overlay_layer)
        for i, t in enumerate(lines):
            dpg.draw_text((tx + 6, ty + 4 + i * line_h), t, size=FONT_SIZE,
                          color=(TEXT if i == 0 else TEXT_DIM) + (255,), parent=self.overlay_layer)

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
        if dpg.is_item_shown(self.context_menu):
            return
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
        item = self._item_under_mouse()
        if item is None:
            return
        self.context_node = item.node
        is_dir = item.node.is_dir
        is_agg = item.node.aggregate
        dpg.configure_item(self.ctx_open, show=is_dir or is_agg)  # aggregate path is its folder
        dpg.configure_item(self.ctx_zoom, show=(is_dir or is_agg) and item.node is not self.view,
                           label="Show contents" if is_agg else "Show only this folder")
        dpg.configure_item(self.ctx_reveal, show=not is_dir and not is_agg)
        dpg.configure_item(self.ctx_back, show=bool(self.history))
        for sel in (self.ctx_open, self.ctx_reveal, self.ctx_zoom, self.ctx_back, self.ctx_copy):
            dpg.set_value(sel, False)
        dpg.configure_item(self.context_menu, show=True, pos=dpg.get_mouse_pos(local=False))

    # -- context menu actions -------------------------------------------------

    def _close_menu(self) -> None:
        dpg.configure_item(self.context_menu, show=False)

    def _ctx_open(self) -> None:
        self._close_menu()
        if self.context_node is not None:
            open_in_explorer(self.context_node.path)

    def _ctx_reveal(self) -> None:
        self._close_menu()
        if self.context_node is not None:
            reveal_in_explorer(self.context_node.path)

    def _ctx_zoom(self) -> None:
        self._close_menu()
        if self.context_node is not None:
            self._zoom_into(self.context_node)

    def _ctx_back(self) -> None:
        self._close_menu()
        self.go_back()

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
    root = argv[0] if argv else None
    if root is not None and not os.path.isdir(root):
        print(f"Not a folder: {root}", file=sys.stderr)
        return 2
    UnhogApp(root).run()
    return 0
