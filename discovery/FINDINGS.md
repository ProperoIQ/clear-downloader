# Discovery Findings — Inwards download flow

**Source:** `discovery/inwards-walkthrough.har` (253 entries, 10.8 MB, captured 20 May 2026)
**Scope walked through:** PAN `AAGCP5410J` (Pisces eServices Private Limited), FY 2025-26, PAN GSTR-2A → Document Level export.
**Analysis tools:** `discovery/parse_har.py` (route summary + downloads + polls) and `discovery/extract_auth.py` (auth headers).

## Top-line conclusion

**Every report in scope (PAN GSTR-2A / 2B / 6A / 8A — Document Level) can be downloaded entirely via authenticated HTTP API calls. No Playwright / browser automation is required.** The flow uses a small set of JSON endpoints under `app.clear.in/api/...` plus a final AWS S3-presigned URL for the file bytes. Auth is by Chrome session cookies (read at runtime via `browser-cookie3`) plus a small bag of static + dynamic `x-*` headers we replay verbatim from the HAR.

This is the **mode (a)** classification from the plan — direct API → `requests` + cookies, no Playwright.

## How auth works

| Layer | What it is | How we get it |
|---|---|---|
| **Session cookies** | Standard HttpOnly cookies on `.clear.in` from your normal Chrome login. Sent automatically by Chrome on every request; required by every `/api/...` endpoint. *(Note: Chrome's HAR export deliberately strips Cookie values from the captured headers — that's why our parser saw no Cookie names. They are still being sent; the server's 200 responses confirm this.)* | `browser-cookie3` reads Chrome's encrypted cookie store on Windows via DPAPI under your user account. Returns a `requests`-compatible `CookieJar`. No OTP needed unless the cookies have expired. |
| **`x-rls-token`** | A short-lived (24h) token scoped to a set of `returnPeriods` + a `workFlow` (e.g. `GSTR2A_REPORTS`). Required on `/api/clear/data-browser/public/export/trigger` only. | Fetched fresh at run time via `POST /api/gst-auto-compute/public/rls/fetch-token?returnPeriods=...&workFlow=...`. Returns `{"token": "<uuid>", "expiry": "<iso8601>"}`. |
| **Identifier headers (static per workspace)** | `x-cleartax-country: in`, `x-cleartax-product: GST`, `x-cleartax-source: APPCLEAR`, `x-cleartax-orgunit: <workspaceId>`, `x-workspace-id: <workspaceId>`, `x-organisation-id: <workspaceId>` | `<workspaceId>` is the UUID in the post-login URL (`?workspaceId=a8a3363c-...`). For your account: `a8a3363c-b12b-4e7d-bd00-f81aafd07a89`. Could also be discovered programmatically from `/api/enterprise-orchestrator/public/business-hierarchy/v1` if we want to avoid hardcoding. |
| **Identifier headers (per request)** | `x-clear-node-id: <comma-separated nodeIds>`, `x-clear-node-type: GSTIN` | Set per call. Node IDs are looked up once from `/api/enterprise-orchestrator/public/business-hierarchy/v1/user_gstins` (returns `gstinNodeId` and `panNodeId` for every GSTIN). |
| **`x-request-id`** | Random per-request token (looks like nanoid). Used for tracing on the server side. | Generate a fresh random string per request — server doesn't validate format strictly. |
| **`x-tenant-name`** | `GST_REPORTS`. Required on the data-browser endpoints. | Hardcode for inwards flow. |

## The endpoint flow (Document Level export, PAN GSTR-2A)

Numbered to match the order the frontend invokes them. Anything marked **(once)** is fetched once at startup or once per script run; anything marked **(per export)** repeats per report download.

### Bootstrap (once per session)

1. `GET /api/enterprise-orchestrator/public/business-hierarchy/v1?nodeType=GSTIN&resource=INGESTION_GSTINVERIFICATION&permission=EDIT`
   → Full hierarchy: workspaces → organisations → PANs → GSTINs. Gives us workspace UUID and PAN/GSTIN nodeIds.
2. `GET /api/enterprise-orchestrator/public/business-hierarchy/v1/user_gstins?isTaxpayerTypeRequired=true&isTokenInfoRequired=true&pageId=LP2_REPORTS`
   → Flat list of GSTINs with `gstin`, `gstinNodeId`, `panNodeId`, `businessName`, `tokenExpiryTimeStamp`. **This is our PAN → GSTIN node-ID map.** We index this once and reuse for all downloads.
3. `GET /api/data-pull/public/onboarding/v2.0/auth/fetchUserSessionStatus`
   → Per-GSTIN: `isSessionActive`, `tokenExpiryTimeStamp`. Tells us whether Clear's stored GSTN session for each GSTIN is still alive (separate from your Clear login session — Clear caches the GSTN credentials). Optional precheck.

### Per (PAN × FY × report-type) download

4. `POST /api/data-pull/public/pull/prefetchStatus`
   - Body: `{"nodeIds": [<all GSTIN nodeIds under the PAN>], "returnPeriods": ["042025", ..., "032026"], "dataSource": "GSTR2A"}` *(field names confirmed; the exact `dataSource` key/value to be verified against the live POST body — captured in the HAR's full body)*
   - Returns per-GSTIN freshness: `prefetchUXNodeLevelResponseList[]` with status info.
   - **Optional but cheap** — gives us "data already available" vs "need to refresh" signal so we skip the trigger step when not needed.
5. `POST /api/data-pull/public/pull/v2/trigger`
   - Body includes:
     ```json
     {
       "nodeType": "GSTIN",
       "nodeIds": ["<gstinNodeId-1>", "<gstinNodeId-2>", ...],
       "dataSources": ["GSTR2A"],
       "returnPeriods": ["042025", "052025", ..., "032026"]
     }
     ```
   - Returns `{"requestId": "<id>", "pullRequestInfo": {...}}`. `requestId` (also called `pullRequestId`) is the **job ID** we poll on next. Example from your walkthrough: `6a0dab711c9ab93806328bdb`.
6. `POST /api/data-pull/public/pull/v3/status` — **POLL**
   - Body: `{"nodeIds": [<same GSTIN nodeIds>]}` (the same list).
   - Returns `{"statusResponses": [{"nodeId":"...","downloadStatus":"DOWNLOADING|DOWNLOADED","downloadPercentage":0..100,"returnPeriods":[...]}, ...]}`
   - **Poll until every entry shows `downloadStatus: "DOWNLOADED"`.** Observed cadence in the HAR: ~10–15s between polls; 9 polls total for FY 2025-26 multi-GSTIN. Use exponential backoff starting at 5s, cap at 30s, timeout at 5–10 minutes.
7. `POST /api/gst-auto-compute/public/rls/fetch-token?returnPeriods=042025&returnPeriods=...&workFlow=GSTR2A_REPORTS`
   - Returns `{"token": "<uuid>", "expiry": "<iso>"}`.
   - Use this token as the `x-rls-token` header on the next call.
8. `POST /api/clear/data-browser/public/export/trigger`
   - Headers must include `x-rls-token: <token from step 7>` and `x-tenant-name: GST_REPORTS`.
   - Body is a JSON SQL-like `statement` describing the export (fields, dataType, filters). The full payload is captured in the HAR — we lift it verbatim and parameterize only the obvious knobs:
     - `dataType: "GOVT_GSTR2A_LINE"` → swap for 2B/6A/8A
     - filters → match on PAN nodeId and return periods (= the FY months)
     - selected columns → match the "Document Level" tab schema
   - **Response is a single 24-hex-char string**, not JSON: e.g. `6a0dabf1a73ff906f7ba7896`. This is the **export job ID**.
9. *(brief wait — likely a few seconds to a minute for Clear's exporter to write the file to S3)*
10. `GET /api/clear/data-browser/public/export/download/<exportJobId>`
    - Returns JSON: `{"fileName": "PAN_MM2A_Document_<PAN>_<MMYYYY>-<MMYYYY>.xlsx.zip", "preSignedUrl": "https://app.clear.in/storage/v1/ap-south-1/einvoice-backend-prod/<workspaceId>/GST_REPORTS/invoice_cdn_line/<filename>?X-Amz-Algorithm=...&X-Amz-Signature=..."}`
    - **Polling behavior unknown from this single capture** — the user waited for the tray notification before clicking download, so we only see the successful 200 case. Need experimental probe: does this endpoint return a different status / empty `preSignedUrl` if the export isn't ready yet? Plan: implement as a poll (5s → 15s → 30s, cap 5 min) that retries on any non-200 OR on a 200 with empty/missing `preSignedUrl`.
11. `GET <preSignedUrl>` (against `app.clear.in/storage/v1/...`)
    - Response: raw `application/octet-stream` bytes (a `.xlsx.zip` file). Captured size in walkthrough: 37,479 bytes.
    - **Auth is the AWS Signature v4 baked into the URL — no cookies / no x-* headers needed.** This is the only call where we use a clean `requests.get(stream=True)` and write the body to disk.
    - The presigned URL is valid for ~24 hours (`X-Amz-Expires=86400`).

### Notifications tray (optional alternative to step 10)

12. `GET /api/data-pull/public/getRecentTriggeredReports?level=USER`
    - Returns `{"jobs": [{"jobId": "<id>", "jobType": "PAN_MM2A_REPORT", "status": "DOWNLOADED", "jobMetadata": {...}}, ...]}`. This is the UI "notifications tray".
    - Could be polled in parallel as a fallback signal for export readiness, but step 10 is the simpler primary mechanism. The `jobId` here is the **pull** request ID (step 5), not the **export** job ID (step 8). They are different IDs in the same flow — be careful not to confuse them.

## Slug values per report type

The frontend distinguishes report types via small string differences. From the HAR we have GSTR-2A confirmed; the others should follow the same naming pattern (confirm during implementation by repeating the walkthrough for one period of each):

| Report | URL `reportType` | `dataSources` | `data_type` (data-browser) | `workFlow` (rls/fetch-token) | `jobType` | File-prefix in S3 |
|---|---|---|---|---|---|---|
| PAN GSTR-2A | `panMm2a` | `["GSTR2A"]` | `GOVT_GSTR2A_LINE` | `GSTR2A_REPORTS` | `PAN_MM2A_REPORT` | `PAN_MM2A_Document_<PAN>_<period>` |
| PAN GSTR-2B | `panMm2b` *(predicted)* | `["GSTR2B"]` | `GOVT_GSTR2B_LINE` | `GSTR2B_REPORTS` | `PAN_MM2B_REPORT` | `PAN_MM2B_Document_<PAN>_<period>` |
| PAN GSTR-6A | `panMm6a` *(predicted)* | `["GSTR6A"]` | `GOVT_GSTR6A_LINE` *(unsure)* | `GSTR6A_REPORTS` *(predicted)* | `PAN_MM6A_REPORT` *(predicted)* | TBD |
| PAN GSTR-8A | `panMm8a` *(predicted)* | `["GSTR8A"]` | `GOVT_GSTR8A_LINE` *(unsure)* | `GSTR8A_REPORTS` *(predicted)* | `PAN_MM8A_REPORT` *(predicted)* | TBD |

**Validation step before implementation:** run the same walkthrough for one period of GSTR-2B and capture a second small HAR (`discovery/2b-walkthrough.har`). Twenty minutes of work, confirms the predicted slugs, and we know the 6A/8A patterns immediately by analogy.

## Period formatting

Periods are passed as `MMYYYY` strings (no separator), e.g. April 2025 → `"042025"`. A full FY is the 12 months from April → next March, e.g. FY 2025-26 = `["042025","052025","062025","072025","082025","092025","102025","112025","122025","012026","022026","032026"]`.

## What we don't yet know (open questions for implementation)

1. **Exact body shape of `pull/prefetchStatus` and `pull/v2/trigger`** — the parser truncated request bodies at 300 chars. Need to grab the full bodies from the HAR (trivial — they're in there) when implementing `data_pull.py`.
2. **Exact `statement` shape of `export/trigger`** — same. Lift the full POST body from the HAR and parameterize the fields that change per report type (`dataType`, filter values) — leave everything else literal at first; trim once it works.
3. **Whether `export/download/<id>` truly serves as a poll** (returning some "not ready" signal) or whether it 404s / returns empty `preSignedUrl` while pending. The HAR shows only the success case. **Probe experimentally during implementation:** call it immediately after `export/trigger` and observe.
4. **Whether `getRecentTriggeredReports` is needed at all** — if `export/download/<id>` is pollable directly, we skip the tray entirely. (Strong preference: skip the tray; one fewer thing to model.)
5. **Cookie expiry behavior.** When session cookies expire, the API should respond with 401 or a redirect to login. Plan: any 401 from a `/api/...` call → exit with `"Clear session expired — open Chrome, log in to ClearGST normally, then re-run."` — same message as the plan envisioned.
6. **GSTR-6A and GSTR-8A slug values** — predicted by analogy but unconfirmed. Validate with a second HAR (one period each) before claiming v1 complete.

## Architectural impact on the plan

- **`browser.py` Playwright mode is unnecessary for v1.** All v1 reports are mode (a). Keep Playwright as a documented escape hatch in case a future report type turns out to be UI-only, but don't build it now.
- **`portal.py` is renamed `api.py`** and becomes a thin `requests.Session` wrapper that:
  - reads cookies via `browser-cookie3` (`Chrome`, profile=`Profile 10`)
  - sets the static `x-cleartax-*` / `x-workspace-id` headers
  - exposes typed methods: `business_hierarchy()`, `user_gstins()`, `trigger_pull(node_ids, periods, data_sources)`, `poll_pull_status(node_ids)`, `fetch_rls_token(periods, workflow)`, `trigger_export(statement, rls_token)`, `get_export_download(export_id)`, `download_file(presigned_url, dest)`.
- **`flows/inwards.py`** composes the above into a per-(PAN, FY, report-type) loop. The four report types share one generic function parameterized by the slug table above.
- **No `auth.py` interactive fallback needed in v1.** Defer.
- **Manifest schema** updated to `(pan, fy, report_type, status, file_path, downloaded_at)` — drops `gstin` and `period` columns (everything happens at PAN × FY level now).
- **Downloads layout** updated to:
  ```
  downloads/<PAN>/FY-<FY>/<report-type>/PAN_MM<X>_Document_<PAN>_<period>.xlsx.zip
  ```

## What to do next (proposed)

1. Capture a small second HAR for GSTR-2B to validate the slug predictions (~15 min on your side).
2. I scaffold `pyproject.toml`, the package skeleton, manifest, logging, and `api.py` with the methods above wired up against the HAR bodies (verbatim where possible, parameterized where obvious).
3. End-to-end test against GSTR-2A for one PAN × one FY, confirm the file lands on disk and opens.
4. Generalize to the four report types.
5. Loop over multiple PANs × multiple FYs.

If you're happy with this analysis, ping me and I'll move to step 1 (the second HAR for 2B validation) or skip straight to step 2 (scaffold and validate experimentally) — your call.

---

## Addendum — GSTR-2A vs 3B vs Books reconciliation flow

**Source HAR:** `app.clear.in.har` (7.3 MB, 169 entries; captured 01 Jun 2026)
**Scope walked through:** PAN `AAECB1261D` (BIRDS EYE SYSTEMS PRIVATE LIMITED), FY 2020-21 (periods `042020..032021`), one GSTIN under the PAN.

### Confirmed slugs / identifiers

| Key | Value | HAR evidence |
|---|---|---|
| URL slug (Referer query) | `reportType=panG3bvs2avsBooks` | entries 72, 114, 126, 149 |
| Pull tenant | `GSTR2A_VS_3B_VS_BOOKS_REPORTS` | entry 72 body |
| RLS workflow (URL param `workFlow=`) | `GSTR2A_VS_3B_VS_BOOKS_REPORTS` | entry 114 URL |
| Preflight S3 prefix | `pan_G2Avs3B_download_Adv` | entry 145/146 URLs |
| Real-export S3 prefix | `pan_G2Avs3BvsBook_download_Adv` | entry 165/166 URLs |
| Preflight filename pattern | `PAN_PAN_GSTR2A_vs_3b_Report_<PAN>_<MMYYYY>-<MMYYYY>` | entries 126, 145/146 |
| Real-export filename pattern | `PAN_GSTR2A_vs_3B_vs_Books_Report_<PAN>_<MMYYYY>-<MMYYYY>` | entries 149, 165/166 |
| Both metadata `reportType` | `panG3bvs2avsBooks` | entries 126, 149 bodies |
| Preflight UI label (`onStart.metadata.filename`) | `GSTR-2A vs 3B Report (XLSX)` | entry 126 body |
| Real UI label (`onStart.metadata.filename`) | `GSTR-2A vs 3B vs Books Report (XLSX)` | entry 149 body |

### Flow ordering (same as 2B-vs-3B-vs-Books and 1-vs-3B-vs-Books)

1. `POST /api/data-pull/public/pull/v2/trigger` — `tenant: GSTR2A_VS_3B_VS_BOOKS_REPORTS`, refreshes 2A side only (3B comes from Clear's existing 3B cache, no separate pull).
2. Poll `POST /api/data-pull/public/pull/v3/status` until DOWNLOADED.
3. `POST /api/gst-auto-compute/public/rls/fetch-token?...&workFlow=GSTR2A_VS_3B_VS_BOOKS_REPORTS` — same RLS token is reused for both export-trigger calls below.
4. **Preflight** `POST /api/clear/data-browser/public/export/trigger` with the `G2A vs 3B` body (no Books). Returns an export id we discard — this primes Clear's reconciliation cube. Skipping it is expected to make step 5 fail with `500 Unknown error occurred` (by analogy with the documented 2B and 1 preflights).
5. **Real export** `POST /api/clear/data-browser/public/export/trigger` with the `G2A vs 3B vs Books` body.
6. Poll `GET /api/clear/data-browser/public/export/download/<id>` until SUCCESS, then download the pre-signed S3 URL.

### Header overrides on both `export/trigger` calls

Verified absent from HAR: **`x-ct-source`** (our session adds `GST_REPORTS` by default — we must override to `None`). Verified present: `baggage` + `sentry-trace` (Sentry distributed-tracing headers — Clear's edge may validate these), `accept-language`, `priority`. The Referer must carry `?reportType=panG3bvs2avsBooks&...` or Clear's backend 500s — same edge-validation behavior as the panG3bvs2bvsBooks endpoint.

### Filename quirks (replicate verbatim)

The **preflight** filename has the same double-`PAN_PAN_` prefix and lowercase `3b` as the 2B-vs-3B-vs-Books and 1-vs-3B-vs-Books preflights (e.g. `PAN_PAN_GSTR2A_vs_3b_Report_...`). This looks like a frontend typo but Clear's backend may key off it, so the captured JSON template preserves it as-is. The real-export filename is clean (`PAN_GSTR2A_vs_3B_vs_Books_Report_...`).

### Periods

No `MIN_FY` floor needed: GSTR-2A is available from the start of GST (Jul 2017). Unlike GSTR-2B-vs-3B-vs-Books (which requires `MIN_START_PERIOD=072020`), this flow can process FY 2017-18 onward without period clipping. *(Validate by attempting an early FY in production — current HAR only covers 2020-21.)*

---

## Addendum — PAN Cash Ledger report

**Source HAR:** `discovery/app.clear.in.CASH-LEDGER.har` (7.8 MB, 143 entries; captured 01 Jun 2026)
**Scope walked through:** PAN `AAGCP5410J` (PISCES ESERVICES PRIVATE LIMITED), 41 GSTINs, date range `01-07-2017 .. 01-06-2026` (full GST era).

### Confirmed slugs / identifiers

| Key | Value | HAR evidence |
|---|---|---|
| URL slug (Referer query) | `reportType=panCashLedger` | entries 80, 108, 118, 136 |
| `timePeriodType` (Referer) | `DATE_RANGE` (not `FISCAL_YEAR`) | same |
| Pull tenant | `CASH_LEDGER_REPORT` | entry 80 body |
| RLS workflow (URL param `workFlow=`) | `CASH_LEDGER_REPORT` | entry 108 URL |
| RLS URL params (note: no `returnPeriods`) | `workFlow=...&tableType=&fromDate=DD-MM-YYYY&toDate=DD-MM-YYYY` | entry 108 URL |
| Export S3 prefix | `cash_ledger_download` | entry 136 response URL |
| Export filename | `PAN_CASH_LEDGER_REPORT_<PAN>_<DD-MM-YYYY>-<DD-MM-YYYY>.xlsx.zip` | entry 136 response |
| Real-export `exportName` | `cash_ledger_download` | entry 118 body |
| Real-export `fileType` | `XLSX` | entry 118 body |
| `staticRowData` keys | `{companyName, gstin, reportPeriod}` | entry 118 body |
| `staticRowData.reportPeriod` format | `"DD-MM-YYYY - DD-MM-YYYY"` | entry 118 body |
| `onStart.metadata.reportType` | `panCashLedger` | entry 118 body |
| Statement template id | `67e2a6e78ede5b3eac89594d` (Clear's stored QUERY template) | entry 118 body |

### Flow ordering (simple — no preflight, like GSTR-2A)

1. `POST /api/data-pull/public/pull/v2/trigger` — `tenant: CASH_LEDGER_REPORT`, `startRange` / `endRange` in **DD-MM-YYYY** (not `MMYYYY`), `gisDownloadBehaviour: null` (HAR sent JSON null, not the usual `"USE_EXISTING_DATA"`).
2. Poll `POST /api/data-pull/public/pull/v3/status` until DOWNLOADED.
3. `POST /api/.../rls/fetch-token?workFlow=CASH_LEDGER_REPORT&tableType=&fromDate=...&toDate=...` — date-range mode of the RLS endpoint (no `returnPeriods=`).
4. **Single** `POST /api/.../export/trigger` with `x-rls-token`, panCashLedger Referer, header overrides (drop `x-ct-source`, add baggage / sentry-trace / accept-language / priority).
5. Poll `GET /api/.../export/download/<id>` until SUCCESS, then download the pre-signed S3 URL.

### Key difference from every other flow: **date range, not periods**

This is the first report in the toolkit that uses **DD-MM-YYYY date strings** instead of `MMYYYY` periods. `api.fetch_rls_token` was extended with a `from_date=` / `to_date=` mode for this; `api.trigger_pull` is range-agnostic and passes its `start_period` / `end_period` strings through verbatim, so DD-MM-YYYY values work without code changes. `api.trigger_pull`'s `gis_download_behaviour` type was widened to `str | None` to pass JSON `null`.

### FY → date-range mapping (implementation detail)

The flow maps each configured FY (e.g. `2024-25`) to a `(start, end)` DD-MM-YYYY pair internally:
- Start: `01-04-<first-year>`, clamped up to `01-07-2017` for FY 2017-18 (GST start).
- End: `31-03-<second-year>`, clamped down to today for the current FY.

---

## Addendum — PAN ITC Ledger report

**Source HAR:** `discovery/app.clear.in.itc.har` (13 MB, 209 entries; captured 02 Jun 2026)
**Scope walked through:** PAN `AAGCP5410J` (PISCES ESERVICES PRIVATE LIMITED), 8 GSTINs, date range `01-07-2017 .. 02-06-2026`.

**Structurally identical to PAN Cash Ledger** (same 5-step pipeline, same DD-MM-YYYY date format, same `gisDownloadBehaviour: null`, same header-override set, no preflight). Only identifiers differ:

| Key | Value | HAR evidence |
|---|---|---|
| URL slug (Referer query) | `reportType=panItcLedger` | entries 88, 149, 159, 174 |
| `timePeriodType` (Referer) | `DATE_RANGE` | same |
| Pull tenant | `ITC_LEDGER_REPORT` | entry 88 body |
| RLS workflow (URL param `workFlow=`) | `ITC_LEDGER_REPORT` | entry 149 URL |
| Export S3 prefix | `itc_ledger_download` | entry 174 response URL |
| Export filename | `PAN_ITC_LEDGER_REPORT_<PAN>_<DD-MM-YYYY>-<DD-MM-YYYY>.xlsx.zip` | entry 174 response |
| Real-export `exportName` | `itc_ledger_download` | entry 159 body |
| Statement template id | `67e2a4bc8ede5b3eac89594a` | entry 159 body |
| Statement columns | myGstin, state_name, description, formatted_date, totalValue, igstValue, cgstValue, sgstValue, cessValue | entry 159 body |

`staticRowData` keys (`companyName / gstin / reportPeriod`), `onStart.metadata` shape, the header-override set (drop `x-ct-source`, add baggage + sentry-trace + accept-language + priority), and the FY → date-range mapping are all identical to the cash ledger flow.
