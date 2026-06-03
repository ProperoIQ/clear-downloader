"""GSTR-8 (TCS by e-commerce operators) — one full download per (PAN × FY).

Mirrors the GSTR-2A flow byte-for-byte; only the report-type slugs, the
statement template, and the filename prefix differ. See `gstr_2a.py` for the
canonical commentary on the 6-step orchestration.
"""

from __future__ import annotations

import copy
import json
import time
from collections import defaultdict
from datetime import date
from importlib import resources
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


REPORT_TYPE = "GSTR-8"
TENANT = "GSTR8_REPORTS"
RLS_WORKFLOW = "GSTR8_REPORTS"
# GSTN introduced GSTR-8 (TCS return for e-commerce operators) in Oct 2018
# (first return period was Oct 2018, FY 2018-19). FYs strictly before
# 2018-19 therefore have no 8 data at all — we short-circuit those without
# making any API call.
MIN_FY = "2018-19"


def _load_statement_template() -> dict:
    """Load the verbatim export-trigger payload captured during HAR discovery.

    Stored as package data at `clear_ola/flows/gstr_8_statement.json`."""
    with resources.files("clear_ola.flows").joinpath("gstr_8_statement.json").open(
        "r", encoding="utf-8"
    ) as f:
        return json.load(f)


def _build_export_payload(
    *,
    template: dict,
    pan: str,
    business_name: str,
    fy: str,
    workspace_id: str,
    periods: list[str],
) -> dict:
    """Take the captured GSTR-8 statement and substitute PAN/FY/workspace-specific bits.

    `periods` should be the same (possibly truncated) list used for the pull —
    so the filename and metadata reflect the actual months requested.

    The `statement` block (columns, filters=null) is left untouched — Clear
    decides scope from headers + workspace context.
    """
    start_range = periods[0]   # e.g. "042025"
    end_range = periods[-1]    # e.g. "032026"
    filename_base = f"PANGSTR8_{pan}_{start_range}-{end_range}"

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": pan,  # Clear calls the PAN "gstin" here — intentional
        "reportPeriod": _periods_to_human(periods),
    }
    p["filename"] = filename_base
    # fileType stays "XLSX" — captured verbatim from the GSTR-8 HAR.

    for callback_key in ("onStart", "onFinish"):
        md = p[callback_key]["metadata"]
        md["orgId"] = workspace_id
        md["workspaceId"] = workspace_id
        md["nodeName"] = pan
        md["startRange"] = start_range
        md["endRange"] = end_range
        md["activeBusiness"] = business_name
        # md["reportType"] stays "panGstr8" — that's what makes this GSTR-8.

    return p


<<<<<<< HEAD
def _build_query_payload(*, template: dict) -> dict:
    """Build the data-browser priming query body.

    Clear's UI calls `/api/clear/data-browser/public/v2/query` between the
    RLS-token fetch and the export trigger (see
    `discovery/app.clear.in.har_GSTR-8.har`, line 68898). The body is just
    `{"statement": <select>}` — the same SELECT the export uses, but with
    `limit: 1000` instead of `0` (a pageable preview). The response is
    discarded; the call's only job is to materialize the result set on
    Clear's side so the subsequent export trigger reads from a populated
    cube instead of serving an empty-shell XLSX.
    """
    statement = copy.deepcopy(template["statement"])
    statement["limit"] = 1000
    return {"statement": statement}


=======
>>>>>>> origin/add-pan-ecrrs-report
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _periods_to_human(periods: list[str]) -> str:
    """`['042026', '052026']` -> `'Apr 2026 - May 2026'`."""
    first, last = periods[0], periods[-1]
    fm, fy_ = int(first[:2]), first[2:]
    lm, ly_ = int(last[:2]), last[2:]
    return f"{_MONTHS[fm]} {fy_} - {_MONTHS[lm]} {ly_}"


def _any_partial(snapshot: list[dict]) -> bool:
    """True if any GSTIN in the snapshot is in DOWNLOADED_PARTIALLY state."""
    return any(s.get("downloadStatus") == "DOWNLOADED_PARTIALLY"
               for s in snapshot)


# Statuses that mean "the user needs to do something in Clear's UI" — we can't
# resolve them programmatically. NOT_DOWNLOADED almost always means the GSTIN's
# stored GSTN session has expired (needs OTP re-auth via Clear). PARTIALLY may
# also remain stuck after the auto-retry.
_NEEDS_USER_ACTION = ("DOWNLOADED_PARTIALLY", "NOT_DOWNLOADED")


