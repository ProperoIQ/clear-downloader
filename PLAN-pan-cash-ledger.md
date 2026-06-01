# Plan — Add PAN Cash Ledger Report

Branch: `add-pan-cash-ledger-report`
HAR source: `discovery/app.clear.in.CASH-LEDGER.har` (7.8 MB, 143 entries)
HAR scope: PAN `AAGCP5410J` (PISCES ESERVICES PRIVATE LIMITED), 41 GSTINs, date range `01-07-2017` to `01-06-2026` (full GST era).

---

## 1. What the HAR confirmed

This is a **simple flow** — same shape as GSTR-2A, **no preflight**. Just one pull, one RLS token, one export trigger, one download.

| Thing | Value | HAR evidence |
|---|---|---|
| URL slug (Referer query) | `reportType=panCashLedger` | entries 80, 108, 118, 136 |
| Pull tenant | `CASH_LEDGER_REPORT` | entry 80 body |
| RLS workflow (URL param `workFlow=`) | `CASH_LEDGER_REPORT` | entry 108 URL |
| RLS URL params | `workFlow=CASH_LEDGER_REPORT&tableType=&fromDate=DD-MM-YYYY&toDate=DD-MM-YYYY` | entry 108 URL |
| Export S3 prefix | `cash_ledger_download` | entry 136 response, file URL |
| Export filename | `PAN_CASH_LEDGER_REPORT_<PAN>_<DD-MM-YYYY>-<DD-MM-YYYY>` | entry 136 response |
| Real-export `exportName` | `cash_ledger_download` | entry 118 body |
| Real-export `fileType` | `XLSX` | entry 118 body |
| `staticRowData` keys | `{companyName, gstin, reportPeriod}` | entry 118 body |
| `staticRowData.reportPeriod` | `"01-07-2017 - 01-06-2026"` (`"DD-MM-YYYY - DD-MM-YYYY"`) | entry 118 body |
| `onStart.metadata.reportType` | `panCashLedger` | entry 118 body |
| Statement template id | `67e2a6e78ede5b3eac89594d` (Clear's stored query template) | entry 118 body |

### Header behavior on the export-related calls

| Call | x-ct-source present? | Notes |
|---|---|---|
| `pull/v2/trigger` (entry 80) | **YES** (`GST_REPORTS`) | matches session default — no override needed |
| `rls/fetch-token` (entry 108) | **NO** | absent in HAR |
| `export/trigger` (entry 118) | **NO** | absent in HAR |
| `export/download/<id>` (entry 136) | **NO** | absent in HAR |

All four calls also carry `baggage`, `sentry-trace`, `accept-language: en-US,en;q=0.9`, `priority: u=1, i`. The pull call carries them too. We replicate this set on the export calls via `header_overrides`, same pattern as the 2A-vs-3B-vs-Books flow.

### Critical difference from every other flow: **DATE RANGE, not FY periods**

Every existing flow uses `MMYYYY` periods (e.g. `042020`). Cash Ledger uses **DD-MM-YYYY date strings**:

- Pull body: `startRange: "01-07-2017"`, `endRange: "01-06-2026"`
- RLS URL: `fromDate=01-07-2017&toDate=01-06-2026`
- Export body: `staticRowData.reportPeriod: "01-07-2017 - 01-06-2026"`, `onStart.metadata.startRange / endRange` use the same DD-MM-YYYY format

This means our flow can't reuse `fy_periods()` from `config.py`. We need a small `fy_to_date_range(fy) -> (start_ddmmyyyy, end_ddmmyyyy)` helper.

### Pull-trigger body has `gisDownloadBehaviour: null`

The pull body sends `"gisDownloadBehaviour": null` (not `"USE_EXISTING_DATA"` like other flows). `api.trigger_pull()` currently defaults to `"USE_EXISTING_DATA"`. **Open question:** does Clear's backend treat `null` and `"USE_EXISTING_DATA"` equivalently for cash ledger? Most likely yes (they're both "use cache when possible"). My plan: pass the default `"USE_EXISTING_DATA"` and only revisit if the pull fails or returns weird state.

### What's NOT in the HAR (and why that's fine)

- **No preflight** call. The page issues ONE export/trigger and that's it. We won't replicate the reconciliation-style preflight.
- **No NOT_APPLICABLE / DOWNLOADED_PARTIALLY observed** — all 41 GSTINs settled `DOWNLOADED` cleanly. We still keep the same partials-handling logic from `gstr_2a.py` defensively — it's free safety for production data that may not be as clean.

---

## 2. Files I will create

### a. `src/clear_ola/flows/pan_cash_ledger_statement.json`
**Verbatim** body of HAR entry #118 (1868 chars). Substituted at runtime: `staticRowData`, `filename`, `onStart.metadata` and `onFinish.metadata` (the `orgId/workspaceId/nodeName/startRange/endRange/activeBusiness` fields). Everything else stays literal — including `statement.from.id: "67e2a6e78ede5b3eac89594d"` (Clear's query template id) and `exportName: "cash_ledger_download"`.

### b. `src/clear_ola/flows/pan_cash_ledger.py`
Closest existing template: `gstr_2a.py` (simple single-export flow, no preflight). Substitutions:

