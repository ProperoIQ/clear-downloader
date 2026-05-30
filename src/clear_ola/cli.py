"""Click-based CLI: `python -m clear_ola <command>`."""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import click
from loguru import logger

from clear_ola.api import ClearAPI, ClearSessionExpired
from clear_ola.config import AppConfig, PanConfig, recent_fys, validate_fy
from clear_ola.cookies import (
    DEFAULT_COOKIE_FILE,
    ChromeRunningError,
    load_clear_cookies,
)
from clear_ola.flows import (
    gstr_1,
    gstr_1_vs_3b_vs_books,
    gstr_2a,
    gstr_2b,
    gstr_2b_vs_3b_vs_books,
    gstr_3b,
    gstr_8,
)
from clear_ola.manifest import Manifest
from clear_ola.partials import build_otp_worklist
from clear_ola.status_report import build_status_report


def _project_root() -> Path:
    # Defaults assume we're invoked from project root (which contains config.yaml)
    return Path.cwd()


def _configure_logging(cfg: AppConfig) -> None:
    logs_dir = _project_root() / "logs"
    logs_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = logs_dir / f"run-{stamp}.log"
    logger.remove()
    logger.add(
        sys.stderr,
        format="<level>{level: <8}</level> <cyan>{time:HH:mm:ss}</cyan> {message}",
        level="INFO",
        colorize=True,
    )
    logger.add(
        log_file,
        format="{level: <8} {time:YYYY-MM-DD HH:mm:ss.SSS} {name}:{function}:{line} | {message}",
        level="DEBUG",
        encoding="utf-8",
    )
    logger.info("Log file: {}", log_file)


@click.group()
@click.option("--config", "config_path",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default="config.yaml", show_default=True,
              help="Path to config.yaml")
@click.pass_context
def cli(ctx: click.Context, config_path: Path) -> None:
    """clear-ola — download ClearGST reports via authenticated APIs."""
    cfg = AppConfig.load(config_path)
    _configure_logging(cfg)
    ctx.obj = cfg


@cli.command()
@click.option("--report", "report_choice",
              type=click.Choice(["GSTR-2A", "GSTR-2B", "GSTR-1", "GSTR-3B",
                                 "GSTR-8",
                                 "GSTR-1-vs-3B-vs-Books",
                                 "GSTR-2B-vs-3B-vs-Books"], case_sensitive=False),
              default="GSTR-2A", show_default=True,
              help="Which report flow to run (v1: GSTR-2A only)")
@click.option("--pan", "pan_filter", default=None,
              help="Process only this PAN (must be in config.yaml). Skips the picker.")
@click.option("--fy", "fy_filter", default=None,
              help="Limit to a specific FY for the selected PAN (e.g. 2025-26).")
@click.option("--all", "process_all", is_flag=True, default=False,
              help="Process every configured PAN x FY (skips the picker).")
@click.option("--variants", "variants_filter", default=None,
              help="GSTR-3B only: comma-separated subset of "
                   "{combined,filed,itc-offset,insights,pdf}. "
                   "Default: all 5. Ignored for other reports.")
