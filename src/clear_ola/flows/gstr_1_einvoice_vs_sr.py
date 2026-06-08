"""E-Invoice Data (GSTR-1) vs Sales Register (SR) — PAN-based reconciliation.

Compares the taxpayer's E-Invoice (GSTR-1) outward documents against the Sales
Register (SR) held in Clear's Books module. Clear's UI path is
`/reconciliation/idt/G1EInvVsSr`; the match type is `MAX_ITC_G1EInv_SR`.
Output: one PAN-level XLSX (a summary / "Action Report") per FY.

This flow is a HYBRID of the two existing recon patterns, confirmed from
discovery/app.clear.in.har__GSTR1 vs SR.har:

  1. POST matching/v2/trigger            -> requestId (kicks off the match;
                                            also fetches the G1/SR data
                                            server-side)
  2. GET  matching/current (poll)        -> wait for taskStatus DATAVIEW_READY,
                                            then read the matching `taskId`
  3. POST recon rls/fetch-token          -> short-lived RLS token (tenant IDT,
       (?tableType=RECON_G1_VS_SR_MATCHING) keyed by the matching taskId)
  4. POST data-browser export/trigger    -> exportId (tenant IDT)
  5. GET  data-browser export/download   -> presigned XLSX url (tenant IDT)
  6. download

So the MATCH runs on the recon/ultimatum backend (like 2A/2B-vs-PR), but the
report is produced by the data-browser export pipeline under tenant IDT (like
GSTR-1-vs-3B-vs-Books) — NOT the workbench/report/generate path the *-vs-PR
flows use. Both the SR (lhs) and G1 (rhs) sides use the same period range.

Two correctness nuances honored here (see api.py):
  - The trigger's `requestId` differs from the matching/current `taskId`, so we
    poll without an id check and read the real `taskId` from the ready payload
    (that id is what the RLS-token / export calls need).
  - We accept only the DATAVIEW_READY whose `returnPeriodRange` matches the
    requested [start_rp, end_rp] (a staleness guard).

The SR column is sourced internally by Clear from its Books / Sales Register
module — no upload is required from this tool. If books aren't loaded into
Clear, that side will be blank/zero (a domain limitation, not a tool bug).

The data-browser template id is hardcoded in
`gstr_1_einvoice_vs_sr_statement.json` (same convention as the other
`*_statement.json` files). If Clear ever rotates it, fetch it from
`GET /api/clear/data-browser/public/v2/config?data_type=RECON_G1_VS_SR_MATCHING`
`&component_type=VIEW&idempotent-key=G1_VS_SR_RECON_SUMMARY` and read `.id`.
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
from clear_ola.config import AppConfig, PanConfig, fy_periods
from clear_ola.manifest import Manifest


REPORT_TYPE = "EInvoice-vs-SR"
MATCH_TYPE = "MAX_ITC_G1EInv_SR"
RLS_TABLE_TYPE = "RECON_G1_VS_SR_MATCHING"
# Tenant for the recon RLS token + data-browser export/status calls.
EXPORT_TENANT = "IDT"
# GST began Jul 2017, so the earliest valid return period is 072017. This
# clips FY 2017-18's Apr-Jun (no GST data) and is a no-op for every later FY.
# Format: "MMYYYY".
MIN_START_PERIOD = "072017"


def _load_statement_template() -> dict:
    """Load the verbatim export-trigger payload captured for G1-vs-SR."""
    with resources.files("clear_ola.flows").joinpath(
        "gstr_1_einvoice_vs_sr_statement.json"
    ).open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_export_payload(
    *,
    template: dict,
    pan: str,
    business_name: str,
    workspace_id: str,
    start_rp: str,
    end_rp: str,
) -> dict:
    """Substitute PAN/FY/workspace-specific fields into the captured template.

    Only the callback metadata (org/workspace ids) and staticRowData change per
    (PAN, FY). The `statement` block stays untouched — Clear resolves the
    $TEMPLATE reference server-side.
    """
    rp_range = f"{start_rp}-{end_rp}"

    p = copy.deepcopy(template)
    p["staticRowData"] = {
        "business_name": business_name,
        "pan_number": pan,
        "rp_range_lhs": rp_range,
        "rp_range_rhs": rp_range,
        "date_range_lhs": "",
        "date_range_rhs": "",
    }
    for callback_key in ("onStart", "onFinish"):
        md = p[callback_key]["metadata"]
        md["orgId"] = workspace_id
        md["workspaceId"] = workspace_id

    return p


def _index_gstins_by_pan(api: ClearAPI) -> dict[str, list]:
    """Return {pan: [GstinNode, ...]} for every PAN under the workspace."""
    nodes = api.user_gstins()
    by_pan: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_pan[n.pan].append(n)
    return dict(by_pan)


def _yyyymm(p: str) -> str:
    """`'042018'` -> `'201804'` so MMYYYY period strings sort chronologically."""
    return p[2:] + p[:2]


def _fy_period_range(fy: str, *, as_of: date) -> tuple[str, str] | None:
    """Map an FY to its (start_rp, end_rp) MMYYYY range for this report.

    Uses the shared `fy_periods` helper (which truncates an in-progress current
    FY at `as_of`), then clips anything before MIN_START_PERIOD. Returns None if
    the whole FY predates GST inception (nothing to download).
    """
    periods = fy_periods(fy, as_of=as_of)
    clipped = [p for p in periods if _yyyymm(p) >= _yyyymm(MIN_START_PERIOD)]
    if not clipped:
        return None
    return clipped[0], clipped[-1]


def run(api: ClearAPI, cfg: AppConfig, manifest: Manifest) -> None:
    """Process every (PAN x FY) in the config for the E-Invoice-vs-SR recon.

    Skips combos already marked done. One PAN-level Excel is produced per FY.
    """
    logger.info("Indexing GSTINs from workspace...")
    by_pan = _index_gstins_by_pan(api)
    logger.info(
        "Found {} PAN(s), {} GSTIN(s) total",
        len(by_pan), sum(len(v) for v in by_pan.values()),
    )

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
            "Generating PAN-level E-Invoice-vs-SR reconciliation per FY.",
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
        today = date.today()
        rng = _fy_period_range(fy, as_of=today)
        if rng is None:
            logger.info(
                "[{}/{}/{}] FY predates GST inception (Jul 2017); "
                "recording no_data and skipping.", pan, fy, REPORT_TYPE,
            )
            manifest.mark_no_data(pan, fy, REPORT_TYPE, gstins_seen=0)
            return
        start_rp, end_rp = rng
        logger.info(
            "[{}/{}] period range {}..{} (SR and G1 sides identical)",
            pan, fy, start_rp, end_rp,
        )

        pan_node_id = gstins[0].pan_node_id
        gstin_node_ids = [g.gstin_node_id for g in gstins]
        business_name = pan_cfg.business_name

        # Step 1/4: trigger the match (also fetches G1/SR data server-side).
        logger.info("[{}/{}] Step 1/4: trigger E-Invoice-vs-SR match", pan, fy)
        request_id = api.recon_matching_trigger(
            pan_node_id=pan_node_id,
            pan=pan,
            start_rp=start_rp,
            end_rp=end_rp,
            match_type=MATCH_TYPE,
            lhs_label="SR Return Period",
            rhs_label="G1 Return Period",
            # Clear allows only one recon match in progress per PAN; on the
            # "Duplicate Request" lock, wait it out and retry instead of failing.
            wait_if_busy=True,
            busy_poll_seconds=cfg.poll_seconds_pull,
            busy_timeout_seconds=cfg.poll_timeout_pull_seconds,
        )
        logger.info("[{}/{}] trigger requestId={}", pan, fy, request_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 2/4: wait until the match is ready, then read the matching
        # taskId from the payload (it differs from the trigger requestId).
        logger.info("[{}/{}] Step 2/4: wait for match (DATAVIEW_READY)", pan, fy)
        payload = api.wait_for_recon_matching(
            pan_node_id=pan_node_id,
            task_id=None,
            match_type=MATCH_TYPE,
            expected_range=(start_rp, end_rp),
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )
        task_id = payload.get("taskId")
        if not task_id:
            raise RuntimeError(
                f"matching/current reached DATAVIEW_READY but exposed no "
                f"taskId: {payload!r}"
            )
        logger.info("[{}/{}] matching taskId={}", pan, fy, task_id)
        manifest.set_pull_id(pan, fy, REPORT_TYPE, task_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 3/4: fetch the recon RLS token (tenant IDT, keyed by taskId).
        logger.info("[{}/{}] Step 3/4: fetch recon RLS token + export", pan, fy)
        rls_token = api.fetch_recon_rls_token(
            table_type=RLS_TABLE_TYPE,
            task_id=task_id,
            tenant=EXPORT_TENANT,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # Trigger the data-browser export under tenant IDT.
        export_payload = _build_export_payload(
            template=template,
            pan=pan,
            business_name=business_name,
            workspace_id=cfg.workspace_id,
            start_rp=start_rp,
            end_rp=end_rp,
        )
        export_id = api.trigger_export(
            export_payload,
            rls_token=rls_token,
            header_overrides={"x-tenant-name": EXPORT_TENANT},
        )
        manifest.set_export_id(pan, fy, REPORT_TYPE, export_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 4/4: wait for the export file, then download it.
        logger.info("[{}/{}] Step 4/4: wait for export", pan, fy)
        ready = api.wait_for_export(
            export_id,
            tenant_name=EXPORT_TENANT,
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
                "XLSX. Open the file to confirm. If empty, the SR/G1 data "
                "for this PAN+FY may not be loaded in Clear, or the "
                "data-browser cube needs priming before the export.",
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
