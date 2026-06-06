# Plan: Add "8A vs PR Reconciliation" (PAN-based) report

## Context

The downloader already supports many PAN-based and GSTIN-based ClearGST reports
(GSTR-2A/2B/1/3B, the "vs 3B vs Books" reconciliations, ledgers, e-invoice, and
the **2A/2B/6A vs PR Reconciliations**). This adds another **PAN-based report:
"8A vs PR Reconciliation"** — Clear's reconciliation of GSTR-8A (TCS credit
reflected per GSTN) against the Purchase Register (PR) uploaded into Clear.
Output: one PAN-level Excel per financial year.

**Hard constraint:** do NOT disturb the existing PAN-based or GSTIN-based flows
(including the working 2A/2B/6A-vs-PR flows). This is purely additive.

### Key finding — identical to 6A/2A/2B vs PR except the match type

This report uses the **same** `recon/ultimatum` *matching-task* pipeline as the
2A/2B/6A-vs-PR reports (NOT the `data-pull` → `data-browser/export` pipeline).
Verified from the HAR `discovery/app.clear.in.GSTR-8A vs PR Recon.har`: the
endpoints, request headers, payload shape, 4-step sequence and download mechanism
are byte-for-byte the same. The only differences are literal strings:

- `matchType` / `x-cleartax-matching-type` / taskContext `xCleartaxMatchingType`
  / `matchingReportGenerationType`: **`MAX_ITC_8A_PR`** (vs `MAX_ITC_6A_PR`).
- taskContext UI path: `/reconciliation/idt/8aVsPr` (vs `6aVsPr`).
- rhs side label: `"8A Return Period"` (derived in code as
  `f"{side_label} Return Period"`).
- Output filename prefix: `GSTR 8A Vs PR_...` (server-supplied; no code change).

Because the API stack is shared and already parameterized by `match_type`
(default `MAX_ITC_2B_PR`), the new 8A variant only needed **one** new entry in
the `_RECON_VARIANTS` map plus a flow module that passes `MAX_ITC_8A_PR`. No
existing recon method was changed, so the 2A/2B/6A flows are unchanged.

### Reverse-engineered end-to-end flow (per PAN × FY)

All calls under `https://app.clear.in`. `matchType` is `MAX_ITC_8A_PR`.
Periods are MMYYYY (e.g. `072017`). Both the PR (lhs) and 8A (rhs) sides use the
**same** period range.

1. **Trigger matching** — `POST /api/recon/ultimatum/public/matching/v2/trigger`
   - Headers: `x-cleartax-matching-type: MAX_ITC_8A_PR`, `x-node-id`,
     `x-node-type: PAN`, `x-clear-node-type: GSTIN`.
   - Body: `panNodeId`, `matchType:"MAX_ITC_8A_PR"`, `nodeName` (=PAN),
     `lhsDocumentFilter` (PR Return Period, `RETURN_PERIOD_RANGE`),
     `rhsDocumentFilter` (8A Return Period, same range), `commonDocumentFilter`
     (`BH_SELECTOR` = panNodeId), and a `taskContext` JSON-string
     (`path:"/reconciliation/idt/8aVsPr"`).
   - Returns: `payload.requestId` (= the **matching task id**) + `status:"CREATED"`.
   - This step also fetches the underlying 8A/PR data server-side, so **no
     separate data-pull is required**.

2. **Poll until ready** — `GET /api/recon/ultimatum/public/matching/current`
   - Headers: `x-cleartax-matching-type: MAX_ITC_8A_PR`, `x-clear-node-id`,
     `x-clear-node-type: PAN`.
   - **Terminal signal:** `taskStatus == "DATAVIEW_READY"` AND `taskId == requestId`.

3. **Generate report** — `POST /api/recon/ultimatum/public/workbench/report/v1/generate`
   - Headers: `x-matching-task-id: <taskId>`.
   - Body: `format:"EXCEL"`, `matchingReportGenerationType:"MAX_ITC_8A_PR"`,
     `version:"V2"`, `reportTypes:["DOCUMENT"]`, `metadata:{businessName,
     govtFromRp, govtToRp, purchaseFromRp, purchaseToRp, panNumber,
     gstinNumber:"", xMatchingTaskId}` (all four Rp = start/end period), plus
     KRAMER callbacks.
   - Returns: a bare UUID string = **reportId** (plain text, not JSON).