@click.pass_obj
def download(
    cfg: AppConfig,
    report_choice: str,
    pan_filter: str | None,
    fy_filter: str | None,
    process_all: bool,
    variants_filter: str | None,
) -> None:
    """Download ClearGST reports. Interactive PAN picker by default.

    Use --pan to skip the picker for a one-shot download:
        python -m clear_ola download --pan AAGCP5410J
        python -m clear_ola download --pan AAGCP5410J --fy 2025-26

    Use --all to process everything in config.yaml in one go.
    """
    # Load cookies + hit user_gstins once upfront. Two reasons:
    #   1. Validate the session before we ask the user to confirm anything.
    #   2. Auto-fill / correct each PAN's `business_name` with Clear's
    #      authoritative value (required by the export trigger to match exactly).
    logger.info("Loading Chrome cookies from profile {!r}...", cfg.chrome_profile)
    try:
        cookies = load_clear_cookies(cfg.chrome_profile)
    except ChromeRunningError as e:
        click.echo(f"\n[CHROME IS OPEN] {e}\n", err=True)
        sys.exit(4)
    except RuntimeError as e:
        click.echo(f"\n[COOKIES ERROR] {e}\n", err=True)
        sys.exit(2)
    logger.info("Loaded {} cookies", len(cookies))

    api = ClearAPI(workspace_id=cfg.workspace_id, cookies=cookies)
    try:
        nodes = api.user_gstins()
    except ClearSessionExpired as e:
        click.echo(f"\n[SESSION EXPIRED] {e}\n", err=True)
        sys.exit(3)
    _enrich_pan_business_names(cfg, nodes)

    # Now resolve scope — picker sees the correct, Clear-canonical names.
    selected = _select_pans(cfg, pan_filter, fy_filter, process_all)
    if not selected:
        click.echo("Nothing to do — exiting.")
        return

    click.echo("\nThis run will process:")
    for p in selected:
        click.echo(f"  {p.pan}  {p.business_name}  FYs: {p.fys}")
    if not process_all and not pan_filter:
        click.confirm("\nProceed?", default=True, abort=True)

    manifest = Manifest(cfg.state_dir / "manifest.sqlite")
    orphans = manifest.recover_orphans()
    if orphans:
        logger.info(
            "Recovered {} orphan 'in_progress' row(s) from a previous "
            "interrupted run -> marked as 'failed' for retry.", orphans,
        )

    # Swap cfg.pans for the filtered set so gstr_2a.run only processes the selection.
    original_pans = cfg.pans
    cfg.pans = selected
    try:
        if report_choice.upper() == "GSTR-2A":
            gstr_2a.run(api, cfg, manifest)
        elif report_choice.upper() == "GSTR-2B":
            gstr_2b.run(api, cfg, manifest)
        elif report_choice.upper() == "GSTR-8":
            gstr_8.run(api, cfg, manifest)
        elif report_choice.upper() == "GSTR-1":
            gstr_1.run(api, cfg, manifest)
        elif report_choice.upper() == "GSTR-3B":
            try:
                gstr_3b.run(
                    api, cfg, manifest,
                    variants_filter=variants_filter,
                )
            except ValueError as e:
                # Raised by parse_variants_filter on an unknown --variants key.
                click.echo(f"\n[ERROR] {e}\n", err=True)
                sys.exit(2)
        elif report_choice.upper() == "GSTR-1-VS-3B-VS-BOOKS":
            gstr_1_vs_3b_vs_books.run(api, cfg, manifest)
        elif report_choice.upper() == "GSTR-2B-VS-3B-VS-BOOKS":
            gstr_2b_vs_3b_vs_books.run(api, cfg, manifest)
        else:
            click.echo(f"Report {report_choice!r} not implemented yet.", err=True)
            sys.exit(2)
    except ClearSessionExpired as e:
        click.echo(f"\n[SESSION EXPIRED] {e}\n", err=True)
        sys.exit(3)
    finally:
        cfg.pans = original_pans

    _print_summary(manifest)


def _enrich_pan_business_names(cfg: AppConfig, nodes: list) -> None:
    """Replace each PAN's `business_name` with Clear's authoritative value.

    Why: Clear's export trigger requires `staticRowData.companyName` /
    `metadata.activeBusiness` to match *exactly* what its `user_gstins` API
    returns. Having the user retype it in config.yaml is just a typo risk;
    we have the real value in our hand from the user_gstins call we just made.

    - PAN missing from the workspace → warn (likely typo in config or wrong
      workspace_id); leave business_name as-is so the picker still works.
    - PAN found but config has no business_name → silently fill it in.
    - PAN found and config name matches → silent.
    - PAN found and config name differs → warn once and overwrite.
    """
    pan_to_business: dict[str, str] = {}
    for n in nodes:
        pan_to_business.setdefault(n.pan, n.business_name)

    for p in cfg.pans:
        clear_name = pan_to_business.get(p.pan)
        if clear_name is None:
            click.echo(
                f"  [warn] PAN {p.pan} is not in your Clear workspace "
                f"(workspace_id={cfg.workspace_id}). Either a typo or wrong "
                f"workspace. Will skip its GSTIN lookup downstream.",
                err=True,
            )
            continue
        if not p.business_name:
            p.business_name = clear_name
        elif p.business_name != clear_name:
            logger.warning(
                "PAN {}: config.yaml has business_name={!r}; Clear's "
                "authoritative value is {!r}. Using Clear's.",
                p.pan, p.business_name, clear_name,
            )
            p.business_name = clear_name