| Constant / value | Cash Ledger |
|---|---|
| `REPORT_TYPE` | `"PAN-Cash-Ledger"` |
| `TENANT` | `"CASH_LEDGER_REPORT"` |
| `RLS_WORKFLOW` | `"CASH_LEDGER_REPORT"` |
| Referer slug | `panCashLedger` |
| Filename prefix | `PAN_CASH_LEDGER_REPORT_<PAN>_<start>-<end>` |
| Period format | `DD-MM-YYYY` strings (not `MMYYYY`) — needs new helper |
| Header overrides on export/trigger | `{x-ct-source: None, baggage, sentry-trace, accept-language, priority}` same set as 2A-vs-3B-vs-Books |
| Preflight | **none** (simpler than reconciliation flows) |

New helper inside this module (private to the flow — no need to touch `config.py`):

```python
def _fy_to_date_range(fy: str, *, as_of: date) -> tuple[str, str]:
    """'2024-25' -> ('01-04-2024', '31-03-2025'). FY 2017-18 starts 01-07-2017
    (GST regime start, not 01-04-2017). Current FY clamps end to today."""
```

### c. `src/clear_ola/cli.py`
Add to `--report` Choice list, add the import, add the dispatch branch. Default stays `"GSTR-2A"`.

### d. `config.yaml`
Add `"PAN-Cash-Ledger"` to the informational `reports:` list. PANs and FYs lists stay as-is — the flow will iterate them like every other report.

### e. `discovery/FINDINGS.md`
Append a small section documenting the slug / tenant / DD-MM-YYYY date format quirk / filename pattern / S3 prefix / source HAR entries.

---

## 3. Files I will NOT touch

- `src/clear_ola/api.py` — already supports `header_overrides` + `referer_override` on `trigger_export` (added on the previous branch). No new API method needed.
- `src/clear_ola/manifest.py` — schema is report-agnostic.
- `src/clear_ola/partials.py` — partials handling stays the same.
- `pyproject.toml` — `package-data = ["*.json"]` already globs the new JSON.
- Any other existing flow.

---

## 4. Verification before writing the flow file

After saving the JSON template, I will print:

1. The exact `filename` field in the JSON (so the code's filename pattern matches HAR exactly).
2. The `staticRowData` keys (sanity-check they're `{companyName, gstin, reportPeriod}`).
3. The `onStart.metadata` keys (so substitution covers all per-(PAN, FY) fields).
4. The Referer URL from entry 118 verbatim (to lock the `report_referer` we build in code).

If all four match what's in §1, I write the flow file. Otherwise I pause and re-confirm with you.

---

## 5. How you'll test it end-to-end

```powershell
# From the activated venv:
python -m clear_ola download --report PAN-Cash-Ledger --pan AAGCP5410J --fy 2024-25
```

That picks a recent FY (full 12 months, no current-FY truncation edge case). Expected output:

```
Step 1/5: refresh Cash Ledger data for 41 underlying GSTINs (01-04-2024..31-03-2025) under tenant CASH_LEDGER_REPORT
Step 2/5: wait for the cash-ledger data refresh
... [polls] ...
Pull settled cleanly. Continuing to export.
Step 3/5: fetch RLS token (workFlow=CASH_LEDGER_REPORT)
Step 4/5: trigger PAN-level cash-ledger export
Step 5/5: wait for export
downloading PAN_CASH_LEDGER_REPORT_AAGCP5410J_01-04-2024-31-03-2025.xlsx.zip
SUCCESS [AAGCP5410J/2024-25/PAN-Cash-Ledger] DONE: ... (N bytes)
```

File lands at `downloads\AAGCP5410J\FY-2024-25\PAN-Cash-Ledger\PAN_CASH_LEDGER_REPORT_AAGCP5410J_01-04-2024-31-03-2025.xlsx.zip`.

**Failure modes to watch for:**
- `500 Unknown error` on export/trigger → header-override set wrong (most likely x-ct-source needed to stay, or Referer slug typo).
- Pull never settles → `tenant` string typo, or `gisDownloadBehaviour: null` is actually meaningful and our `"USE_EXISTING_DATA"` doesn't trigger the cache path.
- Filename mismatch → fix the `_filename_base()` helper to match what Clear's exporter generates.

---

## 6. Open questions / decisions worth confirming before I start

- **FY → date range mapping**: my plan converts each configured FY to `(01-04-YYYY, 31-03-YY+1)`. Special case for 2017-18 = `(01-07-2017, 31-03-2018)` because GST started Jul 2017. Are you OK with this? Alternative would be to add a separate `date_ranges:` field to `config.yaml` and have the cash-ledger flow read it instead — more flexible but more YAML edit. *(My recommendation: per-FY mapping. Keeps the config unchanged; you can always override with `--fy 2024-25` style flags.)*

- **One file per FY vs one big multi-year file**: the HAR captured the full GST era (Jul 2017 → today) in a single file. My plan splits per FY for consistency with every other report (same `downloads/<PAN>/FY-<FY>/<REPORT>/` layout). If you'd prefer one giant file per PAN spanning all years, I can do that too — but per-FY is what the rest of the tool does, and the file naming pattern Clear uses includes the date range, so per-FY files won't collide.

- **`PAN-Cash-Ledger` as the report type string** — fits the existing dash-separated style (`GSTR-2A`, `GSTR-2A-vs-3B-vs-Books`). OK?

---

## 7. Order of work after you approve

1. Save HAR entry #118 body verbatim as `flows/pan_cash_ledger_statement.json`.
2. Report back the 4 verification points from §4.
3. Write `flows/pan_cash_ledger.py` (mirror of `gstr_2a.py` with the substitutions in §2b).
4. Wire `cli.py` and `config.yaml`.
5. Update `discovery/FINDINGS.md`.
6. Hand back the file list + the test command from §5.

No git commit until you've eyeballed the diff.
