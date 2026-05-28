"""Parse a Chrome-exported HAR file and emit a compact, reviewable summary of the
network activity. Intended for reverse-engineering the ClearGST portal.

Usage:
    python discovery/parse_har.py discovery/inwards-walkthrough.har

Writes:
    - stdout : compact human-readable summary
    - discovery/har-summary.json : structured digest for downstream tooling

Design goals:
    1. Never print the full HAR. Show only what's needed to reason about endpoints.
    2. Group requests by (METHOD, host, path-template) where path-template replaces
       UUIDs / long ids with placeholders so we see the *route* shape.
    3. Flag downloads, polls, and likely-async-job endpoints.
    4. Show auth-relevant headers (Authorization, Cookie names, x-* headers) on a
       per-route basis so we know what we'd need to replay.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl


# ---------- URL / path normalization ----------

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
HEX24_RE = re.compile(r"\b[0-9a-f]{24}\b", re.I)      # Mongo-style object ids
HEX32_RE = re.compile(r"\b[0-9a-f]{32,}\b", re.I)
NUMID_RE = re.compile(r"/\d{4,}(?=/|$)")
B64_LONG_RE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")

STATIC_EXTENSIONS = {
    ".js", ".css", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".map",
}

DOWNLOAD_CONTENT_TYPES = (
    "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel",
    "application/octet-stream",
    "application/zip",
    "application/x-zip-compressed",
    "application/pdf",
    "text/csv",
)


def normalize_path(path: str) -> str:
    """Replace UUIDs / long hex ids / large numeric ids in a URL path so requests
    that differ only by an id collapse to the same route template.
    """
    p = UUID_RE.sub("{uuid}", path)
    p = HEX24_RE.sub("{hex24}", p)
    p = HEX32_RE.sub("{hex}", p)
    p = NUMID_RE.sub("/{id}", p)
    return p


def is_static_asset(path: str) -> bool:
    lower = path.lower().split("?", 1)[0]
    for ext in STATIC_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


def header_get(headers: list[dict], name: str) -> str | None:
    needle = name.lower()
    for h in headers:
        if h.get("name", "").lower() == needle:
            return h.get("value")
    return None


def header_names(headers: list[dict]) -> list[str]:
    return [h.get("name", "") for h in headers]


def is_download_response(response: dict) -> bool:
    cd = header_get(response.get("headers", []), "content-disposition") or ""
    if "attachment" in cd.lower():
        return True
    ct = header_get(response.get("headers", []), "content-type") or ""
    return any(ct.startswith(p) for p in DOWNLOAD_CONTENT_TYPES)


def body_preview(content: dict | None, limit: int = 400) -> str:
    if not content:
        return ""
    text = content.get("text") or ""
    if not text:
        return ""
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text


# ---------- HAR walk ----------


def main(har_path: Path) -> None:
    with har_path.open("r", encoding="utf-8") as f:
        har = json.load(f)

    entries = har.get("log", {}).get("entries", [])
    print(f"# HAR analysis: {har_path.name}")
    print(f"Total entries: {len(entries)}\n")

    # Group entries by (method, host, normalized_path)
    routes: dict[tuple, list[dict]] = defaultdict(list)
    hosts = Counter()

    for entry in entries:
        req = entry.get("request", {})
        resp = entry.get("response", {})
        method = req.get("method", "?")
        url = req.get("url", "")
        if not url:
            continue
        sp = urlsplit(url)
        host = sp.netloc
        path = sp.path
        hosts[host] += 1
        if is_static_asset(path):
            continue
        # Also skip Google/analytics/3rd-party
        if any(tag in host for tag in ("google", "googletag", "doubleclick",
                                      "gstatic", "segment.io", "amplitude",
                                      "sentry", "datadoghq", "intercom",
                                      "hotjar", "newrelic", "facebook",
                                      "stripe", "fonts.")):
            continue
        norm = normalize_path(path)
        routes[(method, host, norm)].append(entry)

    print("## Hosts (request counts incl. statics)")
    for host, n in hosts.most_common():
        print(f"  {n:>5}  {host}")
    print()

    # ----- Cookie/Auth overview from one in-app request -----
    print("## Auth signals on a representative app.clear.in API call")
    sample_for_auth = None
    for (method, host, norm), es in routes.items():
        if "clear.in" in host and norm.startswith("/api"):
            sample_for_auth = es[0]
            break
    if not sample_for_auth:
        for (method, host, norm), es in routes.items():
            if "clear.in" in host and method != "OPTIONS":
                sample_for_auth = es[0]
                break

    if sample_for_auth:
        req = sample_for_auth.get("request", {})
        url = req.get("url", "")
        print(f"  Sample URL: {url[:140]}")
        print("  Request headers present:")
        for h in req.get("headers", []):
            name = h.get("name", "")
            val = h.get("value", "")
            lname = name.lower()
            if lname in ("cookie",):
                # Don't print cookie values — just names
                cookie_names = sorted({c.split("=", 1)[0].strip()
                                       for c in val.split(";") if "=" in c})
                print(f"    Cookie names: {cookie_names}")
            elif (lname.startswith("x-")
                  or lname in ("authorization", "content-type", "accept",
                               "referer", "origin")):
                v = val if len(val) < 80 else val[:77] + "..."
                print(f"    {name}: {v}")
    print()

    # ----- Route summary table -----
    print("## Route summary (clear.in only, non-static)")
    print(f"{'#':>3}  {'M':<6}  {'host':<26}  count  download  template")

    interesting = [
        (k, v) for k, v in routes.items()
        if "clear.in" in k[1] and k[0] != "OPTIONS"
    ]
    # Sort: downloads first, then by hit count
    def sort_key(item):
        (method, host, norm), entries_ = item
        has_dl = any(is_download_response(e.get("response", {})) for e in entries_)
        return (not has_dl, -len(entries_), norm)
    interesting.sort(key=sort_key)

    for i, ((method, host, norm), es) in enumerate(interesting, 1):
        has_dl = any(is_download_response(e.get("response", {})) for e in es)
        print(f"{i:>3}  {method:<6}  {host:<26}  {len(es):>5}  "
              f"{'YES' if has_dl else '   ':<8}  {norm}")
    print()

    # ----- Download endpoints, in detail -----
    print("## Detailed: any response classified as a download")
    dl_idx = 0
    for (method, host, norm), es in interesting:
        for e in es:
            resp = e.get("response", {})
            if not is_download_response(resp):
                continue
            dl_idx += 1
            req = e.get("request", {})
            url = req.get("url", "")
            cd = header_get(resp.get("headers", []), "content-disposition")
            ct = header_get(resp.get("headers", []), "content-type")
            size = resp.get("content", {}).get("size", 0)
            print(f"\n  [{dl_idx}] {method} {url}")
            print(f"      content-type: {ct}")
            print(f"      content-disposition: {cd}")
            print(f"      bytes: {size}")
            print(f"      status: {resp.get('status')}")
            # Don't print body — it's binary
    if dl_idx == 0:
        print("  (none)")
    print()

    # ----- Polls: same endpoint hit many times in succession -----
    print("## Likely polls (>= 3 hits on same route)")
    poll_routes = [
        ((m, h, n), es) for (m, h, n), es in interesting if len(es) >= 3
    ]
    if not poll_routes:
        print("  (none with >= 3 hits)")
    for (method, host, norm), es in poll_routes:
        print(f"\n  {method} {host}{norm}  ({len(es)} hits)")
        # show distinct response statuses + 1-line body sample from last hit
        statuses = Counter(e.get("response", {}).get("status") for e in es)
        print(f"    status counts: {dict(statuses)}")
        last = es[-1]
        rb = body_preview(last.get("response", {}).get("content"), 250)
        if rb:
            print(f"    last response (truncated): {rb}")
    print()

    # ----- Per-route deep dive: first 1 sample of each, with bodies -----
    print("## Per-route sample (first occurrence, request + response previews)")
    for i, ((method, host, norm), es) in enumerate(interesting, 1):
        e = es[0]
        req = e.get("request", {})
        resp = e.get("response", {})
        url = req.get("url", "")
        if len(url) > 200:
            url = url[:200] + "..."
        ct_req = header_get(req.get("headers", []), "content-type") or ""
        ct_resp = header_get(resp.get("headers", []), "content-type") or ""
        status = resp.get("status")
        size = resp.get("content", {}).get("size", 0)
        print(f"\n  [{i}] {method} {norm}")
        print(f"      URL: {url}")
        print(f"      query params: {dict(parse_qsl(urlsplit(req.get('url','')).query))}")
        print(f"      request content-type: {ct_req}")
        post = req.get("postData") or {}
        if post.get("text"):
            print(f"      request body (truncated): {body_preview(post, 300)}")
        print(f"      response: {status} {ct_resp} ({size} bytes)")
        if not is_download_response(resp) and ct_resp.startswith(("application/json", "text/")):
            print(f"      response body (truncated): {body_preview(resp.get('content'), 400)}")

    # ----- Write structured digest -----
    digest = {
        "har_file": str(har_path),
        "total_entries": len(entries),
        "hosts": dict(hosts),
        "routes": [],
    }
    for (method, host, norm), es in interesting:
        digest["routes"].append({
            "method": method,
            "host": host,
            "path_template": norm,
            "hits": len(es),
            "has_download": any(is_download_response(e.get("response", {})) for e in es),
            "sample_url": es[0].get("request", {}).get("url", ""),
            "sample_status": es[0].get("response", {}).get("status"),
            "sample_response_ct": header_get(
                es[0].get("response", {}).get("headers", []), "content-type"),
        })
    out = har_path.parent / "har-summary.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump(digest, f, indent=2)
    print(f"\nWrote structured digest to: {out}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    main(Path(sys.argv[1]))