def _select_pans(
    cfg: AppConfig,
    pan_filter: str | None,
    fy_filter: str | None,
    process_all: bool,
) -> list[PanConfig]:
    """Resolve which (PAN, FYs) to process for this run."""
    if process_all:
        if pan_filter or fy_filter:
            click.echo("Note: --all overrides --pan / --fy.")
        return cfg.pans

    # ---- Step 1: select the PAN ----
    if not cfg.pans:
        click.echo("config.yaml has no PANs configured. Add some under `pans:`.", err=True)
        sys.exit(2)

    if pan_filter:
        pan_filter = pan_filter.strip().upper()
        match = next((p for p in cfg.pans if p.pan == pan_filter), None)
        if not match:
            click.echo(
                f"\n[ERROR] PAN {pan_filter!r} is not configured in config.yaml.\n"
                f"  Configured PANs: {[p.pan for p in cfg.pans]}\n"
                f"  To see all PANs in your Clear workspace, run:\n"
                f"      python -m clear_ola pans\n"
                f"  Then add the PAN entry to config.yaml under `pans:`.",
                err=True,
            )
            sys.exit(2)
        selected_pan: PanConfig | None = match
    elif len(cfg.pans) == 1:
        selected_pan = cfg.pans[0]
    else:
        selected_pan = _pick_pan_interactive(cfg.pans)
        if selected_pan is None:
            # User picked "all"
            return cfg.pans

    # ---- Step 2: select the FY for that PAN ----
    if fy_filter:
        fy_filter = fy_filter.strip()
        try:
            validate_fy(fy_filter)
        except ValueError as e:
            click.echo(f"\n[ERROR] {e}", err=True)
            sys.exit(2)
        if fy_filter not in selected_pan.fys:
            click.echo(
                f"Note: FY {fy_filter} is not in config.yaml for {selected_pan.pan} "
                f"(configured: {selected_pan.fys}). Using it anyway."
            )
        return [PanConfig(pan=selected_pan.pan,
                          business_name=selected_pan.business_name,
                          fys=[fy_filter])]

    # No --fy: interactive FY picker (always — even if only one FY is configured)
    return [_pick_fy_interactive(selected_pan)]


def _pick_pan_interactive(pans: list[PanConfig]) -> PanConfig | None:
    """Numbered menu of configured PANs. Returns the chosen PanConfig, or None
    if the user picked '(all)'."""
    click.echo("\nConfigured PANs:")
    for i, p in enumerate(pans, 1):
        click.echo(f"  [{i}] {p.pan}  {p.business_name}  FYs: {p.fys}")
    click.echo(f"  [{len(pans) + 1}] (all)")
    choice = click.prompt(
        "Pick one", type=click.IntRange(1, len(pans) + 1),
    )
    if choice == len(pans) + 1:
        return None
    return pans[choice - 1]


