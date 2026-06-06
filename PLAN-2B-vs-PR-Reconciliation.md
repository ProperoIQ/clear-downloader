# Plan: Add "2B vs PR Reconciliation" (PAN-based) report

## Context

The downloader already supports many PAN-based and GSTIN-based ClearGST reports
(GSTR-2A/2B/1/3B, the "vs 3B vs Books" reconciliations, ledgers, e-invoice, etc.).
This adds a **new PAN-based report: "2B vs PR Reconciliation"** — Clear's
reconciliation of GSTR-2B (ITC available per GSTN) against the Purchase Register
(PR) uploaded into Clear. Output: one PAN-level Excel per financial year.

**Hard constraint:** do NOT disturb the existing PAN-based or GSTIN-based flows.
This is purely additive.

### Key finding — this report uses a DIFFERENT API stack

Every existing report uses the `data-pull` → `data-browser/export/trigger` →
`export/download` pipeline. **"2B vs PR" does not.** It uses Clear's
`recon/ultimatum` *matching-task* pipeline, reverse-engineered from the two HARs
in `discovery/`:
- `app.clear.in.har__2B vs PR Reconciliation.har` (download of an already-matched task)
- `app.clear.in.har__2B vs PR Reconciliation_for new capture.har` (full period-driven flow incl. the matching *trigger*)

Because the API stack differs, the new flow gets **new additive methods on
`ClearAPI`** and a **new flow module** — it cannot reuse `trigger_pull` /
`trigger_export` / `wait_for_export`. The PAN-iteration / manifest / partials
scaffolding *is* reused.

### Reverse-engineered end-to-end flow (per PAN × FY)

All calls under `https://app.clear.in`. `matchType` is fixed: `MAX_ITC_2B_PR`.
Periods are MMYYYY (e.g. `042023`).

1. **Trigger matching** — `POST /api/recon/ultimatum/public/matching/v2/trigger`
   - Headers: `x-cleartax-matching-type: MAX_ITC_2B_PR`, node headers.
   - Body: `panNodeId`, `matchType`, `nodeName` (=PAN), `lhsDocumentFilter`
     (PR Return Period, `RETURN_PERIOD_RANGE`, `filterValueStart/End`),
     `rhsDocumentFilter` (2B Return Period, same range), and a `taskContext`
     JSON-string (xClearNodeId=panNodeId, xClearNodeType=PAN, xCleartaxProduct=GST,
     xCleartaxMatchingType=MAX_ITC_2B_PR, xOrganisationId/xWorkspaceId=workspace_id).
   - Returns: `payload.requestId` (this becomes the **matching task id**) + `status:"CREATED"`.
   - This step also fetches the underlying 2B/PR data server-side (status walks
     `CREATED → DATA_FETCH_INITIATED → … → DATAVIEW_READY`), so **no separate
     data-pull is required**.

2. **Poll until ready** — `GET /api/recon/ultimatum/public/matching/current`
   - Headers: `x-cleartax-matching-type: MAX_ITC_2B_PR`, `x-clear-node-id: <panNodeId>`,
     `x-clear-node-type: PAN`.
   - Returns `payload.taskId`, `payload.taskStatus`, `payload.returnPeriodRange`.
   - **Terminal signal:** `taskStatus == "DATAVIEW_READY"` AND `taskId == requestId`.
   - (`GET /matching/status` with `x-matching-task-id` returns a bare status string
     — usable as a secondary poll, but `matching/current.taskStatus` is the
     authoritative ready signal.)

3. **Generate report** — `POST /api/recon/ultimatum/public/workbench/report/v1/generate`
   - Headers: `x-matching-task-id: <taskId>`.
   - Body: `format:"EXCEL"`, `matchingReportGenerationType:"MAX_ITC_2B_PR"`,
     `version:"V2"`, `reportTypes:["DOCUMENT"]`, `filter:{dslFilters:[]}`,
     `metadata:{businessName, govtFromRp, govtToRp, purchaseFromRp, purchaseToRp,
     panNumber, gstinNumber:"", xMatchingTaskId}` (all four Rp = start/end period),
     plus `onStartCallback`/`onFinishCallback` KRAMER blocks
     (notificationType `REPORT_GENERATION_V2`, tenant `IDT`, nodeId=[panNodeId],
     nodeType PAN, orgId/workspaceId).
   - Returns: a bare UUID string = **reportId** (response is plain text, not JSON).

