# Unhog

Reclaim space on your disk hogged by OneDrive, Dropbox, SharePoint offline files.

Unhog draws a SpaceMonger-style treemap of the files in a cloud-synced folder
that actually occupy local disk space. "Files On-Demand" keeps most files
online-only; Unhog shows only the hydrated ones, so you can see where the
local space goes and free it up.

## Requirements

- Windows, Python 3.9+
- `pip install -r requirements.txt` (only dependency: [Dear PyGui](https://github.com/hoffstadt/DearPyGui))

## Run

```
python -m unhog            # scans %OneDrive%
python -m unhog D:\Other   # scans another folder
```

or double-click `Unhog.pyw`.

## Using it

- **Hover** a rectangle for its full path, local size and file counts.
- **Double-click a folder** to show only that folder's contents. Double-click the
  background (or the current folder's title bar) to go back up. The breadcrumb
  row and the **Up** / **Home** buttons navigate too. **Back** (button, or in
  the right-click menu) returns to the previously shown view, step by step.
- **Right-click a folder** for *Open in Explorer*, *Show only this folder* and
  *Copy path*. Right-click a file for *Open containing folder in Explorer*.
- **Browse...** / the folder box + **Rescan** scan a different folder.
- **Modified** filters by last-modified time: "Older than …" (1 month to
  5 years) or "Newer than …" (1 week to 1 year). Only matching files count
  toward folder sizes and are drawn; folders act as containers for whatever
  inside them matches. A folder's own time is the newest file anywhere inside
  it (shown in the tooltip), so a folder whose newest file is older than the
  cutoff shows in full under "Older than", while a folder with recent changes
  shows just its old parts and is drawn gray to mark it as a container only.
  The tooltip also shows each file's modification date and the unfiltered
  sizes. Switching does not rescan.
- **Local files only** (checked by default) sizes the treemap by bytes on local
  disk and hides online-only placeholders. Uncheck it to see every file sized by
  its full logical size; online-only files are drawn dimmed. Switching does not
  rescan.
- **Min size** (default 1 MB) hides files and folders smaller than the limit as
  separate rectangles. Within each folder they are folded into one grey
  "N smaller items" tile that carries their combined size, so the parent's area
  stays accurate. Double-click that tile (or right-click it and choose *Show
  contents*) to view the folded items on their own; the limit shrinks with the
  view, so they appear individually. Right-click the tile to open its folder
  in Explorer. "Off" shows everything. The limit applies to the scanned root: when you drill into
  a folder it shrinks in proportion to that folder's share of the total, so you
  see the same level of detail at every depth. The effective limit for the
  current folder is shown next to the dropdown.

**Preferences...** opens a dialog with the appearance settings:

- **Scale fonts and padding by folder size** (on by default) grows folder
  titles (and file labels) with the item's share of the folder currently
  shown, from 10 px up to 34 px, so the biggest space hogs jump out. Title
  strips grow to match, and folder frames scale the same way: the folder shown
  gets the full padding, smaller folders get proportionally thinner frames.
- **Padding** (1 px to 32 px, default 12 px) sets how wide the frame is that
  each folder draws around its children.
- **Padding scaling** (Off to Extreme) sets how much thinner the frames of
  smaller folders get: the percentage is what the smallest folders keep of the
  chosen padding, from 100% (uniform) down to 5%. Default is Firm (20%).
  Only applies while scaling by size is on.

Files are colored by type (video, image, audio, document, archive, code, disk
image, other) and shaded darker the deeper they sit.

## How "local storage" is detected

A OneDrive placeholder that is online-only carries the Win32 attribute
`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`. Files without it are hydrated and use
local disk. The scanner reads attributes from the directory listing
(`os.scandir`) and never opens files, so scanning does not trigger downloads.

## Tests

```
python -m unittest discover -s tests
```