def _pick_fy_interactive(p: PanConfig) -> PanConfig:
    """Numbered FY menu: configured FYs (marked) + the last 5 standard FYs +
    an '(all configured)' option (if multiple) + 'Enter custom FY...'.

    Returns a PanConfig with the chosen FY(s) — a new instance scoped to the
    user's selection, not the original config entry.
    """
    # Build the option list: configured first (preserving order), then recent
    # standard FYs that aren't already shown.
    configured = list(p.fys)
    recents = recent_fys(5)
    seen: set[str] = set()
    options: list[str] = []
    for fy in configured + recents:
        if fy not in seen:
            seen.add(fy)
            options.append(fy)

    click.echo(f"\nFYs available for {p.pan} ({p.business_name}):")
    for i, fy in enumerate(options, 1):
        marker = "  (in config.yaml)" if fy in p.fys else ""
        click.echo(f"  [{i}] {fy}{marker}")
    all_choice: int | None = None
    custom_choice: int
    next_idx = len(options) + 1
    if len(configured) > 1:
        all_choice = next_idx
        click.echo(f"  [{all_choice}] (all configured FYs: {configured})")
        next_idx += 1
    custom_choice = next_idx
    click.echo(f"  [{custom_choice}] Enter custom FY...")

    choice = click.prompt(
        "Pick one", type=click.IntRange(1, custom_choice),
    )
    if all_choice is not None and choice == all_choice:
        return p
    if choice == custom_choice:
        while True:
            custom = click.prompt("Enter FY (e.g. 2024-25)", type=str).strip()
            try:
                validate_fy(custom)
            except ValueError as e:
                click.echo(f"  Invalid: {e}")
                continue
            return PanConfig(pan=p.pan, business_name=p.business_name, fys=[custom])
    return PanConfig(pan=p.pan, business_name=p.business_name,
                     fys=[options[choice - 1]])


@cli.command("cookies-import")
def cookies_import() -> None:
    """Print the step-by-step Cookie-Editor export workflow."""
    target = (Path.cwd() / DEFAULT_COOKIE_FILE).resolve()
    click.echo(f"""
Cookie-Editor manual export (one-time setup; re-do only when cookies expire)

Why: Chrome v127+ uses App-Bound Encryption that blocks userland libraries
from decrypting cookies. The cleanest workaround is exporting them yourself.

Steps:

1. Open Chrome (any profile that is logged into ClearGST).
2. Install the "Cookie-Editor" extension from the Chrome Web Store:
     https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm
3. Visit https://app.clear.in/ (make sure you see your logged-in dashboard).
4. Click the Cookie-Editor extension icon in the toolbar.
5. Click the "Export" button (bottom of the popup).
6. Choose "Export as JSON".
   (Cookie-Editor copies the JSON to your clipboard.)
7. Paste it into this file:

     {target}

   Create the parent folder if needed; the file should be a JSON array.

8. Re-run:  python -m clear_ola auth-check

When ClearGST eventually logs you out, repeat steps 3-7.
""")


@cli.command("auth-check")
@click.pass_obj
def auth_check(cfg: AppConfig) -> None:
    """Verify we can read Chrome cookies and hit Clear's API as your user.

    Does NOT trigger any pull or export — purely read-only. Good first thing
    to run after editing config.yaml."""
    logger.info("Loading Chrome cookies from profile {!r}...", cfg.chrome_profile)
    try:
        cookies = load_clear_cookies(cfg.chrome_profile)
    except ChromeRunningError as e:
        click.echo(f"\n[CHROME IS OPEN] {e}\n", err=True)
        sys.exit(4)
    except RuntimeError as e:
        click.echo(f"\n[COOKIES ERROR] {e}\n", err=True)
        sys.exit(2)
    click.echo(f"OK: loaded {len(cookies)} cookies from profile {cfg.chrome_profile!r}")

    api = ClearAPI(workspace_id=cfg.workspace_id, cookies=cookies)
    logger.info("Calling user_gstins() to verify session...")
    try:
        nodes = api.user_gstins()
    except ClearSessionExpired as e:
        click.echo(f"\n[SESSION EXPIRED] {e}\n", err=True)
        sys.exit(3)

    pans = sorted({n.pan for n in nodes})
    click.echo(f"OK: Clear API responded. Workspace has {len(nodes)} GSTIN(s) "
               f"across {len(pans)} PAN(s).")
    for pan in pans:
        gs = [n for n in nodes if n.pan == pan]
        biz = gs[0].business_name if gs else "?"
        click.echo(f"  {pan}  {biz}  ({len(gs)} GSTINs)")


