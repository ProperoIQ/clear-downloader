"""GSTR-1+1A vs 3B vs Books Report — Clear's GSTR-1A-aware reconciliation.

Same family as `gstr_1_vs_3b_vs_books` but Clear treats it as a separate
endpoint with its own slug, RLS workflow, pull tenant, statement template
id and filename pattern. Differences vs the parent (`panG3bvs1vsBooks`)
are all documented inline below; everything else (preflight + real export
ordering, MMYYYY periods, PAN-level output) is identical.

For FYs ending before Aug 2024 — when GSTR-1A was introduced — Clear still
accepts this report and returns a valid XLSX, but the 1A column ends up
empty. That's a domain limitation, not a tool bug. We keep MIN_FY at
2017-18 (matching the parent flow) so users can still pull historic 1+1A
files without us second-guessing the regulator.

Captured POST bodies from discovery/app.clear.in.har__GSTR1+1A vs 3B vs
Books Report_1.har — preflight at entry #280, real export at entry #302.
"""

from __future__ import annotations

import copy
import json
import secrets
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from importlib import resources

from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired
from clear_ola.config import (
    AppConfig,
    PanConfig,
    fy_periods,
)
from clear_ola.manifest import Manifest


REPORT_TYPE = "GSTR-1-1A-vs-3B-vs-Books"
# RLS workflow + pull tenant collapse to the same string for this report.
# Verified against the HAR's fetch-token URL (entry #259) and the
# pull/v2/trigger body (entry #172) — both carry
# "GSTR1_1A_VS_3B_VS_BOOKS_REPORTS". This differs from the parent
# panG3bvs1vsBooks flow where workflow and tenant are two different strings.
RLS_WORKFLOW = "GSTR1_1A_VS_3B_VS_BOOKS_REPORTS"
PULL_TENANT = "GSTR1_1A_VS_3B_VS_BOOKS_REPORTS"
MIN_FY = "2017-18"  # GST regime started Jul 2017; mirrors the parent flow.

# Earliest valid period for this report. Clear's API rejects pre-GST months
# (Apr/May/Jun 2017) with a generic 500 — the HAR's captured run started at
# 072017 even though the FY 2017-18 picker showed the full April-onwards
# range. fy_periods returns the full range; we clip here so the first
# period sent is never earlier than this. Format: "MMYYYY".
MIN_START_PERIOD = "072017"

# How stale upstream GSTR-1 / GSTR-3B data can be before we warn.
_STALE_DAYS = 7


