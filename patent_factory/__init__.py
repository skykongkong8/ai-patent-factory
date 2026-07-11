"""Clean-clone shim; installed builds use the package under src/."""
from pathlib import Path

__path__.append(str(Path(__file__).resolve().parents[1] / "src" / "patent_factory"))
__version__ = "0.1.0"
