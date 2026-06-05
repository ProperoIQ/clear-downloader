# Plan — Add GSTR-1+1A vs 3B vs Books report

Branch: `add-gstr-1-1a-vs-3b-vs-books` (user creates, base = `report_fixes_2`)
HAR source: `discovery/app.clear.in.har__GSTR1+1A vs 3B vs Books Report_1.har` (~35 MB, 322 entries)
HAR scope: PAN `AAKCA2311H` (OLA FLEET TECHNOLOGIES PRIVATE LIMITED), FY `2017-18` (periods `072017..032018`)

This report is the GSTR-1A-aware sibling of the existing `GSTR-1-vs-3B-vs-Books` flow. Same overall shape (preflight + real export, FY-bound MMYYYY periods, RLS reconciliation endpoint, header overrides), but **different slug, tenant, workflow, template id, filename pattern, exportName, and one extra column in the statement payload**. Everything was extracted from the HAR — no guesses below.

---

## 1. What the HAR confirmed

| # | Identifier | Value | Source |
|---|---|---|---|
| 1 | URL slug (`reportType=`) | `G1_1Avs3BvsBooks` | Referer on entries #280 / #302; also `onStart.metadata.reportType` in both export bodies |
| 2 | Pull tenant (`pull/v2/trigger` body) | `GSTR1_1A_VS_3B_VS_BOOKS_REPORTS` | entry #172 |
| 3 | RLS workflow (URL `workFlow=`) | `GSTR1_1A_VS_3B_VS_BOOKS_REPORTS` *(same string as PULL_TENANT — different from the GSTR-1-vs-3B-vs-Books flow where they diverge)* | entry #259 |
| 4 | Date format | MMYYYY periods, FY-bound (`returnPeriods=072017&returnPeriods=082017&...`) | entry #259 URL |
| 5 | Preflight presence | YES — **2** `export/trigger` POSTs (entry #280 preflight, entry #302 real) | scan output |
| 6 | Statement template id (`statement.from.id`) | `68ec86132687ac470c0d769f` — used by BOTH preflight and real | both bodies |
| 7a | Preflight filename | `PAN_GSTR1_1A_vs_3b_Report_<PAN>_<startMMYYYY>-<endMMYYYY>` (lowercase `3b`, single `PAN_` prefix) | entry #280 body |
| 7b | Real filename | `PAN_GSTR1_1A_vs_3B_vs_Books_Report_<PAN>_<startMMYYYY>-<endMMYYYY>` | entry #302 body |
| 8 | Header overrides on `export/trigger` | `x-ct-source` absent → must override to None; `baggage`, `sentry-trace`, `accept-language`, `priority` present | both entries' request headers |

Other captured details that go into the templates verbatim:

| Field | Preflight (#280) | Real (#302) |
|---|---|---|
| `exportName` | `G1_1Avs3B_Export` | `G1_1Avs3BvsBook_Export` |
| `onStart.metadata.filename` (UI label) | `GSTR1+1A vs 3B Report (XLSX)` | `GSTR1+1A vs 3B vs Books Report (XLSX)` |
| `statement.limit` | `100` | `0` |
| `statement.from.templateType` | (absent) | `"QUERY"` |
| top-level `fileType` | (absent) | `"XLSX"` |
| `statement.fields` count | 14 columns (incl. `mygstin`, `sort_order`, `is_bold`, `is_tab`, `mapping_id`, `returnPeriod`, `taxablevalue`, plus tax columns + `totalTaxSum`) | 8 columns (`description`, `taxablevalue`, `totalTax`, `igstValue`, `cgstValue`, `sgstValue`, `cessValue`, `totalTaxSum`) |
| Referer `timePeriodType` | `FISCAL_YEAR` | `FISCAL_YEAR` |

**Same RLS token reused for both export-trigger calls** — confirms the one-fetch-token-two-triggers pattern from `gstr_1_vs_3b_vs_books`.

`pull/v2/trigger` for this report sends `gisDownloadBehaviour: "USE_EXISTING_DATA"` (string, not null — matches existing reconciliation flows).

`accept-language` in the HAR is `en-US,en;q=0.9,ta;q=0.8` (browser-specific — includes Tamil). We'll stick with `en-US,en;q=0.9` (matching the existing `gstr_1_vs_3b_vs_books` flow's hard-coded value) — regional preference doesn't influence Clear's response.

---

## 2. Diff against `gstr_1_vs_3b_vs_books` (the closest existing flow)

This is the template we will copy.

| Constant / value | `gstr_1_vs_3b_vs_books` (current) | `gstr_1_1a_vs_3b_vs_books` (new) |
|---|---|---|
| `REPORT_TYPE` | `"GSTR-1-vs-3B-vs-Books"` | `"GSTR-1-1A-vs-3B-vs-Books"` |
| `RLS_WORKFLOW` | `"G1_VS_3B_VS_BOOKS"` | `"GSTR1_1A_VS_3B_VS_BOOKS_REPORTS"` |
| `PULL_TENANT` | `"GSTRG1_VS_3B_VS_BOOKS_REPORTS"` *(odd `GSTRG1` prefix — verbatim Clear quirk)* | `"GSTR1_1A_VS_3B_VS_BOOKS_REPORTS"` *(same as RLS_WORKFLOW)* |
| Referer slug | `panG3bvs1vsBooks` | `G1_1Avs3BvsBooks` |
| Preflight filename pattern | `PAN_PAN_GSTR1_vs_3b_Report_...` *(double-`PAN_` quirk)* | `PAN_GSTR1_1A_vs_3b_Report_...` *(single `PAN_`)* |
| Real filename pattern | `PAN_GSTR1_vs_3B_vs_Books_Report_...` | `PAN_GSTR1_1A_vs_3B_vs_Books_Report_...` |
| MIN_FY | `"2017-18"` | `"2017-18"` *(HAR-captured FY = 2017-18; Clear's backend accepts pre-2024 FYs — 1A portion will be empty for pre-Aug-2024 months, but the export succeeds. Same MIN as the parent flow.)* |
| MIN_START_PERIOD | `"072017"` | `"072017"` *(HAR confirms identical clipping)* |
| `_STALE_DAYS` warn threshold | `7` | `7` *(unchanged)* |
| JSON template paths | `gstr_1_vs_3b_vs_books_*_statement.json` | `gstr_1_1a_vs_3b_vs_books_*_statement.json` |
| Header overrides on export/trigger | drop `x-ct-source`, add `baggage` / `sentry-trace` / `accept-language` / `priority` | **identical** |

Everything else stays: preflight-then-real call ordering, the pre-export `trigger_pull` + `wait_for_pull` (different tenant), `_warn_if_upstream_stale` on GSTR-1 + GSTR-3B-Combined manifest rows, PAN-level XLSX output path.

**No `api.py` changes needed.** Existing `trigger_export(referer_override=…, header_overrides=…)` and `fetch_rls_token(periods=…, workflow=…)` cover everything this report does. Verified by inspection of [src/clear_ola/flows/gstr_1_vs_3b_vs_books.py](src/clear_ola/flows/gstr_1_vs_3b_vs_books.py).

---

## 3. Files I will create

### a. `src/clear_ola/flows/gstr_1_1a_vs_3b_vs_books_preflight_statement.json`
Verbatim body of HAR entry #280 (already saved to [discovery/har_extract_1_1a_vs_3b_call_1.json](discovery/har_extract_1_1a_vs_3b_call_1.json) by the scan script). Will be copied into the flows package as the canonical template. Per-(PAN, FY) substitutions at runtime: `staticRowData.*`, top-level `filename`, `onStart.metadata.{orgId,workspaceId,nodeName,startRange,endRange,activeBusiness}`, `onFinish.metadata.{same}`. The `statement` block, `exportName`, and the `onStart/onFinish.metadata.filename` UI label stay untouched.

### b. `src/clear_ola/flows/gstr_1_1a_vs_3b_vs_books_statement.json`
Verbatim body of HAR entry #302 ([discovery/har_extract_1_1a_vs_3b_call_2.json](discovery/har_extract_1_1a_vs_3b_call_2.json)). Same substitution policy.

### c. `src/clear_ola/flows/gstr_1_1a_vs_3b_vs_books.py`
Copy of [src/clear_ola/flows/gstr_1_vs_3b_vs_books.py](src/clear_ola/flows/gstr_1_vs_3b_vs_books.py) with the substitutions listed in §2. Docstring updated to describe the GSTR-1A-aware variant and note that for FYs ending before Aug 2024 the 1A column will be empty (domain limitation, not a tool bug). The preflight payload's filename quirk (single `PAN_` prefix vs the parent flow's double `PAN_PAN_`) is documented in the `_build_preflight_payload` docstring.

### d. `src/clear_ola/cli.py`
Three edits, each preserves the existing alphabetical-ish grouping:
- Add `gstr_1_1a_vs_3b_vs_books` to the `from clear_ola.flows import (...)` block (next to `gstr_1_vs_3b_vs_books`).
- Add `"GSTR-1-1A-vs-3B-vs-Books"` to the `click.Choice([...])` list on `download --report`.
- Add an `elif report_choice.upper() == "GSTR-1-1A-VS-3B-VS-BOOKS":` branch in the dispatcher, calling `gstr_1_1a_vs_3b_vs_books.run(api, cfg, manifest)`.

### e. `config.yaml`
Append `"GSTR-1-1A-vs-3B-vs-Books"` to the informational `reports:` list and update the comment block above it to include the new name.

### f. `discovery/FINDINGS.md`
Append an addendum documenting:
- Source HAR: `discovery/app.clear.in.har__GSTR1+1A vs 3B vs Books Report_1.har`, entries #172 (pull), #259 (RLS), #280 (preflight export), #302 (real export).
- Confirmed identifiers (slug, PULL_TENANT, RLS_WORKFLOW, filename pattern, template id).
- Quirks: PULL_TENANT == RLS_WORKFLOW (unlike the parent flow); preflight filename has single `PAN_` not double `PAN_PAN_`; statement adds a `taxablevalue` column.

---

## 4. Files I will NOT touch

- [src/clear_ola/api.py](src/clear_ola/api.py) — already supports `referer_override` + `header_overrides` on `trigger_export`. No new mode needed.
- [src/clear_ola/manifest.py](src/clear_ola/manifest.py), [src/clear_ola/partials.py](src/clear_ola/partials.py) — schema is report-agnostic; new `REPORT_TYPE` string is just another value.
- Any existing flow file. No rename, no refactor, no shared base class.
- Any existing JSON template.
- [pyproject.toml](pyproject.toml) — the `package-data` glob `["*.json"]` already covers new JSON templates (verified by the mission brief).
- `.auth/`, `state/`, `downloads/`, `logs/` — runtime dirs.

---

## 5. Smoke test (after implementation, before you run real)

```powershell
# Create a fresh venv if you don't have one yet (uv recommended):
uv sync
# or:
py -3 -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -e .

# Then:
.\.venv\Scripts\python.exe -c "from clear_ola.flows import gstr_1_1a_vs_3b_vs_books; print('import OK')"
.\.venv\Scripts\python.exe -m clear_ola download --help
```

Expected: import succeeds; `--report` choices listing includes `GSTR-1-1A-vs-3B-vs-Books` (case-insensitive).

---

## 6. Real test on your laptop (after you've reviewed the diff)

```powershell
# In a session where Chrome is closed, with cookies-import already done:
.\.venv\Scripts\python.exe -m clear_ola download --report GSTR-1-1A-vs-3B-vs-Books --pan AAKCA2311H --fy 2017-18
```

This replays the exact (PAN, FY) captured in the HAR. If everything is wired right, the resulting XLSX (or .xlsx.zip) lands at:

```
downloads/AAKCA2311H/FY-2017-18/GSTR-1-1A-vs-3B-vs-Books/PAN_GSTR1_1A_vs_3B_vs_Books_Report_AAKCA2311H_072017-032018.xlsx
```

Failure modes to watch for:
- **`500 Unknown error` on the real export** → either the preflight didn't fire, or the preflight body diverged from HAR. Diff `state/last-export-payload.json` against [discovery/har_extract_1_1a_vs_3b_call_1.json](discovery/har_extract_1_1a_vs_3b_call_1.json).
- **`500` on the preflight** → header overrides wrong (probably `x-ct-source` not being dropped, or wrong Referer slug).
- **Empty/tiny XLSX (~17 KB)** → recon cube didn't materialize. Either GSTR-1 + GSTR-3B hadn't been pulled (run those reports first), or the tenant string on `pull/v2/trigger` is wrong.

---

## 7. Open questions / quirks worth flagging

1. **`PULL_TENANT == RLS_WORKFLOW`** — for this report, both are `GSTR1_1A_VS_3B_VS_BOOKS_REPORTS`. The parent flow had two different strings (one with `_REPORTS` suffix, one without). The HAR is unambiguous, but if you have intuition that Clear's backend treats these as separate namespaces, let me know — we replicate the HAR either way.

2. **HAR was captured for FY 2017-18**, well before GSTR-1A existed (Aug 2024). Clear's API responded 200 and produced a pre-signed URL, so the endpoint itself is FY-agnostic. The 1A column in the resulting XLSX will be empty for any month before Aug 2024. We keep `MIN_FY = "2017-18"`. If you'd prefer to gate this report at FY 2024-25 in our tool (so users don't waste API calls on FYs where 1A is structurally empty), say so.

3. **Filename quirk diverges from the parent**: parent's preflight has `PAN_PAN_GSTR1_vs_3b_Report_...` (double prefix). This report's preflight has `PAN_GSTR1_1A_vs_3b_Report_...` (single prefix). Looks like Clear "fixed" the typo for the new endpoint. We replicate verbatim — single prefix on this one, double on the old one.

4. **Statement has an extra column (`taxablevalue`)** vs the parent's real-export statement. This is fully captured by the verbatim JSON template — no runtime logic needed.

5. **`exportName` format completely different** (`G1_1Avs3BvsBook_Export` vs the parent's `G1vs3BvsBook vertial download pan Adv`). Both verbatim from their HARs; the parent's value has a "vertial" typo that we know works. We trust the HAR.

---

## 8. Order of work after you say "go"

1. Copy the two saved bodies into `src/clear_ola/flows/` as the canonical JSON templates.
2. Write `flows/gstr_1_1a_vs_3b_vs_books.py` from the parent flow with the §2 substitutions.
3. Wire `cli.py` (3 edits) and `config.yaml` (1 edit).
4. Append addendum to `discovery/FINDINGS.md`.
5. Run the §5 smoke tests; report results.
6. Hand back: file list + the §6 real test command.

No git commit, no push. Diff stays on disk for you to review.
