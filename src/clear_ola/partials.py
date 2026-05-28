"""Append a row per partially-downloaded GSTIN to `state/partial-items.csv`,
so the user can take that list to the concerned person (GST team / Clear
support / state filer) and confirm whether the missing data should exist.

The CSV is append-only across runs — easy to forward as-is, easy to filter in
Excel or any tool. We do NOT log NOT_APPLICABLE rows here: those are Clear's
confirmed "no data" answer, not uncertain missing data."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


CSV_HEADERS = [
    "logged_at",
    "pan",
    "business_name",
    "fy",
    "report_type",
    "gstin",
    "state_name",
    "status",
    "download_percentage",
    "periods_in_scope",
    "clear_updated_at",
    "pull_request_id",
]


def log_partial_items(
    csv_path: Path,
    *,
    pan: str,
    business_name: str,
    fy: str,
    report_type: str,
    snapshot: list[dict],
    pull_request_id: str | None = None,
    statuses: Iterable[str] = ("DOWNLOADED_PARTIALLY",),
) -> int:
    """Append one row per matching snapshot entry to `csv_path`. Creates the
    file (with header) if it doesn't exist. Returns the number of rows added.

    Args:
        csv_path: where to append. Parent dir will be created.
        pan, business_name, fy, report_type: context for the rows.
        snapshot: list of `statusResponses` items from pull/v3/status.
        pull_request_id: optional, for traceability back to the failed pull.
        statuses: which downloadStatus values to record. Defaults to
            DOWNLOADED_PARTIALLY only.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    keep = set(statuses)
    rows = [s for s in snapshot if s.get("downloadStatus") in keep]
    if not rows:
        return 0
    is_new = not csv_path.exists()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if is_new:
            w.writeheader()
        for s in rows:
            w.writerow({
                "logged_at": now,
                "pan": pan,
                "business_name": business_name,
                "fy": fy,
                "report_type": report_type,
                "gstin": s.get("nodeValue", ""),
                "state_name": s.get("nodeName", ""),
                "status": s.get("downloadStatus", ""),
                "download_percentage": s.get("downloadPercentage", ""),
                "periods_in_scope": ",".join(s.get("returnPeriods", []) or []),
                "clear_updated_at": s.get("updatedAt", ""),
                "pull_request_id": pull_request_id or "",
            })
    return len(rows)


# ---------- OTP worklist (for client coordination) ----------

WORKLIST_CSV_HEADERS = [
    "pan", "business_name", "fy",
    "gstins_count", "gstins", "state_names",
    "last_seen_at", "clear_navigation",
]


def build_otp_worklist(
    partials_csv: Path,
    worklist_csv: Path,
    worklist_txt: Path | None = None,
) -> dict:
    """Read `partials_csv` (the append-only NOT_DOWNLOADED / PARTIALLY log),
    extract NOT_DOWNLOADED rows only (those need OTP reconnect; PARTIALLY is
    a data-gap issue that OTP won't fix), dedup so each (pan, fy, gstin)
    appears once, group by (pan, fy), and write the result to `worklist_csv`.

    If `worklist_txt` is provided, also writes a plain-text version that's
    easy to paste into chat/email when coordinating with the client.

    Returns: {"total_combos": int, "total_gstins": int}.
    """
    if not partials_csv.exists():
        return {"total_combos": 0, "total_gstins": 0}

    # Dedup: same (pan, fy, gstin) may appear from multiple re-runs. Keep the
    # latest entry (rows are appended; later wins per (pan, fy, gstin)).
    rows_by_key: dict[tuple[str, str, str], dict] = {}
    with partials_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() != "NOT_DOWNLOADED":
                continue
            key = (row["pan"], row["fy"], row["gstin"])
            rows_by_key[key] = row

    # Group by (pan, fy)
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows_by_key.values():
        grouped[(row["pan"], row["fy"])].append(row)

    worklist_csv.parent.mkdir(parents=True, exist_ok=True)
    with worklist_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WORKLIST_CSV_HEADERS)
        w.writeheader()
        for (pan, fy), items in sorted(grouped.items()):
            biz = items[0].get("business_name", "")
            gstins = sorted({r["gstin"] for r in items})
            states = sorted({r["state_name"] for r in items if r.get("state_name")})
            last_seen = max(r.get("logged_at", "") for r in items)
            w.writerow({
                "pan": pan,
                "business_name": biz,
                "fy": fy,
                "gstins_count": len(gstins),
                "gstins": ", ".join(gstins),
                "state_names": ", ".join(states),
                "last_seen_at": last_seen,
                "clear_navigation": (
                    f"ClearGST -> GST -> All Reports -> PAN GSTR-2A -> "
                    f"select PAN '{pan}' + FY '{fy}' -> click "
                    f"'Generate OTP to connect GSTINs'"
                ),
            })

    if worklist_txt is not None:
        worklist_txt.parent.mkdir(parents=True, exist_ok=True)
        with worklist_txt.open("w", encoding="utf-8") as f:
            f.write("OTP Reconnect Worklist for ClearGST\n")
            f.write(
                f"Generated: "
                f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\n\n"
            )
            f.write(
                f"Total: {len(grouped)} (PAN x FY) report page(s) need OTP "
                f"reconnects covering {sum(len(v) for v in grouped.values())} "
                f"GSTIN(s).\n"
            )
            f.write(
                "\nFor each entry below: open ClearGST, navigate as instructed, "
                "click 'Generate OTP to connect GSTINs', enter the OTP for "
                "each listed state.\n\n"
            )
            f.write("=" * 72 + "\n\n")
            for (pan, fy), items in sorted(grouped.items()):
                biz = items[0].get("business_name", "")
                states = sorted({r.get("state_name") or "?" for r in items})
                gstins = sorted({r["gstin"] for r in items})
                f.write(f"PAN:   {pan}    ({biz})\n")
                f.write(f"FY:    {fy}\n")
                f.write(f"GSTINs needing OTP ({len(gstins)}):\n")
                # Show state + GSTIN pairs side-by-side for clarity
                for r in sorted(items, key=lambda x: x.get("state_name") or ""):
                    sn = r.get("state_name") or "?"
                    g = r["gstin"]
                    f.write(f"    - {sn:<20}  {g}\n")
                f.write(
                    f"\n  Navigate: ClearGST -> GST -> All Reports -> "
                    f"PAN GSTR-2A\n"
                    f"            -> select PAN '{pan}' + FY '{fy}'\n"
                    f"            -> click 'Generate OTP to connect GSTINs'\n"
                )
                f.write("\n" + "-" * 72 + "\n\n")

    return {
        "total_combos": len(grouped),
        "total_gstins": sum(len(v) for v in grouped.values()),
    }

