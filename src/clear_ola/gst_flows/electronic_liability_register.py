"""Electronic Liability Register (ELR) — one Excel per (GSTIN x FY).

Other-than-return tax liabilities + interest + penalty maintained by GSTN.
ClearGST exposes it under `reportType=taxLiabilityLedger`. This module
follows the same per-GSTIN single-variant 6-step pipeline as `gstr_6a.py`,
but uses DD-MM-YYYY date-range scoping (like `pan_electronic_reversal_ledger.py`)
instead of MMYYYY monthly periods — the ELR endpoint is range-scoped.

The captured HAR (`discovery/app.clear.in.har__Electronic Liablity Register.har`)
was recorded at PAN level. The per-GSTIN payload here overrides three fields
that the HAR had set to PAN scope:
  - `nodeIds`           -> the single GSTIN's node id
  - `metadata.reportLevel`         -> "GSTIN" (was "PAN")
  - `metadata.nodeNameType` / `nodeName` -> "GSTIN" / <gstin>
  - `staticRowData.gstin`          -> real GSTIN (was the PAN)
Filename pattern is `ELR_<GSTIN>_<DD-MM-YYYY>-<DD-MM-YYYY>` — mirrors the
`GSTR6A_<GSTIN>_<periods>` convention used by the other per-GSTIN flows.

If the smoke test surfaces data for the wrong scope (rows from multiple
GSTINs in a single file, etc.), re-capture HAR with a single GSTIN selected
in Clear's UI and reconcile against this template.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import date
from importlib import resources

from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired, GstinNode
from clear_ola.config import AppConfig, GstinConfig
from clear_ola.gst_manifest import GstManifest
from clear_ola.partials import log_partial_items


REPORT_TYPE = "Electronic-Liability-Register"
TENANT = "TAX_LIABILITY_LEDGER"
RLS_WORKFLOW = "TAX_LIABILITY_LEDGER_REPORT"


def _load_statement_template() -> dict:
    """Load the verbatim export-trigger payload captured from the HAR.

    Stored as package data at
    `clear_ola/gst_flows/electronic_liability_register_statement.json`."""
    with resources.files("clear_ola.gst_flows").joinpath(
        "electronic_liability_register_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


def _fy_to_date_range(fy: str, *, as_of: date) -> tuple[str, str]:
    """Map an FY string ('2024-25') to ('DD-MM-YYYY', 'DD-MM-YYYY').

    No earliest-valid-date clamp (ELR has been available since GST inception
    in July 2017). End is clamped to today for the in-progress FY.
    """
    first, second = fy.split("-")
    start_year = int(first)
    end_year = int(first[:2] + second)
    start = date(start_year, 4, 1)
    end = date(end_year, 3, 31)
    if end > as_of:
        end = as_of
    return start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y")


def _build_export_payload(
    *,
    template: dict,
    gstin: str,
    business_name: str,
    workspace_id: str,
    start_range: str,  # DD-MM-YYYY
    end_range: str,    # DD-MM-YYYY
) -> dict:
    """Adapt the captured (PAN-level) template to a per-GSTIN ELR export.

    Leaves the SELECT statement, exportName ("tax_liability_ledger_download"),
    fileType ("XLSX"), template id, and notificationType untouched — those
    identify the report and are baked into the JSON file.
    """
    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": gstin,
        "reportPeriod": f"{start_range} - {end_range}",
    }
    p["filename"] = f"ELR_{gstin}_{start_range}-{end_range}"

    for callback_key in ("onStart", "onFinish"):
        md = p[callback_key]["metadata"]
        md["orgId"] = workspace_id
        md["workspaceId"] = workspace_id
        md["nodeNameType"] = "GSTIN"
        md["nodeName"] = gstin
        md["startRange"] = start_range
        md["endRange"] = end_range
        md["activeBusiness"] = business_name

    return p


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
        start_range, end_range = _fy_to_date_range(fy, as_of=today)
        logger.info(
            "[{}/{}] Date range for ELR pull: {} .. {}",
            gstin, fy, start_range, end_range,
        )

        # 1. Trigger fresh pull from GSTN for this single GSTIN
        logger.info(
            "[{}/{}] Step 1/6: refresh ELR data for this GSTIN "
            "({}..{}) — prep step, no file produced here.",
            gstin, fy, start_range, end_range,
        )
        pull_id = api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_range,
            end_period=end_range,
            tenant=TENANT,
            report_level="GSTIN",
        )
        manifest.set_pull_id(gstin, fy, REPORT_TYPE, pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # 2. Wait for pull to finish
        logger.info("[{}/{}] Step 2/6: wait for the data refresh", gstin, fy)
        snapshot = api.wait_for_pull(
            gstin_node_ids,
            start_period=start_range,
            end_period=end_range,
            tenant=TENANT,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )

        statuses = {s.get("downloadStatus") for s in snapshot}

        if statuses == {"NOT_APPLICABLE"}:
            logger.info(
                "[{}/{}] No data: GSTIN returned NOT_APPLICABLE for this FY. "
                "Marking as no_data.",
                gstin, fy,
            )
            manifest.mark_no_data(gstin, fy, REPORT_TYPE)
            return

        # 2b. Retry once if partial.
        if _any_partial(snapshot):
            logger.warning(
                "[{}/{}] Settled as DOWNLOADED_PARTIALLY. Re-triggering with "
                "gisDownloadBehaviour=DOWNLOAD_COMPLETE_DATA.",
                gstin, fy,
            )
            api.trigger_pull(
                gstin_node_ids=gstin_node_ids,
                start_period=start_range,
                end_period=end_range,
                tenant=TENANT,
                report_level="GSTIN",
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

        # 2c. Log still-stuck statuses; proceed anyway.
        issue_rows = [s for s in snapshot
                      if s.get("downloadStatus") in _NEEDS_USER_ACTION]
        if issue_rows:
            partials_csv = cfg.state_dir / "partial-items.csv"
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
                    "this GSTIN has expired. Open ClearGST -> Electronic "
                    "Liability Register page for this GSTIN -> 'Generate "
                    "OTP to connect', then re-run."
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

        # 3. Fetch RLS token (date-range mode — workflow == TAX_LIABILITY_LEDGER_REPORT)
        logger.info("[{}/{}] Step 3/6: fetch RLS token", gstin, fy)
        rls_token = api.fetch_rls_token(
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
            from_date=start_range,
            to_date=end_range,
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
            start_range=start_range,
            end_range=end_range,
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

        # 6. Download into downloads/gst/<GSTIN>/FY-<FY>/Electronic-Liability-Register/
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
        raise
    except Exception as e:  # noqa: BLE001 — record + continue with next combo
        logger.exception("[{}/{}/{}] FAILED: {}",
                         gstin, fy, REPORT_TYPE, e)
        manifest.mark_failed(
            gstin, fy, REPORT_TYPE,
            error=f"{type(e).__name__}: {e}",
        )
