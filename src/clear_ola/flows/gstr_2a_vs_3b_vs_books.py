"""GSTR-2A vs 3B vs Books Report — Clear's ITC-reconciliation report.

Compares ITC reflected per GSTR-2A vs ITC claimed per GSTR-3B vs the
taxpayer's Books (purchase register held in Clear's Books module).
Clear's UI slug is `panG3bvs2avsBooks`. Output: one PAN-level XLSX per FY.

Mirrors `gstr_2b_vs_3b_vs_books.py` step-for-step — same preflight-then-real
ordering, same partials handling, same header overrides. Differences from
the 2B variant, all verified from the HAR capture at
`d:/office/test-downloader/app.clear.in.har` (entries #72, #114, #126, #149):

  1. Pull tenant + RLS workflow are `GSTR2A_VS_3B_VS_BOOKS_REPORTS`.
  2. Referer slug is `reportType=panG3bvs2avsBooks`.
  3. No minimum FY: GSTR-2A is available since GST began (Jul 2017), so
     no `MIN_FY` / `MIN_START_PERIOD` clipping (unlike 2B, which starts at 072020).
  4. Preflight filename prefix is `PAN_PAN_GSTR2A_vs_3b_Report_...` (same
     quirky double-PAN_PAN_ + lowercase `3b` typo as the 2B preflight —
     replicated verbatim because Clear's backend may key off it).
  5. Real-export filename prefix is `PAN_GSTR2A_vs_3B_vs_Books_Report_...`.

Captured POST bodies live next to this file:
  - gstr_2a_vs_3b_vs_books_preflight_statement.json  (G2A vs 3B priming call)
  - gstr_2a_vs_3b_vs_books_statement.json            (G2A vs 3B vs Books real export)
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
from clear_ola.partials import log_partial_items


REPORT_TYPE = "GSTR-2A-vs-3B-vs-Books"
# Tenant used on the data-pull trigger / status calls and on the RLS-token
# fetch. Verified from HAR entry #72 (pull/v2/trigger body) and entry #114
# (rls/fetch-token URL `workFlow=` param).
TENANT = "GSTR2A_VS_3B_VS_BOOKS_REPORTS"
RLS_WORKFLOW = TENANT

# How stale upstream GSTR-3B data can be before we warn. This report's own
# pull step refreshes 2A only — 3B comes from Clear's existing 3B cache.
_STALE_DAYS = 7

_NEEDS_USER_ACTION = ("DOWNLOADED_PARTIALLY", "NOT_DOWNLOADED")


def _load_statement_template() -> dict:
    """Load the verbatim export-trigger payload captured for panG3bvs2avsBooks."""
    with resources.files("clear_ola.flows").joinpath(
        "gstr_2a_vs_3b_vs_books_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_preflight_template() -> dict:
    """Load the verbatim 'G2A vs 3B' (no Books) export-trigger payload.

    The panG3bvs2avsBooks page in Clear's UI auto-issues this call first to
    materialize the reconciliation cube in Clear's server-side cache. By
    analogy with the 2B-vs-3B-vs-Books and 1-vs-3B-vs-Books flows, replaying
    only the vs-Books call without this preflight is expected to 500 with
    "Unknown error occurred." Both calls use the same RLS token.
    """
    with resources.files("clear_ola.flows").joinpath(
        "gstr_2a_vs_3b_vs_books_preflight_statement.json"
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
    """Substitute PAN/FY/workspace-specific fields into the captured template.

    Only the metadata + staticRowData + filename change per (PAN, FY). The
    `statement` block stays untouched — Clear resolves the $TEMPLATE
    reference server-side.
    """
    start_range = periods[0]
    end_range = periods[-1]
    filename_base = (
        f"PAN_GSTR2A_vs_3B_vs_Books_Report_{pan}_{start_range}-{end_range}"
    )

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": pan,  # Clear calls the PAN "gstin" here — intentional
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

    return p


def _build_preflight_payload(
    *,
    template: dict,
    pan: str,
    business_name: str,
    workspace_id: str,
    periods: list[str],
) -> dict:
    """Substitute per-(PAN, FY) fields into the preflight ('G2A vs 3B', no
    Books) export template. Same shape as _build_export_payload but with
    call #1's filename pattern.

    The double 'PAN_PAN_' prefix and lowercase 'vs_3b' in the filename are
    verbatim from the HAR — they look like a typo in Clear's frontend but
    Clear's backend may key off them, so we replicate exactly. Same quirk
    as the G2B-vs-3B-vs-Books and G1-vs-3B-vs-Books preflights.
    """
    start_range = periods[0]
    end_range = periods[-1]
    filename_base = (
        f"PAN_PAN_GSTR2A_vs_3b_Report_{pan}_{start_range}-{end_range}"
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

    return p


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _periods_to_human(periods: list[str]) -> str:
    """`['042026', '052026']` -> `'Apr 2026 - May 2026'`."""
    first, last = periods[0], periods[-1]
    fm, fy_ = int(first[:2]), first[2:]
    lm, ly_ = int(last[:2]), last[2:]
    return f"{_MONTHS[fm]} {fy_} - {_MONTHS[lm]} {ly_}"


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
    """Return {pan: [GstinNode, ...]} for every PAN under the workspace."""
    nodes = api.user_gstins()
    by_pan: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_pan[n.pan].append(n)
    return dict(by_pan)


def _warn_if_upstream_3b_stale(
    manifest: Manifest, pan: str, fy: str,
) -> None:
    """Log a WARNING if GSTR-3B-Combined is missing or stale for this (PAN, FY).

    GSTR-2A itself is refreshed by this report's own pull step, but 3B comes
    from Clear's existing 3B cache (no separate pull on the vs-3B-vs-Books
    page). Reuses the same threshold and message shape as gstr_2b_vs_3b_vs_books.
    """
    threshold = datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)
    upstream = "GSTR-3B-Combined"
    row = manifest.get(pan, fy, upstream)
    if not row or row.get("status") != "done":
        logger.warning(
            "[{}/{}] {} not marked 'done' in manifest — this reconciliation "
            "will use Clear's last cached 3B pull. Run "
            "`download --report GSTR-3B --pan {} --fy {}` first for fresh data.",
            pan, fy, upstream, pan, fy,
        )
        return
    completed_at = row.get("completed_at")
    if not completed_at:
        return
    try:
        done_at = datetime.fromisoformat(completed_at)
    except ValueError:
        return
    if done_at < threshold:
        logger.warning(
            "[{}/{}] {} was last downloaded on {} (>{} days ago). "
            "Reconciliation will reflect Clear's 3B data as of that pull.",
            pan, fy, upstream, completed_at, _STALE_DAYS,
        )


def run(
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
) -> None:
    """Process every (PAN x FY) in the config for the GSTR-2A vs 3B vs Books
    reconciliation. Skips combos already marked done. No FY floor — GSTR-2A
    has existed since GST began (Jul 2017).

    GSTINs that settle in NOT_DOWNLOADED or DOWNLOADED_PARTIALLY are logged to
    `state/partial-items.csv` and the reconciliation proceeds with whatever 2A
    data is available — the same way NOT_APPLICABLE GSTINs are handled.
    """
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
            "Generating PAN-level GSTR-2A vs 3B vs Books reconciliation per FY.",
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
        start_period = periods[0]
        end_period = periods[-1]

        # Step 1: Trigger GSTR-2A pull (this report's data-pull step)
        logger.info(
            "[{}/{}] Step 1/6: refresh GSTR-2A data for {} underlying GSTINs "
            "({}..{}) under tenant {}",
            pan, fy, len(gstin_node_ids), start_period, end_period, TENANT,
        )
        pull_id = api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=TENANT,
        )
        manifest.set_pull_id(pan, fy, REPORT_TYPE, pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 2: Wait for pull
        logger.info(
            "[{}/{}] Step 2/6: wait for the 2A data refresh", pan, fy,
        )
        snapshot = api.wait_for_pull(
            gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=TENANT,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )

        # Step 2a: Categorize.
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
                "the PAN-level reconciliation will only contain data from the "
                "{} GSTIN(s) that did.",
                pan, fy, not_applicable_count, len(snapshot), downloaded_count,
            )

        # Step 2b: Retry partials with DOWNLOAD_COMPLETE_DATA (UI's "Download
        # all data again"). NOT_DOWNLOADED can't be fixed programmatically.
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

        # Step 2c: Unified partial/needs-OTP handling.
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
                    "page -> 'Generate OTP to connect GSTINs' -> enter OTP "
                    "for those states, then re-run."
                )
            if has_still_partial:
                hints.append(
                    "DOWNLOADED_PARTIALLY remained even after the force "
                    "re-download. Confirm with the GST team whether data "
                    "should exist for the listed (gstin, period)s."
                )
            hint = " ".join(hints)
            logger.warning(
                "[{}/{}] 2A pull settled with issues: {}. Appended {} row(s) "
                "to {}. Proceeding to export anyway; the reconciliation will "
                "reflect whatever 2A data is available, and rows for affected "
                "GSTIN(s) may be incomplete or absent. {}",
                pan, fy, issues, n_logged, partials_csv, hint,
            )
        else:
            logger.info(
                "[{}/{}] 2A pull settled cleanly. Continuing to export.",
                pan, fy,
            )
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 2d: non-blocking warning if 3B is stale (3B isn't refreshed by
        # this report's pull step; it comes from Clear's existing 3B cache).
        _warn_if_upstream_3b_stale(manifest, pan, fy)

        # Per-call header overrides for the panG3bvs2avsBooks export trigger.
        # Verified from HAR entries #126 and #149:
        #   - x-ct-source: None  — HAR does not send this; our session adds
        #     "GST_REPORTS" by default. This endpoint may reject it.
        #   - baggage + sentry-trace — Sentry distributed-tracing headers
        #     Clear's edge may validate as a proof-of-origin check.
        #   - accept-language + priority — cosmetic HAR alignment.
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

        # Clear's panG3bvs2avsBooks endpoint parses `reportType=` from the
        # Referer header's query string and 500s otherwise — same constraint
        # as panG3bvs2bvsBooks and panG3bvs1vsBooks.
        report_referer = (
            "https://app.clear.in/gst/reports/v2"
            f"?reportType=panG3bvs2avsBooks"
            f"&activeBusiness={urllib.parse.quote(pan_cfg.business_name)}"
            f"&pan={pan}"
            f"&panNodeId={pan_node_id}"
            f"&timePeriodType=FISCAL_YEAR"
            f"&section=REPORT_VIEW"
        )

        # Step 3: Fetch RLS token
        logger.info("[{}/{}] Step 3/6: fetch RLS token", pan, fy)
        rls_token = api.fetch_rls_token(
            periods,
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 4: Preflight — prime Clear's reconciliation cube. Without this
        # the vs-Books export is expected to 500 (by analogy with the
        # 2B-vs-3B-vs-Books and 1-vs-3B-vs-Books preflights). Discard the
        # returned export_id.
        logger.info(
            "[{}/{}] Step 4/6: preflight (priming Clear's reconciliation cube)",
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
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 5: Trigger the real vs-Books export
        logger.info(
            "[{}/{}] Step 5/6: trigger PAN-level reconciliation export",
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

        # Step 6: Wait + download
        logger.info("[{}/{}] Step 6/6: wait for export", pan, fy)
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
