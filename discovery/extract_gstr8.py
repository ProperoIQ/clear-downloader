"""Extract GSTR-8 specific call chain from a Chrome-exported HAR file."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl


def header_get(headers, name):
    needle = name.lower()
    for h in headers:
        if h.get("name", "").lower() == needle:
            return h.get("value")
    return None


def is_gstr8_related(entry):
    """Heuristic: is this entry part of the GSTR-8 flow?"""
    req = entry.get("request", {})
    url = req.get("url", "")
    post = (req.get("postData") or {}).get("text", "") or ""
    resp_text = ((entry.get("response", {}).get("content") or {}).get("text") or "")
    full = url + " " + post + " " + resp_text[:5000]
    tokens = (
        "GSTR8", "GSTR_8", "gstr8", "gstr_8",
        "panGstr8", "panGSTR8", "PAN_MM8",
        "GOVT_GSTR8_DOCS", "RESTRICT_OLD_GSTR8_DATA",
    )
    return any(t in full for t in tokens)


def summarize_entry(entry, idx):
    req = entry.get("request", {})
    resp = entry.get("response", {})
    url = req.get("url", "")
    method = req.get("method", "?")
    post_text = (req.get("postData") or {}).get("text", "") or ""
    resp_text = ((resp.get("content") or {}).get("text") or "")
    sp = urlsplit(url)
    return {
        "idx": idx,
        "method": method,
        "url": url,
        "host": sp.netloc,
        "path": sp.path,
        "query": dict(parse_qsl(sp.query)),
        "request_headers": [
            {"name": h["name"], "value": h["value"]}
            for h in req.get("headers", [])
            if h["name"].lower() not in ("cookie",)
        ],
        "request_body": post_text,
        "response_status": resp.get("status"),
        "response_content_type": header_get(resp.get("headers", []), "content-type"),
        "response_body": resp_text,
    }


def main(har_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with har_path.open("r", encoding="utf-8") as f:
        har = json.load(f)

    entries = har["log"]["entries"]
    print(f"Total entries in HAR: {len(entries)}")

    matches = []
    for i, entry in enumerate(entries):
        if is_gstr8_related(entry):
            matches.append((i, entry))

    print(f"GSTR-8-related entries (rough): {len(matches)}")

    # Group by interesting endpoint
    buckets = {
        "01-pull-trigger": [],
        "02-pull-status": [],
        "03-rls-fetch-token": [],
        "04-export-trigger": [],
        "05-export-download": [],
        "06-file-download": [],
        "07-other": [],
    }

    for i, entry in matches:
        url = entry["request"]["url"]
        method = entry["request"]["method"]
        if "/api/data-pull/public/pull/v2/trigger" in url and method == "POST":
            buckets["01-pull-trigger"].append((i, entry))
        elif "/api/data-pull/public/pull/v3/status" in url and method == "POST":
            buckets["02-pull-status"].append((i, entry))
        elif "/api/gst-auto-compute/public/rls/fetch-token" in url and method == "POST":
            buckets["03-rls-fetch-token"].append((i, entry))
        elif "/api/clear/data-browser/public/export/trigger" in url and method == "POST":
            buckets["04-export-trigger"].append((i, entry))
        elif "/api/clear/data-browser/public/export/download/" in url and method == "GET":
            buckets["05-export-download"].append((i, entry))
        else:
            buckets["07-other"].append((i, entry))

    # Look for file downloads tied to GSTR-8 — search by amazon/s3 nearby export-download responses
    # Also include all amazonaws responses regardless (so we can correlate)
    print()
    for name, lst in buckets.items():
        print(f"  {name}: {len(lst)} hit(s)")
        for i, entry in lst[:3]:
            url = entry["request"]["url"]
            if len(url) > 160:
                url = url[:160] + "..."
            print(f"     [{i}] {entry['request']['method']} {url}")

    # Write each bucket to a file
    for name, lst in buckets.items():
        if not lst:
            continue
        out_file = out_dir / f"gstr8_{name}.json"
        data = [summarize_entry(e, i) for (i, e) in lst]
        with out_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  wrote {out_file}")

    # Now look for the *file download* — typically presigned S3 right after export-download success.
    # Find any amazonaws.com URLs whose path/query contains GSTR-8-y tokens, or that occur RIGHT AFTER
    # an export-download success for GSTR-8.
    print("\n--- Searching for related S3/cloudfront file downloads ---")
    s3_hits = []
    for i, entry in enumerate(entries):
        url = entry["request"]["url"]
        if any(s in url for s in ("amazonaws.com", "cloudfront.net")):
            method = entry["request"]["method"]
            if method != "GET":
                continue
            # Filter to ones whose URL looks export-y
            if any(s in url.lower() for s in ("export", "gstr", "mm8", "panmm8", "complete_report")):
                s3_hits.append((i, entry))

    print(f"S3/cloudfront candidates: {len(s3_hits)}")
    for i, entry in s3_hits[:20]:
        url = entry["request"]["url"]
        print(f"  [{i}] {url[:200]}")

    if s3_hits:
        out_file = out_dir / "gstr8_06-file-download-candidates.json"
        data = [summarize_entry(e, i) for (i, e) in s3_hits]
        # Strip response_body for binary downloads
        for d in data:
            ct = (d.get("response_content_type") or "")
            if not ct.startswith(("application/json", "text/")):
                d["response_body"] = f"<binary, content-type={ct}>"
        with out_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  wrote {out_file}")


if __name__ == "__main__":
    har = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("discovery/app.clear.in.har")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("discovery/gstr8-extracted")
    main(har, out)
