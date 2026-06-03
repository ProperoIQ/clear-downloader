"""PAN Electronic Credit Reversal & Re-claimed Statement (ECRRS).

Tracks ITC reversals and re-claims under Rule 37/37A/38/42/43, aggregated
across every GSTIN under a PAN. Clear's UI slug is `panElectronicReversalLedger`.
Output: one PAN-level XLSX per FY.

Structurally similar to `pan_itc_ledger.py` (single pull → poll → RLS →
single export → poll → S3, no preflight, DD-MM-YYYY date range, same header
overrides), with three ECRRS-specific quirks verified from HAR
(`discovery/app.clear.in.ecrrs.har`, entries #83 / #101 / #113 / #130):

  1. **Pull tenant ≠ RLS workflow.** The data pull piggybacks on the
     Electronic Cash Ledger source — `tenant: "ELECTRONIC_CASH_LEDGER"` on
     `pull/v2/trigger` and `pull/v3/status`. But the RLS-token URL uses
     `workFlow=ELECTRONIC_REVERSAL_REPORT`. Two constants, not one.

  2. **MIN_FY = 2023-24.** GSTN introduced ECRRS in August 2023. FYs
     strictly before 2023-24 have zero data — recorded as `no_data` and
     skipped (same pattern as `gstr_2b.py`'s MIN_FY). FY 2023-24 itself
     gets its start clamped from 01-04-2023 to 31-08-2023.

  3. **Filename pattern quirk.** The body `filename` is
     `PANElectronicReversalLedger_<PAN>_<DD-MM-YYYY>-<DD-MM-YYYY>` — note
     no underscore between `PAN` and `Electronic`, and CamelCase rather
     than SNAKE_CASE used by Cash/ITC ledger filenames. Replicated verbatim
     from HAR; Clear's backend may key off it.

Clear's server returns a different fileName for the final S3 download —
`"Electronic Credit Reversal and Re-claimed Statement..xlsx.zip"` (with a
literal double-dot before `xlsx`, spaces preserved). Our code uses
`ready.file_name` from the API response, so the on-disk filename matches
exactly what Clear gives us.

Captured POST body lives next to this file as
`pan_electronic_reversal_ledger_statement.json` (HAR entry #113).
"""

from __future__ import annotations

import copy
import json
import secrets
import time
import urllib.parse
from collections import defaultdict
from datetime import date
from importlib import resources

from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired
from clear_ola.config import (
    AppConfig,
    PanConfig,
)
from clear_ola.manifest import Manifest
from clear_ola.partials import log_partial_items


REPORT_TYPE = "PAN-Electronic-Reversal-Ledger"
# Pull tenant differs from RLS workflow — see module docstring, quirk #1.
PULL_TENANT = "ELECTRONIC_CASH_LEDGER"
RLS_WORKFLOW = "ELECTRONIC_REVERSAL_REPORT"

# ECRRS was introduced by GSTN in August 2023. FYs before 2023-24 have no
# data; FY 2023-24 has data only from 31-08-2023 onwards.
ECRRS_START_DATE = date(2023, 8, 31)
MIN_FY = "2023-24"

_NEEDS_USER_ACTION = ("DOWNLOADED_PARTIALLY", "NOT_DOWNLOADED")


def _load_statement_template() -> dict:
    """Load the verbatim export-trigger payload captured for panElectronicReversalLedger."""
    with resources.files("clear_ola.flows").joinpath(
        "pan_electronic_reversal_ledger_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


def _fy_to_date_range(fy: str, *, as_of: date) -> tuple[str, str]:
    """Map an FY string ('2024-25') to ('DD-MM-YYYY', 'DD-MM-YYYY').

    Rules:
      - Start: 01-04-<first-year>, clamped up to 31-08-2023 if the FY's
        natural start predates ECRRS (only affects FY 2023-24).
      - End:   31-03-<second-year>, clamped down to today for the current FY.
    """
    first, second = fy.split("-")
    start_year = int(first)
    end_year = int(first[:2] + second)
    start = date(start_year, 4, 1)
    if start < ECRRS_START_DATE:
        start = ECRRS_START_DATE
    end = date(end_year, 3, 31)
    if end > as_of:
        end = as_of
    return start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y")


def _build_export_payload(
    *,
    template: dict,
    pan: str,
    business_name: str,
    workspace_id: str,
    start_range: str,  # DD-MM-YYYY
    end_range: str,    # DD-MM-YYYY
) -> dict:
    """Substitute PAN/FY/workspace-specific fields into the captured template.

    CamelCase filename pattern is verbatim from HAR — see module docstring
    quirk #3. Don't normalise.
    """
    filename_base = f"PANElectronicReversalLedger_{pan}_{start_range}-{end_range}"

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": pan,
        "reportPeriod": f"{start_range} - {end_range}",
    }
    p["filename"] = filename_base

    for callback_key in ("onStart", "onFinish"):
        md = p[callback_key]["metadata"]
        md["orgId"] = workspace_id
        md["workspaceId"] = workspace_id
        md["nodeName"] = pan
        md["startRange"] = start_range
        md["endRange"] = end_range
        md["activeBusiness"] = business_name
        # md["filename"] stays "ECL Report" — Clear's literal UI label.

    return p


