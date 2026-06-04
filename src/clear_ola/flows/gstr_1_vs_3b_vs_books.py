"""GSTR-1 vs 3B vs Books Report — Clear's reconciliation report.

Compares outward supplies declared in GSTR-1 vs GSTR-3B vs the taxpayer's
Books (sales register held in Clear's Books module). Clear's UI slug is
`panG3bvs1vsBooks`. Output: one PAN-level XLSX per FY.

Differs from the gstr_1/2a/2b flows: there is NO separate data-pull step.
Clear reuses its already-cached GSTR-1 + GSTR-3B data; the report is
computed server-side from that cache. If the user hasn't recently run
`download --report GSTR-1` and `--report GSTR-3B`, this report will use
Clear's last cached pull — we surface a WARNING but do not block.

The Books column is sourced internally by Clear from its Books / Sales
Register module. No upload is required from this tool. If books aren't
loaded into Clear, that column will be blank/zero — a domain limitation,
not a tool bug.

Captured POST body from discovery/app.clear.in.har around line 27860.
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


REPORT_TYPE = "GSTR-1-vs-3B-vs-Books"
# RLS workflow string — taken verbatim from the HAR's fetch-token call for
# this report (discovery/app.clear.in.har line 60691 and 3 more). Earlier
# guesses (`G1_VS_3B_BOOKS_REPORTS` with `_REPORTS` suffix, then
# `GSTR1_REPORTS` as a fallback) both produced HTTP 500 on /export/trigger:
# Clear's export endpoint validates that the RLS token was issued under the
# matching workflow scope, and a GSTR-1-scoped token is rejected for this
# slug with a generic "Unknown error occurred."
RLS_WORKFLOW = "G1_VS_3B_VS_BOOKS"
# Tenant for the data-pull (pull/v2/trigger + pull/v3/status). Verified
# against the fresh PAN-based HAR (entry 92) — Clear's UI auto-issues this
# pull BEFORE the RLS fetch to load cached GSTR-1+GSTR-3B data into the
# reconciliation cube. Skipping it makes the export trigger return a
# valid-shape-but-empty 17,357-byte XLSX regardless of how correct the
# template payloads are.
PULL_TENANT = "GSTRG1_VS_3B_VS_BOOKS_REPORTS"
MIN_FY = "2017-18"  # GST regime started Jul 2017; GSTR-1 introduced same.

# Earliest valid period for this report. Clear's panG3bvs1vsBooks endpoint
# rejects pre-GST months (Apr/May/Jun 2017) with a generic 500 — confirmed
# by HAR comparison: the captured UI request used startRange=072017 for FY
# 2017-18 even though the full FY starts in April. fy_periods returns the
# full April-onwards range; we clip it here so the first period sent is
# never earlier than this. Format: "MMYYYY".
MIN_START_PERIOD = "072017"

# How stale upstream GSTR-1 / GSTR-3B data can be before we warn. The
# report itself doesn't enforce this — Clear renders against whatever it
# has cached — but a fresh-enough warning helps users avoid surprises.
_STALE_DAYS = 7


def _load_statement_template() -> dict:
    """Load the verbatim export-trigger payload captured for panG3bvs1vsBooks."""
    with resources.files("clear_ola.flows").joinpath(
        "gstr_1_vs_3b_vs_books_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_preflight_template() -> dict:
    """Load the verbatim 'G1 vs 3B' (no Books) export-trigger payload.

    The panG3bvs1vsBooks page in Clear's UI auto-issues this call first
    (captured at discovery/app.clear.in.har:579) to materialize the
    reconciliation cube in Clear's server-side cache. Replaying only the
    vs-Books call without this preflight makes /export/trigger 500 with
    "Unknown error occurred." Both calls use the same RLS token.
    """
    with resources.files("clear_ola.flows").joinpath(
        "gstr_1_vs_3b_vs_books_preflight_statement.json"
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
    start_range = periods[0]   # e.g. "042024"
    end_range = periods[-1]    # e.g. "032025"
    filename_base = (
        f"PAN_GSTR1_vs_3B_vs_Books_Report_{pan}_{start_range}-{end_range}"
    )

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "companyName": business_name,
        "gstin": pan,  # Clear calls the PAN "gstin" here — intentional, see gstr_1.py
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
        # md["reportType"] stays "panG3bvs1vsBooks" — that's the slug.
        # md["filename"] stays "PAN GSTR1 vs 3B vs Books Report (XLSX)" — UI label.

    return p


def _build_preflight_payload(
    *,
    template: dict,
    pan: str,
    business_name: str,
    workspace_id: str,
    periods: list[str],
) -> dict:
    """Substitute per-(PAN, FY) fields into the preflight ('G1 vs 3B', no
    Books) export template. Same shape as _build_export_payload but with
    call #1's filename pattern.

    The double 'PAN_PAN_' prefix and the lowercase 'vs_3b' in the filename
    are verbatim from the HAR — they look like a typo in Clear's frontend
    but Clear's backend may key off them, so we replicate exactly.
    """
    start_range = periods[0]
    end_range = periods[-1]
    filename_base = (
        f"PAN_PAN_GSTR1_vs_3b_Report_{pan}_{start_range}-{end_range}"
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
        # md["filename"] stays "PAN GSTR1 vs 3B Report (XLSX)" — no "vs Books".

    return p


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _periods_to_human(periods: list[str]) -> str:
    """`['042026', '052026']` -> `'Apr 2026 - May 2026'`."""
    first, last = periods[0], periods[-1]
    fm, fy_ = int(first[:2]), first[2:]
    lm, ly_ = int(last[:2]), last[2:]
    return f"{_MONTHS[fm]} {fy_} - {_MONTHS[lm]} {ly_}"


def _index_gstins_by_pan(api: ClearAPI) -> dict[str, list]:
    """Return {pan: [GstinNode, ...]} for every PAN under the workspace."""
    nodes = api.user_gstins()
    by_pan: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_pan[n.pan].append(n)
    return dict(by_pan)


def _warn_if_upstream_stale(
    manifest: Manifest, pan: str, fy: str,
) -> None:
    """Log a WARNING if GSTR-1 or GSTR-3B-Combined are missing or stale for
    this (PAN, FY). Does not block — Clear renders against whatever it has
    cached, and the user may have already pulled via the UI.
    """
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
    """Process every (PAN x FY) in the config for the GSTR-1 vs 3B vs Books
    reconciliation. Skips combos already marked done, and short-circuits FYs
    before 2017-18 (when neither GSTR-1 nor 3B existed).
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
            "Generating PAN-level GSTR-1 vs 3B vs Books reconciliation per FY.",
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

        # Clip periods earlier than MIN_START_PERIOD. Only affects FY 2017-18
        # (Apr/May/Jun 2017 are pre-GST and break the vs-Books template).
        # Period strings ("MMYYYY") aren't lexicographically orderable — flip
        # to ("YYYYMM") for the comparison.
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

        # Step 0: freshness check — non-blocking warning if upstream is stale.
        _warn_if_upstream_stale(manifest, pan, fy)

        # Step 0.5: trigger Clear's reconciliation data pull. Verified from
        # the fresh HAR (entry 92 of
        # discovery/app.clear.in.har_pan GSTr-1 vs 3B  vs 3B vs Books Report.har)
        # that Clear's UI calls this BEFORE the RLS fetch. Without it the
        # export trigger returns a 17,357-byte empty-shell XLSX even when
        # the template payloads are byte-identical to the HAR. The pull uses
        # OPTIMIZED_PULL + USE_EXISTING_DATA, so it does NOT pull fresh data
        # from GSTN — it just primes Clear's recon cube from whatever
        # GSTR-1 / GSTR-3B data Clear already has cached for this PAN+FY.
        # If Clear has no cached data for this PAN+FY, the export will
        # still produce an empty file — the user needs to run
        # `--report GSTR-1` and `--report GSTR-3B` first (the warnings
        # above flag that).
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

        # Clear's panG3bvs1vsBooks endpoint parses `reportType=` from the
        # Referer header's query string and 500s with "Unknown error
        # occurred." if it's anything else. The generic session-default
        # referer (`/gst/reports?section=ALL`) is what GSTR-2A/2B/1 use and
        # tolerate, but this report requires its own slug. We don't have
        # the jobId / localStorageKey Clear's UI generates client-side, but
        # empirically the reportType + pan + panNodeId is what the backend
        # validates.
        report_referer = (
            "https://app.clear.in/gst/reports/v2"
            f"?reportType=panG3bvs1vsBooks"
            f"&activeBusiness={urllib.parse.quote(pan_cfg.business_name)}"
            f"&pan={pan}"
            f"&panNodeId={pan_node_id}"
            f"&timePeriodType=FISCAL_YEAR"
            f"&section=REPORT_VIEW"
        )

        # Per-call header overrides for the panG3bvs1vsBooks export trigger.
        # Iteration 3: forensic HAR diff vs our session defaults shows three
        # candidate differences. Send all of them; bisect later if we still
        # 500. A value of None suppresses a session-default header.
        #   - x-ct-source: None  — HAR does not send this; our session adds
        #     "GST_REPORTS" by default. Older endpoints tolerate it; this one
        #     may reject unknown header values.
        #   - baggage + sentry-trace — Sentry's distributed-tracing headers.
        #     Clear's edge may validate them as a proof-of-origin check. We
        #     don't run a real Sentry SDK; the values below are plausible
        #     placeholders matching the format of the HAR's pair.
        #   - accept-language + priority — cosmetic alignment with HAR; cheap
        #     to include while bisecting.
        sentry_trace_id = secrets.token_hex(16)   # 32 hex chars
        sentry_span_id = secrets.token_hex(8)     # 16 hex chars
        sentry_public_key = "607fd3b42fc9b74117f75a6900f89b00"  # from HAR
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

        # Step 1: Fetch RLS token
        logger.info("[{}/{}] Step 1/4: fetch RLS token", pan, fy)
        rls_token = api.fetch_rls_token(
            periods,
            gstin_node_ids=gstin_node_ids,
            workflow=RLS_WORKFLOW,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 2: Preflight — materialize reconciliation cube in Clear's
        # cache. Without this, the vs-Books export trigger 500s with
        # "Unknown error occurred." Clear's UI auto-issues this when the
        # page opens; we replay it here. The returned export id is
        # intentionally discarded — we don't poll or download this report.
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
        # Clear's UI waits ~13s here; the recon cube must materialize on
        # Clear's side before the real export trigger fires, otherwise the
        # downloaded XLSX is a valid-shape empty shell (see HAR
        # discovery/app.clear.in.har_GSTR-1 vs 3B vs Books Report.har).
        time.sleep(cfg.wait_after_priming_seconds)

        # Step 3: Trigger export (the actual vs-Books file)
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

        # Step 4: Wait for export + download
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
