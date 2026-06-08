# Plan: Add "E-Invoice Data (GSTR-1) vs Sales Register (SR)" — PAN-based flow

## Context

The tool already downloads many ClearGST reports, split into two tracks:
- **PAN-based** (`download` command, `state/manifest.sqlite`) — GSTR-2A/2B/1/3B/8, the `*-vs-3B-vs-Books` recons, the `*-vs-PR-Reconciliation` recons, ledgers, Outward-E-Invoice.
- **GSTIN-based** (`gst-download` command, `state/gst-manifest.sqlite`) — GSTR-6A/6/9-8A, Electronic-Liability-Register.

We need a **new PAN-based** report: **E-Invoice Data (GSTR-1) vs Sales Register (SR)**, CLI token **`EInvoice-vs-SR`**. The HAR
`discovery/app.clear.in.har__GSTR1 vs SR.har` was captured from Clear's UI generating this report.

**Hard constraint from the user: do NOT disturb existing PAN-based or GSTIN-based flows.** All API changes below are *additive* (new methods, or new optional params with defaults that preserve current behavior).

## What the HAR tells us (the key insight)

This flow is a **hybrid** of the two existing patterns:

1. It computes the match via the **recon/ultimatum matching backend** (same as `2A-vs-PR-Reconciliation`):
   - `POST /api/recon/ultimatum/public/matching/v2/trigger` → `requestId`, status `CREATED`
   - `GET  /api/recon/ultimatum/public/matching/current` (poll) → `taskStatus == DATAVIEW_READY`, with a **`taskId`**
2. But it downloads via the **data-browser export backend** (same as `GSTR-1-vs-3B-vs-Books`), under tenant **`IDT`** (not `GST_REPORTS`):
   - `POST /api/recon/ultimatum/public/rls/fetch-token?tableType=RECON_G1_VS_SR_MATCHING` (header `x-matching-task-id`, `x-cleartax-tenant: IDT`) → `token`
   - `POST /api/clear/data-browser/public/export/trigger` (header `x-rls-token`, `x-tenant-name: IDT`) → exportId
   - `GET  /api/clear/data-browser/public/export/download/{exportId}` (poll, header `x-tenant-name: IDT`) → `preSignedUrl`, `taskStatus SUCCESS`
   - download presigned URL

Captured constants (verbatim from HAR):
- matchType: **`MAX_ITC_G1EInv_SR`**
- recon UI path: `/reconciliation/idt/G1EInvVsSr`
- LHS = **SR** ("SR Return Period"), RHS = **G1 / E-Invoice** ("G1 Return Period") — both sides use the *same* period range
- RLS table type: **`RECON_G1_VS_SR_MATCHING`**
- data-browser template id: `69161168a800c836e2dcd41c`
- export `notificationType: G1_VS_SR_RECON_SUMMARY`, `product: MaxItc`, `tenant: IDT`
- exportName: `G1 vs SR excel Action Report` (a **summary / "Action Report"**, not document-level)

**Two correctness nuances surfaced by the HAR (must be honored / validated):**
- **(A) trigger `requestId` ≠ matching/current `taskId`.** Unlike the PR-recon flows (where the existing `wait_for_recon_matching` requires `current.taskId == trigger.requestId`), here they differ. The downstream `x-matching-task-id` must be the **`taskId` read from `matching/current`**, not the trigger's `requestId`. So we must poll for `DATAVIEW_READY` *without* the identity check and capture the returned `taskId`.
- **(B) staleness guard.** Because we no longer match on the trigger id, a stale prior `DATAVIEW_READY` could be read prematurely. Guard by checking the payload's `returnPeriodRange` matches the requested `[start_rp, end_rp]` (and `matchType == MAX_ITC_G1EInv_SR`) before accepting. This must be verified on the first live run.

## Reused existing code (no behavior change)

- `ClearAPI.recon_matching_trigger(...)` in `src/clear_ola/api.py` — extend the variant table + add optional labels (below).
- `ClearAPI.recon_matching_current` / `wait_for_recon_matching` — add optional behavior for nuance (A).
- `ClearAPI.trigger_export` — already supports `header_overrides`; pass `{"x-tenant-name": "IDT"}`. **No edit needed.**
- `ClearAPI.download_file` — reused as-is.
- Flow scaffolding (`_index_gstins_by_pan`, `_yyyymm`, `_fy_period_range`, manifest start/done/failed/no_data, "already done" skip, small-file warning) — copied from `gstr_2a_vs_pr_reconciliation.py` and `gstr_1_vs_3b_vs_books.py`.
- PAN `Manifest` (keyed by `pan, fy, report_type`) — reused, no schema change.
- Download path convention `downloads/<PAN>/FY-<FY>/<REPORT_TYPE>/<file>` — reused.

