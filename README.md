# Unhog

Reclaim space on your disk hogged by OneDrive, Dropbox, SharePoint offline files.

Unhog draws a SpaceMonger-style treemap of the files in a cloud-synced folder
that actually occupy local disk space. "Files On-Demand" keeps most files
online-only; Unhog shows only the hydrated ones, so you can see where the
local space goes and free it up.

![Unhog showing where the local space in a OneDrive folder goes](docs/images/home.png)

## Download

**[Download Unhog.exe (latest release)](https://github.com/lassoan/unhog/releases/latest/download/Unhog.exe)**

Requires Windows 10 or 11. No installation needed: it is a single file that
you can put anywhere and run. All releases, with release notes, are listed on
the [releases page](https://github.com/lassoan/unhog/releases).

When you run it the first time, Windows SmartScreen may show "Windows
protected your PC" because the file is not code-signed. Click **More info**,
then **Run anyway**.

## Run

Double-click `Unhog.exe`. It scans your OneDrive folder and shows the treemap.

To scan a different folder, use the **Browse...** button, or start it from a
command prompt with the folder as argument:

```
Unhog.exe D:\Other
```

## Freeing up space

Unhog only shows where the space goes; it does not delete or change anything.
To free the space a file or folder uses, right-click it in Unhog and choose
*Open in Explorer* (for a file: *Open containing folder in Explorer*), then in
Explorer right-click the item and choose **Free up space**. The file stays in
the cloud and is downloaded again when you open it.

## How to use

Hover a rectangle for details, double-click a folder to zoom in, right-click for
options. The toolbar filters by modification time, size and local storage.
All controls and settings are explained in [USAGE.md](USAGE.md).

## How it works

Unhog asks Windows which files in the folder are online-only placeholders and
which are actually present on disk. It reads only the directory listing and
never opens files, so scanning does not download anything.

It is tested with OneDrive, OneDrive for Business (SharePoint), and Dropbox
but it should be compatible with other cloud storage providers as well.

## Privacy

Unhog runs entirely on your PC, does not connect to the internet, and does not
send any information anywhere.

## For developers

Running from source, tests, building the exe and making releases are described
in [DEVELOPMENT.md](DEVELOPMENT.md).

## License

[MIT](LICENSE)