4. **Poll + download** — `GET /api/recon/ultimatum/public/workbench/report/v1/{reportId}/download`
   - Headers: `x-matching-task-id: <taskId>`.
   - Returns `{ "status":"SUCCESS", "fileInfos":[{ "url": <presigned URL>,
     "filename": "GSTR 8A Vs PR_...xlsx" }] }`. Poll until `status=="SUCCESS"`.

5. **Download the file** from `fileInfos[0].url` (reuse existing `api.download_file`).

### Period rule (same as 2A/2B/6A vs PR)

- FY **2017-18** → periods **072017 … 032018** (GST began Jul 2017).
- Every later FY → **04…03** (Apr→Mar), e.g. FY 2023-24 → `042023…032024`.
- Exactly `config.fy_periods(fy, as_of=today)` then clip anything before
  `072017` (`MIN_START_PERIOD = "072017"`).

## Files created

### 1. `src/clear_ola/flows/gstr_8a_vs_pr_reconciliation.py` (new flow module)
Faithful copy of `gstr_6a_vs_pr_reconciliation.py`, changing constants to
```python
REPORT_TYPE = "8A-vs-PR-Reconciliation"
MATCH_TYPE  = "MAX_ITC_8A_PR"
MIN_START_PERIOD = "072017"
```
and passing `match_type=MATCH_TYPE` into `recon_matching_trigger`,
`wait_for_recon_matching`, and `recon_report_generate`. Scaffolding
(`_index_gstins_by_pan`, `run()` loop, `_yyyymm`, `_fy_period_range`, manifest
calls, small-file warning, `ClearSessionExpired` passthrough) is identical.

## Files modified (additive only)

### 2. `src/clear_ola/api.py` — add one `_RECON_VARIANTS` entry
Added `"MAX_ITC_8A_PR": ("8A", "/reconciliation/idt/8aVsPr"),` to the
`_RECON_VARIANTS` map. The existing recon methods (already parameterized by
`match_type`, default `MAX_ITC_2B_PR`) derive the rhs label and UI path from this
entry, so no method signature or logic changed.

### 3. `src/clear_ola/cli.py` — register in the PAN `download` command (3 edits)
- Import `gstr_8a_vs_pr_reconciliation` in the `from clear_ola.flows import (...)` block.
- Add `"8A-vs-PR-Reconciliation"` to the `--report` `click.Choice([...])` list.
- Add dispatch branch:
  ```python
  elif report_choice.upper() == "8A-VS-PR-RECONCILIATION":
      gstr_8a_vs_pr_reconciliation.run(api, cfg, manifest)
  ```

### 4. `config.yaml` — add `"8A-vs-PR-Reconciliation"` to the informational `reports:` list + comment block.

## What is NOT changed
`config.py`, `manifest.py`, `cookies.py`, `partials.py`, `status_report.py`,
`gst_manifest.py`, the entire `gst_flows/` (GST-based) package, the 2A/2B/6A-vs-PR
flow modules, and every other module in `flows/`. The new report reuses
`Manifest`, `AppConfig`/`fy_periods`, `user_gstins()`/`GstinNode`, and
`download_file`. The default `match_type` everywhere stays `MAX_ITC_2B_PR`.

## Verification

1. **Static**: `python -c "import clear_ola.cli"` and
   `python -c "from clear_ola.flows import gstr_8a_vs_pr_reconciliation"` import clean. ✓
2. **CLI surface**: `python -m clear_ola download --help` shows
   `8A-vs-PR-Reconciliation` in the `--report` choices. ✓
3. **End-to-end (one PAN, one FY)** — with valid cookies:
   `python -m clear_ola download --report 8A-vs-PR-Reconciliation --pan <PAN> --fy 2017-18`
   - Watch logs for: matching trigger → `taskId` → poll to `DATAVIEW_READY`
     → generate `reportId` → download `SUCCESS` → file saved under
     `downloads/<PAN>/FY-2017-18/8A-vs-PR-Reconciliation/GSTR 8A Vs PR_<name>_*.xlsx`.
   - Open the XLSX; confirm recon rows (not an empty shell).
4. **Regression (siblings untouched)**: run
   `python -m clear_ola download --report 6A-vs-PR-Reconciliation --pan <PAN> --fy 2023-24`
   to confirm the shared parameterization still behaves as before.
5. **Manifest**: confirm an `8A-vs-PR-Reconciliation` row is marked `done` in
   `state/manifest.sqlite` and re-running skips it.
