# Plan — Add PAN ITC Ledger Report

Branch: `add-pan-itc-ledger-report` (based on `add-pan-cash-ledger-report` — inherits the api.py date-range + nullable `gisDownloadBehaviour` changes)
HAR source: `discovery/app.clear.in.itc.har` (13 MB, 209 entries)
HAR scope: PAN `AAGCP5410J` (PISCES ESERVICES PRIVATE LIMITED), 8 GSTINs, date range `01-07-2017 .. 02-06-2026` (full GST era).

---

## TL;DR — it's the cash-ledger flow with different strings

Same 5-step pipeline. Same DD-MM-YYYY date-range format. Same `pull/v2/trigger → poll → rls/fetch-token (date-range mode) → single export/trigger → poll export/download → S3`. **No preflight**, same header-override set, same `gisDownloadBehaviour: null` in the pull body. We can almost-literally copy `flows/pan_cash_ledger.py` and substitute six strings.

## Diff against cash ledger

| Thing | Cash Ledger | ITC Ledger |
|---|---|---|
| `REPORT_TYPE` | `PAN-Cash-Ledger` | `PAN-ITC-Ledger` |
| Slug (Referer) | `panCashLedger` | `panItcLedger` |
| `TENANT` / `RLS_WORKFLOW` | `CASH_LEDGER_REPORT` | `ITC_LEDGER_REPORT` |
| S3 prefix | `cash_ledger_download` | `itc_ledger_download` |
| Filename prefix | `PAN_CASH_LEDGER_REPORT_<PAN>_<DD-MM-YYYY>-<DD-MM-YYYY>` | `PAN_ITC_LEDGER_REPORT_<PAN>_<DD-MM-YYYY>-<DD-MM-YYYY>` |
| Statement template id | `67e2a6e78ede5b3eac89594d` | `67e2a4bc8ede5b3eac89594a` |
| Statement columns | `mygstin / id / date / totalAmount / igst / cgst / sgst / cess` | `myGstin / state_name / description / formatted_date / totalValue / igstValue / cgstValue / sgstValue / cessValue` |

Everything else (staticRowData keys, onStart/onFinish metadata shape, header-override set, FY → date-range mapping, partials handling) stays identical.

## Files to create

1. `src/clear_ola/flows/pan_itc_ledger_statement.json` — verbatim body of HAR entry #159 (2015 chars).
2. `src/clear_ola/flows/pan_itc_ledger.py` — copy of `pan_cash_ledger.py` with the 6 string substitutions above.
3. One-line entries in `cli.py` (import + Choice + dispatch).
4. One-line entry in `config.yaml` `reports:` list.
5. Short addendum in `discovery/FINDINGS.md`.

## Files I will NOT touch

- `src/clear_ola/api.py` — already has date-range mode for `fetch_rls_token` and nullable `gis_download_behaviour` (added on the cash-ledger branch). Zero changes needed.
- `src/clear_ola/manifest.py`, `partials.py`, `cookies.py`, `config.py` — all report-agnostic.
- `pyproject.toml` — `package-data = ["*.json"]` already globs the new template.

## How you'll test

```powershell
python -m clear_ola download --report PAN-ITC-Ledger --pan AAGCP5410J --fy 2024-25
```

Expected file: `downloads\AAGCP5410J\FY-2024-25\PAN-ITC-Ledger\PAN_ITC_LEDGER_REPORT_AAGCP5410J_01-04-2024-31-03-2025.xlsx.zip`.

## Order of work

1. Save HAR entry #159 body verbatim as `flows/pan_itc_ledger_statement.json`.
2. Write `flows/pan_itc_ledger.py`.
3. Wire `cli.py` (3 lines), `config.yaml` (1 line).
4. Append addendum to `discovery/FINDINGS.md`.
5. Smoke-test the import + CLI listing.
