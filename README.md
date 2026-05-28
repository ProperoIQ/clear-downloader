# clear-ola

Download ClearGST reports (v1: **GSTR-2A Document Level**) for one or many PANs across one or many FYs, by reusing your existing Chrome login session against Clear's authenticated APIs.

- No browser automation. No OTP prompts (as long as your Chrome session is still alive).
- Resumable. A SQLite manifest tracks what's been downloaded; re-running skips completed combinations.
- **Auth = a one-time Cookie-Editor JSON export from your logged-in Chrome.** Refresh only when Clear's session actually expires (typically days). Chrome can be open or closed during runs — doesn't matter.

## Prerequisites

- Windows + Python 3.10+.
- Google Chrome with a profile already logged into ClearGST.
- Your Clear workspace UUID (from the URL after login: `https://app.clear.in/?workspaceId=<UUID>`).
- The Chrome profile folder name (e.g. `Default`, `Profile 1`, `Profile 10`). On Windows these live under `C:\Users\<you>\AppData\Local\Google\Chrome\User Data\`.

## One-time setup

```powershell
# From the project root (D:\Projects\Claude-Data\clear-ola-data):
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Edit `config.yaml`:
- `workspace_id`: your Clear workspace UUID (from URL: `https://app.clear.in/?workspaceId=<UUID>`)
- `chrome_profile`: kept for reference; not used when you have a cookie file
- `fys`: **global** FYs applied to every PAN (write them once)
- `pans`: list of `{pan, business_name}`. A PAN inherits the global `fys` unless it has its own `fys:` override (rarely needed — PANs that didn't yet exist in a given FY are auto-skipped as `no_data` in the manifest).

Export your Clear cookies (once):

```powershell
python -m clear_ola cookies-import   # prints step-by-step instructions
```

In short: install the **Cookie-Editor** Chrome extension, visit `https://app.clear.in/`, click the extension → Export as JSON, paste into `.auth/clear-cookies.json`.

## Run

Chrome can be open or closed — doesn't matter, since we read cookies from your exported `.auth/clear-cookies.json`.

```powershell
# 1. Verify cookies + Clear API access (read-only, no side effects on Clear):
python -m clear_ola auth-check

# 2. See every PAN in your Clear workspace and which are in config.yaml:
python -m clear_ola pans

# 3a. Fully non-interactive — name the PAN and FY:
python -m clear_ola download --pan AAGCP5410J --fy 2025-26

# 3b. Name the PAN, pick the FY from a menu (configured FYs + recent 5 + "enter custom"):
python -m clear_ola download --pan AAGCP5410J

# 3c. Pick both PAN and FY from menus:
python -m clear_ola download

# 3d. Fire every configured (PAN x FY) in one go (no prompts):
python -m clear_ola download --all

# 4. See progress any time:
python -m clear_ola status

# Force-redownload a specific PAN/FY/report:
python -m clear_ola reset --pan AAGCP5410J --fy 2025-26 --report GSTR-2A
python -m clear_ola download
```

Logs go to `logs/run-YYYYMMDD-HHMMSS.log`. Files land at:

```
downloads/<PAN>/FY-<FY>/<REPORT>/PAN_MM2A_Document_<PAN>_<start>-<end>.xlsx.zip
```

## What's happening under the hood

For each `(PAN, FY)` combination:

1. **Trigger pull** — `POST /api/data-pull/public/pull/v2/trigger` with all the GSTIN node IDs under the PAN and the FY's month range. Tells Clear "make sure fresh data is loaded from GSTN."
2. **Wait for pull** — poll `POST /api/data-pull/public/pull/v3/status` every ~10s until every GSTIN reports `downloadStatus: DOWNLOADED`.
3. **Fetch RLS token** — `POST /api/gst-auto-compute/public/rls/fetch-token` to get a short-lived token gated to the periods + workflow.
4. **Trigger export** — `POST /api/clear/data-browser/public/export/trigger` (with `x-rls-token`). Body is a canned SELECT statement for the GSTR-2A Document Level columns. Returns an export job ID.
5. **Wait for export** — poll `GET /api/clear/data-browser/public/export/download/<id>` every ~5s until `taskStatus: SUCCESS`. Response then contains the S3 pre-signed URL.
6. **Download file** — `GET <preSignedUrl>` streams the `.xlsx.zip` bytes to disk.

See `discovery/FINDINGS.md` for the full reverse-engineering notes (auth model, slug values, open questions).

## Handling partial data

For older FYs (especially 2017-18 to 2019-20), Clear sometimes can't fetch the full dataset from GSTN for one or more states. You'll see this in the logs as `DOWNLOADED_PARTIALLY`.

The script's policy:

1. **Auto-retry once** with `gisDownloadBehaviour=DOWNLOAD_COMPLETE_DATA` (equivalent to the UI's "Download all data again" button on the partial-data modal).
2. **If still partial**, append details to `state/partial-items.csv` and **fail** — so you can take that CSV to the GST team / state filer / Clear support and confirm whether the missing data should exist.
3. **`--force-partial`** flag lets you proceed to export anyway after you've confirmed the gap is real / acceptable. The resulting Excel will include whatever data Clear has, just missing rows for the partial GSTIN/months.

```powershell
# Default — strict, fails on persistent partial, writes state/partial-items.csv
python -m clear_ola download --pan AACCO4289J --fy 2019-20

# After reviewing partial-items.csv with the GST team, accept the gap:
python -m clear_ola download --pan AACCO4289J --fy 2019-20 --force-partial
```

The CSV is append-only across runs. Columns:

| column | what it is |
|---|---|
| `logged_at` | UTC timestamp of this log entry |
| `pan`, `business_name`, `fy`, `report_type` | which job |
| `gstin`, `state_name` | the partial registration |
| `status`, `download_percentage` | always `DOWNLOADED_PARTIALLY` + Clear's % |
| `periods_in_scope` | months we tried to pull (comma-separated MMYYYY) |
| `clear_updated_at` | last update timestamp Clear gave for this GSTIN |
| `pull_request_id` | trace back to the trigger that produced this state |

## When things go wrong

| Error | What it means | Fix |
|---|---|---|
| `[SESSION EXPIRED] ... 401/403 ...` | Your Clear cookies have expired or you were logged out. | Open Chrome, log into ClearGST again, re-export `.auth/clear-cookies.json` via Cookie-Editor, then re-run. |
| `Cookie file ... contained no clear.in / cleartax.in cookies` | You exported from the wrong page (or no cookies were filtered for the right domains). | Re-do the export while you're actually on `https://app.clear.in/`. |
| `At least one cookie ... has already expired` (warning) | The exported file is stale. | Re-export the JSON from Cookie-Editor. |
| `No clear.in / cleartax.in cookies could be read ... App-Bound Encryption ...` | You don't have `.auth/clear-cookies.json` and Chrome v127+ blocks direct decryption. | Run `python -m clear_ola cookies-import` for step-by-step instructions. |
| `Pull failed for GSTINs: ...` | Clear couldn't refresh data for one or more GSTINs (e.g. their GSTN session expired). | Log into the affected GSTIN(s) inside ClearGST manually (the "session" prompt on the inwards page), then re-run. |
| `Export ... did not complete within Xs` | Clear's Excel exporter took longer than `poll_timeout_export_seconds`. | Increase the value in `config.yaml`. |

## What's not in v1 yet

- GSTR-2B / 6A / 8A (predicted slugs are in `discovery/FINDINGS.md`; needs a 15-min HAR validation walkthrough each).
- The "data missing" branch (when some periods aren't yet pulled). The flow assumes the trigger-then-wait path always works; if a GSTIN's GSTN session is expired, you'll see a "Pull failed" error and need to refresh that session in Clear's UI first.
- Multi-PAN auto-discovery (currently you list PANs in `config.yaml`).
- Headless / scheduled mode. It runs interactively for now; can be wrapped in Task Scheduler later.