4. **Poll + download** — `GET /api/recon/ultimatum/public/workbench/report/v1/{reportId}/download`
   - Headers: `x-matching-task-id: <taskId>`.
   - Returns `{ "status": "SUCCESS", "fileInfos": [{ "reportType":"DOCUMENT",
     "url": <presigned app.clear.in/storage URL>, "filename": "...xlsx",
     "extension":"xlsx" }] }`. Poll until `status == "SUCCESS"` and `fileInfos`
     is non-empty.

5. **Download the file** from `fileInfos[0].url` (reuse existing `api.download_file`).

### Period rule (confirmed by user + HAR)

- FY **2017-18** → periods **072017 … 032018** (GST began Jul 2017).
- Every later FY → **04…03** (Apr→Mar), e.g. FY 2023-24 → `042023…032024`.
- This is exactly `config.fy_periods(fy, as_of=today)` (already truncates the
  in-progress current FY), then clip anything before `072017`. Reuse the
  `_yyyymm` clip pattern from `gstr_2b_vs_3b_vs_books.py` (around lines 342-358)
  with `MIN_START_PERIOD = "072017"`. No `MIN_FY` skip — 2017-18 onward is in
  scope (pre-2020 simply shows PR docs as "Missing in 2B", which is valid output).

## Files to create

### 1. `src/clear_ola/flows/gstr_2b_vs_pr_reconciliation.py` (new flow module)
Model on `gstr_2b_vs_3b_vs_books.py` for the **scaffolding** (PAN indexing via
`_index_gstins_by_pan`, `run()` loop over `cfg.pans`/`fy`, manifest
`is_done`/`mark_started`/`mark_done`/`mark_failed`/`mark_no_data`,
`ClearSessionExpired` passthrough), but replace the body with the 4-step recon
flow above. Constants:
```python
REPORT_TYPE = "2B-vs-PR-Reconciliation"
MATCH_TYPE  = "MAX_ITC_2B_PR"
MIN_START_PERIOD = "072017"   # GST inception; clips FY 2017-18's Apr–Jun
```
`_run_one(...)` sequence:
1. Build `periods = fy_periods(fy, as_of=today)`, clip `< 072017`; if empty →
   `mark_no_data`. `start_rp, end_rp = periods[0], periods[-1]`.
2. `pan_node_id = gstins[0].pan_node_id`; `business_name = pan_cfg.business_name`.
3. `task_id = api.recon_matching_trigger(pan_node_id, pan, start_rp, end_rp, workspace_id)`.
4. `api.wait_for_recon_matching(pan_node_id, task_id, poll_seconds, timeout)` →
   blocks until `taskStatus == DATAVIEW_READY`.
5. `report_id = api.recon_report_generate(task_id, pan_node_id, pan, business_name,
   start_rp, end_rp, workspace_id)`.
6. `file_info = api.wait_for_recon_report(report_id, task_id, poll_seconds, timeout)`.
7. `dest = cfg.downloads_dir / pan / f"FY-{fy}" / REPORT_TYPE / file_info.filename`;
   `bytes = api.download_file(file_info.url, dest, gstin_node_ids=[...])`;
   small-file warning like the 2B-vs-3B module; `manifest.mark_done(...)`.

No JSON template file is needed (the generate body is small and fully
parameterized — build it inline), unlike the data-browser reports whose huge
`*_statement.json` payloads justify on-disk templates.

## Files to modify (additive only)

### 2. `src/clear_ola/api.py` — add new recon methods (do NOT touch existing ones)
New methods mirroring the existing `_request` usage and `_node_headers` helper:
- `recon_matching_trigger(pan_node_id, pan, start_rp, end_rp, workspace_id) -> str`
  (POST `matching/v2/trigger`; returns `payload.requestId`).
- `recon_matching_current(pan_node_id) -> dict` (GET `matching/current` with
  matching-type + PAN node headers).
