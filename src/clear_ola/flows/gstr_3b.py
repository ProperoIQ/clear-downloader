"""GSTR-3B Reports — 5 downloadable variants from the same data pull.

Unlike GSTR-2A/2B/1 (which use `data-browser/export/trigger`), GSTR-3B uses
Clear's `/api/gst-reports/reports/v1.0/...` backend: no RLS token, no
statement JSON template, just a 4-field POST body per variant. The 5
variants (Filed / ITC Offset / Insights / PDF / Combined) all share a single
data-pull job per (PAN, FY); only the per-variant `reportDownload` call and
its status polls differ. See discovery/app.clear.in.har lines 48698 (pull
trigger) and 104758 (Combined download trigger) for the verbatim captures
this module was modelled on.

Manifest layout: each variant is its own row keyed by
`(pan, fy, report_type)` where `report_type` is e.g. `"GSTR-3B-Combined"`.
This way a failed variant retries independently of the others on re-run.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date
from pathlib import Path

from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired
from clear_ola.config import (
    AppConfig,
    PanConfig,
    fy_human,
    fy_periods,
)
from clear_ola.manifest import Manifest
from clear_ola.partials import log_partial_items


REPORT_TYPE_PREFIX = "GSTR-3B"
JOB_TYPE = "PAN_MM3B_REPORT"
MIN_FY = "2017-18"   # GST regime started Jul 2017

# Per-variant config. Only `combined` is HAR-verified — the other 4 sheet_type
# strings are extrapolations from Clear's naming convention. If a sheet_type
# is wrong Clear's reportDownload responds 4xx with an explicit message; the
# fix is a one-line edit here per variant.
VARIANTS: dict[str, dict[str, str]] = {
    "combined": {
        "report_type": "GSTR-3B-Combined",
        "sheet_type": "GSTR_3B_COMBINED_REPORT",  # ✓ HAR-verified
        "output_type": "EXCEL",
        "ext": "xlsx",
        "label": "Combined",
    },
    "filed": {
        "report_type": "GSTR-3B-Filed",
        "sheet_type": "GSTR_3B_FILED_REPORT",     # ⚠ extrapolated
        "output_type": "EXCEL",
        "ext": "xlsx",
        "label": "Filed",
    },
    "itc-offset": {
        "report_type": "GSTR-3B-ITC-Offset",
        "sheet_type": "GSTR_3B_ITC_OFFSET_REPORT", # ⚠ extrapolated
        "output_type": "EXCEL",
        "ext": "xlsx",
        "label": "ITC-Offset",
    },
    "insights": {
        "report_type": "GSTR-3B-Insights",
        "sheet_type": "GSTR_3B_INSIGHTS_REPORT",   # ⚠ extrapolated
        "output_type": "EXCEL",
        "ext": "xlsx",
        "label": "Insights",
    },
    "pdf": {
        "report_type": "GSTR-3B-PDF",
        # PDF likely uses the FILED template internally but with a PDF
        # outputType; if Clear's reportDownload 4xxs with a different
        # sheet_type, swap to the value the error suggests.
        "sheet_type": "GSTR_3B_FILED_REPORT",      # ⚠ extrapolated
        "output_type": "PDF",
        "ext": "pdf",
        "label": "PDF",
    },
}
DEFAULT_VARIANTS = list(VARIANTS.keys())


# --- helpers reused from the 2A/2B/1 flows (identical logic) -----------------

# What the per-GSTIN downloadStatus values mean for this backend:
#   COMPLETED            — Clear has fresh 3B data for that GSTIN. Good.
#   NOT_APPLICABLE       — GSTIN didn't yet exist in this FY. Good (no-op).
#   FAILED               — Clear's stored session for that GSTIN has expired
#                          (analog of 2A/2B/1's NOT_DOWNLOADED). User must
#                          open ClearGST -> this PAN+FY's report page ->
#                          'Generate OTP to connect GSTINs' and re-auth.
#   DOWNLOADED_PARTIALLY — partial data; may need a retry (UI's "Download
#                          all data again" button). Same as 2A/2B/1.
#   NOT_DOWNLOADED       — not observed for 3B in HAR but included as a
#                          defensive synonym for FAILED.
_NEEDS_USER_ACTION = ("DOWNLOADED_PARTIALLY", "NOT_DOWNLOADED", "FAILED")


def _any_partial(snapshot: list[dict]) -> bool:
    return any(s.get("downloadStatus") == "DOWNLOADED_PARTIALLY" for s in snapshot)


def _summarize_issues(snapshot: list[dict]) -> str:
    return ", ".join(
        f"{s.get('nodeName', '?')} ({s.get('downloadStatus')})"
        for s in snapshot
        if s.get("downloadStatus") in _NEEDS_USER_ACTION
    )


def _index_gstins_by_pan(api: ClearAPI) -> dict[str, list]:
    nodes = api.user_gstins()
    by_pan: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_pan[n.pan].append(n)
    return dict(by_pan)


# --- variant parsing ---------------------------------------------------------

def parse_variants_filter(raw: str | None) -> list[str]:
    """Turn the `--variants combined,filed` CLI string into a validated list.

    None / empty → all 5. Unknown keys raise ValueError with the bad key
    listed so the CLI can surface a friendly message.
    """
    if not raw:
        return list(DEFAULT_VARIANTS)
    keys = [k.strip().lower() for k in raw.split(",") if k.strip()]
    if not keys:
        return list(DEFAULT_VARIANTS)
    bad = [k for k in keys if k not in VARIANTS]
    if bad:
        raise ValueError(
            f"Unknown GSTR-3B variant(s): {bad}. "
            f"Valid keys: {list(VARIANTS.keys())}"
        )
    # Preserve user order but dedupe.
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# --- entry point -------------------------------------------------------------

def run(
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
    *,
    variants_filter: str | None = None,
) -> None:
    """Process every (PAN × FY) in the config for GSTR-3B.

    A single data pull powers all selected variants per (PAN, FY); each
    variant becomes its own manifest row so failures retry independently.

    GSTINs that settle in NOT_DOWNLOADED or DOWNLOADED_PARTIALLY are logged to
    `state/partial-items.csv` and the export proceeds with whatever data is
    available — the same way NOT_APPLICABLE GSTINs are handled.
    """
    selected_variants = parse_variants_filter(variants_filter)
    logger.info("GSTR-3B variants selected: {}", selected_variants)

    logger.info("Indexing GSTINs from workspace...")
    by_pan = _index_gstins_by_pan(api)
    logger.info("Found {} PAN(s), {} GSTIN(s) total",
                len(by_pan), sum(len(v) for v in by_pan.values()))

    for pan_cfg in cfg.pans:
        gstins = by_pan.get(pan_cfg.pan, [])
        if not gstins:
            logger.error("No GSTINs found for PAN {} ({}). Skipping all its FYs.",
                         pan_cfg.pan, pan_cfg.business_name)
            for fy in pan_cfg.fys:
                for v in selected_variants:
                    rt = VARIANTS[v]["report_type"]
                    manifest.mark_started(pan_cfg.pan, fy, rt)
                    manifest.mark_failed(
                        pan_cfg.pan, fy, rt,
                        error=f"No GSTINs returned by user_gstins for PAN {pan_cfg.pan}",
                    )
            continue
        logger.info(
            "PAN {} ({}) has {} state-wise GSTIN(s) registered.",
            pan_cfg.pan, pan_cfg.business_name, len(gstins),
        )

        for fy in pan_cfg.fys:
            _run_one(
                api=api, cfg=cfg, manifest=manifest,
                pan_cfg=pan_cfg, gstins=gstins, fy=fy,
                selected_variants=selected_variants,
            )


def _run_one(
    *,
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
    pan_cfg: PanConfig,
    gstins: list,
    fy: str,
    selected_variants: list[str],
) -> None:
    pan = pan_cfg.pan

    # MIN_FY guard — analogous to gstr_2b.py / gstr_1.py. GST regime started
    # Jul 2017; any FY earlier than 2017-18 is recorded as no_data for all
    # selected variants and skipped.
    if fy < MIN_FY:
        for v in selected_variants:
            rt = VARIANTS[v]["report_type"]
            if manifest.is_done(pan, fy, rt):
                continue
            logger.info(
                "[{} / {} / {}] FY predates GST regime; recording no_data.",
                pan, fy, rt,
            )
            manifest.mark_started(pan, fy, rt)
            manifest.mark_no_data(pan, fy, rt, gstins_seen=0)
        return

    # Skip if every selected variant for this (PAN, FY) is already done.
    todo = [v for v in selected_variants
            if not manifest.is_done(pan, fy, VARIANTS[v]["report_type"])]
    if not todo:
        logger.info(
            "[{}/{}] all selected GSTR-3B variants already done — skipping",
            pan, fy,
        )
        return

    logger.info("=" * 70)
    logger.info("[{}/{}/{}] starting {} variant(s): {}",
                pan, fy, REPORT_TYPE_PREFIX, len(todo),
                [VARIANTS[v]["label"] for v in todo])
    for v in todo:
        manifest.mark_started(pan, fy, VARIANTS[v]["report_type"])

    try:
        gstin_node_ids = [g.gstin_node_id for g in gstins]
        pan_node_id = gstins[0].pan_node_id
        today = date.today()
        periods = fy_periods(fy, as_of=today)
        if len(periods) < 12:
            logger.info(
                "[{}/{}] Today is {} — FY isn't complete yet. "
                "Requesting {} period(s): {}..{}",
                pan, fy, today.isoformat(),
                len(periods), periods[0], periods[-1],
            )

        # ===== Step 1: shared data pull (once for all variants) =====
        logger.info(
            "[{}/{}] Step 1/5: triggering 3B data pull "
            "(shared across {} variant(s), {} GSTIN(s))",
            pan, fy, len(todo), len(gstin_node_ids),
        )
        data_pull_job_id = api.trigger_3b_data_pull(
            pan_node_id=pan_node_id,
            gstin_node_ids=gstin_node_ids,
            return_periods=periods,
            workspace_id=cfg.workspace_id,
        )
        for v in todo:
            manifest.set_pull_id(pan, fy, VARIANTS[v]["report_type"],
                                 data_pull_job_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # ===== Step 2: wait for data pull =====
        logger.info("[{}/{}] Step 2/5: waiting for 3B data pull", pan, fy)
        snapshot = api.wait_for_3b_data_pull(
            data_pull_job_id,
            gstin_node_ids=gstin_node_ids,
            workspace_id=cfg.workspace_id,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )

        # 2a. Whole-PAN no-data: every GSTIN reported NOT_APPLICABLE.
        not_applicable = sum(1 for s in snapshot
                             if s.get("downloadStatus") == "NOT_APPLICABLE")
        if (snapshot
                and not_applicable == len(snapshot)
                and not _any_partial(snapshot)):
            logger.info(
                "[{}/{}] No data for this PAN x FY: all {} GSTIN(s) returned "
                "NOT_APPLICABLE. Marking all selected variants as no_data.",
                pan, fy, not_applicable,
            )
            for v in todo:
                manifest.mark_no_data(pan, fy, VARIANTS[v]["report_type"],
                                      gstins_seen=not_applicable)
            return

        # 2b. Per-GSTIN issues (DOWNLOADED_PARTIALLY / NOT_DOWNLOADED). The 3B
        # backend doesn't expose a "force re-pull" knob in the captured HAR,
        # so we just log them and proceed (same as 2a for NOT_APPLICABLE).
        # If a future HAR reveals an equivalent of 2A/2B/1's
        # gisDownloadBehaviour=DOWNLOAD_COMPLETE_DATA retry, add it here.
        issue_rows = [s for s in snapshot
                      if s.get("downloadStatus") in _NEEDS_USER_ACTION]
        if issue_rows:
            partials_csv = cfg.state_dir / "partial-items.csv"
            n_logged = log_partial_items(
                partials_csv,
                pan=pan,
                business_name=pan_cfg.business_name,
                fy=fy,
                # Log against the prefix so the OTP worklist groups all 5
                # 3B variants together — they share the same underlying pull.
                report_type=REPORT_TYPE_PREFIX,
                snapshot=snapshot,
                pull_request_id=data_pull_job_id,
                statuses=_NEEDS_USER_ACTION,
            )
            issues = _summarize_issues(snapshot)
            logger.warning(
                "[{}/{}] 3B pull settled with issues: {}. Appended {} row(s) "
                "to {}. Proceeding to report-download for {} variant(s) with "
                "whatever data is available; affected GSTINs' rows may be "
                "incomplete or absent. NOT_DOWNLOADED needs OTP re-auth in "
                "ClearGST's UI; DOWNLOADED_PARTIALLY needs confirmation from "
                "the GST team.",
                pan, fy, issues, n_logged, partials_csv, len(todo),
            )
        else:
            logger.info("[{}/{}] 3B pull settled cleanly.", pan, fy)

        time.sleep(cfg.inter_call_delay_seconds)

        # ===== Step 2.5: prime the report-builder via fetch/3BSummary =====
        # Required: without this, Clear's reportDownload returns a valid-shape
        # XLSX/PDF whose cells are all zero. The UI calls this between the
        # pull settling and the user clicking Download; mirroring that here
        # is what populates the report.
        logger.info(
            "[{}/{}] Step 2.5/5: priming report-builder (fetch/3BSummary)",
            pan, fy,
        )
        api.fetch_3b_summary(
            data_pull_job_id=data_pull_job_id,
            gstin_node_ids=gstin_node_ids,
            workspace_id=cfg.workspace_id,
        )
        # Clear's UI waits ~10s here; the backend priming must propagate
        # before reportDownload reads it, otherwise the XLSX/PDF cells are all
        # zero (see discovery/app.clear.in.har_3b.har).
        time.sleep(cfg.wait_after_priming_seconds)

        # ===== Steps 3-5: per-variant report download =====
        for v in todo:
            spec = VARIANTS[v]
            rt = spec["report_type"]
            try:
                logger.info(
                    "[{}/{}/{}] Step 3/5: triggering {} report download",
                    pan, fy, rt, spec["label"],
                )
                report_job_id = api.trigger_3b_report_download(
                    data_pull_job_id=data_pull_job_id,
                    sheet_type=spec["sheet_type"],
                    output_type=spec["output_type"],
                    gstin_node_ids=gstin_node_ids,
                    workspace_id=cfg.workspace_id,
                )
                manifest.set_export_id(pan, fy, rt, report_job_id)
                time.sleep(cfg.inter_call_delay_seconds)

                logger.info(
                    "[{}/{}/{}] Step 4/5: waiting for {} report",
                    pan, fy, rt, spec["label"],
                )
                ready = api.wait_for_3b_report(
                    report_job_id,
                    gstin_node_ids=gstin_node_ids,
                    workspace_id=cfg.workspace_id,
                    poll_seconds=cfg.poll_seconds_export,
                    timeout_seconds=cfg.poll_timeout_export_seconds,
                )

                start_period, end_period = periods[0], periods[-1]
                filename = (
                    f"PAN_MM3B_{spec['label']}_{pan}_"
                    f"{start_period}-{end_period}.{spec['ext']}"
                )
                dest = (cfg.downloads_dir / pan / f"FY-{fy}"
                        / rt / filename)
                logger.info(
                    "[{}/{}/{}] Step 5/5: downloading {}",
                    pan, fy, rt, filename,
                )
                bytes_written = api.download_file(
                    ready.report_uri, dest,
                    gstin_node_ids=gstin_node_ids,
                )
                if bytes_written < 25 * 1024:
                    logger.warning(
                        "[{}/{}/{}] Downloaded file is suspiciously small "
                        "({} bytes < 25 KB) — Clear may have served an "
                        "empty-shell XLSX/PDF. Open the file to confirm.",
                        pan, fy, rt, bytes_written,
                    )

                manifest.mark_done(
                    pan, fy, rt,
                    file_path=str(dest), file_bytes=bytes_written,
                )
                logger.success(
                    "[{}/{}/{}] DONE: {} ({} bytes)",
                    pan, fy, rt, dest, bytes_written,
                )
            except ClearSessionExpired:
                # Bail the whole flow; CLI prints a one-line message + exits.
                raise
            except Exception as e:   # noqa: BLE001 — record + continue
                logger.exception(
                    "[{}/{}/{}] FAILED: {}", pan, fy, rt, e,
                )
                manifest.mark_failed(
                    pan, fy, rt, error=f"{type(e).__name__}: {e}",
                )
                # Continue with the next variant — others can still succeed.

    except ClearSessionExpired:
        raise
    except Exception as e:   # noqa: BLE001
        # A failure in the SHARED steps (pull / pull-wait / partial check)
        # fails every selected variant for this (PAN, FY).
        logger.exception(
            "[{}/{}] FAILED at shared step: {}", pan, fy, e,
        )
        for v in todo:
            rt = VARIANTS[v]["report_type"]
            manifest.mark_failed(
                pan, fy, rt,
                error=f"shared-pull: {type(e).__name__}: {e}",
            )
