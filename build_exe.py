"""Build a single-file Unhog.exe with PyInstaller.

Usage (from the repo root, with any Python 3.9+ that is not the Microsoft
Store build):

    python -m venv .venv-build
    .venv-build/Scripts/pip install -r requirements.txt pyinstaller
    .venv-build/Scripts/python build_exe.py

The result is dist/Unhog.exe. It is windowed (no console) and self-contained;
the UI font is read from C:/Windows/Fonts at run time, so nothing else ships.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--noconsole",
    "--name", "Unhog",
    "--paths", HERE,
    "--collect-binaries", "dearpygui",
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    "--exclude-module", "test",
    "--distpath", os.path.join(HERE, "dist"),
    "--workpath", os.path.join(HERE, "build"),
    "--specpath", os.path.join(HERE, "build"),
    os.path.join(HERE, "Unhog.pyw"),
]
print(" ".join(cmd))
sys.exit(subprocess.call(cmd, cwd=HERE))
