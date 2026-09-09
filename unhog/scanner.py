"""Filesystem scanner that builds a tree of files occupying local storage.

OneDrive "Files On-Demand" placeholders (online-only files) carry the
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS attribute. Files without it are
hydrated, i.e. they use local disk space. Reading attributes through
``os.scandir`` / ``DirEntry.stat`` uses the directory listing only and
never opens the file, so scanning does not trigger downloads.

The tree is built in place by ``TreeBuilder``: every file found is added to
the totals of all its parent folders at once, so the partially built tree is
always consistent enough to be drawn while the scan is still running.
"""

from __future__ import annotations

import os
import stat as stat_mod
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Container, Optional, Sequence

FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

# Called now and then during a scan with the root of the partially built tree
# and an estimate (0..1) of how far the scan has got, see ``TreeBuilder.progress``.
# The root's ``total_files``, ``file_count`` and ``size`` are the running totals.
ProgressCallback = Callable[["Node", float], None]

# One file as listed by the scanner: (name, path, size, local, mtime, pinned).
FileEntry = tuple[str, str, int, bool, float, bool]


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
    """Folder scanned when none is given.

    On Windows this is the OneDrive folder (from the environment variables
    OneDrive sets, else ``~/OneDrive``). On Linux and macOS there is no
    OneDrive placeholder attribute to look for, so the home folder is
    scanned, or the file system root if the home folder does not exist.
    """
    home = os.path.expanduser("~")
    if os.name != "nt":
        return home if os.path.isdir(home) else os.path.abspath(os.sep)
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        value = os.environ.get(var)
        if value and os.path.isdir(value):
            return value
    return os.path.join(home, "OneDrive")


