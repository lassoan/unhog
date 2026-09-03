"""Unhog - a SpaceMonger-style treemap of OneDrive files that use local storage."""

import os
import subprocess


def _version() -> str:
    """Version string: from the generated ``_version`` module (built exe), else git, else "dev".

    ``build_exe.py`` writes ``unhog/_version.py`` from the git tag before
    packaging. When running from a source checkout the tag is read from git
    directly, so a tagged commit reports "0.1.0" and later commits something
    like "0.1.0-3-g1a2b3c4" (plus "-dirty" for uncommitted changes).
    """
    try:
        from ._version import version
        return version
    except ImportError:
        pass
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(repo, ".git")):
        try:
            out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=repo,
                                 capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            if out:
                return out[1:] if out.startswith("v") else out
        except (OSError, subprocess.SubprocessError):
            pass
    return "dev"


__version__ = _version()
