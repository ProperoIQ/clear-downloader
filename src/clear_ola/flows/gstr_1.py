"""GSTR-1 Reports — two exports per (PAN × FY) from one data pull.

Mirrors the GSTR-2A / GSTR-2B flows for the shared half (pull + wait + RLS
token), then — matching the latest ClearTax UI capture
(`discovery/app.clear.in.har__GSTR1_updated.har`) — produces TWO Excel exports
off that single pull + token:

  1. Detail    — `G1_report_section_level_summary` (38-column per-invoice rows,
                 template 673733c8…); the original `gstr_1_statement.json`.
  2. Aggregate — `G1_report_section_level_summary_aggregate` (10-column section
                 totals, template 673733e2…); `gstr_1_aggregate_statement.json`.

Each export is its own manifest row (`report_type` "GSTR-1" and
"GSTR-1-Aggregate") and lands in its own subfolder, so a failed variant retries
independently and the two identically-named Clear files don't collide on disk.

Empty-Excel fix: like gstr_8.py, we prime Clear's data-browser cube via
`/v2/query` (the same SELECT, `limit: 1000`) BEFORE each `export/trigger`. The
ClearTax UI always does this between the RLS-token fetch and the export; without
it Clear can serve a valid-shape-but-empty XLSX even though the data is present
in its own UI. See `api.run_data_browser_query` for the rationale.

Note on Clear's internal naming:
    The Detail export's `exportName` is `G1_report_section_level_summary` and the
    runtime filename starts with `PAN_GSTR1_Section Level Summary_`, even though
    the user-facing label is `"GSTR-1 Document Level Report"`. Both values are
    lifted verbatim from the HAR.
"""

from __future__ import annotations

import copy
import json
import time
from collections import defaultdict
from datetime import date
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


REPORT_TYPE_PREFIX = "GSTR-1"
TENANT = "GSTR1_REPORTS"
RLS_WORKFLOW = "GSTR1_REPORTS"
# GSTR-1 has existed since July 2017 (start of the GST regime). FY 2017-18's
# first 3 months (Apr-Jun 2017) pre-date GSTR-1, but Clear's pull endpoint
# handles those as NOT_APPLICABLE per-GSTIN/per-period (existing fallthrough).
# MIN_FY is effectively a no-op against the current config but kept for
# structural symmetry with gstr_2b.py.
MIN_FY = "2017-18"

# The two exports the ClearTax UI produces per (PAN × FY) off a single data
# pull + RLS token. `report_type` is the manifest key AND the on-disk subfolder.
# "GSTR-1" (detail) keeps its historical slug so already-downloaded detail
# reports stay "done" across this change. Each export's differing template id,
# columns, limit, and exportName live entirely inside its statement JSON.
VARIANTS: dict[str, dict[str, str]] = {
    "detail": {
        "report_type": "GSTR-1",
        "template_file": "gstr_1_statement.json",
        "label": "Detail",
    },
    "aggregate": {
        "report_type": "GSTR-1-Aggregate",
        "template_file": "gstr_1_aggregate_statement.json",
        "label": "Aggregate",
    },
}