def _summarize_issues(snapshot: list[dict]) -> str:
    """Human-readable per-GSTIN summary, e.g. 'delhi (NOT_DOWNLOADED), karnataka (DOWNLOADED_PARTIALLY)'."""
    return ", ".join(
        f"{s.get('nodeName', '?')} ({s.get('downloadStatus')})"
        for s in snapshot
        if s.get("downloadStatus") in _NEEDS_USER_ACTION
    )


def _index_gstins_by_pan(api: ClearAPI) -> dict[str, list]:
    """Return {pan: [GstinNode, ...]} for every PAN under the workspace."""
    nodes = api.user_gstins()
    by_pan: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_pan[n.pan].append(n)
    return dict(by_pan)


def run(
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
) -> None:
    """Process every (PAN × FY) in the config for GSTR-8. Skips combos already
    marked done, and short-circuits FYs before 2018-19 (when GSTR-8 did not exist).
    Records progress + errors to the manifest after every step.

    GSTINs that settle in NOT_DOWNLOADED or DOWNLOADED_PARTIALLY are logged to
    `state/partial-items.csv` and the export proceeds with whatever data is
    available — the same way NOT_APPLICABLE GSTINs are handled.
    """
    logger.info("Indexing GSTINs from workspace...")
    by_pan = _index_gstins_by_pan(api)
    logger.info("Found {} PAN(s), {} GSTIN(s) total",
                len(by_pan), sum(len(v) for v in by_pan.values()))

    template = _load_statement_template()

    for pan_cfg in cfg.pans:
        gstins = by_pan.get(pan_cfg.pan, [])
        if not gstins:
            logger.error("No GSTINs found for PAN {} ({}). Skipping all its FYs.",
                         pan_cfg.pan, pan_cfg.business_name)
            for fy in pan_cfg.fys:
                manifest.mark_started(pan_cfg.pan, fy, REPORT_TYPE)
                manifest.mark_failed(
                    pan_cfg.pan, fy, REPORT_TYPE,
                    error=f"No GSTINs returned by user_gstins for PAN {pan_cfg.pan}",
                )
            continue
        logger.info(
            "PAN {} ({}) has {} state-wise GSTIN(s) registered. "
            "We will refresh data from each (Clear's internal step), then "
            "produce ONE consolidated PAN-level Excel report per FY.",
            pan_cfg.pan, pan_cfg.business_name, len(gstins),
        )

        for fy in pan_cfg.fys:
            _run_one(
                api=api, cfg=cfg, manifest=manifest,
                pan_cfg=pan_cfg, gstins=gstins, fy=fy, template=template,
            )


