"""GSTR-6A Report — one Excel per (GSTIN x FY).

The first GST-based flow in the project. Unlike GSTR-2A/2B/1/8 (which
aggregate at PAN level), the GSTR-6A backend operates per-GSTIN:
  - pull `metadata.reportLevel = "GSTIN"` with a single GSTIN node id
  - export `nodeNameType = "GSTIN"`, filename `GSTR6A_<GSTIN>_<periods>`
  - one file per GSTIN, saved under downloads/gst/<GSTIN>/FY-<FY>/GSTR-6A/

Mirrors the structure of `clear_ola.flows.gstr_2a` so partial-retry,
NOT_APPLICABLE-as-no-data, and OTP-needed handling stay consistent.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import date
from importlib import resources

from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired, GstinNode
from clear_ola.config import (
    AppConfig,
    GstinConfig,
    fy_periods,
)
from clear_ola.gst_manifest import GstManifest
from clear_ola.partials import log_partial_items


REPORT_TYPE = "GSTR-6A"
TENANT = "GSTR6A_REPORTS"
RLS_WORKFLOW = "GSTR6A_REPORTS"


def _load_statement_template() -> dict:
    """Load the verbatim export-trigger payload captured from the HAR.

    Stored as package data at `clear_ola/gst_flows/gstr_6a_statement.json`."""
    with resources.files("clear_ola.gst_flows").joinpath(
        "gstr_6a_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _periods_to_human(periods: list[str]) -> str:
    """`['042026', '052026']` -> `'Apr 2026 - May 2026'`."""
    first, last = periods[0], periods[-1]
    fm, fy_ = int(first[:2]), first[2:]
    lm, ly_ = int(last[:2]), last[2:]
    return f"{_MONTHS[fm]} {fy_} - {_MONTHS[lm]} {ly_}"


def _build_export_payload(
    *,
    template: dict,
    gstin: str,
    business_name: str,
    workspace_id: str,
    periods: list[str],
) -> dict:
    """Substitute the captured 6A template with this run's GSTIN/business/period.

    Leave the 26-field SELECT statement untouched — Clear decides scope from
    headers (rls token + node id) plus the metadata in onStart/onFinish, not
    from the SELECT body.
    """
    start_range = periods[0]   # e.g. "042025"
    end_range = periods[-1]    # e.g. "032026"

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": gstin,  # actual GSTIN here (unlike 2A which puts the PAN)
        "reportPeriod": _periods_to_human(periods),
    }
    p["filename"] = f"GSTR6A_{gstin}_{start_range}-{end_range}"
    # exportName stays "G6aView" (the captured S3 prefix for 6A).
    # fileType stays "XLSX".

    for callback_key in ("onStart", "onFinish"):
        md = p[callback_key]["metadata"]
        md["orgId"] = workspace_id
        md["workspaceId"] = workspace_id
        md["nodeName"] = gstin            # GSTIN, not PAN
        md["startRange"] = start_range
        md["endRange"] = end_range
        md["activeBusiness"] = business_name
        # md["nodeNameType"] stays "GSTIN"
        # md["reportType"] stays "gstr6a"

    return p


# Statuses that mean "the user needs to do something in Clear's UI" — we can't
# resolve them programmatically (same vocabulary as the PAN-based flows).
_NEEDS_USER_ACTION = ("DOWNLOADED_PARTIALLY", "NOT_DOWNLOADED")


def _any_partial(snapshot: list[dict]) -> bool:
    return any(s.get("downloadStatus") == "DOWNLOADED_PARTIALLY"
               for s in snapshot)


def _summarize_issues(snapshot: list[dict]) -> str:
    return ", ".join(
        f"{s.get('nodeName', '?')} ({s.get('downloadStatus')})"
        for s in snapshot
        if s.get("downloadStatus") in _NEEDS_USER_ACTION
    )


def _index_gstins(api: ClearAPI) -> dict[str, GstinNode]:
    """Return {gstin_str: GstinNode} for every GSTIN under the workspace."""
    return {n.gstin: n for n in api.user_gstins()}


def run(
    api: ClearAPI,
    cfg: AppConfig,
    manifest: GstManifest,
) -> None:
    """Process every (GSTIN x FY) in `cfg.gstins`. Skips combos already
    marked done. Records progress + errors to the manifest after every step.
    """
    logger.info("Indexing GSTINs from workspace...")
    by_gstin = _index_gstins(api)
    logger.info("Workspace has {} GSTIN(s) total", len(by_gstin))

    template = _load_statement_template()

    for g_cfg in cfg.gstins:
        node = by_gstin.get(g_cfg.gstin)
        if node is None:
            logger.error(
                "GSTIN {} ({}) not found in workspace — skipping all its FYs.",
                g_cfg.gstin, g_cfg.business_name,
            )
            for fy in g_cfg.fys:
                manifest.mark_started(g_cfg.gstin, fy, REPORT_TYPE)
                manifest.mark_failed(
                    g_cfg.gstin, fy, REPORT_TYPE,
                    error=f"GSTIN {g_cfg.gstin} not in workspace",
                )
            continue

        # Prefer Clear's authoritative business name where the config didn't
        # provide one (same convention as the PAN-based flow).
        biz_name = g_cfg.business_name or node.business_name

        logger.info(
            "GSTIN {} ({}) — state {}: processing FYs {}",
            g_cfg.gstin, biz_name, node.state_name, g_cfg.fys,
        )

        for fy in g_cfg.fys:
            _run_one(
                api=api, cfg=cfg, manifest=manifest,
                g_cfg=g_cfg, node=node, biz_name=biz_name,
                fy=fy, template=template,
            )


def _run_one(
    *,
    api: ClearAPI,
    cfg: AppConfig,
    manifest: GstManifest,
    g_cfg: GstinConfig,
    node: GstinNode,
    biz_name: str,
    fy: str,
    template: dict,
) -> None:
    gstin = g_cfg.gstin
    if manifest.is_done(gstin, fy, REPORT_TYPE):
        logger.info("[{} / {} / {}] already done — skipping",
                    gstin, fy, REPORT_TYPE)
        return

    logger.info("=" * 70)
    logger.info("[{} / {} / {}] starting", gstin, fy, REPORT_TYPE)
    manifest.mark_started(gstin, fy, REPORT_TYPE)

    try:
        gstin_node_ids = [node.gstin_node_id]
        today = date.today()
        periods = fy_periods(fy, as_of=today)
        start_period = periods[0]
        end_period = periods[-1]
        if len(periods) < 12:
            logger.info(
                "[{}/{}] Today is {} — FY isn't complete yet. "
                "Will request only {} period(s): {}..{}",
                gstin, fy, today.isoformat(),
                len(periods), start_period, end_period,
            )

        # 1. Trigger fresh pull from GSTN for this single GSTIN
        logger.info(
            "[{}/{}] Step 1/6: refresh GSTR-6A data for this GSTIN "
            "({}..{}) — prep step, no file produced here.",
            gstin, fy, start_period, end_period,
        )
        pull_id = api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=TENANT,
            report_level="GSTIN",
        )
        manifest.set_pull_id(gstin, fy, REPORT_TYPE, pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # 2. Wait for pull to finish
        logger.info("[{}/{}] Step 2/6: wait for the data refresh", gstin, fy)
        snapshot = api.wait_for_pull(
            gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=TENANT,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )

        # 2a. Categorise. With a single-GSTIN pull the snapshot is tiny.
        statuses = {s.get("downloadStatus") for s in snapshot}

        if statuses == {"NOT_APPLICABLE"}:
            # GSTIN wasn't ISD-registered (or had no inward distributions)
            # during this FY. Settle as no_data and move on.
            logger.info(
                "[{}/{}] No data: GSTIN returned NOT_APPLICABLE for this FY "
                "(likely not ISD-registered then). Marking as no_data.",
                gstin, fy,
            )
            manifest.mark_no_data(gstin, fy, REPORT_TYPE)
            return

        # 2b. Retry once if partial (same as 2A).
        if _any_partial(snapshot):
            logger.warning(
                "[{}/{}] Settled as DOWNLOADED_PARTIALLY. Re-triggering with "
                "gisDownloadBehaviour=DOWNLOAD_COMPLETE_DATA.",
                gstin, fy,
            )
            api.trigger_pull(
                gstin_node_ids=gstin_node_ids,
                start_period=start_period,
                end_period=end_period,
                tenant=TENANT,
                report_level="GSTIN",
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

        # 2c. Unified handling for still-stuck statuses (log + proceed).
        issue_rows = [s for s in snapshot
                      if s.get("downloadStatus") in _NEEDS_USER_ACTION]
        if issue_rows:
            partials_csv = cfg.state_dir / "partial-items.csv"
            # partials.py is keyed by `pan`; pass the GSTIN there. Same CSV
            # shape; the consumer-facing OTP worklist already groups by
            # (pan, fy) and treats the field as an opaque entity id.
            n_logged = log_partial_items(
                partials_csv,
                pan=gstin,
                business_name=biz_name,
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
            hint = ""
            if has_not_downloaded:
                hint = (
                    "NOT_DOWNLOADED means Clear's stored GSTN session for "
                    "this GSTIN has expired. Open ClearGST -> GSTR-6A page "
                    "for this GSTIN -> 'Generate OTP to connect', then re-run."
                )
            logger.warning(
                "[{}/{}] Pull settled with issues: {}. Appended {} row(s) "
                "to {}. Proceeding to export anyway. {}",
                gstin, fy, issues, n_logged, partials_csv, hint,
            )
        else:
            logger.info("[{}/{}] Pull settled cleanly. Continuing to export.",
                        gstin, fy)
        time.sleep(cfg.inter_call_delay_seconds)

        # 3. Fetch RLS token
        logger.info("[{}/{}] Step 3/6: fetch RLS token", gstin, fy)
        rls_token = api.fetch_rls_token(
            periods,
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # 4. Trigger Excel export
        logger.info("[{}/{}] Step 4/6: trigger the per-GSTIN Excel export",
                    gstin, fy)
        payload = _build_export_payload(
            template=template,
            gstin=gstin,
            business_name=biz_name,
            workspace_id=cfg.workspace_id,
            periods=periods,
        )
        export_id = api.trigger_export(payload, rls_token=rls_token)
        manifest.set_export_id(gstin, fy, REPORT_TYPE, export_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # 5. Wait for export
        logger.info("[{}/{}] Step 5/6: wait for export", gstin, fy)
        ready = api.wait_for_export(
            export_id,
            poll_seconds=cfg.poll_seconds_export,
            timeout_seconds=cfg.poll_timeout_export_seconds,
        )

        # 6. Download. Lives under downloads/gst/<GSTIN>/FY-<FY>/GSTR-6A/
        # so the GST-based output never collides with the PAN-based tree.
        logger.info("[{}/{}] Step 6/6: download {}",
                    gstin, fy, ready.file_name)
        dest = (cfg.downloads_dir / "gst" / gstin
                / f"FY-{fy}" / REPORT_TYPE / ready.file_name)
        bytes_written = api.download_file(
            ready.pre_signed_url, dest,
            gstin_node_ids=gstin_node_ids,
        )

        manifest.mark_done(
            gstin, fy, REPORT_TYPE,
            file_path=str(dest), file_bytes=bytes_written,
        )
        logger.success("[{}/{}/{}] DONE: {} ({} bytes)",
                       gstin, fy, REPORT_TYPE, dest, bytes_written)

    except ClearSessionExpired:
        # Bubble up to the CLI for a friendly one-line message + non-zero exit.
        raise
    except Exception as e:  # noqa: BLE001 — record + continue with next combo
        logger.exception("[{}/{}/{}] FAILED: {}",
                         gstin, fy, REPORT_TYPE, e)
        manifest.mark_failed(
            gstin, fy, REPORT_TYPE,
            error=f"{type(e).__name__}: {e}",
        )
