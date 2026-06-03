"""GSTR-9 Table 8A Report — two Excel files per (GSTIN x FY).

GSTR-9-8A is the auto-drafted ITC statement attached to the annual return
GSTR-9. ClearGST exposes it as a per-GSTIN report (like GSTR-6A), but its
UI produces TWO downloadable files per (GSTIN, FY):

  - Detail  (exportName="view_9_8a",    Clear's S3 file: View.xlsx.zip)
  - Summary (exportName="summary_9_8a", Clear's S3 file: Summary.xlsx.zip)

The pull + RLS token flow is shared across both — only Steps 4-6 (trigger
export, wait, download) run twice. Each variant is tracked as its own
manifest row ("GSTR-9-8A-Detail" / "GSTR-9-8A-Summary") so a partial
failure can be retried per-variant. This mirrors how the multi-variant
PAN-based flows (GSTR-3B) handle their sub-tables.

Period encoding is annual: GSTR-9 uses a single month "03<endyear>"
(March of the FY end year) as both startRange and endRange. FY 2024-25
becomes "032025", not the 12 monthly periods that GSTR-6A uses.
"""

from __future__ import annotations

import copy
import json
import time
from importlib import resources
from pathlib import Path

from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired, GstinNode
from clear_ola.config import AppConfig, GstinConfig
from clear_ola.gst_manifest import GstManifest
from clear_ola.partials import log_partial_items


REPORT_TYPE = "GSTR-9-8A"
TENANT = "GSTR9_8A_REPORTS"
RLS_WORKFLOW = "GSTR9_8A_REPORTS"

# (manifest/filename suffix, statement-template filename in package data)
EXPORT_VARIANTS: list[tuple[str, str]] = [
    ("Detail",  "gstr_9_8a_view_statement.json"),
    ("Summary", "gstr_9_8a_summary_statement.json"),
]


def _load_statement_template(name: str) -> dict:
    """Load a verbatim export-trigger payload captured from the HAR."""
    with resources.files("clear_ola.gst_flows").joinpath(name).open(
        "r", encoding="utf-8",
    ) as f:
        return json.load(f)


def _fy_to_period(fy: str) -> str:
    """GSTR-9 is annual: the report period is March of the FY end year.
    '2024-25' -> '032025'."""
    end_yr = 2000 + int(fy.split("-")[1])
    return f"03{end_yr}"


def _fy_human(fy: str) -> str:
    """'2024-25' -> 'Mar 2025 - Mar 2025' (matches ClearGST UI)."""
    end_yr = 2000 + int(fy.split("-")[1])
    return f"Mar {end_yr} - Mar {end_yr}"


def _build_export_payload(
    *,
    template: dict,
    gstin: str,
    business_name: str,
    workspace_id: str,
    period: str,
    fy: str,
) -> dict:
    """Substitute the captured 9-8A template with this run's GSTIN/business/period.

    Leaves the SELECT statement, exportName, templateType, and template-id
    untouched — those identify the variant (Detail vs Summary) and are
    baked into the JSON file.
    """
    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": gstin,
        "reportPeriod": _fy_human(fy),
    }
    p["filename"] = f"GSTR98A_{gstin}_{period}-{period}"

    for callback_key in ("onStart", "onFinish"):
        md = p[callback_key]["metadata"]
        md["orgId"] = workspace_id
        md["workspaceId"] = workspace_id
        md["nodeName"] = gstin
        md["startRange"] = period
        md["endRange"] = period
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
    """Return {gstin_str: GstinNode} for every GSTIN under the workspace."""
    return {n.gstin: n for n in api.user_gstins()}


def _variant_report_type(suffix: str) -> str:
    return f"{REPORT_TYPE}-{suffix}"


