# Plan — Add PAN Electronic Credit Reversal & Re-claimed Statement (ECRRS)

Branch: `add-pan-ecrrs-report` (based on `add-pan-itc-ledger-report` — inherits api.py date-range + nullable-`gisDownloadBehaviour` changes)
HAR source: `discovery/app.clear.in.ecrrs.har` (5.7 MB, 139 entries)
HAR scope: PAN `AAGCP5410J`, 8 GSTINs, date range `31-08-2023 .. 02-06-2026` (ECRRS earliest valid date → today).

---

## TL;DR — cash-ledger-shaped flow with 3 quirks worth knowing

Same 5-step pipeline (single pull → poll → RLS → single export → poll → S3). Same DD-MM-YYYY date format. Same header-override set on export/trigger. **No preflight.** But three things are unlike Cash Ledger / ITC Ledger:

### Quirk 1: pull tenant ≠ RLS workflow

The pull endpoint takes `tenant: "ELECTRONIC_CASH_LEDGER"` — ECRRS data piggybacks on the same GSTN data source as the Electronic Cash Ledger. But the RLS-token URL uses `workFlow=ELECTRONIC_REVERSAL_REPORT`. So the flow needs **two** constants, not one:

```python
PULL_TENANT = "ELECTRONIC_CASH_LEDGER"
RLS_WORKFLOW = "ELECTRONIC_REVERSAL_REPORT"
```

The `wait_for_pull` polls also need to be issued with `tenant=PULL_TENANT`.

### Quirk 2: earliest valid date is 31-08-2023, not 01-07-2017

GSTN introduced the ECRRS in August 2023. FYs before 2023-24 should be recorded as `no_data` and skipped. FY 2023-24 itself needs its start date clamped from `01-04-2023` to `31-08-2023`.

The existing `_fy_to_date_range()` in `pan_cash_ledger.py` already has a similar clamp (it clamps FY 2017-18 to `01-07-2017`). The ECRRS flow gets its own constant:

```python
ECRRS_START_DATE = date(2023, 8, 31)
MIN_FY = "2023-24"          # FYs strictly earlier than this -> no_data
```

### Quirk 3: filename quirk (replicate verbatim)

The body's `filename` field captured in HAR is `PANElectronicReversalLedger_<PAN>_<DD-MM-YYYY>-<DD-MM-YYYY>` — note **no underscore between `PAN` and `Electronic`**, and CamelCase rather than the SNAKE_CASE used by Cash/ITC ledgers. This is what Clear's UI sends. Replicate it exactly.

Clear's server actually returns a different fileName for the final download: `"Electronic Credit Reversal and Re-claimed Statement..xlsx.zip"` (with a literal double-dot before `xlsx`). Our code uses `ready.file_name` from the API response, so the file on disk uses Clear's name — same pattern as every other flow.

## Diff against PAN ITC Ledger (the closest sibling)

| Thing | ITC Ledger | ECRRS |
|---|---|---|
| `REPORT_TYPE` | `PAN-ITC-Ledger` | `PAN-Electronic-Reversal-Ledger` |
| Slug | `panItcLedger` | `panElectronicReversalLedger` |
| Pull tenant | `ITC_LEDGER_REPORT` | `ELECTRONIC_CASH_LEDGER` *(quirk #1)* |
| RLS workflow | `ITC_LEDGER_REPORT` | `ELECTRONIC_REVERSAL_REPORT` *(quirk #1)* |
| Pull-status tenant (same as pull tenant) | `ITC_LEDGER_REPORT` | `ELECTRONIC_CASH_LEDGER` |
| S3 prefix / `exportName` | `itc_ledger_download` | `ELECTRONIC_CASH_LEDGER_TRANSACTION` |
| Body `filename` pattern | `PAN_ITC_LEDGER_REPORT_<PAN>_<dates>` | `PANElectronicReversalLedger_<PAN>_<dates>` *(quirk #3)* |
| Statement template id | `67e2a4bc8ede5b3eac89594a` | `676e7c58fedefe6d880609ba` |
| `MIN_FY` floor | none | `2023-24` *(quirk #2)* |
| Date floor inside FY | none (`01-07-2017` clamp for 2017-18 only) | `31-08-2023` for FY 2023-24 |

Everything else (`staticRowData` shape, `onStart`/`onFinish` substitution loop, header overrides, partials handling) is unchanged.

## Files to create

1. `src/clear_ola/flows/pan_electronic_reversal_ledger_statement.json` — already extracted from HAR entry #113 (saved during scan). 2191-char verbatim body.
2. `src/clear_ola/flows/pan_electronic_reversal_ledger.py` — copy of `pan_itc_ledger.py` with the diffs from §"Diff" above, plus the two new constants (`PULL_TENANT` + `MIN_FY` / `ECRRS_START_DATE`) and the modified `_fy_to_date_range()` that clamps to `31-08-2023`.
3. One-line entries in `cli.py` (import + Choice + dispatch).
4. One-line entry in `config.yaml` `reports:` list.
5. Short addendum in `discovery/FINDINGS.md`.

## Files I will NOT touch

- `src/clear_ola/api.py` — date-range mode for `fetch_rls_token` + nullable `gis_download_behaviour` already present. Zero changes.
- Everything else report-agnostic.

## How you'll test

```powershell
python -m clear_ola download --report PAN-Electronic-Reversal-Ledger --pan AAGCP5410J --fy 2024-25
```

Expected file: `downloads\AAGCP5410J\FY-2024-25\PAN-Electronic-Reversal-Ledger\Electronic Credit Reversal and Re-claimed Statement..xlsx.zip` (filename comes from Clear's API response, includes spaces and a literal double-dot — that's verbatim what Clear sends and what we write to disk).

For FY 2023-24, the flow clamps start date to `31-08-2023` (not `01-04-2023`). For FY 2022-23 and earlier, the flow records `no_data` in the manifest and skips.

## Order of work

1. Save JSON template (done during HAR scan — `pan_electronic_reversal_ledger_statement.json` already on disk).
2. Write `flows/pan_electronic_reversal_ledger.py`.
3. Wire `cli.py` (3 lines), `config.yaml` (1 line).
4. Append addendum to `discovery/FINDINGS.md`.
5. Smoke-test import + CLI listing.