class TreeBuilder:
    """Grows a tree in place, keeping folder totals current after every file.

    Used by ``scan`` and by the demo's replayed scan. ``add_dir`` attaches a
    folder to its parent right away; ``add_files`` (a folder's files at once,
    what the scanner uses) and ``add_file`` (one file, used by the demo) add
    the files' numbers to every folder above them, so the tree can be drawn
    while it is still being built. ``enter_dir`` / ``finish_dir`` bracket the
    scan of each folder's contents and feed the ``progress`` estimate.
    ``progress_cb`` is called with the root and that estimate at most every
    ``PROGRESS_INTERVAL`` seconds; a set ``cancel`` event raises
    ``ScanCancelled`` at the next folder.

    Another thread may read the tree while it grows: attributes are updated
    one at a time and files are appended to their folder only after the
    totals above include them, so folder totals never fall behind their
    children.
    """

    PROGRESS_INTERVAL = 0.1

    def __init__(self, root: Node, progress_cb: Optional[ProgressCallback] = None,
                 cancel: Optional[threading.Event] = None):
        self.root = root
        self.progress_cb = progress_cb
        self.cancel = cancel
        self._next_progress = time.monotonic() + self.PROGRESS_INTERVAL
        # One entry per folder whose contents are being scanned, outermost first:
        # [subfolders finished, subfolders it has]. Drives ``progress``.
        self._open: list[list[int]] = []

    def add_dir(self, parent: Node, name: str, path: str) -> Node:
        child = Node(name, path, True, parent=parent, local=False)
        parent.children.append(child)
        return child

    def add_file(self, parent: Node, name: str, path: str, size: int, local: bool,
                 mtime: float, pinned: bool = False) -> Optional[Node]:
        """Record a file; returns its node, or None for an empty file (counted, not shown)."""
        node = None
        if size > 0:
            node = Node(name, path, False, size=size if local else 0, file_count=1 if local else 0,
                        total_files=1, pinned=pinned, parent=parent, total_size=size, local=local,
                        mtime=mtime)
            self._propagate(parent, 1, size, size if local else 0, 1 if local else 0, mtime)
            parent.children.append(node)
        else:
            self._propagate(parent, 1, 0, 0, 0, 0.0)
        self.tick()
        return node

    def add_files(self, parent: Node, files: Sequence[FileEntry]) -> None:
        """Record all the files directly inside ``parent`` at once.

        ``files`` holds ``(name, path, size, local, mtime, pinned)`` tuples.
        The totals above are updated once for the whole batch, and the nodes
        are attached only after that, so the tree stays consistent for a
        concurrent reader just as with ``add_file``. Empty files are counted
        but get no node.
        """
        nodes: list[Node] = []
        total_size = local_size = local_files = 0
        newest = 0.0
        for name, path, size, local, mtime, pinned in files:
            if size <= 0:
                continue
            total_size += size
            if local:
                local_size += size
                local_files += 1
                nodes.append(Node(name, path, False, size, 1, 1, pinned, parent=parent,
                                  total_size=size, mtime=mtime))
            else:
                nodes.append(Node(name, path, False, 0, 0, 1, pinned, parent=parent,
                                  total_size=size, local=False, mtime=mtime))
            if mtime > newest:
                newest = mtime
        if files:
            self._propagate(parent, len(files), total_size, local_size, local_files, newest)
        if nodes:
            parent.children.extend(nodes)
        self.tick()

    @staticmethod
    def _propagate(parent: Node, files: int, total_size: int, local_size: int, local_files: int,
                   newest: float) -> None:
        """Add a batch of files' numbers to ``parent`` and every folder above it."""
        anc: Optional[Node] = parent
        while anc is not None:
            anc.total_files += files
            if total_size:
                anc.total_size += total_size
                if local_size:
                    anc.size += local_size
                    anc.file_count += local_files
                    anc.local = True
                if newest > anc.mtime:
                    anc.mtime = newest
            anc = anc.parent

    def enter_dir(self, node: Node, subdirs: int) -> None:
        """Call before adding ``node``'s contents; ``subdirs`` is how many folders it holds."""
        self._open.append([0, subdirs])

    def finish_dir(self, node: Node) -> None:
        """Call once all of ``node``'s contents are added: drops it if empty, else sorts it."""
        if self._open:
            self._open.pop()
            if self._open:
                self._open[-1][0] += 1
        if node.total_size == 0 and node.parent is not None:
            node.parent.children.remove(node)  # nothing to show: drop empty subtree
            node.parent = None
            return
        node.children.sort(key=lambda n: n.total_size, reverse=True)
        node.pinned = bool(node.children) and all(c.pinned for c in node.children)

    def progress(self) -> float:
        """How far the scan has got, 0..1, judged by folders alone.

        Nothing is known about a folder's size before it is scanned, so each
        folder being scanned is treated as if its subfolders were equally big:
        the estimate is the share of the root's subfolders finished, plus the
        current one's share times the same estimate one level down, and so on.
        This never goes backwards and ends at 1 when the root is finished.
        """
        frac = 0.0
        for done, total in reversed(self._open):
            frac = (done + frac) / total if total > 0 else 0.0
        return 1.0 if not self._open else min(1.0, frac)

    def tick(self) -> None:
        """Check for cancellation and report progress; ``add_file`` calls this."""
        if self.cancel is not None and self.cancel.is_set():
            raise ScanCancelled()
        if self.progress_cb is not None:
            now = time.monotonic()
            if now >= self._next_progress:
                self._next_progress = now + self.PROGRESS_INTERVAL
                self.progress_cb(self.root, self.progress())

    def finish(self) -> Node:
        """Finalize the root and report the finished tree; returns the root."""
        self.finish_dir(self.root)
        if self.progress_cb is not None:
            self.progress_cb(self.root, 1.0)
        return self.root


FILE_ATTRIBUTE_DIRECTORY = stat_mod.FILE_ATTRIBUTE_DIRECTORY
FILE_ATTRIBUTE_REPARSE_POINT = stat_mod.FILE_ATTRIBUTE_REPARSE_POINT
_WINDOWS = os.name == "nt"


