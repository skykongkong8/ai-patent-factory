#!/usr/bin/env python3
"""Report SerpApi credential presence, and optionally the free remaining-search quota.

Offline mode (default) never touches the network: it reports only presence via the
same redacted diagnostic used for KIPRIS. The optional --live flag queries the free
SerpApi account endpoint (account.json does not consume a monthly search) and reports
the remaining count. The credential value is never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from patent_factory.adapters.google_patents import serpapi_account  # noqa: E402
from patent_factory.privacy import credential_diagnostic, environment_secret  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-name", default="SERPAPI_API_KEY")
    parser.add_argument(
        "--live", action="store_true",
        help="query the free account endpoint for remaining searches (no search is spent)",
    )
    args = parser.parse_args()

    result = credential_diagnostic(args.check_name)
    if not args.live:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if result["status"] == "present" else 1

    api_key = environment_secret(args.check_name)
    if not api_key:
        result["quota_status"] = "unavailable_missing_credential"
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1
    try:
        account = serpapi_account(api_key)
    except (ValueError, OSError):
        # Never surface a message that might embed the URL/credential.
        result["quota_status"] = "query_failed"
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1
    result.update({
        "quota_status": "ok",
        "total_searches_left": account["total_searches_left"],
        "plan_searches_left": account["plan_searches_left"],
        "this_month_usage": account["this_month_usage"],
        "searches_per_month": account["searches_per_month"],
        "plan_renewal_date": account["plan_renewal_date"],
    })
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    left = account["total_searches_left"]
    return 0 if left is not None and left > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
