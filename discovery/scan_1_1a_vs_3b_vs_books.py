"""Targeted scanner for the GSTR-1+1A vs 3B vs Books HAR.

Extracts the 8 identifiers the implementation needs:
  1. URL slug (Referer reportType=...)
  2. Pull tenant (pull/v2/trigger body)
  3. RLS workflow (rls/fetch-token URL workFlow=...)
  4. Date format (fetch-token URL: periods vs date range)
  5. Preflight presence (count of export/trigger calls)
  6. Statement template id(s) (statement.from.id in export/trigger body)
  7. Filename pattern(s) (top-level filename in export/trigger body)
  8. Header overrides on export/trigger (x-ct-source, baggage, sentry-trace, etc.)

Also writes the captured POST bodies for both preflight and real export-trigger
calls to discovery/har_extract_1_1a_vs_3b_*.json for later template extraction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl


HAR = Path(
    "d:/office/gst-downloads/clear-downloader/discovery/"
    "app.clear.in.har__GSTR1+1A vs 3B vs Books Report_1.har"
)
OUT_DIR = HAR.parent


def header_get(headers: list[dict], name: str) -> str | None:
    needle = name.lower()
    for h in headers:
        if h.get("name", "").lower() == needle:
            return h.get("value")
    return None


def header_names(headers: list[dict]) -> list[str]:
    return [h.get("name", "") for h in headers]


def short(s: str | None, n: int = 100) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[:n] + "..."


def main() -> None:
    print(f"Loading: {HAR}")
    print(f"Size: {HAR.stat().st_size:,} bytes")
    with HAR.open("r", encoding="utf-8") as f:
        har = json.load(f)

    entries = har["log"]["entries"]
    print(f"Total entries: {len(entries)}\n")

    export_triggers: list[dict] = []
    rls_tokens: list[dict] = []
    pull_v2: list[dict] = []
    downloads: list[dict] = []

    for idx, entry in enumerate(entries):
        req = entry.get("request", {})
        resp = entry.get("response", {})
        url = req.get("url", "")
        method = req.get("method", "")

        if "/api/clear/data-browser/public/export/trigger" in url and method == "POST":
            export_triggers.append({"idx": idx, "entry": entry})
        elif "/rls/fetch-token" in url and method == "POST":
            rls_tokens.append({"idx": idx, "entry": entry})
        elif "/api/data-pull/public/pull/v2/trigger" in url and method == "POST":
            pull_v2.append({"idx": idx, "entry": entry})
        elif "/api/clear/data-browser/public/export/download" in url:
            downloads.append({"idx": idx, "entry": entry})

    print("=" * 72)
    print(f"Found {len(export_triggers)} export/trigger POST(s)")
    print(f"Found {len(rls_tokens)} rls/fetch-token POST(s)")
    print(f"Found {len(pull_v2)} pull/v2/trigger POST(s)")
    print(f"Found {len(downloads)} export/download GET(s)")
    print("=" * 72)
    print()

    # ---- Pull v2 trigger: extract tenant ----
    print("## pull/v2/trigger calls (PULL_TENANT source)")
    for h in pull_v2:
        e = h["entry"]
        req = e["request"]
        body_text = (req.get("postData") or {}).get("text", "")
        try:
            body = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError:
            body = {}
        print(f"  entry #{h['idx']}: url={short(req['url'], 130)}")
        print(f"    tenant = {body.get('tenant')!r}")
        print(f"    reportLevel = {body.get('reportLevel')!r}")
        print(f"    gisDownloadBehaviour = {body.get('gisDownloadBehaviour')!r}")
        print(f"    startReturnPeriod = {body.get('startReturnPeriod')!r}")
        print(f"    endReturnPeriod = {body.get('endReturnPeriod')!r}")
        print(f"    fromDate = {body.get('fromDate')!r}")
        print(f"    toDate = {body.get('toDate')!r}")
        print(f"    body keys = {sorted(body.keys()) if body else []}")
    print()

    # ---- RLS fetch-token: extract workflow + date format ----
    print("## rls/fetch-token calls (RLS_WORKFLOW + date-format source)")
    for h in rls_tokens:
        e = h["entry"]
        req = e["request"]
        url = req["url"]
        q = dict(parse_qsl(urlsplit(url).query))
        print(f"  entry #{h['idx']}: url={short(url, 130)}")
        print(f"    workFlow = {q.get('workFlow')!r}")
        print(f"    returnPeriods = {q.get('returnPeriods')!r}")
        print(f"    fromDate = {q.get('fromDate')!r}")
        print(f"    toDate = {q.get('toDate')!r}")
    print()

    # ---- Export trigger calls: dump bodies + extract slug/template/headers ----
    print("## export/trigger calls (preflight + real export)")
    for i, h in enumerate(export_triggers, 1):
        e = h["entry"]
        req = e["request"]
        body_text = (req.get("postData") or {}).get("text", "")
        try:
            body = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError:
            body = {}

        referer = header_get(req.get("headers", []), "referer") or ""
        ref_q = dict(parse_qsl(urlsplit(referer).query)) if referer else {}

        print(f"  [{i}] entry #{h['idx']}: url={short(req['url'], 130)}")
        print(f"      filename = {body.get('filename')!r}")
        stmt = body.get("statement") or {}
        from_ = stmt.get("from") or {}
        print(f"      statement.from.id = {from_.get('id')!r}")
        print(f"      statement.from.name = {from_.get('name')!r}")
        on_start_md = ((body.get("onStart") or {}).get("metadata") or {})
        print(f"      onStart.metadata.reportType = {on_start_md.get('reportType')!r}")
        print(f"      onStart.metadata.filename = {on_start_md.get('filename')!r}")
        print(f"      onStart.metadata.startRange = {on_start_md.get('startRange')!r}")
        print(f"      onStart.metadata.endRange = {on_start_md.get('endRange')!r}")
        print(f"      referer slug (reportType) = {ref_q.get('reportType')!r}")
        print(f"      referer timePeriodType = {ref_q.get('timePeriodType')!r}")
        print(f"      header names = {sorted(set(h_['name'].lower() for h_ in req.get('headers', [])))}")
        # Check key headers
        print(f"      x-ct-source = {header_get(req.get('headers', []), 'x-ct-source')!r}")
        print(f"      baggage present = {bool(header_get(req.get('headers', []), 'baggage'))}")
        print(f"      sentry-trace present = {bool(header_get(req.get('headers', []), 'sentry-trace'))}")
        print(f"      accept-language = {header_get(req.get('headers', []), 'accept-language')!r}")
        print(f"      priority = {header_get(req.get('headers', []), 'priority')!r}")
        print(f"      top-level body keys = {sorted(body.keys())}")

        # Save the captured body for template extraction later
        out_file = OUT_DIR / f"har_extract_1_1a_vs_3b_call_{i}.json"
        with out_file.open("w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, ensure_ascii=False)
        print(f"      -> saved body to {out_file.name}")

        # Also save full request headers for later header-override work
        hdr_file = OUT_DIR / f"har_extract_1_1a_vs_3b_call_{i}_headers.json"
        with hdr_file.open("w", encoding="utf-8") as f:
            json.dump(
                [{"name": h_["name"], "value": h_["value"]}
                 for h_ in req.get("headers", [])
                 if h_["name"].lower() != "cookie"],
                f, indent=2,
            )
        print(f"      -> saved headers to {hdr_file.name}")
        print()

    # ---- Download responses: detect the final XLSX so we know which export id is "real" ----
    print("## export/download calls (final downloads)")
    for h in downloads:
        e = h["entry"]
        req = e["request"]
        resp = e.get("response", {})
        ct = header_get(resp.get("headers", []), "content-type") or ""
        cd = header_get(resp.get("headers", []), "content-disposition") or ""
        size = resp.get("content", {}).get("size", 0)
        print(f"  entry #{h['idx']}: {req['method']} {short(req['url'], 130)}")
        print(f"    content-type = {ct!r}")
        print(f"    content-disposition = {cd!r}")
        print(f"    size = {size:,} bytes  status = {resp.get('status')}")
    print()


if __name__ == "__main__":
    main()
