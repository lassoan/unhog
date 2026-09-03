"""Filesystem scanner that builds a tree of files occupying local storage.

OneDrive "Files On-Demand" placeholders (online-only files) carry the
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS attribute. Files without it are
hydrated, i.e. they use local disk space. Reading attributes through
``os.scandir`` / ``DirEntry.stat`` uses the directory listing only and
never opens the file, so scanning does not trigger downloads.
"""

from __future__ import annotations

import os
import stat as stat_mod
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

ProgressCallback = Callable[[int, int, int], None]  # files seen, local files, local bytes


def is_local(attrs: int) -> bool:
    """True if a file with these Win32 attributes occupies local storage."""
    return not (attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


def is_pinned(attrs: int) -> bool:
    """True if the file is marked "Always keep on this device"."""
    return bool(attrs & FILE_ATTRIBUTE_PINNED)


def is_unpinned(attrs: int) -> bool:
    """True if the file is marked "Free up space" (online-only requested)."""
    return bool(attrs & FILE_ATTRIBUTE_UNPINNED)


@dataclass(eq=False, slots=True)
class Node:
    name: str
    path: str
    is_dir: bool
    size: int = 0            # local bytes (for dirs: sum of children)
    file_count: int = 0      # local files in this subtree (1 for a local file)
    total_files: int = 0     # all files in this subtree, incl. online-only
    pinned: bool = False
    children: list["Node"] = field(default_factory=list)
    parent: Optional["Node"] = field(default=None, repr=False, compare=False)
    total_size: int = 0      # logical bytes of all files, incl. online-only
    local: bool = True       # for files: occupies local storage; for dirs: has any local bytes
    aggregate: bool = False  # synthetic "N smaller items" tile created by the layout
    mtime: float = 0.0       # last modification time (folders: newest file anywhere inside)
    # The same four numbers restricted to files matching the active filter
    # (see ``apply_filter``); -1 means "no filter", i.e. use the raw value.
    fsize: int = -1
    ftotal_size: int = -1
    ffile_count: int = -1
    ftotal_files: int = -1
    # Folders: False when the folder itself fails the active filter (judged by
    # its newest file) and is shown only as a container for matching contents.
    fmatch: bool = True

    # What the treemap shows: the filtered numbers when a filter is active.
    @property
    def view_size(self) -> int:
        return self.fsize if self.fsize >= 0 else self.size

    @property
    def view_total_size(self) -> int:
        return self.ftotal_size if self.ftotal_size >= 0 else self.total_size

    @property
    def view_file_count(self) -> int:
        return self.ffile_count if self.ffile_count >= 0 else self.file_count

    @property
    def view_total_files(self) -> int:
        return self.ftotal_files if self.ftotal_files >= 0 else self.total_files

    @property
    def extension(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    def ancestors(self) -> list["Node"]:
        """Path from the root down to (and including) this node."""
        chain: list[Node] = []
        node: Optional[Node] = self
        while node is not None:
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain


class ScanCancelled(Exception):
    """Raised inside the scanner when the cancel event is set."""


def default_root() -> str:
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        value = os.environ.get(var)
        if value and os.path.isdir(value):
            return value
    return os.path.join(os.path.expanduser("~"), "OneDrive")


class _Scanner:
    PROGRESS_EVERY = 500

    def __init__(self, progress_cb: Optional[ProgressCallback], cancel: Optional[threading.Event]):
        self.progress_cb = progress_cb
        self.cancel = cancel
        self.files_seen = 0
        self.local_files = 0
        self.local_bytes = 0

    def _tick(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise ScanCancelled()
        if self.progress_cb is not None and self.files_seen % self.PROGRESS_EVERY == 0:
            self.progress_cb(self.files_seen, self.local_files, self.local_bytes)

    def scan_dir(self, node: Node) -> None:
        try:
            entries = list(os.scandir(node.path))
        except OSError:
            return

        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            attrs = getattr(st, "st_file_attributes", 0)

            if entry.is_symlink():
                continue  # avoid cycles and double counting

            if entry.is_dir(follow_symlinks=False):
                if attrs & stat_mod.FILE_ATTRIBUTE_REPARSE_POINT and _is_link_reparse(st):
                    continue  # junction / symlink: avoid cycles and double counting
                child = Node(entry.name, entry.path, True, parent=node)
                self.scan_dir(child)
                node.total_files += child.total_files
                node.total_size += child.total_size
                node.size += child.size
                node.file_count += child.file_count
                if child.total_size > 0:
                    node.children.append(child)
                else:
                    child.parent = None  # nothing to show: drop empty subtree
            elif entry.is_file(follow_symlinks=False):
                self.files_seen += 1
                node.total_files += 1
                size = st.st_size
                local = is_local(attrs)
                if size > 0:
                    node.total_size += size
                    if local:
                        self.local_files += 1
                        self.local_bytes += size
                        node.size += size
                        node.file_count += 1
                    node.children.append(
                        Node(entry.name, entry.path, False, size=size if local else 0,
                             file_count=1 if local else 0, total_files=1,
                             pinned=is_pinned(attrs), parent=node, total_size=size, local=local,
                             mtime=st.st_mtime)
                    )
                self._tick()

        node.children.sort(key=lambda n: n.total_size, reverse=True)
        node.mtime = max((c.mtime for c in node.children), default=0.0)
        node.local = node.size > 0
        node.pinned = bool(node.children) and all(c.pinned for c in node.children)


IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # junction
IO_REPARSE_TAG_SYMLINK = 0xA000000C


def _is_link_reparse(st: os.stat_result) -> bool:
    """True for junctions and symlinks, which must not be descended.

    Cloud sync roots (e.g. SharePoint libraries synced by OneDrive, tag
    0x9000xxxA) and placeholder folders are reparse points too, but they are
    ordinary folders for our purposes and are scanned.
    """
    tag = getattr(st, "st_reparse_tag", 0)
    return tag in (IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK)


def scan(root: str, progress_cb: Optional[ProgressCallback] = None,
         cancel: Optional[threading.Event] = None) -> Node:
    """Scan ``root`` and return a tree of all non-empty files and folders.

    Each node records both ``size`` (bytes on local storage) and
    ``total_size`` (logical bytes incl. online-only placeholders).
    Raises ``ScanCancelled`` if ``cancel`` is set while scanning.
    """
    root = os.path.abspath(root)
    node = Node(os.path.basename(root.rstrip("\\/")) or root, root, True)
    scanner = _Scanner(progress_cb, cancel)
    scanner.scan_dir(node)
    if progress_cb is not None:
        progress_cb(scanner.files_seen, scanner.local_files, scanner.local_bytes)
    return node


FilePredicate = Callable[[Node], bool]


def apply_filter(root: Node, predicate: Optional[FilePredicate]) -> None:
    """Recompute the filtered sizes/counts (``fsize`` etc.) for the whole tree.

    ``predicate`` decides per file whether it counts; folders sum up their
    matching contents. ``None`` means everything counts, so the filtered
    numbers equal the unfiltered ones.
    """
    if predicate is None:
        for node in _walk(root):
            node.fsize = node.ftotal_size = node.ffile_count = node.ftotal_files = -1
            node.fmatch = True
        return
    for node in _walk_postorder(root):
        if node.is_dir:
            # Folders are containers: they show whatever inside them passes.
            # (A folder whose newest file passes "older than" therefore shows
            # in full; a folder with recent changes shows just its old parts.)
            node.fsize = sum(c.fsize for c in node.children)
            node.ftotal_size = sum(c.ftotal_size for c in node.children)
            node.ffile_count = sum(c.ffile_count for c in node.children)
            node.ftotal_files = sum(c.ftotal_files for c in node.children)
            node.fmatch = bool(predicate(node))
        elif predicate(node):
            node.fsize, node.ftotal_size = node.size, node.total_size
            node.ffile_count, node.ftotal_files = node.file_count, node.total_files
        else:
            node.fsize = node.ftotal_size = node.ffile_count = node.ftotal_files = 0


def _walk(root: Node):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _walk_postorder(root: Node):
    """Children before parents, without recursion (trees can be deep)."""
    order: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        order.append(node)
        stack.extend(node.children)
    return reversed(order)


def format_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"