- `wait_for_recon_matching(pan_node_id, task_id, *, poll_seconds, timeout_seconds) -> dict`
  (loop on `recon_matching_current`, return when `taskStatus==DATAVIEW_READY` and
  `taskId==task_id`; raise on `corrupted`/`errorInfo` or timeout).
- `recon_report_generate(task_id, pan_node_id, pan, business_name, start_rp, end_rp, workspace_id) -> str`
  (POST `workbench/report/v1/generate` with `x-matching-task-id`; `expect_text=True`
  since the body is a bare UUID).
- `recon_report_download(report_id, task_id) -> dict` and
  `wait_for_recon_report(report_id, task_id, *, poll_seconds, timeout_seconds) -> ReconFile`
  (GET `workbench/report/v1/{report_id}/download`; return first `fileInfos` entry
  when `status==SUCCESS`).
- Small dataclass `ReconFile(url, filename, extension)` (or reuse a plain tuple).
- `download_file` already exists and works on the presigned `app.clear.in/storage`
  URL — reuse as-is.

These are purely additive; the 2B/2A/1/3B/Books/ledger flows call none of them.

### 3. `src/clear_ola/cli.py` — register in the PAN `download` command (3 edits)
- Add `gstr_2b_vs_pr_reconciliation` to the `from clear_ola.flows import (...)` block.
- Add `"2B-vs-PR-Reconciliation"` to the `--report` `click.Choice([...])` list.
- Add dispatch branch next to the other recon branches:
  ```python
  elif report_choice.upper() == "2B-VS-PR-RECONCILIATION":
      gstr_2b_vs_pr_reconciliation.run(api, cfg, manifest)
  ```
Leave the `gst-download` command and all GSTIN flows untouched.

### 4. `config.yaml` — add `"2B-vs-PR-Reconciliation"` to the informational `reports:` list.

## What is NOT changed
`config.py`, `manifest.py`, `cookies.py`, `partials.py`, `status_report.py`,
`gst_manifest.py`, the entire `gst_flows/` package, and every existing module in
`flows/`. The new report reuses `Manifest` (generic `report_type` column),
`AppConfig`/`fy_periods`, `user_gstins()`/`GstinNode` (provides `pan_node_id` +
`business_name`), and `download_file`.

## Verification

1. **Static**: `python -c "import clear_ola.cli"` and
   `python -c "from clear_ola.flows import gstr_2b_vs_pr_reconciliation"` import clean.
2. **CLI surface**: `python -m clear_ola download --help` shows
   `2B-vs-PR-Reconciliation` in the `--report` choices.
3. **End-to-end (one PAN, one FY)** — with valid cookies:
   `python -m clear_ola download --report 2B-vs-PR-Reconciliation --pan <PAN> --fy 2023-24`
   - Watch logs for: matching trigger → `requestId` → poll to `DATAVIEW_READY`
     → generate `reportId` → download `SUCCESS` → file saved under
     `downloads/<PAN>/FY-2023-24/2B-vs-PR-Reconciliation/GSTR 2B Vs PR_<name>_*.xlsx`.
   - Open the XLSX; confirm it contains recon rows (not an empty shell) and that
     the period header matches Jul→Mar clamping for FY 2017-18 when tested with `--fy 2017-18`.
4. **Regression**: run one existing report
   (`python -m clear_ola download --report GSTR-2B --pan <PAN> --fy 2023-24`)
   to confirm the additive changes didn't disturb the data-pull path.
5. **Manifest**: confirm a `2B-vs-PR-Reconciliation` row is marked `done` in
   `state/manifest.sqlite` and re-running skips it.

## Open follow-ups (flagged, not blocking)
- `matching/status` terminal value beyond `DATA_FETCH_INITIATED` wasn't captured
  (the UI switched to `matching/current` once ready). We gate readiness on
  `matching/current.taskStatus==DATAVIEW_READY`, which IS captured. If
  long-running matches surface a distinct failure status, add it to the poller's
  error set once observed.
- The recon `download` returned `SUCCESS` immediately in both HARs; the poller
  tolerates an intermediate non-SUCCESS status defensively in case large reports
  return `IN_PROGRESS` first.