## Changes (as implemented)

### 1. `src/clear_ola/api.py` (additive only)

a. Added the G1-vs-SR variant to `_RECON_VARIANTS`: `"MAX_ITC_G1EInv_SR": ("G1", "/reconciliation/idt/G1EInvVsSr")`.

b. Parameterized the filter labels in `recon_matching_trigger` with `lhs_label="PR Return Period"` / `rhs_label=None` defaults (PR flows byte-identical). The G1-vs-SR flow passes `lhs_label="SR Return Period", rhs_label="G1 Return Period"`.

c. `wait_for_recon_matching` now takes `task_id: str | None = None` and `expected_range: tuple[str, str] | None = None`. With `task_id` set → exact taskId match (PR flows unchanged). With `task_id=None` → accept the first `DATAVIEW_READY` whose `returnPeriodRange` matches `expected_range`, and return the payload (caller reads `payload["taskId"]`). Logic factored into `_recon_match_accepted`.

d. New method `fetch_recon_rls_token(table_type, task_id, tenant="IDT")` → POSTs `/api/recon/ultimatum/public/rls/fetch-token?tableType=...` with `x-matching-task-id`, `x-cleartax-tenant`, `x-clear-node-type: GSTIN`.

e. `get_export_status` / `wait_for_export` gained `tenant_name="GST_REPORTS"` (default unchanged); the new flow passes `tenant_name="IDT"`.

### 2. `src/clear_ola/flows/gstr_1_einvoice_vs_sr_statement.json` (new)

Verbatim export-trigger body from the HAR. Per-(PAN,FY) substitutions (org/workspace ids + staticRowData) are made in code.

### 3. `src/clear_ola/flows/gstr_1_einvoice_vs_sr.py` (new)

Constants: `REPORT_TYPE="EInvoice-vs-SR"`, `MATCH_TYPE="MAX_ITC_G1EInv_SR"`, `RLS_TABLE_TYPE="RECON_G1_VS_SR_MATCHING"`, `EXPORT_TENANT="IDT"`, `MIN_START_PERIOD="072017"`.

`_run_one` steps: trigger match → wait DATAVIEW_READY (by range) + read taskId → recon RLS token → build + trigger export (tenant IDT) → wait + download → manifest done. Standard `ClearSessionExpired`/`Exception` handling.

### 4. `src/clear_ola/cli.py` (3 edits)

Import `gstr_1_einvoice_vs_sr`; add `"EInvoice-vs-SR"` to the `--report` choices; add `elif report_choice.upper() == "EINVOICE-VS-SR": gstr_1_einvoice_vs_sr.run(api, cfg, manifest)`.

### 5. `config.yaml`

Added `- "EInvoice-vs-SR"` to the `reports:` list + comment block.

## Files

- New: `src/clear_ola/flows/gstr_1_einvoice_vs_sr.py`, `src/clear_ola/flows/gstr_1_einvoice_vs_sr_statement.json`
- Edit: `src/clear_ola/api.py` (additive), `src/clear_ola/cli.py` (3 edits), `config.yaml` (1 line + comment)
- Untouched: every existing flow module, both manifests, the `gst-download` command, all PR-recon and vs-Books flows.

## Verification

1. **Import sanity** (done): `python -c "import clear_ola.cli"` and `python -m clear_ola download --help` — `einvoice-vs-sr` appears, no other choice changed; modules byte-compile.
2. **Regression smoke (no disturbance)**: run one existing recon, e.g. `python -m clear_ola download --report 2A-vs-PR-Reconciliation --pan <PAN> --fy 2023-24`, confirm it still produces its file.
3. **New flow happy path**: `python -m clear_ola download --report EInvoice-vs-SR --pan AAKCA2311H --fy 2017-18` (the PAN/FY from the HAR). Watch logs for: trigger `requestId` → `DATAVIEW_READY` with matching `returnPeriodRange` → recon RLS token → export id → `SUCCESS` → download. Confirm a non-trivial file lands at `downloads/AAKCA2311H/FY-2017-18/EInvoice-vs-SR/...` and `state/manifest.sqlite` row is `done`.
4. **Correctness checks to confirm live** (the two HAR nuances): (A) the `x-matching-task-id` used downstream is the `matching/current` `taskId`, not the trigger `requestId`; (B) the staleness guard accepts only the `DATAVIEW_READY` whose period range matches the request. If step 3 downloads an empty-shell XLSX, add the `IDT` priming `run_data_browser_query` call before the export trigger.
5. **`--all` dry check**: `python -m clear_ola download --report EInvoice-vs-SR --all` resumes correctly (skips `done`, records `no_data` for pre-GST FYs).
