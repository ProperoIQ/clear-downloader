"""2B vs PR Reconciliation — PAN-based recon of GSTR-2B vs Purchase Register.

Compares ITC available per GSTR-2B (pulled from GSTN) against the Purchase
Register (PR) the taxpayer uploads into Clear. Clear's UI path is
`/reconciliation/idt/2bVsPr`; the match type is `MAX_ITC_2B_PR`. Output: one
PAN-level XLSX per FY.

This report uses a DIFFERENT backend from every other report in this package.
Instead of the data-pull -> data-browser export pipeline, it drives Clear's
recon "matching task" backend (`/api/recon/ultimatum/public/...`). The flow,
confirmed from discovery/app.clear.in.har__2B vs PR Reconciliation*.har, is:

  1. POST matching/v2/trigger            -> taskId (kicks off the match; also
                                            fetches the 2B/PR data server-side)
  2. GET  matching/current (poll)        -> wait for taskStatus DATAVIEW_READY
  3. POST workbench/report/v1/generate   -> reportId
  4. GET  workbench/report/v1/{id}/download (poll) -> presigned XLSX url
  5. download

Both the PR and 2B sides of the match use the SAME period range (per the HAR).
There is no separate data-pull and no RLS/export-trigger step, so the
partial-OTP machinery used by the data-pull reports doesn't apply here.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date

from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired
from clear_ola.config import AppConfig, PanConfig, fy_periods
from clear_ola.manifest import Manifest


REPORT_TYPE = "2B-vs-PR-Reconciliation"
MATCH_TYPE = "MAX_ITC_2B_PR"
# GST began Jul 2017, so the earliest valid return period is 072017. This
# clips FY 2017-18's Apr-Jun (no GST data) and is a no-op for every later FY.
# Format: "MMYYYY".
MIN_START_PERIOD = "072017"


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
    """Process every (PAN x FY) in the config for the 2B-vs-PR reconciliation.

    Skips combos already marked done. One PAN-level Excel is produced per FY.
    """
    logger.info("Indexing GSTINs from workspace...")
    by_pan = _index_gstins_by_pan(api)
    logger.info(
        "Found {} PAN(s), {} GSTIN(s) total",
        len(by_pan), sum(len(v) for v in by_pan.values()),
    )

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
            "Generating PAN-level 2B-vs-PR reconciliation per FY.",
            pan_cfg.pan, pan_cfg.business_name, len(gstins),
        )

        for fy in pan_cfg.fys:
            _run_one(
                api=api, cfg=cfg, manifest=manifest,
                pan_cfg=pan_cfg, gstins=gstins, fy=fy,
            )


def _run_one(
    *,
    api: ClearAPI,
    cfg: AppConfig,
    manifest: Manifest,
    pan_cfg: PanConfig,
    gstins: list,
    fy: str,
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
            "[{}/{}] period range {}..{} (PR and 2B sides identical)",
            pan, fy, start_rp, end_rp,
        )

        pan_node_id = gstins[0].pan_node_id
        gstin_node_ids = [g.gstin_node_id for g in gstins]
        business_name = pan_cfg.business_name

        # Step 1/4: trigger the match (also fetches the 2B/PR data server-side).
        logger.info("[{}/{}] Step 1/4: trigger 2B-vs-PR match", pan, fy)
        task_id = api.recon_matching_trigger(
            pan_node_id=pan_node_id,
            pan=pan,
            start_rp=start_rp,
            end_rp=end_rp,
        )
        manifest.set_pull_id(pan, fy, REPORT_TYPE, task_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 2/4: wait until the match is computed and the data view is ready.
        logger.info("[{}/{}] Step 2/4: wait for match (DATAVIEW_READY)", pan, fy)
        api.wait_for_recon_matching(
            pan_node_id=pan_node_id,
            task_id=task_id,
            poll_seconds=cfg.poll_seconds_pull,
            timeout_seconds=cfg.poll_timeout_pull_seconds,
        )
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 3/4: request the Excel report for the ready task.
        logger.info("[{}/{}] Step 3/4: generate recon report", pan, fy)
        report_id = api.recon_report_generate(
            task_id=task_id,
            pan_node_id=pan_node_id,
            pan=pan,
            business_name=business_name,
            start_rp=start_rp,
            end_rp=end_rp,
        )
        manifest.set_export_id(pan, fy, REPORT_TYPE, report_id)
        time.sleep(cfg.inter_call_delay_seconds)

        # Step 4/4: wait for the report file, then download it.
        logger.info("[{}/{}] Step 4/4: wait for report file", pan, fy)
        ready = api.wait_for_recon_report(
            report_id,
            task_id=task_id,
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
