"""Unhog - a SpaceMonger-style treemap of OneDrive files that use local storage."""

import os
import subprocess


def _version() -> str:
    """Version string: from git in a source checkout, else the generated ``_version``
    module, else the installed package metadata, else "dev".

    In a source checkout the tag is read from git directly, so a tagged commit
    reports "0.1.0" and later commits something like "0.1.0-3-g1a2b3c4" (plus
    "-dirty" for uncommitted changes). Git takes precedence over a leftover
    ``_version.py`` so that a stale file from an earlier build cannot mislead.

    Outside a checkout the version comes from ``unhog/_version.py``, which
    setuptools-scm (``pip install``, ``python -m build``) and ``build_exe.py``
    write from the git tag and which ships in the wheel and the exe.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(repo, ".git")):
        try:
            out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=repo,
                                 capture_output=True, text=True, timeout=5, check=True).stdout.strip()
            if out:
                return out[1:] if out.startswith("v") else out
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        from ._version import version
        return version
    except ImportError:
        pass
    try:
        from importlib.metadata import version as metadata_version
        return metadata_version("unhog")
    except Exception:  # not installed as a package
        pass
    return "dev"


__version__ = _version()
