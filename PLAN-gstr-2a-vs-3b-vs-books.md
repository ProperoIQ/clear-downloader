# Plan — Add GSTR-2A vs 3B vs Books report

Branch: `add-gstr-2a-vs-3b-vs-books`
HAR source: `D:\office\test-downloader\app.clear.in.har` (7.3 MB, 169 entries)
HAR scope: PAN `AAECB1261D`, single GSTIN node, FY `2020-21` (periods `042020..032021`)

---

## 1. What the HAR confirmed

Already extracted, no more guessing required.

| Thing | Value | Source |
|---|---|---|
| URL slug (Referer query) | `reportType=panG3bvs2avsBooks` | entries 72/114/126/149 |
| Pull tenant | `GSTR2A_VS_3B_VS_BOOKS_REPORTS` | entry 72 body |
| RLS workflow (query string) | `workflow=GSTR2A_VS_3B_VS_BOOKS_REPORTS` *(to verify when extracting full RLS URL)* | entry 114 URL |
| Preflight S3 prefix | `pan_G2Avs3B_download_Adv` | entry 145/146 URLs |
| Real-export S3 prefix | `pan_G2Avs3BvsBook_download_Adv` | entry 165/166 URLs |
| Preflight filename pattern | `PAN_PAN_GSTR2A_vs_3b_...` (double `PAN_PAN_`, lowercase `3b` — same quirk as 2B variant) | entries 145/146 |
| Real-export filename pattern | `PAN_GSTR2A_vs_3B...` *(exact suffix TBD when reading the body)* | entries 165/166 |
| Preflight export id | `6a1d554e1f7bf61357f41e77` | entry 126 response |
| Real-export export id | `6a1d55575e5375fce4b05818` | entry 149 response |

Header set on `export/trigger` (both calls): `content-type`, `baggage`, `sentry-trace`, `priority`, `accept-language`, `referer` (with the panG3bvs2avsBooks slug), `x-rls-token` (same token reused for both). **`x-ct-source` is absent** — identical to the 2B-vs-3B-vs-Books pattern. The flow code must therefore set `x-ct-source: None` in `header_overrides`.

Same RLS token (`28fe7a22-...`) is reused for both export/trigger calls — confirming the existing 2B-vs-3B-vs-Books pattern of "one fetch-token, two triggers."

`pull/v2/trigger` for this report uses `gisDownloadBehaviour: USE_EXISTING_DATA` (the default — same as 2B variant), with `metadata: {reportLevel: "PAN"}`.

---

## 2. Files I will create

### a. `src/clear_ola/flows/gstr_2a_vs_3b_vs_books_preflight_statement.json`
**Verbatim** body of HAR entry #126 (the "G2A vs 3B" preflight call). Substituted at runtime: `staticRowData`, `filename`, `onStart.metadata.{orgId,workspaceId,nodeName,startRange,endRange,activeBusiness}`, `onFinish.metadata.{same}`. Everything else (the `statement` SELECT block, `exportName`, etc.) is left untouched.

### b. `src/clear_ola/flows/gstr_2a_vs_3b_vs_books_statement.json`
**Verbatim** body of HAR entry #149 (the real "G2A vs 3B vs Books" export call). Same substitution policy.

### c. `src/clear_ola/flows/gstr_2a_vs_3b_vs_books.py`
Copy of `gstr_2b_vs_3b_vs_books.py` with these targeted changes:

| Constant / value | 2B variant | 2A variant |
|---|---|---|
| `REPORT_TYPE` | `"GSTR-2B-vs-3B-vs-Books"` | `"GSTR-2A-vs-3B-vs-Books"` |
| `TENANT` / `RLS_WORKFLOW` | `"GSTR2B_VS_3B_VS_BOOKS_REPORTS"` | `"GSTR2A_VS_3B_VS_BOOKS_REPORTS"` |
| `MIN_FY` | `"2020-21"` | **REMOVE** (GSTR-2A exists since GST began, Jul 2017) |
| `MIN_START_PERIOD` | `"072020"` | **REMOVE** (no period clipping needed) |
| Referer slug | `panG3bvs2bvsBooks` | `panG3bvs2avsBooks` |
| Preflight filename | `PAN_PAN_GSTR2B_vs_3b_Report_<PAN>_<start>-<end>` | `PAN_PAN_GSTR2A_vs_3b_Report_<PAN>_<start>-<end>` *(adjust to match what the HAR body actually says)* |
| Real filename | `PAN_GSTR2B_vs_3B_vs_Books_Report_<PAN>_<start>-<end>` | `PAN_GSTR2A_vs_3B_vs_Books_Report_<PAN>_<start>-<end>` *(adjust to HAR body)* |
| JSON template paths | `gstr_2b_vs_3b_vs_books_*_statement.json` | `gstr_2a_vs_3b_vs_books_*_statement.json` |
| Docstring | "Compares ITC available per GSTR-2B vs ITC claimed per GSTR-3B vs Books" | "Compares ITC available per GSTR-2A vs ITC claimed per GSTR-3B vs Books" |

