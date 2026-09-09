"""A plausible, made-up OneDrive folder tree for demos and screenshots.

``demo_scan`` has the same signature as ``scanner.scan`` so the app can use
it in place of a real disk scan (``python -m unhog --demo``). Nothing on disk
is touched. The tree is deterministic: the same seed always gives the same
names, sizes and modification ages (ages are relative to the time the tree is
generated, so the "Modified" filter behaves the same whenever it is run).
The scan is replayed file by file over ``DEMO_SCAN_SECONDS`` so the live
treemap can be watched filling in; ``demo_disk_usage`` stands in for
``shutil.disk_usage`` so the free-space tile has something to show.
"""

from __future__ import annotations

import datetime
import random
import threading
import time
from collections import namedtuple
from typing import Callable, Optional, Sequence

from .scanner import Exclusions, Node, ProgressCallback, TreeBuilder

DEMO_ROOT = r"C:\Users\Sam\OneDrive"
DEMO_SCAN_SECONDS = 8.0  # how long the replayed demo scan takes

KB = 1024
MB = 1024 * KB
GB = 1024 * MB
DAY = 86400.0

PHOTO_EXTS = (".jpg",) * 8 + (".heic",) * 3 + (".png", ".dng")
DOC_KINDS = {
    ".docx": (40 * KB, 4 * MB), ".xlsx": (30 * KB, 12 * MB), ".pptx": (2 * MB, 90 * MB),
    ".pdf": (200 * KB, 25 * MB), ".txt": (2 * KB, 60 * KB), ".md": (2 * KB, 40 * KB),
}
CODE_EXTS = (".py", ".js", ".ts", ".json", ".html", ".css", ".yaml", ".sql")
CODE_STEMS = ("main", "utils", "model", "api", "index", "config", "test_scan", "layout", "app", "types")
ADJECTIVES = ("Annual", "Draft", "Final", "Revised", "Internal", "Q1", "Q2", "Q3", "Q4", "Client")
NOUNS = ("budget", "report", "plan", "proposal", "notes", "summary", "roadmap", "review",
         "checklist", "agenda", "forecast", "minutes", "overview", "analysis", "brief")
ARTISTS = ("Blue Harbor", "The Latecomers", "Nadia Vale", "Quiet Engines", "Sol y Sombra",
           "Iron Meadow", "Cassette Club", "Lumen")
ALBUM_WORDS = ("Live", "Sessions", "Vol. 2", "Nightfall", "Anthology", "Sketches", "Northbound")
FAMILY_VIDEOS = ("Christmas 2019", "Summer trip 2020", "Graduation", "Beach day", "Road trip 2022",
                 "Birthday party", "Skiing 2023", "Wedding highlights", "Garden timelapse",
                 "Halloween", "New year 2025", "Hiking Dolomites")

Namer = Callable[[int], str]


def _file(name: str, size: int, local: bool, mtime: float, pinned: bool = False) -> Node:
    return Node(name, "", False, size=size if local else 0, file_count=1 if local else 0,
                total_files=1, pinned=pinned, total_size=size, local=local, mtime=mtime)


def _dir(name: str, children: Sequence[Node]) -> Node:
    node = Node(name, "", True)
    node.children = list(children)
    return node


def _finalize(node: Node, path: str) -> None:
    """Fill in paths, parents and folder totals the way the real scanner does."""
    node.path = path
    if not node.is_dir:
        return
    kept: list[Node] = []
    for child in node.children:
        child.parent = node
        _finalize(child, path + "\\" + child.name)
        node.total_files += child.total_files
        node.total_size += child.total_size
        node.size += child.size
        node.file_count += child.file_count
        if child.total_size > 0:
            kept.append(child)
        else:
            child.parent = None
    kept.sort(key=lambda n: n.total_size, reverse=True)
    node.children = kept
    node.mtime = max((c.mtime for c in kept), default=0.0)
    node.local = node.size > 0
    node.pinned = bool(kept) and all(c.pinned for c in kept)


