# Developing Unhog

User documentation is in [README.md](README.md) and [USAGE.md](USAGE.md). This page covers running from
source, testing, building the standalone exe and publishing releases.

## Requirements

- Windows, Python 3.9+
- `pip install -r requirements.txt` (only dependency: [Dear PyGui](https://github.com/hoffstadt/DearPyGui))

## Run from source

```
python -m unhog            # scans %OneDrive%
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

## Layout

- `unhog/scanner.py` walks the folder tree and builds the size model.
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

- `Unhog-win64.zip` (about 11 MB): PyInstaller's one-folder build, a folder
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

- `build_exe.py` runs `git describe --tags` (or uses the `UNHOG_VERSION`
  environment variable if set) and writes the result to `unhog/_version.py`,
  which is packaged into the exe and ignored by git.
- Running from a source checkout, `unhog/__init__.py` asks git directly, so a
  tagged commit reports `0.1.0` and later commits something like
  `0.1.0-3-g1a2b3c4`, with `-dirty` appended for uncommitted changes.
- Without git or a checkout the version is `dev`.

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
1 year" filter, and "Local files only" unchecked) and writes one PNG per
scene using Dear PyGui's frame-buffer capture. It must run on Windows with
Segoe UI installed so the text matches what users see. The window is set to
a fixed size, so the images have the same dimensions every time. Re-render
after changing anything visible (colors, fonts, layout rules, toolbar) and
commit the PNGs together with the change. The dates in tooltips shift with the
time of rendering, which is expected.

To add a scene, add a block to `ScreenshotApp.shoot()`: navigate with
`set_view`, `set_modified` or `set_local_only`, call `hover` on the node to
show a tooltip for, then `save` with the file name. Reference the new image
from `USAGE.md` (or `README.md` for the overview image). To change what the demo tree contains, edit `unhog/demo.py`
and run the tests, which check that the tree is internally consistent.

## Making a release

Pushing a tag that starts with `v` runs the GitHub Actions workflow in
`.github/workflows/release.yml`, which builds the exe on a Windows runner, runs
the tests, and attaches `Unhog-win64.zip` and `Unhog.exe` to a GitHub release
for that tag:

```
git tag v0.1.0
git push origin v0.1.0
```

If a release for the tag already exists (e.g. one created by hand on GitHub),
the exe is added to it. The tag name is the version, so nothing in the source
needs to be changed before tagging.

The download link in the README points at the latest release's `Unhog-win64.zip`
asset, so it updates automatically whenever a new release is published.