def _any_partial(snapshot: list[dict]) -> bool:
    return any(s.get("downloadStatus") == "DOWNLOADED_PARTIALLY"
               for s in snapshot)


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


def run(
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
) -> None:
    """Process every (PAN x FY) in the config for PAN ECRRS. Skips combos
    already marked done, short-circuits FYs before MIN_FY as `no_data`."""
    logger.info("Indexing GSTINs from workspace...")
    by_pan = _index_gstins_by_pan(api)
    logger.info("Found {} PAN(s), {} GSTIN(s) total",
                len(by_pan), sum(len(v) for v in by_pan.values()))

    template = _load_statement_template()

    for pan_cfg in cfg.pans:
        gstins = by_pan.get(pan_cfg.pan, [])
        if not gstins:
            logger.error(
                "No GSTINs found for PAN {} ({}). Skipping all its FYs.",
                pan_cfg.pan, pan_cfg.business_name,
            )
            for fy in pan_cfg.fys:
                manifest.mark_started(pan_cfg.pan, fy, REPORT_TYPE)
                manifest.mark_failed(
                    pan_cfg.pan, fy, REPORT_TYPE,
                    error=f"No GSTINs returned by user_gstins for PAN {pan_cfg.pan}",
                )
            continue
        logger.info(
            "PAN {} ({}) has {} state-wise GSTIN(s) registered. "
            "Generating PAN-level ECRRS per FY.",
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

    if fy < MIN_FY:
        if manifest.is_done(pan, fy, REPORT_TYPE):
            return
        logger.info(
            "[{}/{}/{}] FY predates ECRRS (introduced Aug 2023); "
            "recording no_data and skipping.", pan, fy, REPORT_TYPE,
        )
        manifest.mark_started(pan, fy, REPORT_TYPE)
        manifest.mark_no_data(pan, fy, REPORT_TYPE, gstins_seen=0)
        return

    if manifest.is_done(pan, fy, REPORT_TYPE):
        logger.info("[{}/{}/{}] already done — skipping", pan, fy, REPORT_TYPE)
        return

    logger.info("=" * 70)
    logger.info("[{}/{}/{}] starting", pan, fy, REPORT_TYPE)
    manifest.mark_started(pan, fy, REPORT_TYPE)

    try:
        gstin_node_ids = [g.gstin_node_id for g in gstins]
        pan_node_id = gstins[0].pan_node_id
        today = date.today()
        start_range, end_range = _fy_to_date_range(fy, as_of=today)
        logger.info(
            "[{}/{}] Date range for ECRRS pull: {} .. {}",
            pan, fy, start_range, end_range,
        )

        # Step 1: Trigger ECRRS pull — uses ELECTRONIC_CASH_LEDGER as the
        # data-pull tenant (the underlying GSTN data source is the same).
        logger.info(
            "[{}/{}] Step 1/5: refresh ECRRS data for {} underlying GSTINs "
            "({}..{}) under pull tenant {}",
            pan, fy, len(gstin_node_ids), start_range, end_range, PULL_TENANT,
        )
        pull_id = api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_range,
            end_period=end_range,
            tenant=PULL_TENANT,
            gis_download_behaviour=None,
        )
        manifest.set_pull_id(pan, fy, REPORT_TYPE, pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 2: Wait for pull (same tenant)
        logger.info(
            "[{}/{}] Step 2/5: wait for the ECRRS data refresh", pan, fy,
        )
        snapshot = api.wait_for_pull(
            gstin_node_ids,
            start_period=start_range,
            end_period=end_range,
            tenant=PULL_TENANT,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )

        downloaded_count = sum(
            1 for s in snapshot if s.get("downloadStatus") == "DOWNLOADED"
        )
        not_applicable_count = sum(
            1 for s in snapshot if s.get("downloadStatus") == "NOT_APPLICABLE"
        )

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
                "the PAN-level ECRRS will only contain data from the "
                "{} GSTIN(s) that did.",
                pan, fy, not_applicable_count, len(snapshot), downloaded_count,
            )

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
                start_period=start_range,
                end_period=end_range,
                tenant=PULL_TENANT,
                gis_download_behaviour="DOWNLOAD_COMPLETE_DATA",
            )
            time.sleep(cfg.inter_call_delay_seconds)
            snapshot = api.wait_for_pull(
                gstin_node_ids,
                start_period=start_range,
                end_period=end_range,
                tenant=PULL_TENANT,
                poll_seconds=cfg.poll_seconds_pull,
                timeout_seconds=cfg.poll_timeout_pull_seconds,
            )

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
            logger.warning(
                "[{}/{}] ECRRS pull settled with issues: {}. "
                "Appended {} row(s) to {}. Proceeding to export anyway.",
                pan, fy, issues, n_logged, partials_csv,
            )
        else:
            logger.info(
                "[{}/{}] ECRRS pull settled cleanly. Continuing to export.",
                pan, fy,
            )
        time.sleep(cfg.inter_call_delay_seconds)

        # Per-call header overrides for the panElectronicReversalLedger export
        # trigger. Verified from HAR (entries 101/113/130): x-ct-source absent,
        # baggage + sentry-trace present, accept-language en-US, priority u=1.
        sentry_trace_id = secrets.token_hex(16)
        sentry_span_id = secrets.token_hex(8)
        sentry_public_key = "607fd3b42fc9b74117f75a6900f89b00"
        header_overrides: dict[str, str | None] = {
            "x-ct-source": None,
            "baggage": (
                "sentry-environment=production,"
                f"sentry-public_key={sentry_public_key},"
                f"sentry-trace_id={sentry_trace_id},"
                "sentry-sample_rate=1,sentry-sampled=true"
            ),
            "sentry-trace": f"{sentry_trace_id}-{sentry_span_id}-1",
            "accept-language": "en-US,en;q=0.9",
            "priority": "u=1, i",
        }

        report_referer = (
            "https://app.clear.in/gst/reports/v2"
            f"?reportType=panElectronicReversalLedger"
            f"&activeBusiness={urllib.parse.quote(pan_cfg.business_name)}"
            f"&pan={pan}"
            f"&panNodeId={pan_node_id}"
            f"&timePeriodType=DATE_RANGE"
            f"&section=REPORT_VIEW"
        )

        # Step 3: Fetch RLS token — uses ELECTRONIC_REVERSAL_REPORT workflow
        # (different from the pull tenant — see module docstring quirk #1).
        logger.info(
            "[{}/{}] Step 3/5: fetch RLS token (workFlow={})",
            pan, fy, RLS_WORKFLOW,
        )
        rls_token = api.fetch_rls_token(
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
            from_date=start_range,
            to_date=end_range,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 4: Trigger the export (single call, no preflight)
        logger.info(
            "[{}/{}] Step 4/5: trigger PAN-level ECRRS export", pan, fy,
        )
        payload = _build_export_payload(
            template=template,
            pan=pan,
            business_name=pan_cfg.business_name,
            workspace_id=cfg.workspace_id,
            start_range=start_range,
            end_range=end_range,
        )
        export_id = api.trigger_export(
            payload, rls_token=rls_token,
            referer_override=report_referer,
            header_overrides=header_overrides,
        )
        manifest.set_export_id(pan, fy, REPORT_TYPE, export_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 5: Wait + download. Clear returns a server-chosen fileName
        # ("Electronic Credit Reversal and Re-claimed Statement..xlsx.zip")
        # which is what lands on disk.
        logger.info("[{}/{}] Step 5/5: wait for export", pan, fy)
        ready = api.wait_for_export(
            export_id,
            poll_seconds=cfg.poll_seconds_export,
            timeout_seconds=cfg.poll_timeout_export_seconds,
        )

        logger.info("[{}/{}] downloading {}", pan, fy, ready.file_name)
        dest = cfg.downloads_dir / pan / f"FY-{fy}" / REPORT_TYPE / ready.file_name
        bytes_written = api.download_file(
            ready.pre_signed_url, dest,
            gstin_node_ids=gstin_node_ids,
        )

        manifest.mark_done(
            pan, fy, REPORT_TYPE,
            file_path=str(dest), file_bytes=bytes_written,
        )
        logger.success("[{}/{}/{}] DONE: {} ({} bytes)",
                       pan, fy, REPORT_TYPE, dest, bytes_written)

    except ClearSessionExpired:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("[{}/{}/{}] FAILED: {}", pan, fy, REPORT_TYPE, e)
        manifest.mark_failed(pan, fy, REPORT_TYPE, error=f"{type(e).__name__}: {e}")