def _run_one(
    *,
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
    pan_cfg: PanConfig,
    gstins: list,
    fy: str,
    template: dict,
) -> None:
    pan = pan_cfg.pan

    # GSTR-8 did not exist before Oct 2018 (FY 2018-19). Skip pre-2018-19
    # FYs without making any API call — Clear would just return NOT_APPLICABLE
    # for every period and we'd waste a pull cycle. Record as no_data so the
    # row appears in the status-report's "No Data Available" sheet alongside
    # PANs that genuinely had no 8 activity.
    # ("YYYY-YY" strings sort lexicographically because YYYY is fixed-width.)
    if fy < MIN_FY:
        if manifest.is_done(pan, fy, REPORT_TYPE):
            return
        logger.info(
            "[{} / {} / {}] FY predates GSTR-8 (introduced Oct 2018); "
            "recording as no_data and skipping.", pan, fy, REPORT_TYPE,
        )
        manifest.mark_started(pan, fy, REPORT_TYPE)
        manifest.mark_no_data(pan, fy, REPORT_TYPE, gstins_seen=0)
        return

    if manifest.is_done(pan, fy, REPORT_TYPE):
        logger.info("[{} / {} / {}] already done — skipping", pan, fy, REPORT_TYPE)
        return

    logger.info("=" * 70)
    logger.info("[{} / {} / {}] starting", pan, fy, REPORT_TYPE)
    manifest.mark_started(pan, fy, REPORT_TYPE)

    try:
        gstin_node_ids = [g.gstin_node_id for g in gstins]
        today = date.today()
        periods = fy_periods(fy, as_of=today)
        start_period = periods[0]
        end_period = periods[-1]
        if len(periods) < 12:
            logger.info(
                "[{}/{}] Today is {} — FY isn't complete yet. "
                "Will request only {} period(s): {}..{} "
                "(asking for future months would just produce DOWNLOADED_PARTIALLY).",
                pan, fy, today.isoformat(), len(periods), start_period, end_period,
            )

        # 1. Trigger fresh pull from GSTN (or no-op if recent enough)
        logger.info(
            "[{}/{}] Step 1/6: refresh GSTR-8 data for {} underlying GSTINs "
            "({}..{}) — prep step, no file is produced here.",
            pan, fy, len(gstin_node_ids), start_period, end_period,
        )
        pull_id = api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=TENANT,
        )
        manifest.set_pull_id(pan, fy, REPORT_TYPE, pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # 2. Wait for pull to finish (all GSTINs DOWNLOADED)
        logger.info(
            "[{}/{}] Step 2/6: wait for the data refresh (Clear only reports "
            "status for GSTINs that actually needed re-fetching)", pan, fy,
        )
        snapshot = api.wait_for_pull(
            gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=TENANT,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )

        # 2a. Categorize.
        downloaded_count = sum(
            1 for s in snapshot if s.get("downloadStatus") == "DOWNLOADED"
        )
        not_applicable_count = sum(
            1 for s in snapshot if s.get("downloadStatus") == "NOT_APPLICABLE"
        )

        # 2a.i. Entire PAN is NOT_APPLICABLE for this FY → mark no_data and
        #       move on (the PAN didn't yet exist anywhere during this FY,
        #       or 8 simply hasn't been generated for any of its GSTINs).
        if (not_applicable_count == len(snapshot) and len(snapshot) > 0
                and not _any_partial(snapshot)):
            logger.info(
                "[{}/{}] No data for this PAN x FY: all {} underlying GSTIN(s) "
                "returned NOT_APPLICABLE. Marking as no_data and moving on.",
                pan, fy, not_applicable_count,
            )
            manifest.mark_no_data(
                pan, fy, REPORT_TYPE, gstins_seen=not_applicable_count,
            )
            return
        if not_applicable_count > 0:
            logger.info(
                "[{}/{}] {} of {} underlying GSTIN(s) returned NOT_APPLICABLE; "
                "the PAN-level Excel will only contain data from the {} GSTIN(s) "
                "that did.",
                pan, fy, not_applicable_count, len(snapshot), downloaded_count,
            )

        # 2b. If any GSTIN settled DOWNLOADED_PARTIALLY, retry once with
        #     "DOWNLOAD_COMPLETE_DATA" — equivalent to the UI's "Download all
        #     data again" button. NOT_DOWNLOADED can't be fixed by retry
        #     (needs OTP re-auth in Clear's UI), so we don't bother for it.
        if _any_partial(snapshot):
            partial_gstins = ", ".join(
                s.get("nodeName", "?") for s in snapshot
                if s.get("downloadStatus") == "DOWNLOADED_PARTIALLY"
            )
            logger.warning(
                "[{}/{}] Some GSTINs settled as DOWNLOADED_PARTIALLY: {}. "
                "Re-triggering with gisDownloadBehaviour=DOWNLOAD_COMPLETE_DATA.",
                pan, fy, partial_gstins,
            )
            api.trigger_pull(
                gstin_node_ids=gstin_node_ids,
                start_period=start_period,
                end_period=end_period,
                tenant=TENANT,
                gis_download_behaviour="DOWNLOAD_COMPLETE_DATA",
            )
            time.sleep(cfg.inter_call_delay_seconds)
            snapshot = api.wait_for_pull(
                gstin_node_ids,
                start_period=start_period,
                end_period=end_period,
                tenant=TENANT,
                poll_seconds=cfg.poll_seconds_pull,
                timeout_seconds=cfg.poll_timeout_pull_seconds,
            )

        # 2c. After any retry, unified handling of GSTINs that need user
        #     action (NOT_DOWNLOADED = session expired, DOWNLOADED_PARTIALLY =
        #     gap remains). Log to CSV, then proceed to export anyway —
        #     same as 2a handles partial NOT_APPLICABLE.
        issue_rows = [s for s in snapshot
                      if s.get("downloadStatus") in _NEEDS_USER_ACTION]
        if issue_rows:
            partials_csv = cfg.state_dir / "partial-items.csv"
            n_logged = log_partial_items(
                partials_csv,
                pan=pan,
                business_name=pan_cfg.business_name,
                fy=fy,
                report_type=REPORT_TYPE,
                snapshot=snapshot,
                pull_request_id=pull_id,
                statuses=_NEEDS_USER_ACTION,
            )
            issues = _summarize_issues(snapshot)
            has_not_downloaded = any(
                s.get("downloadStatus") == "NOT_DOWNLOADED" for s in issue_rows
            )
            has_still_partial = any(
                s.get("downloadStatus") == "DOWNLOADED_PARTIALLY" for s in issue_rows
            )
            hints = []
            if has_not_downloaded:
                hints.append(
                    "NOT_DOWNLOADED means Clear's stored GSTN session for that "
                    "GSTIN has expired. Open ClearGST -> this PAN+FY's report "
                    "page -> 'Generate OTP to connect GSTINs' -> enter OTP for "
                    "those states, then re-run."
                )
            if has_still_partial:
                hints.append(
                    "DOWNLOADED_PARTIALLY remained even after the force "
                    "re-download. Confirm with the GST team whether data "
                    "should exist for the listed (gstin, period)s."
                )
            hint = " ".join(hints)
            logger.warning(
                "[{}/{}] Pull settled with issues: {}. Appended {} row(s) "
                "to {}. Proceeding to export anyway; rows for the affected "
                "GSTIN(s) may be incomplete or absent. {}",
                pan, fy, issues, n_logged, partials_csv, hint,
            )
        else:
            logger.info(
                "[{}/{}] Pull settled cleanly. Continuing to export.",
                pan, fy,
            )
        time.sleep(cfg.inter_call_delay_seconds)

        # 3. Fetch RLS token
        logger.info("[{}/{}] Step 3/6: fetch RLS token", pan, fy)
        rls_token = api.fetch_rls_token(
            periods,
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
        )
        time.sleep(cfg.inter_call_delay_seconds)

<<<<<<< HEAD
        # 3.5. Prime Clear's data-browser cube. Without this call, the export
        # trigger serves a valid-shape-but-empty XLSX. Discovered via HAR diff:
        # Clear's UI hits POST /v2/query between RLS-token fetch and export
        # trigger (see discovery/app.clear.in.har_GSTR-8.har, line 68898).
        logger.info(
            "[{}/{}] Step 3.5/6: prime Clear's data-browser cube via /v2/query",
            pan, fy,
        )
        query_payload = _build_query_payload(template=template)
        api.run_data_browser_query(query_payload, rls_token=rls_token)
        # Clear's UI waits ~5-15s here; the cube must materialize before the
        # real export trigger reads it.
        time.sleep(cfg.wait_after_priming_seconds)

=======
>>>>>>> origin/add-pan-ecrrs-report
        # 4. Trigger Excel export (this is the step that produces the single
        #    PAN-level Excel file)
        logger.info(
            "[{}/{}] Step 4/6: trigger the PAN-level Excel export "
            "(one consolidated file)", pan, fy,
        )
        payload = _build_export_payload(
            template=template,
            pan=pan,
            business_name=pan_cfg.business_name,
            fy=fy,
            workspace_id=cfg.workspace_id,
            periods=periods,
        )
        export_id = api.trigger_export(payload, rls_token=rls_token)
        manifest.set_export_id(pan, fy, REPORT_TYPE, export_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # 5. Wait for export to be ready (taskStatus=SUCCESS)
        logger.info("[{}/{}] Step 5/6: wait for export", pan, fy)
        ready = api.wait_for_export(
            export_id,
            poll_seconds=cfg.poll_seconds_export,
            timeout_seconds=cfg.poll_timeout_export_seconds,
        )

        # 6. Download the file
        logger.info("[{}/{}] Step 6/6: download {}", pan, fy, ready.file_name)
        dest = cfg.downloads_dir / pan / f"FY-{fy}" / REPORT_TYPE / ready.file_name
        bytes_written = api.download_file(
            ready.pre_signed_url, dest,
            gstin_node_ids=gstin_node_ids,
        )
<<<<<<< HEAD
        if bytes_written < 10 * 1024:
            logger.warning(
                "[{}/{}/{}] Downloaded file is suspiciously small "
                "({} bytes < 10 KB). May be a Clear empty-shell, or the PAN "
                "genuinely has minimal GSTR-8 (TCS) activity. Cross-check "
                "with Clear's portal before treating as a bug.",
                pan, fy, REPORT_TYPE, bytes_written,
            )
=======
>>>>>>> origin/add-pan-ecrrs-report

        manifest.mark_done(
            pan, fy, REPORT_TYPE,
            file_path=str(dest), file_bytes=bytes_written,
        )
        logger.success("[{}/{}/{}] DONE: {} ({} bytes)",
                       pan, fy, REPORT_TYPE, dest, bytes_written)

    except ClearSessionExpired:
        # Re-raise; CLI catches this and prints a one-line, actionable message
        # then exits non-zero. We don't continue to other PANs/FYs in that case.
        raise
    except Exception as e:  # noqa: BLE001 — record + continue
        logger.exception("[{}/{}/{}] FAILED: {}", pan, fy, REPORT_TYPE, e)
        manifest.mark_failed(pan, fy, REPORT_TYPE, error=f"{type(e).__name__}: {e}")
