"""Double-click launcher (no console window). Scans %OneDrive% by default (the home folder on Linux and macOS)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unhog.app import main  # noqa: E402

sys.exit(main())