def _load_statement_template(filename: str) -> dict:
    """Load a verbatim export-trigger payload stored as package data under
    `clear_ola/flows/`."""
    with resources.files("clear_ola.flows").joinpath(filename).open(
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
    """Take a captured GSTR-1 statement and substitute PAN/FY/workspace-specific bits.

    `periods` should be the same (possibly truncated) list used for the pull —
    so the filename and metadata reflect the actual months requested.

    The `statement` block (columns, template id, limit, filters=null) is left
    untouched — Clear decides scope from headers + workspace context, and the
    differing `exportName` stays baked into each template file.
    """
    start_range = periods[0]   # e.g. "042024"
    end_range = periods[-1]    # e.g. "032025"
    # Clear's internal naming: the runtime filename has the literal string
    # "Section Level Summary" (with spaces) for both exports. Match the HAR
    # capture exactly — the two exports differ only by exportName/S3 prefix, so
    # we keep them apart on disk via the per-variant report_type subfolder.
    filename_base = f"PAN_GSTR1_Section Level Summary_{pan}_{start_range}-{end_range}"

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": pan,  # Clear calls the PAN "gstin" here — intentional
        "reportPeriod": _periods_to_human(periods),
    }
    p["filename"] = filename_base
    # exportName stays as the captured value — it's the S3 prefix Clear uses
    # for this export (detail vs aggregate).

    for callback_key in ("onStart", "onFinish"):
        md = p[callback_key]["metadata"]
        md["orgId"] = workspace_id
        md["workspaceId"] = workspace_id
        md["nodeName"] = pan
        md["startRange"] = start_range
        md["endRange"] = end_range
        md["activeBusiness"] = business_name
        # md["reportType"] stays "panGstr1" — that's what makes this GSTR-1.
        # md["filename"] stays "GSTR-1 Document Level Report" — UI label Clear
        # shows in its notifications tray for this report type.

    return p


def _build_query_payload(*, template: dict) -> dict:
    """Build the data-browser priming query body (the empty-Excel fix).

    Clear's UI calls `/api/clear/data-browser/public/v2/query` between the
    RLS-token fetch and the export trigger (see
    `discovery/app.clear.in.har__GSTR1_updated.har`). The body is just
    `{"statement": <select>}` — the same SELECT the export uses, but with
    `limit: 1000` (a pageable preview). The response is discarded; the call's
    only job is to materialize the result set on Clear's side so the
    subsequent export trigger reads from a populated cube instead of serving
    an empty-shell XLSX.
    """
    statement = copy.deepcopy(template["statement"])
    statement["limit"] = 1000
    return {"statement": statement}


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
    """Process every (PAN × FY) in the config for GSTR-1. Skips combos already
    marked done, and short-circuits FYs before 2017-18 (when GSTR-1 did not
    exist). Records progress + errors to the manifest after every step.

    A single data pull powers both exports (Detail + Aggregate) per (PAN, FY);
    each export is its own manifest row so failures retry independently.

    GSTINs that settle in NOT_DOWNLOADED or DOWNLOADED_PARTIALLY are logged to
    `state/partial-items.csv` and the export proceeds with whatever data is
    available — the same way NOT_APPLICABLE GSTINs are handled.
    """
    logger.info("Indexing GSTINs from workspace...")
    by_pan = _index_gstins_by_pan(api)
    logger.info("Found {} PAN(s), {} GSTIN(s) total",
                len(by_pan), sum(len(v) for v in by_pan.values()))

    templates = {
        vk: _load_statement_template(spec["template_file"])
        for vk, spec in VARIANTS.items()
    }

    for pan_cfg in cfg.pans:
        gstins = by_pan.get(pan_cfg.pan, [])
        if not gstins:
            logger.error("No GSTINs found for PAN {} ({}). Skipping all its FYs.",
                         pan_cfg.pan, pan_cfg.business_name)
            for fy in pan_cfg.fys:
                for spec in VARIANTS.values():
                    rt = spec["report_type"]
                    manifest.mark_started(pan_cfg.pan, fy, rt)
                    manifest.mark_failed(
                        pan_cfg.pan, fy, rt,
                        error=f"No GSTINs returned by user_gstins for PAN {pan_cfg.pan}",
                    )
            continue
        logger.info(
            "PAN {} ({}) has {} state-wise GSTIN(s) registered. "
            "We will refresh data from each (Clear's internal step), then "
            "produce TWO consolidated PAN-level Excel reports (Detail + "
            "Aggregate) per FY.",
            pan_cfg.pan, pan_cfg.business_name, len(gstins),
        )

        for fy in pan_cfg.fys:
            _run_one(
                api=api, cfg=cfg, manifest=manifest,
                pan_cfg=pan_cfg, gstins=gstins, fy=fy, templates=templates,
            )


