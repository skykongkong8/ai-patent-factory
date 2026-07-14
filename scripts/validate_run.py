#!/usr/bin/env python3
"""Run the stable path-bound validation CLI without guessing a run identifier."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--workspace-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = [sys.executable, "-m", "patent_factory", "validate", "--run", str(args.run)]
    if args.run_id:
        command.extend(("--run-id", args.run_id))
    if args.workspace_root:
        command.extend(("--workspace-root", str(args.workspace_root)))
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        command, cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