Everything else stays: preflight-then-real call ordering, partials handling, `_warn_if_upstream_3b_stale` (3B still comes from cache, only 2A side is refreshed by this report's pull step), the exact same `header_overrides` set (drop `x-ct-source`, add baggage/sentry-trace/accept-language/priority).

### d. `src/clear_ola/flows/__init__.py`
Add the new flow to the report registry (single import + dict entry, follows the pattern already in this file).

### e. `src/clear_ola/cli.py`
Add `"GSTR-2A-vs-3B-vs-Books"` to the `--report` choices list (the existing CLI is just a dispatch table).

### f. `pyproject.toml`
Add the two new JSON files to `[tool.setuptools.package-data]` so they ship with the editable install. They sit alongside the existing `gstr_2a_statement.json` / `gstr_2b_vs_3b_vs_books_*_statement.json` entries — same glob pattern likely covers them already; will verify.

### g. `config.yaml`
Add `"GSTR-2A-vs-3B-vs-Books"` to the informational `reports:` list. No other config changes.

### h. `discovery/FINDINGS.md`
Append a row to the slug table (or whichever section enumerates report metadata) recording: slug, tenant, RLS workflow, preflight required, filename pattern, header-override set, source HAR path + entry numbers.

### i. *(Optional but worth it)* `discovery/gstr-2a-vs-3b-vs-books-walkthrough.har`
Move/copy the source HAR into `discovery/` so it lives alongside the other reverse-engineering artifacts and the JSON templates are traceable to the capture.

---

## 3. Files I will NOT touch

- `src/clear_ola/api.py` — already supports `referer_override` and `header_overrides` on `trigger_export`. No changes needed.
- `src/clear_ola/manifest.py` — schema is report-agnostic; the new `REPORT_TYPE` string just appears as another value.
- `src/clear_ola/partials.py` — same.
- Any other existing flow file. No rename/refactor.
- Any of the existing JSON template files.
- `.auth/`, `state/`, `downloads/`, `logs/` — runtime dirs, not part of this change.

---

## 4. Verification before writing the flow file

After saving the two JSON templates, I will print:

1. The exact `filename` field from each JSON (so we lock the filename pattern in code to match).
2. The `staticRowData` keys (sanity-check they're `{companyName, gstin, reportPeriod}` like every other flow).
3. The `onStart.metadata.reportType` value from the real body (expected: `panG3bvs2avsBooks`).
4. The full Referer URL from entry #149 (to confirm the exact set of query params our flow's `report_referer` must replicate).
5. The rls/fetch-token URL's `workflow=` param (to confirm `RLS_WORKFLOW`).

I will report these back to you before writing `gstr_2a_vs_3b_vs_books.py`. No code is written on assumptions.

---

## 5. How you'll test it end-to-end

```powershell
# From the activated venv:
python -m clear_ola auth-check
python -m clear_ola download --report GSTR-2A-vs-3B-vs-Books --pan AAECB1261D --fy 2020-21
```

That replays the exact (PAN, FY) the HAR was captured for. If our flow is correct, the resulting `.xlsx.zip` should land at `downloads/AAECB1261D/FY-2020-21/GSTR-2A-vs-3B-vs-Books/PAN_GSTR2A_vs_3B_vs_Books_Report_AAECB1261D_042020-032021.xlsx.zip` (filename derived from the captured pattern) and the manifest row goes to `status=done`.

Failure modes to watch for:
- `500 Unknown error` on the **real** export-trigger → preflight wasn't actually required OR our preflight body differs from HAR. Diff `state/last-export-payload.json` against entry #126 body.
- `500 Unknown error` on the **preflight** export-trigger → header overrides wrong (probably `x-ct-source` not being dropped, or wrong Referer slug).
- Stuck `pull/v3/status` poll → wrong tenant string on `pull/v2/trigger`, or the captured FY's GSTIN session is now expired (would need fresh OTP in Clear's UI).

---

## 6. Order of work after you approve

1. Re-run the HAR scan to dump entries #126 and #149 bodies in full and save them as the two JSON template files. **(blocked: needs your "go")**
2. Report back the five verification points from §4 above.
3. Write `flows/gstr_2a_vs_3b_vs_books.py` from the 2B template with the substitutions in §2c.
4. Wire `flows/__init__.py`, `cli.py`, `pyproject.toml`, `config.yaml`.
5. Update `discovery/FINDINGS.md` and move the HAR into `discovery/`.
6. Hand back: file list + the test command from §5.

No git commit until you've eyeballed the diff.