def _load_statement_template() -> dict:
    """Load the verbatim G1+1A-vs-3B-vs-Books real-export payload."""
    with resources.files("clear_ola.flows").joinpath(
        "gstr_1_1a_vs_3b_vs_books_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_preflight_template() -> dict:
    """Load the verbatim G1+1A-vs-3B (no Books) preflight payload.

    Clear's UI auto-issues this first to materialize the reconciliation cube
    in the server-side cache before the vs-Books export trigger fires.
    Skipping it would make /export/trigger 500 with "Unknown error
    occurred." Both calls reuse the same RLS token.
    """
    with resources.files("clear_ola.flows").joinpath(
        "gstr_1_1a_vs_3b_vs_books_preflight_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_export_payload(
    *,
    template: dict,
    pan: str,
    business_name: str,
    workspace_id: str,
    periods: list[str],
) -> dict:
    """Substitute PAN/FY/workspace-specific fields into the real-export template."""
    start_range = periods[0]
    end_range = periods[-1]
    filename_base = (
        f"PAN_GSTR1_1A_vs_3B_vs_Books_Report_{pan}_{start_range}-{end_range}"
    )

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": pan,
        "reportPeriod": _periods_to_human(periods),
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
        # md["reportType"] stays "G1_1Avs3BvsBooks" — that's the slug.
        # md["filename"] stays "GSTR1+1A vs 3B vs Books Report (XLSX)" — UI label.

    return p


def _build_preflight_payload(
    *,
    template: dict,
    pan: str,
    business_name: str,
    workspace_id: str,
    periods: list[str],
) -> dict:
    """Substitute per-(PAN, FY) fields into the preflight ("G1+1A vs 3B",
    no Books) export template.

    Filename quirk: single "PAN_" prefix and lowercase "3b" — verbatim from
    HAR entry #280. The parent panG3bvs1vsBooks preflight has a *double*
    "PAN_PAN_" prefix; Clear appears to have "fixed" that typo for this
    newer endpoint. Replicate exactly — Clear's backend may key off it.
    """
    start_range = periods[0]
    end_range = periods[-1]
    filename_base = (
        f"PAN_GSTR1_1A_vs_3b_Report_{pan}_{start_range}-{end_range}"
    )

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": pan,
        "reportPeriod": _periods_to_human(periods),
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
        # md["filename"] stays "GSTR1+1A vs 3B Report (XLSX)" — no "vs Books".

    return p


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _periods_to_human(periods: list[str]) -> str:
    first, last = periods[0], periods[-1]
    fm, fy_ = int(first[:2]), first[2:]
    lm, ly_ = int(last[:2]), last[2:]
    return f"{_MONTHS[fm]} {fy_} - {_MONTHS[lm]} {ly_}"


def _index_gstins_by_pan(api: ClearAPI) -> dict[str, list]:
    nodes = api.user_gstins()
    by_pan: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_pan[n.pan].append(n)
    return dict(by_pan)


def _warn_if_upstream_stale(
    manifest: Manifest, pan: str, fy: str,
) -> None:
    threshold = datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)
    for upstream in ("GSTR-1", "GSTR-3B-Combined"):
        row = manifest.get(pan, fy, upstream)
        if not row or row.get("status") != "done":
            logger.warning(
                "[{}/{}] {} not marked 'done' in manifest — this reconciliation "
                "will use Clear's last cached pull. Run "
                "`download --report {} --pan {} --fy {}` first for fresh data.",
                pan, fy, upstream, upstream, pan, fy,
            )
            continue
        completed_at = row.get("completed_at")
        if not completed_at:
            continue
        try:
            done_at = datetime.fromisoformat(completed_at)
        except ValueError:
            continue
        if done_at < threshold:
            logger.warning(
                "[{}/{}] {} was last downloaded on {} (>{} days ago). "
                "Reconciliation will reflect Clear's data as of that pull.",
                pan, fy, upstream, completed_at, _STALE_DAYS,
            )


