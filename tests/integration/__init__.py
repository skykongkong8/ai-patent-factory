"""Pin test temporary directories to a symlink-free path (macOS /var -> /private/var)."""
from __future__ import annotations

import os
import tempfile

tempfile.tempdir = os.path.realpath(tempfile.gettempdir())