def _scan_dir(builder: TreeBuilder, node: Node) -> None:
    """List ``node``'s folder, add its files as one batch, then recurse into its subfolders.

    This loop runs once per entry of the scanned tree, so it is kept lean: on
    Windows the entry's kind is read from the attributes that ``stat`` already
    returned (the directory listing supplies them, no extra system call), and
    symlinks and junctions are told apart by their reparse tag.
    """
    files: list[FileEntry] = []
    subdirs: list[os.DirEntry] = []
    try:
        with os.scandir(node.path) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _WINDOWS:
                    attrs = st.st_file_attributes
                    if attrs & FILE_ATTRIBUTE_REPARSE_POINT and _is_link_reparse(st):
                        continue  # symlink / junction: avoid cycles and double counting
                    if attrs & FILE_ATTRIBUTE_DIRECTORY:
                        subdirs.append(entry)
                    else:
                        files.append((entry.name, entry.path, st.st_size,
                                      not attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, st.st_mtime,
                                      bool(attrs & FILE_ATTRIBUTE_PINNED)))
                else:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry)
                    elif entry.is_file(follow_symlinks=False):
                        files.append((entry.name, entry.path, st.st_size, True, st.st_mtime, False))
    except OSError:
        pass

    builder.enter_dir(node, len(subdirs))
    builder.add_files(node, files)
    for entry in subdirs:
        child = builder.add_dir(node, entry.name, entry.path)
        _scan_dir(builder, child)
        builder.finish_dir(child)


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
    ``progress_cb`` receives the root of the growing tree and a progress
    estimate now and then, and once more when the scan is complete. Raises
    ``ScanCancelled`` if ``cancel`` is set while scanning.
    """
    root = os.path.abspath(root)
    node = Node(os.path.basename(root.rstrip("\\/")) or root, root, True, local=False)
    return scan_into(node, progress_cb, cancel)


def scan_into(node: Node, progress_cb: Optional[ProgressCallback] = None,
              cancel: Optional[threading.Event] = None) -> Node:
    """Scan ``node.path`` into ``node``, an empty folder node.

    The node may already hang in a tree: every file found is then added to
    the folders above it as well, so a single folder can be scanned again in
    place (see ``detach`` and ``refresh_upwards``). Returns ``node``.
    """
    builder = TreeBuilder(node, progress_cb, cancel)
    _scan_dir(builder, node)
    return builder.finish()


def detach(node: Node) -> Optional[Node]:
    """Take ``node`` out of its tree, removing its numbers from every folder above.

    Returns the former parent (None for a root). Used before scanning the same
    folder again into a fresh node attached in its place.
    """
    parent = node.parent
    if parent is None:
        return None
    parent.children.remove(node)
    node.parent = None
    anc: Optional[Node] = parent
    while anc is not None:
        anc.total_files -= node.total_files
        anc.total_size -= node.total_size
        anc.size -= node.size
        anc.file_count -= node.file_count
        anc.local = anc.size > 0
        anc = anc.parent
    return parent


def refresh_upwards(folder: Node) -> None:
    """Recompute what ``finish_dir`` derives (child order, newest file, pinned)
    for ``folder`` and every folder above it, after a subtree was replaced."""
    anc: Optional[Node] = folder
    while anc is not None:
        anc.children.sort(key=lambda n: n.total_size, reverse=True)
        anc.mtime = max((c.mtime for c in anc.children), default=0.0)
        anc.pinned = bool(anc.children) and all(c.pinned for c in anc.children)
        anc.local = anc.size > 0
        anc = anc.parent


FilePredicate = Callable[[Node], bool]


def apply_filter(root: Node, predicate: Optional[FilePredicate],
                 hidden: Optional[Container[str]] = None) -> None:
    """Recompute the filtered sizes/counts (``fsize`` etc.) for the whole tree.

    ``predicate`` decides per file whether it counts; folders sum up their
    matching contents. ``None`` means everything counts, so the filtered
    numbers equal the unfiltered ones. Folders whose path is in ``hidden``
    count as empty, so they and everything inside them drop out of the
    totals above them (the nodes inside keep their own numbers).
    """
    if not hidden:
        hidden = ()
        if predicate is None:
            for node in _walk(root):
                node.fsize = node.ftotal_size = node.ffile_count = node.ftotal_files = -1
                node.fmatch = True
            return
    if predicate is None:
        def predicate(node: Node) -> bool:
            return True
    for node in _walk_postorder(root):
        if node.is_dir and node.path in hidden:
            node.fsize = node.ftotal_size = node.ffile_count = node.ftotal_files = 0
            node.fmatch = True
        elif node.is_dir:
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