def run(
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
) -> None:
    logger.info("Indexing GSTINs from workspace...")
    by_pan = _index_gstins_by_pan(api)
    logger.info("Found {} PAN(s), {} GSTIN(s) total",
                len(by_pan), sum(len(v) for v in by_pan.values()))

    template = _load_statement_template()
    preflight_template = _load_preflight_template()

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
            "Generating PAN-level GSTR-1+1A vs 3B vs Books reconciliation per FY.",
            pan_cfg.pan, pan_cfg.business_name, len(gstins),
        )

        for fy in pan_cfg.fys:
            _run_one(
                api=api, cfg=cfg, manifest=manifest,
                pan_cfg=pan_cfg, gstins=gstins, fy=fy,
                template=template, preflight_template=preflight_template,
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
    preflight_template: dict,
) -> None:
    pan = pan_cfg.pan

    if fy < MIN_FY:
        if manifest.is_done(pan, fy, REPORT_TYPE):
            return
        logger.info(
            "[{}/{}/{}] FY predates GST regime; recording no_data and skipping.",
            pan, fy, REPORT_TYPE,
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
        periods = fy_periods(fy, as_of=today)
        if len(periods) < 12:
            logger.info(
                "[{}/{}] Today is {} — FY isn't complete yet. "
                "Requesting {} period(s): {}..{}",
                pan, fy, today.isoformat(),
                len(periods), periods[0], periods[-1],
            )

        def _yyyymm(p: str) -> str:
            return p[2:] + p[:2]
        clipped = [p for p in periods if _yyyymm(p) >= _yyyymm(MIN_START_PERIOD)]
        if not clipped:
            logger.info(
                "[{}/{}/{}] All FY periods predate GST regime; recording no_data.",
                pan, fy, REPORT_TYPE,
            )
            manifest.mark_no_data(pan, fy, REPORT_TYPE, gstins_seen=0)
            return
        if len(clipped) != len(periods):
            logger.info(
                "[{}/{}] Clipped {} pre-GST period(s); requesting {}..{} ({} period(s))",
                pan, fy, len(periods) - len(clipped),
                clipped[0], clipped[-1], len(clipped),
            )
        periods = clipped

        _warn_if_upstream_stale(manifest, pan, fy)

        logger.info(
            "[{}/{}] Step 0.5/5: priming reconciliation data pull "
            "(tenant={})",
            pan, fy, PULL_TENANT,
        )
        start_period, end_period = periods[0], periods[-1]
        api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=PULL_TENANT,
            gis_download_behaviour="USE_EXISTING_DATA",
            report_level="PAN",
        )
        snapshot = api.wait_for_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=PULL_TENANT,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )
        not_downloaded = sum(
            1 for s in snapshot
            if s.get("downloadStatus") in ("NOT_DOWNLOADED", "DOWNLOADED_PARTIALLY")
        )
        if not_downloaded:
            logger.warning(
                "[{}/{}] {} of {} GSTIN(s) report NOT_DOWNLOADED / "
                "DOWNLOADED_PARTIALLY for the recon pull — Clear's cache "
                "for those is empty or partial. The reconciliation XLSX "
                "may be missing rows. Run `download --report GSTR-1` and "
                "`download --report GSTR-3B` for this PAN+FY first if you "
                "want a complete file.",
                pan, fy, not_downloaded, len(snapshot),
            )
        time.sleep(cfg.inter_call_delay_seconds)

        report_referer = (
            "https://app.clear.in/gst/reports/v2"
            f"?reportType=G1_1Avs3BvsBooks"
            f"&activeBusiness={urllib.parse.quote(pan_cfg.business_name)}"
            f"&pan={pan}"
            f"&panNodeId={pan_node_id}"
            f"&timePeriodType=FISCAL_YEAR"
            f"&section=REPORT_VIEW"
        )

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

        logger.info("[{}/{}] Step 1/4: fetch RLS token", pan, fy)
        rls_token = api.fetch_rls_token(
            periods,
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        logger.info(
            "[{}/{}] Step 2/4: preflight (priming Clear's reconciliation cube)",
            pan, fy,
        )
        preflight_payload = _build_preflight_payload(
            template=preflight_template,
            pan=pan,
            business_name=pan_cfg.business_name,
            workspace_id=cfg.workspace_id,
            periods=periods,
        )
        preflight_export_id = api.trigger_export(
            preflight_payload, rls_token=rls_token,
            referer_override=report_referer,
            header_overrides=header_overrides,
        )
        logger.info(
            "[{}/{}] preflight export id {} — discarded (cache priming only)",
            pan, fy, preflight_export_id,
        )
        time.sleep(cfg.wait_after_priming_seconds)

        logger.info(
            "[{}/{}] Step 3/4: trigger PAN-level reconciliation export",
            pan, fy,
        )
        payload = _build_export_payload(
            template=template,
            pan=pan,
            business_name=pan_cfg.business_name,
            workspace_id=cfg.workspace_id,
            periods=periods,
        )
        export_id = api.trigger_export(
            payload, rls_token=rls_token,
            referer_override=report_referer,
            header_overrides=header_overrides,
        )
        manifest.set_export_id(pan, fy, REPORT_TYPE, export_id)
        time.sleep(cfg.inter_call_delay_seconds)

        logger.info("[{}/{}] Step 4/4: wait for export", pan, fy)
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
        if bytes_written < 50 * 1024:
            logger.warning(
                "[{}/{}/{}] Downloaded file is suspiciously small "
                "({} bytes < 50 KB) — Clear may have served an empty-shell "
                "XLSX. Open the file to confirm.",
                pan, fy, REPORT_TYPE, bytes_written,
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
