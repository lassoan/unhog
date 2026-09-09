# Developing Unhog

User documentation is in [README.md](README.md) and [USAGE.md](USAGE.md). This page covers running from
source, testing, building the standalone exe and publishing releases.

## Requirements

- Windows, Python 3.9+
- `pip install -r requirements.txt` (only dependency: [Dear PyGui](https://github.com/hoffstadt/DearPyGui))

Alternatively `pip install -e .` installs the checkout as an editable package,
which also puts an `unhog` command on the path (the same GUI entry point the
PyPI package provides).

## Run from source

```
python -m unhog            # scans %OneDrive% (the home folder on Linux and macOS)
python -m unhog D:\Other   # scans another folder
```

or double-click `Unhog.pyw`.

## Demo mode

```
python -m unhog --demo
```

shows a made-up but plausible OneDrive folder tree instead of scanning the
disk (the built exe accepts `--demo` too). Use it to try the UI without a
cloud-synced folder or to reproduce a layout issue with data that can be
shared. The tree comes from `unhog/demo.py`: a fixed random seed generates the
same names and sizes every time, and modification times are relative to the
current time, so the **Modified** filter behaves the same whenever it is run.
Nothing is read from or written to disk; *Open in Explorer* on a demo item
therefore opens nothing useful.

The demo "scan" is not instant: `demo_scan` replays the generated tree file by
file through the same `TreeBuilder` the real scanner uses, spread over about
8 seconds (`DEMO_SCAN_SECONDS`), so the live-updating treemap can be watched
filling in. Callers that need the tree at once pass `duration=0`, as the tests
and the screenshot tool do. `demo_disk_usage` supplies made-up drive figures
for the free-space tile.

## Layout

- `unhog/scanner.py` walks the folder tree and builds the size model. Its
  `TreeBuilder` adds each folder's files to the totals of all folders above
  as soon as the folder is listed and reports the growing tree to a progress
  callback, so the app can draw the treemap while the scan is still running.
- `unhog/treemap.py` lays out the squarified treemap and does hit testing.
- `unhog/app.py` is the Dear PyGui user interface.
- `unhog/win_dialogs.py` wraps the native Windows folder picker via ctypes.
- `unhog/demo.py` generates the made-up folder tree for demo mode and screenshots.
- `tools/make_screenshots.py` renders the README screenshots from that tree.
- `tests/` holds unit tests for the scanner, the treemap layout and the demo tree.

## How "local storage" is detected

A OneDrive placeholder that is online-only carries the Win32 attribute
`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`. Files without it are hydrated and use
local disk. The scanner reads attributes from the directory listing
(`os.scandir`) and never opens files, so scanning does not trigger downloads.

This attribute only exists on Windows. On other platforms every file currently
counts as local, so the app degrades to a plain disk-usage treemap.

The scan loop in `_scan_dir` runs once per entry of the scanned tree, so it
is kept lean: on Windows the entry's kind (folder, file, symlink or junction)
is read from the attributes the directory listing already supplied, with no
per-entry method calls or extra system calls, and a folder's files are added
to the tree in one batch (`TreeBuilder.add_files`), so the folders above are
updated once per folder rather than once per file. Roughly two thirds of a
warm-cache scan's time is now the directory listing itself.

## Drawing while scanning

The scan runs in a worker thread and grows the tree in place; the UI thread
lays out and draws that same tree while the scan runs, at most every
`LIVE_REDRAW_S` seconds and, after a slow redraw (very large trees), no sooner
than `LIVE_REDRAW_BUDGET` times the redraw's own duration. No lock is taken:
attribute updates are atomic under the GIL, and `TreeBuilder` adds a folder's
files to the totals of the folders above before appending them to the
folder's children, so a folder never shows less than its visible contents. A
redraw may see a folder whose children do not yet add up to its total; the
next redraw corrects it.

The two threads do not run in parallel: both are Python code, so the GIL
serializes them, and every millisecond the UI thread spends laying out or
drawing is a millisecond the scan stands still (more threads would not help;
only a separate process would, at the cost of shipping the tree across). The
redraw limits above therefore double as a cap on how much the live view slows
the scan: with a redraw taking `t`, redraws take at most
`t / max(LIVE_REDRAW_S, LIVE_REDRAW_BUDGET * t)` of the time, under a tenth
either way. The layout weighs every child of every visible folder on each
redraw, so `redraw` uses a C-level attribute getter as the weight when no
filter is active, and `make_aggregate` sums the folded-away small items in a
single pass; for a 650,000-file tree a redraw takes about 60 ms. The drive figures behind the optional
free-space tile and the whole-drive progress bar come from `shutil.disk_usage`,
fetched by the same worker before the scan starts.

*Rescan* in the right-click menu scans one folder again in place:
`scanner.detach` takes the old subtree's numbers out of the folders above and
unlinks it, an empty node is attached where it was, and `scanner.scan_into`
fills that node, propagating totals upwards as usual, so the treemap shows the
folder filling in. `scanner.refresh_upwards` then restores child order, newest
file and pinned state up the chain. The demo uses `demo_rescan`, which replays
the matching part of the demo tree.

Dear PyGui normally runs widget callbacks on its own thread. The app switches
that off (`manual_callback_management`) and runs the queued callbacks itself
at the start of every frame, so button, combo and mouse handlers execute on
the UI thread and can never rebuild the drawlist while a live redraw is in
progress. Mouse moves only set a flag; the hover tooltip is redrawn once per
frame.

## Display scaling

`win_dialogs.ui_scale` declares the process DPI-aware (otherwise Windows
renders it at 96 DPI and stretches the bitmap, which blurs all text) and
returns the display scale. `app.py` calls it at import time, before the
viewport exists, and puts every pixel size through `px()`, so fonts, widget
widths and treemap paddings keep their physical size at 100%, 150% or 200%
scaling. Setting `UNHOG_SCALE` (for example `1.25`) overrides the detected
scale; the screenshot tool sets it to `1` so the images do not depend on the
machine that renders them.

## Progress estimate

`TreeBuilder.progress` judges how far a scan has got from folders alone, since
nothing is known about a folder's size before it is scanned. `enter_dir`
records how many subfolders a folder has and `finish_dir` counts them off; the
estimate is the share of the root's subfolders finished, plus the current
one's share times the same estimate one level down, and so on. It never goes
backwards and reaches 1 when the root is finished. When a whole drive is
scanned the app ignores it and shows bytes found against the drive's used
bytes instead, which is exact.

## Tests

```
python -m unittest discover -s tests
```

## Building the release files

The exe needs no Python on the target machine. Use a regular python.org
install (not the Microsoft Store one) to build it:

```
python -m venv .venv-build
.venv-build\Scripts\pip install -r requirements.txt pyinstaller
.venv-build\Scripts\python build_exe.py
```

This produces two files in `dist\`:

- `Unhog.zip` (about 11 MB): PyInstaller's one-folder build, a folder
  `Unhog` with a small `Unhog.exe` and its runtime in `_internal`. This is
  the download the README points to. It runs in place, which keeps Windows
  Defender's heuristics much quieter than the single-file build.
- `Unhog.exe` (about 12 MB): the single-file build. It unpacks itself to a
  temp folder on every start, which Defender's machine-learning detection
  has flagged as a false positive in the past.

Both are windowed (no console) and take an optional folder argument like
`python -m unhog` does. The zip's name has no version in it so that the
README's link to the latest release keeps working; the version is in the
window title and on the release page.

## Version number

There is no version number in the source. The version shown in the window
title comes from git:

- Running from a source checkout, `unhog/__init__.py` asks git directly, so a
  tagged commit reports `0.1.0` and later commits something like
  `0.1.0-3-g1a2b3c4`, with `-dirty` appended for uncommitted changes. Git is
  consulted first so that a leftover `unhog/_version.py` from an earlier
  build cannot show a stale version.
- `build_exe.py` runs `git describe --tags` (or uses the `UNHOG_VERSION`
  environment variable if set) and writes the result to `unhog/_version.py`,
  which is packaged into the exe and ignored by git.
- The PyPI package gets its version from
  [setuptools-scm](https://setuptools-scm.readthedocs.io/), configured in
  `pyproject.toml`, which reads the same tag (`v0.4.1` gives `0.4.1`; an
  untagged commit gives a PEP 440 version like `0.4.2.dev3+g1a2b3c4`) and
  writes the same `unhog/_version.py` into the wheel. When running from an
  installed package that file supplies the version; failing that, the
  package metadata does.
- Without any of these the version is `dev`.

The release workflow passes the tag name as `UNHOG_VERSION`, so a release
built from tag `v0.2.0` shows "Unhog 0.2.0".

## Updating the screenshots

The images in `docs/images` are rendered from the demo tree, so they never
show anyone's real files, and they can be regenerated after any change to the
look of the app:

```
python tools/make_screenshots.py
```

This opens the app window briefly, drives it through the scenes listed in
`tools/make_screenshots.py` (home view, a folder zoomed in, the "Older than
1 year" filter, "Local files only" unchecked, and "Show free space" checked) and writes one PNG per
scene using Dear PyGui's frame-buffer capture. It must run on Windows with
Segoe UI installed so the text matches what users see. The window is set to
a fixed size, so the images have the same dimensions every time. Re-render
after changing anything visible (colors, fonts, layout rules, toolbar) and
commit the PNGs together with the change. The dates in tooltips shift with the
time of rendering, which is expected.

To add a scene, add a block to `ScreenshotApp.shoot()`: navigate with
`set_view`, `set_modified`, `set_local_only` or `set_show_free`, call `hover` on the node to
show a tooltip for, then `save` with the file name. Reference the new image
from `USAGE.md` (or `README.md` for the overview image). To change what the demo tree contains, edit `unhog/demo.py`
and run the tests, which check that the tree is internally consistent.

## Making a release

Pushing a tag that starts with `v` runs the GitHub Actions workflow in
`.github/workflows/release.yml`, which builds the exe on a Windows runner, runs
the tests, and attaches `Unhog.zip` and `Unhog.exe` to a GitHub release
for that tag:

```
git tag v0.1.0
git push origin v0.1.0
```

If a release for the tag already exists (e.g. one created by hand on GitHub),
the exe is added to it. The tag name is the version, so nothing in the source
needs to be changed before tagging.

The download link in the README points at the latest release's `Unhog.zip`
asset, so it updates automatically whenever a new release is published.

### PyPI

The same workflow has a second job, `pypi`, which runs after the exe build
and tests succeed on a tag. It builds the sdist and wheel with
`python -m build`, checks them with `twine check`, and uploads them to
[PyPI](https://pypi.org/project/unhog/) with
[trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API
token is stored in the repository. This needs a one-time setup:

1. On PyPI, log in and open *Your account*, *Publishing*. Under *Add a new
   pending publisher* choose GitHub and enter: PyPI project name `unhog`,
   owner `lassoan`, repository name `unhog`, workflow name `release.yml`,
   environment name `pypi`. (Once the project exists, the same form is under
   the project's *Manage*, *Publishing* page.)
2. On GitHub, in the repository's *Settings*, *Environments*, create an
   environment named `pypi`. Optionally restrict it to tags matching `v*` or
   require a reviewer; that is where a manual approval step for PyPI uploads
   would go.

After that, every `v*` tag publishes to PyPI. A version can only be uploaded
once; to fix a broken release, tag a new patch version rather than re-tagging.

To build and check the package locally (the files land in `dist/`):

```
pip install build twine
python -m build
python -m twine check dist/*
```

To try the upload path without touching the real index, `python -m twine
upload --repository testpypi dist/*` publishes to
[TestPyPI](https://test.pypi.org/), which needs a separate account and API
token there.