class _Demo:
    def __init__(self, seed: int, now: float):
        self.rng = random.Random(seed)
        self.now = now

    # -- primitives -----------------------------------------------------------

    def size(self, lo: int, hi: int) -> int:
        """Log-uniform size between ``lo`` and ``hi`` bytes."""
        return int(lo * (hi / lo) ** self.rng.random())

    def age(self, lo_days: float, hi_days: float) -> float:
        return self.now - self.rng.uniform(lo_days, hi_days) * DAY

    def local(self, share: float) -> bool:
        return self.rng.random() < share

    def mtime_in_year(self, year: int) -> float:
        """A random moment in calendar year ``year`` (not later than now)."""
        start = datetime.datetime(year, 1, 1).timestamp()
        end = min(datetime.datetime(year + 1, 1, 1).timestamp(), self.now)
        return self.rng.uniform(start, end) if end > start else min(start, self.now)

    def pick(self, options: Sequence[str]) -> str:
        return self.rng.choice(options)

    def files(self, count: int, name: Namer, lo: int, hi: int, local_share: float,
              lo_days: float = 0, hi_days: float = 0, pinned: bool = False,
              year: Optional[int] = None) -> list[Node]:
        """Modified ``lo_days``..``hi_days`` ago, or anywhere in calendar ``year`` if given."""
        def mtime() -> float:
            return self.mtime_in_year(year) if year is not None else self.age(lo_days, hi_days)
        return [_file(name(i), self.size(lo, hi), self.local(local_share) or pinned, mtime(), pinned)
                for i in range(count)]

    def doc_name(self, i: int) -> str:
        ext = self.pick(list(DOC_KINDS))
        suffix = self.pick(("", "", " v2", " v3", " (1)"))
        return f"{self.pick(ADJECTIVES)} {self.pick(NOUNS)}{suffix}{ext}"

    def docs(self, count: int, local_share: float, lo_days: float, hi_days: float,
             pinned: bool = False) -> list[Node]:
        out = []
        for i in range(count):
            name = self.doc_name(i)
            lo, hi = DOC_KINDS["." + name.rsplit(".", 1)[1]]
            out.append(_file(name, self.size(lo, hi), self.local(local_share) or pinned,
                             self.age(lo_days, hi_days), pinned))
        return out

    def stamp(self, year: int) -> str:
        r = self.rng
        return (f"{year}{r.randint(1, 12):02d}{r.randint(1, 28):02d}_"
                f"{r.randint(6, 23):02d}{r.randint(0, 59):02d}{r.randint(0, 59):02d}")

    def date(self, lo_year: int, hi_year: int) -> str:
        r = self.rng
        return f"{r.randint(lo_year, hi_year)}-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"

    # -- folders ----------------------------------------------------------------

    def camera_roll(self) -> Node:
        years = []
        # (year, photos, share hydrated). 2020 was opened in full once and never
        # freed: the classic hog. Recent years are always local.
        for year, count, share in ((2018, 260, 0.08), (2019, 340, 0.12), (2020, 410, 0.92),
                                   (2021, 300, 0.15), (2022, 380, 0.25), (2023, 450, 0.9),
                                   (2024, 520, 1.0), (2025, 310, 1.0)):
            photos = self.files(count, lambda i, y=year: f"IMG_{self.stamp(y)}{self.pick(PHOTO_EXTS)}",
                                int(1.8 * MB), 9 * MB, share, year=year)
            clips = self.files(count // 25 + 2, lambda i, y=year: f"VID_{self.stamp(y)}.mp4",
                               40 * MB, 350 * MB, share, year=year)
            years.append(_dir(str(year), photos + clips))
        return _dir("Camera Roll", years)

    def pictures(self) -> Node:
        screenshots = self.files(140, lambda i: f"Screenshot {self.rng.randint(1, 900)}.png",
                                 80 * KB, 3 * MB, 1.0, 1, 700)
        wallpapers = self.files(18, lambda i: f"wallpaper-{self.rng.randint(1000, 9999)}.jpg",
                                1 * MB, 14 * MB, 0.6, 400, 2200)
        raw = self.files(90, lambda i: f"DSC_{self.rng.randint(100, 9999):04d}.NEF",
                         18 * MB, 34 * MB, 0.85, 800, 1500)
        return _dir("Pictures", [self.camera_roll(), _dir("Screenshots", screenshots),
                                 _dir("Wallpapers", wallpapers), _dir("Nikon RAW 2022", raw)])

    def videos(self) -> Node:
        family = [_file(f"{n}.{self.pick(('mp4', 'mp4', 'mov', 'mkv'))}",
                        self.size(600 * MB, 4200 * MB), self.local(0.45), self.age(300, 2400))
                  for n in FAMILY_VIDEOS]
        recordings = self.files(9, lambda i: f"Recording {self.rng.randint(1, 60)} - Teams meeting.mp4",
                                120 * MB, 900 * MB, 1.0, 5, 200)
        drone = self.files(14, lambda i: f"DJI_{self.rng.randint(1, 999):04d}.MP4",
                           300 * MB, 2200 * MB, 0.3, 500, 1100)
        return _dir("Videos", [_dir("Family videos", family), _dir("Screen recordings", recordings),
                               _dir("Drone footage", drone)])

    def documents(self) -> Node:
        falcon = _dir("Project Falcon", self.docs(38, 1.0, 2, 240, pinned=True))
        reports = _dir("Reports", self.docs(70, 0.5, 100, 1900))
        contracts = _dir("Contracts", self.files(
            22, lambda i: f"Contract {self.rng.randint(2018, 2025)}-{self.rng.randint(1, 99):03d} signed.pdf",
            300 * KB, 9 * MB, 0.7, 200, 2600))
        decks = _dir("Presentations", self.files(
            16, lambda i: f"{self.pick(ADJECTIVES)} {self.pick(NOUNS)} deck.pptx",
            15 * MB, 240 * MB, 0.8, 20, 1400))
        work = _dir("Work", [falcon, reports, contracts, decks])

        tax_kinds = ("T4", "Return", "Receipt", "Assessment", "Donation")
        taxes = _dir("Taxes", [
            _dir(str(y), self.files(self.rng.randint(4, 12),
                                    lambda i, y=y: f"{self.pick(tax_kinds)} {y}{self.pick(('', ' (1)', ' scan'))}.pdf",
                                    90 * KB, 6 * MB, 0.3 if y < 2024 else 1.0, year=y + 1))
            for y in range(2018, 2026)])
        receipts = _dir("Receipts", self.files(
            260, lambda i: f"receipt_{self.date(2019, 2025).replace('-', '')}.pdf",
            40 * KB, 900 * KB, 0.9, 1, 2400))
        insurance = _dir("Insurance", self.docs(9, 0.5, 100, 1500))
        recipes = _dir("Recipes", self.docs(24, 0.8, 30, 2000))
        personal = _dir("Personal", [taxes, receipts, insurance, recipes])

        books = _dir("Books", self.files(
            34, lambda i: (f"{self.pick(('The', 'A', 'On', 'Beyond'))} {self.pick(NOUNS).title()} "
                           f"of {self.pick(ARTISTS)}.{self.pick(('epub', 'pdf', 'pdf'))}"),
            1 * MB, 70 * MB, 0.2, 600, 2800))
        scans = _dir("Scans", self.files(
            48, lambda i: f"Scan {self.date(2019, 2025)}.pdf", 500 * KB, 20 * MB, 0.6, 30, 2300))
        return _dir("Documents", [work, personal, books, scans])

    def code_tree(self, name: str, files: int, lo_days: float, hi_days: float,
                  local_share: float = 1.0) -> Node:
        def src(i: int) -> str:
            return f"{self.pick(CODE_STEMS)}{self.rng.randint(0, 40) or ''}{self.pick(CODE_EXTS)}"
        top = self.files(files, src, 1 * KB, 90 * KB, local_share, lo_days, hi_days)
        deps = self.files(files * 6, src, 300, 40 * KB, local_share, lo_days, hi_days)
        assets = self.files(files // 3 + 1, lambda i: f"asset-{self.rng.randint(1, 999)}.png",
                            10 * KB, 2 * MB, local_share, lo_days, hi_days)
        readme = _file("README.md", self.size(2 * KB, 30 * KB), True, self.age(lo_days, hi_days))
        return _dir(name, top + [_dir("node_modules", deps), _dir("assets", assets), readme])

    def projects(self) -> Node:
        data = _dir("data-analysis", [
            _file("measurements_2025.csv", self.size(400 * MB, 1400 * MB), True, self.age(3, 60)),
            _file("measurements_2024.csv", self.size(300 * MB, 900 * MB), True, self.age(300, 420)),
            _file("archive_2019-2023.parquet", self.size(1 * GB, 3 * GB), False, self.age(500, 900)),
            _file("analysis.ipynb", self.size(2 * MB, 40 * MB), True, self.age(1, 30)),
            _file("figures.pptx", self.size(20 * MB, 80 * MB), True, self.age(1, 30)),
            _dir("cache", self.files(60, lambda i: f"chunk_{i:03d}.npy", 4 * MB, 60 * MB, 1.0, 5, 90)),
        ])
        thesis = _dir("thesis", self.files(12, lambda i: f"chapter{i + 1}.docx", 1 * MB, 15 * MB, 0.4, 900, 1500)
                      + self.files(80, lambda i: f"fig_{i:02d}.png", 200 * KB, 12 * MB, 0.4, 900, 1500)
                      + [_file("thesis_final.pdf", 38 * MB, True, self.age(880, 900))])
        return _dir("Projects", [self.code_tree("website", 40, 5, 300), data, thesis,
                                 self.code_tree("old-intranet", 30, 1600, 2500, 0.15),
                                 _file("old-projects.zip", self.size(2 * GB, 4 * GB), True, self.age(1500, 1700))])

    def virtual_machines(self) -> Node:
        return _dir("Virtual Machines", [
            _file("Ubuntu 22.04 dev.vhdx", 11 * GB + 300 * MB, True, self.age(600, 700)),
            _file("Windows 11 eval.iso", 5 * GB + 200 * MB, True, self.age(400, 450)),
            _file("Windows 10 test.vhdx", 19 * GB, False, self.age(900, 1000)),
            _file("router-lab.vmdk", self.size(2 * GB, 4 * GB), True, self.age(30, 90)),
        ])

    def backups(self) -> Node:
        laptop = _dir("Old laptop 2021", [
            _file("Users.zip", self.size(4 * GB, 7 * GB), True, self.age(1500, 1600)),
            _file("Program data.zip", self.size(1 * GB, 3 * GB), False, self.age(1500, 1600)),
            _file("Desktop.zip", self.size(300 * MB, 1200 * MB), True, self.age(1500, 1600)),
        ])
        return _dir("Backups", [
            laptop,
            _file("Outlook archive 2019.pst", self.size(3 * GB, 5 * GB), True, self.age(2200, 2400)),
            _file("Outlook archive 2023.pst", self.size(2 * GB, 3 * GB), False, self.age(700, 800)),
            _file("phone backup 2022.zip", self.size(6 * GB, 9 * GB), False, self.age(1200, 1300)),
            _file("phone backup 2025.zip", self.size(8 * GB, 12 * GB), True, self.age(40, 90)),
        ])

    def music(self) -> Node:
        albums = []
        for artist in ARTISTS:
            for _ in range(self.rng.randint(1, 3)):
                fmt = self.pick((".mp3", ".mp3", ".flac"))
                lo, hi = (3 * MB, 12 * MB) if fmt == ".mp3" else (18 * MB, 60 * MB)
                share = self.rng.choice((0.0, 0.0, 0.5, 1.0))
                tracks = self.files(self.rng.randint(8, 15),
                                    lambda i, f=fmt: f"{i + 1:02d} - {self.pick(NOUNS).title()} {self.pick(ALBUM_WORDS)}{f}",
                                    lo, hi, share, 300, 3000)
                albums.append(_dir(f"{artist} - {self.pick(ALBUM_WORDS)}", tracks))
        return _dir("Music", albums)

    def desktop(self) -> Node:
        return _dir("Desktop", [
            _file("todo.txt", 3 * KB, True, self.age(0, 2)),
            _file("Meeting notes.docx", self.size(30 * KB, 200 * KB), True, self.age(1, 10)),
            _file("setup-tool-4.2.1.exe", self.size(80 * MB, 400 * MB), True, self.age(20, 120)),
            _file("dataset-sample.zip", self.size(200 * MB, 900 * MB), True, self.age(10, 60)),
            _file("Screenshot (3).png", self.size(200 * KB, 2 * MB), True, self.age(0, 5)),
            _file("invoice.pdf", self.size(80 * KB, 600 * KB), True, self.age(2, 40)),
        ])

    def notebooks(self) -> Node:
        names = ("Work", "Personal", "Ideas", "Travel", "Home", "Study", "Archive")
        return _dir("Notebooks", self.files(7, lambda i: f"{names[i]} notebook.one",
                                            5 * MB, 120 * MB, 0.7, 1, 900))

    def build(self, root: str) -> Node:
        name = root.rstrip("\\/").replace("/", "\\").rsplit("\\", 1)[-1] or root
        tree = _dir(name, [self.pictures(), self.videos(), self.documents(), self.projects(),
                           self.virtual_machines(), self.backups(), self.music(), self.desktop(),
                           self.notebooks()])
        _finalize(tree, root)
        return tree


