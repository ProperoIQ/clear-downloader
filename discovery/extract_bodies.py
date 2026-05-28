"""Dump the full request/response bodies for the endpoints we'll be implementing,
into discovery/reference-bodies/ as separate files. These become the source of
truth for payload shapes when writing api.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


TARGETS = {
    # (path-substring, method): output-file-basename
    ("/api/data-pull/public/pull/prefetchStatus", "POST"): "01-pull-prefetchStatus",
    ("/api/data-pull/public/pull/v2/trigger", "POST"):     "02-pull-v2-trigger",
    ("/api/data-pull/public/pull/v3/status", "POST"):      "03-pull-v3-status",
    ("/api/gst-auto-compute/public/rls/fetch-token", "POST"): "04-rls-fetch-token",
    ("/api/clear/data-browser/public/export/trigger", "POST"): "05-export-trigger",
    ("/api/clear/data-browser/public/export/download/", "GET"): "06-export-download",
    ("/api/data-pull/public/getRecentTriggeredReports", "GET"): "07-recent-reports",
    ("/api/enterprise-orchestrator/public/business-hierarchy/v1/user_gstins", "GET"): "08-user-gstins",
}


def main(har_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with har_path.open("r", encoding="utf-8") as f:
        har = json.load(f)

    entries = har["log"]["entries"]
    used = {}

    for entry in entries:
        req = entry["request"]
        resp = entry["response"]
        url = req["url"]
        method = req["method"]

        for (path_sub, want_method), base in TARGETS.items():
            if want_method != method:
                continue
            if path_sub not in url:
                continue
            # Only capture the first matching occurrence per target
            if base in used:
                continue
            used[base] = True

            block = {
                "method": method,
                "url": url,
                "request_headers": [
                    {"name": h["name"], "value": h["value"]}
                    for h in req.get("headers", [])
                    if h["name"].lower() not in ("cookie",)  # never persist cookies
                ],
                "request_body": (req.get("postData") or {}).get("text", ""),
                "response_status": resp["status"],
                "response_content_type": next(
                    (h["value"] for h in resp.get("headers", [])
                     if h["name"].lower() == "content-type"), None),
                "response_body": resp.get("content", {}).get("text", ""),
            }

            # Try to pretty-print JSON bodies so they're diff-friendly
            for key in ("request_body", "response_body"):
                val = block[key]
                if not val:
                    continue
                try:
                    parsed = json.loads(val)
                    block[key + "_parsed"] = parsed
                except (json.JSONDecodeError, TypeError):
                    pass

            outfile = out_dir / f"{base}.json"
            with outfile.open("w", encoding="utf-8") as f:
                json.dump(block, f, indent=2, ensure_ascii=False)
            size = outfile.stat().st_size
            print(f"  wrote {outfile.name}  ({size:>7} bytes)")

    # Report misses
    print()
    misses = [base for (_, _), base in TARGETS.items() if base not in used]
    if misses:
        print("MISSING (target not found in HAR):")
        for m in misses:
            print(f"  - {m}")


if __name__ == "__main__":
    har = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("discovery/inwards-walkthrough.har")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("discovery/reference-bodies")
    main(har, out)
