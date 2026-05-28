"""Read state/partial-items.csv + state/manifest.sqlite and categorize:

  A. Clear-side issues (GSTN session expired / OTP needed / partial data)
  B. Network-side issues (DNS, connection reset, timeout to Clear's host)
  C. Other failures (anything else we should look at manually)

Run from project root:
    python analyze_failures.py
"""
from __future__ import annotations

import csv
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path


PROJECT = Path(__file__).parent
PARTIALS_CSV = PROJECT / "state" / "partial-items.csv"
MANIFEST = PROJECT / "state" / "manifest.sqlite"


# Regex hints for classifying error_message strings on failed rows.
NETWORK_PATTERNS = (
    r"NameResolutionError", r"getaddrinfo failed", r"WinError 11001",
    r"ConnectionError", r"Max retries exceeded",
    r"Connection (?:aborted|reset|refused)",
    r"ConnectTimeoutError", r"ConnectTimeout",
    r"ReadTimeoutError", r"ReadTimeout(?!.*Pull did not settle)",
    r"ProtocolError", r"SSLError", r"NewConnectionError",
)
TIMEOUT_PATTERNS = (
    r"Pull did not settle within",
    r"Export .* did not complete within",
)


def classify(error_message: str | None) -> str:
    em = error_message or ""
    for p in NETWORK_PATTERNS:
        if re.search(p, em, re.I):
            return "network"
    for p in TIMEOUT_PATTERNS:
        if re.search(p, em):
            return "clear-slow"
    if "Pull settled with issues" in em:
        # These were already captured into partial-items.csv at the same time
        return "clear-pull-issues"
    if "DOWNLOADED_PARTIALLY" in em or "NOT_DOWNLOADED" in em:
        return "clear-pull-issues"
    if "session expired" in em.lower():
        return "session-expired"
    if not em.strip():
        return "unknown"
    return "other"


def main() -> None:
    # --- partial-items.csv ---
    if not PARTIALS_CSV.exists():
        print(f"(no {PARTIALS_CSV} yet)")
        partials_by_status: Counter = Counter()
        partial_combos: set[tuple] = set()
    else:
        with PARTIALS_CSV.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        # De-dup: a combo logged multiple times across re-runs counts once
        # for "needs OTP" vs "still partial" purposes, but show both.
        partials_by_status = Counter(r["status"] for r in rows)
        partial_combos = {(r["pan"], r["fy"], r["gstin"], r["status"]) for r in rows}
    print(f"=== partial-items.csv: {PARTIALS_CSV.name} ===")
    if not partials_by_status:
        print("  (empty)")
    else:
        print(f"  Total rows (incl. duplicates from re-runs): "
              f"{sum(partials_by_status.values())}")
        print(f"  Distinct (pan, fy, gstin, status) entries: {len(partial_combos)}")
        for st, n in partials_by_status.most_common():
            print(f"    {st:<24} {n}")

    # --- manifest failed rows ---
    if not MANIFEST.exists():
        print(f"\n(no {MANIFEST} found)")
        return

    cx = sqlite3.connect(MANIFEST)
    cx.row_factory = sqlite3.Row
    failed = cx.execute("""
        SELECT pan, fy, report_type, error_message, completed_at
        FROM downloads WHERE status='failed'
        ORDER BY completed_at
    """).fetchall()
    by_category = defaultdict(list)
    for r in failed:
        by_category[classify(r["error_message"])].append(dict(r))

    print(f"\n=== manifest.sqlite — failed rows: {len(failed)} total ===")
    for cat, items in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  [{cat}]  {len(items)} combo(s)")
        for item in items[:8]:
            em = (item["error_message"] or "").replace("\n", " ")
            if len(em) > 110:
                em = em[:110] + "..."
            print(f"    {item['pan']:<12} {item['fy']:<9} {em}")
        if len(items) > 8:
            print(f"    ... and {len(items) - 8} more")

    # Bottom-line summary the user asked for
    network_n = len(by_category.get("network", []))
    clear_n = (len(by_category.get("clear-pull-issues", []))
               + len(by_category.get("clear-slow", []))
               + len(by_category.get("session-expired", [])))
    other_n = (len(by_category.get("other", []))
               + len(by_category.get("unknown", [])))

    print("\n" + "=" * 60)
    print("BOTTOM LINE")
    print("=" * 60)
    print(f"  Clear-side issues (need OTP / partial / slow):  {clear_n} combo(s) "
          f"(see also partial-items.csv distinct entries: {len(partial_combos)})")
    print(f"  Network-side issues (DNS / connection blips):   {network_n} combo(s)")
    if other_n:
        print(f"  Other / unknown (eyeball these):                {other_n} combo(s)")
    if not (clear_n or network_n or other_n):
        print("  No failed combos. Manifest is clean.")


if __name__ == "__main__":
    main()
