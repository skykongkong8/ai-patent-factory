"""Make the src-layout package runnable from a clean repository clone."""
from __future__ import annotations
import sys
from pathlib import Path
SRC = str(Path(__file__).resolve().parent / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