def _run_one(
    *,
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
    pan_cfg: PanConfig,
    gstins: list,
    fy: str,
    templates: dict[str, dict],
) -> None:
    pan = pan_cfg.pan

    # GSTR-1 did not exist before Jul 2017 (FY 2017-18). MIN_FY is effectively
    # a no-op against the current config, but we keep the same structural guard
    # as gstr_2b.py. Within FY 2017-18 itself, the Apr-Jun 2017 months pre-date
    # GSTR-1 and Clear returns NOT_APPLICABLE for them per-GSTIN — handled by the
    # existing NOT_APPLICABLE fallthrough below. ("YYYY-YY" strings sort
    # lexicographically because YYYY is fixed-width.)
    if fy < MIN_FY:
        for spec in VARIANTS.values():
            rt = spec["report_type"]
            if manifest.is_done(pan, fy, rt):
                continue
            logger.info(
                "[{} / {} / {}] FY predates GSTR-1 (introduced Jul 2017); "
                "recording as no_data and skipping.", pan, fy, rt,
            )
            manifest.mark_started(pan, fy, rt)
            manifest.mark_no_data(pan, fy, rt, gstins_seen=0)
        return

    # Skip if every variant for this (PAN, FY) is already done.
    todo = [vk for vk, spec in VARIANTS.items()
            if not manifest.is_done(pan, fy, spec["report_type"])]
    if not todo:
        logger.info(
            "[{}/{}] both GSTR-1 exports already done — skipping", pan, fy,
        )
        return

    logger.info("=" * 70)
    logger.info("[{}/{}/{}] starting {} export(s): {}",
                pan, fy, REPORT_TYPE_PREFIX, len(todo),
                [VARIANTS[vk]["label"] for vk in todo])
    for vk in todo:
        manifest.mark_started(pan, fy, VARIANTS[vk]["report_type"])

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

        # 1. Trigger fresh pull from GSTN (shared across both exports).
        logger.info(
            "[{}/{}] Step 1/7: refresh GSTR-1 data for {} underlying GSTINs "
            "({}..{}) — prep step, no file is produced here.",
            pan, fy, len(gstin_node_ids), start_period, end_period,
        )
        pull_id = api.trigger_pull(
            gstin_node_ids=gstin_node_ids,
            start_period=start_period,
            end_period=end_period,
            tenant=TENANT,
        )
        for vk in todo:
            manifest.set_pull_id(pan, fy, VARIANTS[vk]["report_type"], pull_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # 2. Wait for pull to finish (all GSTINs DOWNLOADED)
        logger.info(
            "[{}/{}] Step 2/7: wait for the data refresh (Clear only reports "
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

        # 2a.i. Entire PAN is NOT_APPLICABLE for this FY → mark no_data for
        #       both exports and move on.
        if (not_applicable_count == len(snapshot) and len(snapshot) > 0
                and not _any_partial(snapshot)):
            logger.info(
                "[{}/{}] No data for this PAN x FY: all {} underlying GSTIN(s) "
                "returned NOT_APPLICABLE. Marking as no_data and moving on.",
                pan, fy, not_applicable_count,
            )
            for vk in todo:
                manifest.mark_no_data(
                    pan, fy, VARIANTS[vk]["report_type"],
                    gstins_seen=not_applicable_count,
                )
            return
        if not_applicable_count > 0:
            logger.info(
                "[{}/{}] {} of {} underlying GSTIN(s) returned NOT_APPLICABLE; "
                "the PAN-level Excels will only contain data from the {} GSTIN(s) "
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
        #     gap remains). Log to CSV against the prefix (so the OTP worklist
        #     groups both exports together — they share one pull), then proceed
        #     to export anyway — same as 2a handles partial NOT_APPLICABLE.
        issue_rows = [s for s in snapshot
                      if s.get("downloadStatus") in _NEEDS_USER_ACTION]
        if issue_rows:
            partials_csv = cfg.state_dir / "partial-items.csv"
            n_logged = log_partial_items(
                partials_csv,
                pan=pan,
                business_name=pan_cfg.business_name,
                fy=fy,
                report_type=REPORT_TYPE_PREFIX,
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

        # 3. Fetch RLS token (shared across both exports).
        logger.info("[{}/{}] Step 3/7: fetch RLS token", pan, fy)
        rls_token = api.fetch_rls_token(
            periods,
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # 4-7. Per-export: prime cube -> trigger -> wait -> download. Each
        #      export is wrapped in its own try/except so one failing export
        #      records mark_failed and the other still runs.
        for vk in todo:
            spec = VARIANTS[vk]
            rt = spec["report_type"]
            template = templates[vk]
            try:
                # 4. Prime Clear's data-browser cube via /v2/query (empty-Excel
                #    fix). The UI does this between the RLS-token fetch and the
                #    export trigger; without it Clear can serve an empty XLSX.
                logger.info(
                    "[{}/{}/{}] Step 4/7: prime data-browser cube via /v2/query",
                    pan, fy, rt,
                )
                query_payload = _build_query_payload(template=template)
                api.run_data_browser_query(query_payload, rls_token=rls_token)
                # The cube must materialize before the export trigger reads it.
                time.sleep(cfg.wait_after_priming_seconds)

                # 5. Trigger the Excel export.
                logger.info(
                    "[{}/{}/{}] Step 5/7: trigger the {} PAN-level Excel export",
                    pan, fy, rt, spec["label"],
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
                manifest.set_export_id(pan, fy, rt, export_id)
                time.sleep(cfg.inter_call_delay_seconds)

                # 6. Wait for export to be ready (taskStatus=SUCCESS).
                logger.info("[{}/{}/{}] Step 6/7: wait for export", pan, fy, rt)
                ready = api.wait_for_export(
                    export_id,
                    poll_seconds=cfg.poll_seconds_export,
                    timeout_seconds=cfg.poll_timeout_export_seconds,
                )

                # 7. Download the file. Both exports share Clear's filename, so
                #    the per-variant report_type subfolder keeps them apart.
                logger.info("[{}/{}/{}] Step 7/7: download {}",
                            pan, fy, rt, ready.file_name)
                dest = cfg.downloads_dir / pan / f"FY-{fy}" / rt / ready.file_name
                bytes_written = api.download_file(
                    ready.pre_signed_url, dest,
                    gstin_node_ids=gstin_node_ids,
                )

                manifest.mark_done(
                    pan, fy, rt,
                    file_path=str(dest), file_bytes=bytes_written,
                )
                logger.success("[{}/{}/{}] DONE: {} ({} bytes)",
                               pan, fy, rt, dest, bytes_written)
            except ClearSessionExpired:
                # Bail the whole flow; CLI prints a one-line message + exits.
                raise
            except Exception as e:  # noqa: BLE001 — record + continue
                logger.exception("[{}/{}/{}] FAILED: {}", pan, fy, rt, e)
                manifest.mark_failed(
                    pan, fy, rt, error=f"{type(e).__name__}: {e}",
                )
                # Continue with the other export — it can still succeed.

    except ClearSessionExpired:
        # Re-raise; CLI catches this and prints a one-line, actionable message
        # then exits non-zero. We don't continue to other PANs/FYs in that case.
        raise
    except Exception as e:  # noqa: BLE001 — record + continue
        # A failure in the SHARED steps (pull / pull-wait / token) fails every
        # not-yet-done export for this (PAN, FY).
        logger.exception("[{}/{}] FAILED at shared step: {}", pan, fy, e)
        for vk in todo:
            manifest.mark_failed(
                pan, fy, VARIANTS[vk]["report_type"],
                error=f"shared-pull: {type(e).__name__}: {e}",
            )