def demo_tree(root: str = DEMO_ROOT, seed: int = 7, now: Optional[float] = None) -> Node:
    """Return the made-up folder tree rooted at ``root`` (no disk access)."""
    return _Demo(seed, time.time() if now is None else now).build(root)


class _Pacer:
    """Spreads ``count`` steps evenly over ``duration`` seconds.

    ``wait`` sleeps until the next step is due. Steps that are already late
    (a coarse sleep timer overshot) do not sleep, so the total stays close to
    ``duration`` regardless of timer resolution.
    """

    def __init__(self, count: int, duration: float):
        self.step = duration / max(1, count)
        self.due = time.monotonic()

    def wait(self) -> None:
        self.due += self.step
        delay = self.due - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def _replay(src: Node, dst: Node, builder: TreeBuilder, pacer: _Pacer) -> None:
    """Add ``src``'s contents to ``dst`` the way a disk scan would find them
    (honouring the builder's exclusions like ``scanner._scan_dir`` does)."""
    if builder.abandon_if_excluded(dst):
        return
    builder.enter_dir(dst, sum(1 for c in src.children if c.is_dir))
    for child in sorted(src.children, key=lambda n: n.name.lower()):  # directory-listing order
        if child.is_dir:
            if child.path in builder.exclusions.paths:
                builder.skip_dir()
                continue
            copy = builder.add_dir(dst, child.name, child.path)
            _replay(child, copy, builder, pacer)
            builder.finish_dir(copy)
        else:
            pacer.wait()
            builder.add_file(dst, child.name, child.path, child.total_size, child.local, child.mtime,
                             child.pinned)


