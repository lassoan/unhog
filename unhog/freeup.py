"""Free up space: mark files online-only and watch the cloud client unload them.

Explorer's "Free up space" command deletes nothing. It sets the Win32
attribute ``FILE_ATTRIBUTE_UNPINNED`` (and clears ``FILE_ATTRIBUTE_PINNED``,
"Always keep on this device") on the file or folder and on everything inside
it; the sync client (OneDrive) notices and, in its own time and only once a
file's contents are safely in the cloud, replaces the local copy with an
online-only placeholder. ``free_up_space`` sets the same attributes, which is
also what ``attrib +U -P /S /D`` does, and then keeps listing the folders
concerned to see which files have been unloaded, reporting them to the caller
in batches so the treemap can drop them as they go. Attributes are read from
directory listings and written with ``SetFileAttributes``; no file is ever
opened, so nothing is downloaded.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .scanner import (FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_PINNED,
                      FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FILE_ATTRIBUTE_REPARSE_POINT,
                      FILE_ATTRIBUTE_UNPINNED, _is_link_reparse)

FILE_ATTRIBUTE_NORMAL = 0x00000080  # valid only on its own; dropped once other bits are set

POLL_S = 1.0             # how soon after marking (and after a change) the folders are listed again
POLL_MAX_S = 10.0        # the interval doubles while nothing changes, up to this
GIVE_UP_S = 30 * 60.0    # stop watching after this long without a file being unloaded
MARK_REPORT_S = 0.2      # how often progress is reported while marking


@dataclass
class FreeUpEvent:
    """What ``free_up_space`` reports as it goes.

    ``kind`` is "marking" (progress while the attributes are being set),
    "marked" (all set; ``remaining`` files were on local storage and are
    being watched), "unloaded" (``paths`` are no longer on local storage)
    or "done" (the watch ended; ``remaining`` files were still local).
    """
    kind: str
    files: int = 0       # files marked so far
    failed: int = 0      # files and folders whose attributes could not be changed
    remaining: int = 0   # marked files still on local storage
    paths: list[str] = field(default_factory=list)


FreeUpReport = Callable[[FreeUpEvent], None]
# Folder path -> names of the files in it that were on local storage when marked
# and have not been seen unloaded since.
Watch = dict[str, set[str]]

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.SetFileAttributesW.restype = wintypes.BOOL

    class WIN32_FILE_ATTRIBUTE_DATA(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                    ("nFileSizeHigh", wintypes.DWORD), ("nFileSizeLow", wintypes.DWORD)]

    _kernel32.GetFileAttributesExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                               ctypes.POINTER(WIN32_FILE_ATTRIBUTE_DATA)]
    _kernel32.GetFileAttributesExW.restype = wintypes.BOOL
    GetFileExInfoStandard = 0

    def get_attributes(path: str) -> tuple[int, int]:
        """The Win32 attributes and size of ``path``, from the directory entry (the file is not opened)."""
        data = WIN32_FILE_ATTRIBUTE_DATA()
        if not _kernel32.GetFileAttributesExW(path, GetFileExInfoStandard, ctypes.byref(data)):
            raise ctypes.WinError(ctypes.get_last_error())
        return data.dwFileAttributes, (data.nFileSizeHigh << 32) | data.nFileSizeLow

    def set_attributes(path: str, attrs: int) -> None:
        if not _kernel32.SetFileAttributesW(path, attrs):
            raise ctypes.WinError(ctypes.get_last_error())

else:  # pragma: no cover - other platforms

    def get_attributes(path: str) -> tuple[int, int]:
        raise OSError("Free up space is only available on Windows")

    def set_attributes(path: str, attrs: int) -> None:
        raise OSError("Free up space is only available on Windows")


def unpinned_attributes(attrs: int) -> int:
    """``attrs`` with the "Free up space" state: unpinned, and not pinned."""
    new = (attrs | FILE_ATTRIBUTE_UNPINNED) & ~FILE_ATTRIBUTE_PINNED
    if new != FILE_ATTRIBUTE_NORMAL:
        new &= ~FILE_ATTRIBUTE_NORMAL
    return new


def _unpin(path: str, attrs: int) -> bool:
    """Give ``path`` the "Free up space" attributes; True if it has them now."""
    if attrs & FILE_ATTRIBUTE_UNPINNED and not attrs & FILE_ATTRIBUTE_PINNED:
        return True  # already marked: nothing to write
    try:
        set_attributes(path, unpinned_attributes(attrs))
    except OSError:
        return False
    return True


def mark_online_only(path: str, report: Optional[FreeUpReport] = None,
                     cancel: Optional[threading.Event] = None) -> tuple[Watch, int, int]:
    """Set the "Free up space" attributes on ``path`` and, for a folder, everything inside it.

    Returns ``(watch, files, failed)``: the files that were on local storage
    (and so are expected to be unloaded), grouped by folder; how many files
    were marked; and how many files and folders could not be. Symlinks and
    junctions are not followed, as when scanning. ``report`` gets "marking"
    events now and then; a set ``cancel`` stops the walk early.
    """
    attrs, size = get_attributes(path)
    watch: Watch = {}
    files = failed = 0
    if not _unpin(path, attrs):
        failed += 1
    if not attrs & FILE_ATTRIBUTE_DIRECTORY:
        if not attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS and size > 0:
            watch[os.path.dirname(path)] = {os.path.basename(path)}
        return watch, 1 - failed, failed
    next_report = time.monotonic() + MARK_REPORT_S
    stack = [path]
    while stack:
        if cancel is not None and cancel.is_set():
            break
        folder = stack.pop()
        local: set[str] = set()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        failed += 1
                        continue
                    a = st.st_file_attributes
                    if a & FILE_ATTRIBUTE_REPARSE_POINT and _is_link_reparse(st):
                        continue
                    marked = _unpin(entry.path, a)
                    if not marked:
                        failed += 1
                    if a & FILE_ATTRIBUTE_DIRECTORY:
                        stack.append(entry.path)  # its contents are marked even if it could not be
                        continue
                    if not marked:
                        continue
                    files += 1
                    if not a & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS and st.st_size > 0:
                        local.add(entry.name)
        except OSError:
            failed += 1
        if local:
            watch[folder] = local
        if report is not None and time.monotonic() >= next_report:
            next_report = time.monotonic() + MARK_REPORT_S
            report(FreeUpEvent("marking", files, failed))
    return watch, files, failed


def _local_names(folder: str, names: set[str]) -> set[str]:
    """Which of ``names`` (files in ``folder``) are still on local storage.

    Read from the directory listing alone. A file that is gone, or a folder
    that is gone, counts as unloaded: no local storage is used either way.
    """
    still: set[str] = set()
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if entry.name in names:
                    try:
                        attrs = entry.stat(follow_symlinks=False).st_file_attributes
                    except OSError:
                        continue
                    if not attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS:
                        still.add(entry.name)
    except OSError:
        pass
    return still


def unloaded_files(watch: Watch) -> list[str]:
    """List the watched folders again: the paths of the files that have left local
    storage since the last look, which are then no longer watched."""
    unloaded: list[str] = []
    for folder in list(watch):
        names = watch[folder]
        still = _local_names(folder, names)
        unloaded.extend(os.path.join(folder, name) for name in sorted(names - still))
        if still:
            watch[folder] = still
        else:
            del watch[folder]
    return unloaded


def watch_unloading(watch: Watch, report: FreeUpReport, cancel: threading.Event,
                    poll_s: float = POLL_S, poll_max_s: float = POLL_MAX_S,
                    give_up_s: float = GIVE_UP_S) -> int:
    """Report the watched files as they are unloaded; returns how many never were.

    The folders are listed again every ``poll_s`` seconds; while nothing
    changes the interval doubles up to ``poll_max_s``, and after ``give_up_s``
    without a change the watch ends (the sync client may be paused, or a
    file may have changes still to upload). Ends at once when ``cancel`` is set.
    """
    interval = poll_s
    last_change = time.monotonic()
    while watch and not cancel.wait(interval):
        unloaded = unloaded_files(watch)
        now = time.monotonic()
        if unloaded:
            report(FreeUpEvent("unloaded", paths=unloaded))
            interval = poll_s
            last_change = now
        else:
            interval = min(poll_max_s, interval * 2)
            if now - last_change >= give_up_s:
                break
    return sum(len(names) for names in watch.values())


def free_up_space(path: str, report: FreeUpReport, cancel: threading.Event,
                  poll_s: float = POLL_S, poll_max_s: float = POLL_MAX_S,
                  give_up_s: float = GIVE_UP_S) -> None:
    """Explorer's "Free up space" for ``path``, followed by a watch on the result.

    Marks the file, or the folder and everything in it, to be kept online-only
    (see the module docstring; nothing is deleted or downloaded), then reports
    the files as the sync client unloads them: a "marked" event once the
    attributes are set, "unloaded" events with the paths concerned, and a
    "done" event when every marked file is online-only, ``cancel`` is set, or
    nothing has changed for ``give_up_s``. Meant to run on a worker thread.
    """
    watch, files, failed = mark_online_only(path, report, cancel)
    report(FreeUpEvent("marked", files, failed, sum(len(n) for n in watch.values())))
    remaining = watch_unloading(watch, report, cancel, poll_s, poll_max_s, give_up_s)
    report(FreeUpEvent("done", files, failed, remaining))
