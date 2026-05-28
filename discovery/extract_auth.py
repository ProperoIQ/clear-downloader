"""Pull request headers + cookies from specific endpoints in the HAR so we can
see the actual auth mechanism (cookies vs bearer vs custom headers).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Endpoints we care about for auth confirmation
TARGETS = [
    "/api/data-pull/public/pull/v2/trigger",
    "/api/data-pull/public/pull/v3/status",
    "/api/clear/data-browser/public/export/trigger",
    "/api/clear/data-browser/public/export/download/",
    "/storage/v1/ap-south-1/",  # the actual file download
    "/api/data-pull/public/getRecentTriggeredReports",
    "/api/gst-auto-compute/public/rls/fetch-token",
]


def header_get(headers, name):
    needle = name.lower()
    for h in headers:
        if h.get("name", "").lower() == needle:
            return h.get("value", "")
    return None


def main(har_path: Path) -> None:
    with har_path.open("r", encoding="utf-8") as f:
        har = json.load(f)

    entries = har["log"]["entries"]
    print(f"# Auth header analysis of {har_path.name}\n")

    for target in TARGETS:
        # Find first matching entry
        match = None
        for e in entries:
            url = e.get("request", {}).get("url", "")
            if target in url:
                match = e
                break
        if not match:
            print(f"## {target}")
            print("  (no matching request)\n")
            continue

        req = match["request"]
        resp = match["response"]
        print(f"## {target}")
        print(f"  URL: {req['url'][:160]}")
        print(f"  Method: {req['method']}")
        print(f"  Status: {resp['status']}")
        print("  Request headers:")
        for h in req.get("headers", []):
            name = h["name"]
            val = h["value"]
            lname = name.lower()
            if lname == "cookie":
                # Show only cookie *names*, never values
                cookies = sorted({c.split("=", 1)[0].strip()
                                  for c in val.split(";") if "=" in c})
                print(f"    Cookie names ({len(cookies)}): {cookies}")
                continue
            # Mask any obvious token values but show header name
            if lname in ("authorization", "x-csrf-token", "x-xsrf-token",
                         "x-auth-token"):
                preview = val[:24] + "..." if val and len(val) > 24 else val
                print(f"    {name}: <present, {len(val)} chars, preview: {preview!r}>")
                continue
            # Headers worth seeing in full
            if (lname.startswith("x-")
                    or lname in ("accept", "content-type", "origin", "referer",
                                 "host", "sec-fetch-mode", "sec-fetch-site")):
                v = val if len(val) < 100 else val[:97] + "..."
                print(f"    {name}: {v}")
        # Response headers worth checking (e.g. Set-Cookie)
        sc = header_get(resp.get("headers", []), "set-cookie")
        if sc:
            print(f"  Response Set-Cookie present: {sc[:80]}...")
        print()

    # Also: collect ALL unique cookie names across the whole HAR (against clear.in)
    all_cookies = set()
    for e in entries:
        req = e["request"]
        if "clear.in" not in req.get("url", ""):
            continue
        cookie_hdr = header_get(req.get("headers", []), "cookie")
        if not cookie_hdr:
            continue
        for c in cookie_hdr.split(";"):
            if "=" in c:
                all_cookies.add(c.split("=", 1)[0].strip())
    print("## All cookie names seen on app.clear.in requests")
    for c in sorted(all_cookies):
        print(f"  - {c}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1
         else Path("discovery/inwards-walkthrough.har"))