def demo_scan(root: str = DEMO_ROOT, progress_cb: Optional[ProgressCallback] = None,
              cancel: Optional[threading.Event] = None, exclusions: Optional[Exclusions] = None,
              duration: float = DEMO_SCAN_SECONDS) -> Node:
    """Drop-in replacement for ``scanner.scan`` that "scans" ``demo_tree``.

    The files are added one by one, spread over about ``duration`` seconds,
    and ``progress_cb`` receives the growing tree just as with a real scan,
    so the live-updating treemap can be tried without a cloud folder.
    ``duration=0`` returns at once. ``root`` only names the tree; the contents
    are the same for any path.
    """
    final = demo_tree(root or DEMO_ROOT)
    builder = TreeBuilder(Node(final.name, final.path, True, local=False, scanned=False), progress_cb,
                          cancel, exclusions)
    _replay(final, builder.root, builder, _Pacer(final.total_files, duration))
    return builder.finish()


def demo_rescan(node: Node, progress_cb: Optional[ProgressCallback] = None,
                cancel: Optional[threading.Event] = None, exclusions: Optional[Exclusions] = None,
                duration: Optional[float] = None) -> Node:
    """Stand-in for ``scanner.scan_into``: replays the demo subtree at ``node.path`` into ``node``.

    ``node`` is an empty folder node hanging in a tree made by ``demo_scan``;
    its place in that tree says which part of ``demo_tree`` to replay. Takes
    the same share of ``DEMO_SCAN_SECONDS`` as the subtree's share of files,
    unless ``duration`` is given.
    """
    chain = node.ancestors()
    full = demo_tree(chain[0].path)
    src = full
    for step in chain[1:]:
        src = next((c for c in src.children if c.name == step.name), None)
        if src is None:
            break
    builder = TreeBuilder(node, progress_cb, cancel, exclusions)
    node.scanned = False
    if src is not None and src.is_dir:
        if duration is None:
            duration = DEMO_SCAN_SECONDS * src.total_files / max(1, full.total_files)
        _replay(src, node, builder, _Pacer(src.total_files, duration))
    else:
        builder.enter_dir(node, 0)
    return builder.finish()


DiskUsage = namedtuple("DiskUsage", "total used free")  # same fields as shutil.disk_usage
DEMO_DISK_TOTAL = 476 * GB   # a "512 GB" drive
DEMO_DISK_FREE = 41 * GB


def demo_disk_usage(path: str) -> DiskUsage:
    """Made-up drive figures for the demo, in place of ``shutil.disk_usage``."""
    return DiskUsage(DEMO_DISK_TOTAL, DEMO_DISK_TOTAL - DEMO_DISK_FREE, DEMO_DISK_FREE)