@cli.command("pans")
@click.pass_obj
def list_pans(cfg: AppConfig) -> None:
    """List every PAN in your Clear workspace, marking which are in config.yaml.

    Useful for figuring out what to add when you want to download more PANs."""
    logger.info("Loading Chrome cookies from profile {!r}...", cfg.chrome_profile)
    try:
        cookies = load_clear_cookies(cfg.chrome_profile)
    except ChromeRunningError as e:
        click.echo(f"\n[CHROME IS OPEN] {e}\n", err=True)
        sys.exit(4)
    except RuntimeError as e:
        click.echo(f"\n[COOKIES ERROR] {e}\n", err=True)
        sys.exit(2)

    api = ClearAPI(workspace_id=cfg.workspace_id, cookies=cookies)
    try:
        nodes = api.user_gstins()
    except ClearSessionExpired as e:
        click.echo(f"\n[SESSION EXPIRED] {e}\n", err=True)
        sys.exit(3)

    configured = {p.pan: p for p in cfg.pans}
    by_pan: dict[str, list] = defaultdict(list)
    for n in nodes:
        by_pan[n.pan].append(n)

    click.echo(
        f"\nWorkspace has {len(by_pan)} PAN(s), {len(nodes)} GSTIN(s). "
        f"{len(configured)} PAN(s) in config.yaml.\n"
    )
    click.echo(f"  {'cfg':<5} {'PAN':<12} {'business':<48} {'GSTINs':>7}")
    click.echo(f"  {'-'*5} {'-'*12} {'-'*48} {'-'*7}")
    for pan in sorted(by_pan):
        gs = by_pan[pan]
        biz = gs[0].business_name[:48]
        mark = " yes " if pan in configured else "  -  "
        click.echo(f"  {mark:<5} {pan:<12} {biz:<48} {len(gs):>7}")

    not_configured = [p for p in sorted(by_pan) if p not in configured]
    if not_configured:
        click.echo(
            f"\n{len(not_configured)} PAN(s) NOT yet in config.yaml. "
            f"To add (example for the first few):\n"
        )
        click.echo("pans:")
        # Preserve any existing entries by also listing them
        for p in cfg.pans:
            click.echo(f'  - pan: "{p.pan}"')
            click.echo(f'    business_name: "{p.business_name}"')
            click.echo(f"    fys:")
            for fy in p.fys:
                click.echo(f'      - "{fy}"')
        for pan in not_configured[:3]:
            gs = by_pan[pan]
            biz = gs[0].business_name
            click.echo(f'  - pan: "{pan}"')
            click.echo(f'    business_name: "{biz}"')
            click.echo(f"    fys:")
            click.echo(f'      - "2025-26"  # edit as needed')
        if len(not_configured) > 3:
            click.echo(f"  # ... and {len(not_configured) - 3} more")


@cli.command("otp-worklist")
@click.pass_obj
def otp_worklist(cfg: AppConfig) -> None:
    """Build a (PAN x FY) worklist of GSTINs that need OTP reconnect in ClearGST.

    Reads state/partial-items.csv (populated by previous download runs that hit
    NOT_DOWNLOADED states) and writes:
        state/otp-worklist.csv  -- one row per (PAN, FY); good for client sharing
        state/otp-worklist.txt  -- human-readable; good for pasting into chat/email

    Run this after a `download --all` so the partial-items log is populated, then
    forward the generated files to whoever does the OTP reconnects in Clear's UI.
    """
    partials_csv = cfg.state_dir / "partial-items.csv"
    worklist_csv = cfg.state_dir / "otp-worklist.csv"
    worklist_txt = cfg.state_dir / "otp-worklist.txt"

    if not partials_csv.exists():
        click.echo(
            f"No partial-items.csv at {partials_csv}.\n"
            f"Run `python -m clear_ola download` first so the log is populated."
        )
        return

    summary = build_otp_worklist(partials_csv, worklist_csv, worklist_txt)
    if summary["total_combos"] == 0:
        click.echo(
            "Found 0 NOT_DOWNLOADED rows in partial-items.csv — no OTP reconnects "
            "needed. (If you expected some, check that previous runs actually "
            "hit DOWNLOADED_PARTIALLY / NOT_DOWNLOADED states; only those land "
            "in this log.)"
        )
        return

    click.echo(
        f"\nGenerated:\n"
        f"  {worklist_csv}\n"
        f"  {worklist_txt}\n\n"
        f"Summary:\n"
        f"  {summary['total_combos']} (PAN x FY) report page(s) need OTP reconnects\n"
        f"  {summary['total_gstins']} GSTIN(s) total across those pages\n\n"
        f"Forward either file to the person doing the manual OTP reconnects in "
        f"ClearGST.\nAfter they've reconnected, re-run `python -m clear_ola "
        f"download --all`; the previously-failed combos will retry and most "
        f"should now succeed."
    )