def run(
    api: ClearAPI,
    cfg: AppConfig,
    manifest: GstManifest,
) -> None:
    """Process every (GSTIN x FY) in `cfg.gstins`. Downloads both Detail and
    Summary variants per combo. Skips variants already marked done. Records
    progress + errors to the manifest after every step.
    """
    logger.info("Indexing GSTINs from workspace...")
    by_gstin = _index_gstins(api)
    logger.info("Workspace has {} GSTIN(s) total", len(by_gstin))

    templates = {
        suffix: _load_statement_template(name)
        for suffix, name in EXPORT_VARIANTS
    }

    for g_cfg in cfg.gstins:
        node = by_gstin.get(g_cfg.gstin)
        if node is None:
            logger.error(
                "GSTIN {} ({}) not found in workspace — skipping all its FYs.",
                g_cfg.gstin, g_cfg.business_name,
            )
            for fy in g_cfg.fys:
                for suffix, _ in EXPORT_VARIANTS:
                    rt = _variant_report_type(suffix)
                    manifest.mark_started(g_cfg.gstin, fy, rt)
                    manifest.mark_failed(
                        g_cfg.gstin, fy, rt,
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
                fy=fy, templates=templates,
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
    templates: dict[str, dict],
) -> None:
    gstin = g_cfg.gstin

    # Variant-level idempotency: skip the whole combo only if BOTH are done.
    variant_done = {
        suffix: manifest.is_done(gstin, fy, _variant_report_type(suffix))
        for suffix, _ in EXPORT_VARIANTS
    }
    if all(variant_done.values()):
        logger.info("[{} / {} / {}] all variants already done — skipping",
                    gstin, fy, REPORT_TYPE)
        return

    logger.info("=" * 70)
    pending_suffixes = [s for s, done in variant_done.items() if not done]
    logger.info("[{} / {} / {}] starting (variants: {})",
                gstin, fy, REPORT_TYPE, pending_suffixes)
    for suffix in pending_suffixes:
        manifest.mark_started(gstin, fy, _variant_report_type(suffix))

    try:
        gstin_node_ids = [node.gstin_node_id]
        period = _fy_to_period(fy)

        # 1. Trigger pull. GSTR-9 uses a single annual period.
        logger.info(
            "[{}/{}] Step 1/6: refresh GSTR-9-8A data for this GSTIN "
            "(period {}) — prep step, no file produced here.",
            gstin, fy, period,
        )
        pull_id = api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=period,
            end_period=period,
            tenant=TENANT,
            report_level="GSTIN",
        )
        for suffix in pending_suffixes:
            manifest.set_pull_id(gstin, fy, _variant_report_type(suffix), pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # 2. Wait for pull
        logger.info("[{}/{}] Step 2/6: wait for the data refresh", gstin, fy)
        snapshot = api.wait_for_pull(
            gstin_node_ids,
            start_period=period,
            end_period=period,
            tenant=TENANT,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )

        statuses = {s.get("downloadStatus") for s in snapshot}

        if statuses == {"NOT_APPLICABLE"}:
            logger.info(
                "[{}/{}] No data: GSTIN returned NOT_APPLICABLE for this FY "
                "(no GSTR-9 filed / no inward supplies). Marking as no_data.",
                gstin, fy,
            )
            for suffix in pending_suffixes:
                manifest.mark_no_data(gstin, fy, _variant_report_type(suffix))
            return

        # 2b. Retry once if partial
        if _any_partial(snapshot):
            logger.warning(
                "[{}/{}] Settled as DOWNLOADED_PARTIALLY. Re-triggering with "
                "gisDownloadBehaviour=DOWNLOAD_COMPLETE_DATA.",
                gstin, fy,
            )
            api.trigger_pull(
                gstin_node_ids=gstin_node_ids,
                start_period=period,
                end_period=period,
                tenant=TENANT,
                report_level="GSTIN",
                gis_download_behaviour="DOWNLOAD_COMPLETE_DATA",
            )
            time.sleep(cfg.inter_call_delay_seconds)
            snapshot = api.wait_for_pull(
                gstin_node_ids,
                start_period=period,
                end_period=period,
                tenant=TENANT,
                poll_seconds=cfg.poll_seconds_pull,
                timeout_seconds=cfg.poll_timeout_pull_seconds,
            )

        # 2c. Log still-stuck statuses; proceed anyway
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
                    "this GSTIN has expired. Open ClearGST -> GSTR-9 / 8A "
                    "page for this GSTIN -> 'Generate OTP to connect', then "
                    "re-run."
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

        # 3. Fetch RLS token (shared across both variants)
        logger.info("[{}/{}] Step 3/6: fetch RLS token", gstin, fy)
        rls_token = api.fetch_rls_token(
            [period],
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # 4-6. Loop over pending variants (Detail + Summary). One export
        # trigger + wait + download per variant. Files share the parent
        # folder downloads/gst/<GSTIN>/FY-<FY>/GSTR-9-8A/ but are renamed
        # so Detail and Summary don't collide on Clear's generic
        # "View.xlsx.zip" / "Summary.xlsx.zip" S3 names.
        for suffix in pending_suffixes:
            variant_rt = _variant_report_type(suffix)
            template = templates[suffix]

            logger.info(
                "[{}/{}/{}] Step 4/6: trigger {} export",
                gstin, fy, variant_rt, suffix,
            )
            payload = _build_export_payload(
                template=template,
                gstin=gstin,
                business_name=biz_name,
                workspace_id=cfg.workspace_id,
                period=period,
                fy=fy,
            )
            export_id = api.trigger_export(payload, rls_token=rls_token)
            manifest.set_export_id(gstin, fy, variant_rt, export_id)
            time.sleep(cfg.inter_call_delay_seconds)

            logger.info(
                "[{}/{}/{}] Step 5/6: wait for {} export",
                gstin, fy, variant_rt, suffix,
            )
            ready = api.wait_for_export(
                export_id,
                poll_seconds=cfg.poll_seconds_export,
                timeout_seconds=cfg.poll_timeout_export_seconds,
            )

            # Rename: Clear's S3 returns generic "View.xlsx.zip" /
            # "Summary.xlsx.zip"; embed GSTIN+period+variant so each file is
            # self-describing on disk.
            ext = "".join(Path(ready.file_name).suffixes) or ".xlsx.zip"
            dest_name = f"GSTR98A_{gstin}_{period}-{period}_{suffix}{ext}"
            dest = (cfg.downloads_dir / "gst" / gstin
                    / f"FY-{fy}" / REPORT_TYPE / dest_name)

            logger.info(
                "[{}/{}/{}] Step 6/6: download {} -> {}",
                gstin, fy, variant_rt, ready.file_name, dest_name,
            )
            bytes_written = api.download_file(
                ready.pre_signed_url, dest,
                gstin_node_ids=gstin_node_ids,
            )

            manifest.mark_done(
                gstin, fy, variant_rt,
                file_path=str(dest), file_bytes=bytes_written,
            )
            logger.success("[{}/{}/{}] DONE: {} ({} bytes)",
                           gstin, fy, variant_rt, dest, bytes_written)
            time.sleep(cfg.inter_call_delay_seconds)

    except ClearSessionExpired:
        raise
    except Exception as e:  # noqa: BLE001 — record + continue with next combo
        logger.exception("[{}/{}/{}] FAILED: {}",
                         gstin, fy, REPORT_TYPE, e)
        # Mark all still-pending variants as failed (the done ones stay done).
        for suffix in pending_suffixes:
            variant_rt = _variant_report_type(suffix)
            if not manifest.is_done(gstin, fy, variant_rt):
                manifest.mark_failed(
                    gstin, fy, variant_rt,
                    error=f"{type(e).__name__}: {e}",
                )
