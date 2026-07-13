#!/usr/bin/env python3
"""Emit a redacted KIPRIS credential diagnostic without making a network call."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from patent_factory.privacy import credential_diagnostic


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-name", default="KIPRIS_PLUS_API_KEY")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--simulate-invalid", action="store_true")
    mode.add_argument("--fixture-usable", action="store_true")
    args = parser.parse_args()
    result = credential_diagnostic(
        args.check_name,
        simulated_invalid=args.simulate_invalid,
        fixture_usable=args.fixture_usable,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] in {"present", "fixture_usable"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
