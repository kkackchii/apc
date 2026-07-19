"""Base directory resolution — works both running from source and as a frozen
PyInstaller exe. When frozen, __file__-based paths resolve inside the bundled
_internal folder, not next to the .exe, so apc.db/data/ would end up hidden
there instead of alongside the exe where users expect to find/manage them.
"""
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent.parent