@cli.command("status-report")
@click.pass_obj
def status_report(cfg: AppConfig) -> None:
    """Build a 4-category report Excel + TXT summary for the GST team / client.

    Reads state/manifest.sqlite + state/partial-items.csv and writes:
      state/status-report.xlsx  -- 4 sheets:
                                   1) OTP Required, 2) Partial Data Only,
                                   3) Downloaded Complete, 4) No Data Available
      state/status-report.txt   -- plain-text summary (paste into chat/email)

    Forward either to the GST team / client for coordination.
    """
    manifest = Manifest(cfg.state_dir / "manifest.sqlite")
    pan_to_business = {p.pan: p.business_name for p in cfg.pans if p.business_name}

    summary = build_status_report(
        pan_to_business=pan_to_business,
        partials_csv=cfg.state_dir / "partial-items.csv",
        manifest_rows=manifest.all_rows(),
        xlsx_out=cfg.state_dir / "status-report.xlsx",
        txt_out=cfg.state_dir / "status-report.txt",
    )
    click.echo(
        f"\nGenerated:\n"
        f"  {cfg.state_dir / 'status-report.xlsx'}\n"
        f"  {cfg.state_dir / 'status-report.txt'}\n\n"
        f"Summary:\n"
        f"  1. OTP Required:           {summary['otp_required_pages']:>4} "
        f"PAN x FY pages ({summary['otp_required_gstins']} GSTIN(s))\n"
        f"  2. Partial Data Only:      {summary['partial_data_pages']:>4} "
        f"PAN x FY pages ({summary['partial_data_gstins']} GSTIN(s))\n"
        f"  3. Downloaded Complete:    {summary['downloaded']:>4} PAN x FY combinations\n"
        f"  4. No Data Available:      {summary['no_data']:>4} PAN x FY combinations"
    )
    if summary["failed_other"]:
        click.echo(
            f"  (Other failures still in manifest: {summary['failed_other']} — "
            f"re-run download to retry network blips before sharing.)"
        )


@cli.command()
@click.pass_obj
def status(cfg: AppConfig) -> None:
    """Show what's done / pending / failed in the manifest."""
    manifest = Manifest(cfg.state_dir / "manifest.sqlite")
    _print_summary(manifest, verbose=True)


@cli.command()
@click.option("--pan", required=True, help="PAN to reset (required)")
@click.option("--fy", default=None, help="Limit to this FY (optional)")
@click.option("--report", "report_type", default=None, help="Limit to this report (optional)")
@click.pass_obj
def reset(cfg: AppConfig, pan: str, fy: str | None, report_type: str | None) -> None:
    """Delete manifest rows so the next run re-downloads them."""
    manifest = Manifest(cfg.state_dir / "manifest.sqlite")
    pan = pan.strip().upper()
    deleted = manifest.reset(pan, fy=fy, report_type=report_type)
    click.echo(f"Deleted {deleted} manifest row(s) for pan={pan} fy={fy} report={report_type}")


def _print_summary(manifest: Manifest, verbose: bool = False) -> None:
    rows = manifest.all_rows()
    if not rows:
        click.echo("(manifest empty)")
        return

    counts = {"done": 0, "no_data": 0, "in_progress": 0, "failed": 0, "pending": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    click.echo("\n--- manifest summary ---")
    for k in ("done", "no_data", "in_progress", "failed", "pending"):
        click.echo(f"  {k:<12} {counts.get(k, 0)}")

    if verbose:
        click.echo("\n--- rows ---")
        for r in rows:
            line = (f"  {r['pan']:<12} {r['fy']:<9} {r['report_type']:<10} "
                    f"{r['status']:<11} {r['file_path'] or ''}")
            if r["status"] == "failed" and r.get("error_message"):
                line += f"  [ERROR: {r['error_message'][:120]}]"
            click.echo(line)
