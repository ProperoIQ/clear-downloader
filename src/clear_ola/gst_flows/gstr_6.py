"""GSTR-6 Report — two Excel files per (GSTIN x FY).

GSTR-6 is the monthly Input Service Distributor (ISD) return filed at the
government portal. ClearGST exposes it as a per-GSTIN report and produces
TWO downloadable views per (GSTIN, FY):

  - Summary     (18 cols: section-by-section ISD return summary with
                 distribution / redistribution breakdowns)
  - Eligibility (4 cols: eligibleITC / ineligibleITC by return period)

Both views share the same pull and RLS token — only Steps 4–6 (trigger
export, wait, download) run per variant. Each is tracked as its own
manifest row ("GSTR-6-Summary" / "GSTR-6-Eligibility") so a partial
failure can be retried per-variant. Mirrors GSTR-9-8A.

Period encoding is monthly (same as GSTR-6A): `fy_periods(fy)` returns
['042025', ..., '032026'] for FY 2025-26, truncated to the current month
for the in-progress FY.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import date
from importlib import resources
from pathlib import Path

from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired, GstinNode
from clear_ola.config import AppConfig, GstinConfig, fy_periods
from clear_ola.gst_manifest import GstManifest
from clear_ola.partials import log_partial_items


REPORT_TYPE = "GSTR-6"
TENANT = "GSTR6_REPORTS"
RLS_WORKFLOW = "GSTR6_REPORTS"

# (manifest/filename suffix, statement-template filename in package data)
EXPORT_VARIANTS: list[tuple[str, str]] = [
    ("Summary",     "gstr_6_summary_statement.json"),
    ("Eligibility", "gstr_6_eligibility_statement.json"),
]


def _load_statement_template(name: str) -> dict:
    """Load a verbatim export-trigger payload captured from the HAR."""
    with resources.files("clear_ola.gst_flows").joinpath(name).open(
        "r", encoding="utf-8",
    ) as f:
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
    """Substitute the captured GSTR-6 template with this run's GSTIN/period.

    Leaves the SELECT statement, exportName, templateType, and template-id
    untouched — those identify the variant (Summary vs Eligibility) and
    are baked into the JSON file.
    """
    start_range = periods[0]   # e.g. "042025"
    end_range = periods[-1]    # e.g. "032026"

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": gstin,
        "reportPeriod": _periods_to_human(periods),
    }
    p["filename"] = f"GSTR6_{gstin}_{start_range}-{end_range}"

    for callback_key in ("onStart", "onFinish"):
        md = p[callback_key]["metadata"]
        md["orgId"] = workspace_id
        md["workspaceId"] = workspace_id
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
    """Return {gstin_str: GstinNode} for every GSTIN under the workspace."""
    return {n.gstin: n for n in api.user_gstins()}


def _variant_report_type(suffix: str) -> str:
    return f"{REPORT_TYPE}-{suffix}"


def run(
    api: ClearAPI,
    cfg: AppConfig,
    manifest: GstManifest,
) -> None:
    """Process every (GSTIN x FY) in `cfg.gstins`. Downloads both Summary
    and Eligibility variants per combo. Skips variants already marked done.
    Records progress + errors to the manifest after every step.
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

    # Variant-level idempotency: skip the whole combo only if ALL are done.
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
            "[{}/{}] Step 1/6: refresh GSTR-6 data for this GSTIN "
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
        for suffix in pending_suffixes:
            manifest.set_pull_id(gstin, fy, _variant_report_type(suffix), pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # 2. Wait for pull
        logger.info("[{}/{}] Step 2/6: wait for the data refresh", gstin, fy)
        snapshot = api.wait_for_pull(
            gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=TENANT,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )

        statuses = {s.get("downloadStatus") for s in snapshot}

        if statuses == {"NOT_APPLICABLE"}:
            # GSTIN wasn't ISD-registered (or had no inward distributions)
            # during this FY. Settle as no_data and move on.
            logger.info(
                "[{}/{}] No data: GSTIN returned NOT_APPLICABLE for this FY "
                "(likely not ISD-registered then). Marking as no_data.",
                gstin, fy,
            )
            for suffix in pending_suffixes:
                manifest.mark_no_data(gstin, fy, _variant_report_type(suffix))
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
                    "this GSTIN has expired. Open ClearGST -> GSTR-6 page "
                    "for this GSTIN -> 'Generate OTP to connect', then "
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
            periods,
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # 4-6. Loop over pending variants (Summary + Eligibility). One
        # export trigger + wait + download per variant. Files share the
        # parent folder downloads/gst/<GSTIN>/FY-<FY>/GSTR-6/ but are
        # renamed so the variants don't collide — Clear returns the same
        # generic filename for both exports.
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
                periods=periods,
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

            ext = "".join(Path(ready.file_name).suffixes) or ".xlsx.zip"
            dest_name = f"GSTR6_{gstin}_{start_period}-{end_period}_{suffix}{ext}"
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
        for suffix in pending_suffixes:
            variant_rt = _variant_report_type(suffix)
            if not manifest.is_done(gstin, fy, variant_rt):
                manifest.mark_failed(
                    gstin, fy, variant_rt,
                    error=f"{type(e).__name__}: {e}",
                )
