"""PAN ITC Ledger Report — one full download per (PAN x FY).

Aggregates the per-GSTIN Input Tax Credit (ITC) ledger entries across every
GSTIN under a PAN for a given date range. Clear's UI slug is `panItcLedger`.
Output: one PAN-level XLSX per FY (we map each configured FY to its
`01-04-YYYY` → `31-03-YY+1` date range; FY 2017-18 is clamped to
`01-07-2017` because GST began that month).

Structurally identical to `pan_cash_ledger.py` — same 5-step flow (single
pull, single export, no preflight) with different identifiers:

  - Pull tenant + RLS workflow: ITC_LEDGER_REPORT
  - Referer slug: reportType=panItcLedger
  - Filename prefix: PAN_ITC_LEDGER_REPORT_<PAN>_<start>-<end>
  - Captured statement template id: 67e2a4bc8ede5b3eac89594a (Clear's stored
    QUERY template for ITC-ledger column set: myGstin, state_name,
    description, formatted_date, totalValue, igstValue, cgstValue, sgstValue,
    cessValue).

Captured POST body lives next to this file as
`pan_itc_ledger_statement.json` (HAR entry #159 in
`discovery/app.clear.in.itc.har`).
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


REPORT_TYPE = "PAN-ITC-Ledger"
TENANT = "ITC_LEDGER_REPORT"
RLS_WORKFLOW = TENANT

# GST regime began 01-07-2017 — earlier dates would be rejected / produce no data.
GST_START_DATE = date(2017, 7, 1)

_NEEDS_USER_ACTION = ("DOWNLOADED_PARTIALLY", "NOT_DOWNLOADED")


def _load_statement_template() -> dict:
    """Load the verbatim export-trigger payload captured for panItcLedger."""
    with resources.files("clear_ola.flows").joinpath(
        "pan_itc_ledger_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


def _fy_to_date_range(fy: str, *, as_of: date) -> tuple[str, str]:
    """Map an FY string ('2024-25') to ('DD-MM-YYYY', 'DD-MM-YYYY').

    Rules:
      - Start: 01-04-<first-year>, clamped up to 01-07-2017 for FY 2017-18.
      - End:   31-03-<second-year> (next March), clamped down to today for
        the current FY (asking for future dates returns empty data anyway).
    """
    first, second = fy.split("-")  # "2024-25" -> "2024", "25"
    start_year = int(first)
    end_year = int(first[:2] + second)  # "20" + "25" -> 2025
    start = date(start_year, 4, 1)
    if start < GST_START_DATE:
        start = GST_START_DATE
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

    Only the metadata + staticRowData + filename change per (PAN, FY).
    The `statement` block is left untouched — Clear resolves the
    `$TEMPLATE` reference (id `67e2a4bc8ede5b3eac89594a`) server-side.
    """
    filename_base = f"PAN_ITC_LEDGER_REPORT_{pan}_{start_range}-{end_range}"

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": pan,  # Clear's field name — actually the PAN
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
    """Process every (PAN x FY) in the config for PAN ITC Ledger. Skips combos
    already marked done. GSTINs in NOT_DOWNLOADED / DOWNLOADED_PARTIALLY are
    logged to `state/partial-items.csv` and the export proceeds anyway."""
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
            "Generating PAN-level ITC Ledger per FY.",
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
            "[{}/{}] Date range for ITC-ledger pull: {} .. {}",
            pan, fy, start_range, end_range,
        )

        # Step 1: Trigger ITC-ledger pull
        logger.info(
            "[{}/{}] Step 1/5: refresh ITC Ledger data for {} underlying "
            "GSTINs ({}..{}) under tenant {}",
            pan, fy, len(gstin_node_ids), start_range, end_range, TENANT,
        )
        pull_id = api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_range,  # DD-MM-YYYY passes through verbatim
            end_period=end_range,
            tenant=TENANT,
            gis_download_behaviour=None,  # HAR sent JSON null
        )
        manifest.set_pull_id(pan, fy, REPORT_TYPE, pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 2: Wait for pull
        logger.info(
            "[{}/{}] Step 2/5: wait for the ITC-ledger data refresh", pan, fy,
        )
        snapshot = api.wait_for_pull(
            gstin_node_ids,
            start_period=start_range,
            end_period=end_range,
            tenant=TENANT,
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
                "the PAN-level ITC ledger will only contain data from the "
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
                tenant=TENANT,
                gis_download_behaviour="DOWNLOAD_COMPLETE_DATA",
            )
            time.sleep(cfg.inter_call_delay_seconds)
            snapshot = api.wait_for_pull(
                gstin_node_ids,
                start_period=start_range,
                end_period=end_range,
                tenant=TENANT,
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
                "[{}/{}] ITC-ledger pull settled with issues: {}. "
                "Appended {} row(s) to {}. Proceeding to export anyway; "
                "rows for the affected GSTIN(s) may be incomplete or absent.",
                pan, fy, issues, n_logged, partials_csv,
            )
        else:
            logger.info(
                "[{}/{}] ITC-ledger pull settled cleanly. Continuing to export.",
                pan, fy,
            )
        time.sleep(cfg.inter_call_delay_seconds)

        # Per-call header overrides for the panItcLedger export trigger.
        # Verified from HAR (entries 149/159/174): x-ct-source absent,
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

        # Referer must carry reportType=panItcLedger and timePeriodType=DATE_RANGE.
        report_referer = (
            "https://app.clear.in/gst/reports/v2"
            f"?reportType=panItcLedger"
            f"&activeBusiness={urllib.parse.quote(pan_cfg.business_name)}"
            f"&pan={pan}"
            f"&panNodeId={pan_node_id}"
            f"&timePeriodType=DATE_RANGE"
            f"&section=REPORT_VIEW"
        )

        # Step 3: Fetch RLS token (date-range mode)
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
            "[{}/{}] Step 4/5: trigger PAN-level ITC-ledger export", pan, fy,
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

        # Step 5: Wait + download
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
