# Phase 0 — Walkthrough Notes

Fill this in as you do the walkthrough described in `HOW-TO-CAPTURE.md`. Replace each `…` placeholder with actual values. If a section doesn't apply (e.g. a report wasn't available), say so explicitly rather than leaving it blank.

---

## 1. Scope of this walkthrough

- **Date / time of capture:** … 20/05/2026 18:00
- **Chrome profile used (folder name under `User Data\`):** … *(e.g. `Profile 1` or `Default`)* - 

Executable Path	C:\Program Files\Google\Chrome\Application\chrome.exe
Profile Path	C:\Users\LokashChellaiya\AppData\Local\Google\Chrome\User Data\Profile 10

- **PAN walked through:** … AAGCP5410J
- **Business name shown in Clear's switcher for that PAN:** … Pisces eServices Private Limited
- **GSTIN walked through:** … Entire PAN
- **Tax period walked through:** … *(e.g. `April 2024` / FY 2024-25)* FY 2025-26

---

## 2. After business / PAN switch

- **URL after switch:** … 

https://app.clear.in/?workspaceId=a8a3363c-b12b-4e7d-bd00-f81aafd07a89 - homepage
https://app.clear.in/gst - gst page
https://app.clear.in/gst/reports?section=ALL - reports page

- **Anything that needed clicking before the switcher appeared** (e.g. profile menu → switch business): …

in the selector we need to choose pan & period and press generate report

---

## 3. After GSTIN switch

- **URL after switch:** …

https://app.clear.in/gst/reports/v2?reportType=panMm2a&activeBusiness=PISCES%20ESERVICES%20PRIVATE%20LIMITED&startDate=2025-04-01&endDate=2026-03-31&pan=AAGCP5410J&panNodeId=8b26df83-cf60-4963-8e4d-e5d7790ee2aa&timePeriodType=FISCAL_YEAR&localStorageKey=OGIyNmRmODMtY2Y2MC00OTYzLThlNGQtZTVkNzc5MGVlMmFhXzE3NzkyODA1NTc2NzI%3D

after selecting the pan & period
- **Where the GSTIN switcher lives** (top-bar / sidebar / settings page): …
03-pan-period-selector-03.png - after clicking generate report it will show the list of avaibale data - here we need to click generate report 

sometimes data might be missing in this - that have different workflow - that i'll update after testing this flow.

https://app.clear.in/gst/reports/v2?reportType=panMm2a&activeBusiness=PISCES%20ESERVICES%20PRIVATE%20LIMITED&startDate=2025-04-01&endDate=2026-03-31&pan=AAGCP5410J&panNodeId=8b26df83-cf60-4963-8e4d-e5d7790ee2aa&timePeriodType=FISCAL_YEAR&localStorageKey=OGIyNmRmODMtY2Y2MC00OTYzLThlNGQtZTVkNzc5MGVlMmFhXzE3NzkyODA1NTc2NzI%3D&section=REPORT_VIEW&jobId=6a0dab711c9ab93806328bdb

the above is the gstr-2a report page



---

## 4. Inwards section navigation

- **Exact menu path you clicked** (e.g. `Sidebar → GST → Inwards → 2A/2B Download`): …
- **URL of the inwards landing page:** …
- **Sub-tabs visible on the inwards landing page** (e.g. `Download 2A`, `Download 2B`, `Purchase Register`, `Recon`): …

---

## 5. FY / period picker

- **Where the FY selector is** (top of inwards page / inside a modal / sidebar): …
- **Where the period selector is:** …
- **URL after picking FY + period (if it changes):** …
- **Format of the period value** (e.g. `04/2024`, `Apr-2024`, `2024-04`): …

---

## 6. Per-report downloads

### 6a. GSTR-2A

- **Case (A=immediate / B=queued / C=modal):** …
- **Downloaded file name:** …
- **If B (queued): approx wait time:** …
- **If B (queued): any visible "job ID" / "report ID" / status string:** …
- **Any unexpected modal/prompt:** …

### 6b. GSTR-2B

- **Case (A/B/C):** …
- **Downloaded file name:** …
- **If queued: wait time + any visible ID:** …
- **Any unexpected modal/prompt:** …

### 6c. Purchase Register

- **Case (A/B/C) — or "not available":** …
- **Downloaded file name:** …
- **If queued: wait time + any visible ID:** …
- **Where in the UI this lives** (same section as 2A/2B or different): …

### 6d. 2A/2B vs Purchase Register Recon

- **Case (A/B/C) — or "not available":** …
- **Downloaded file name:** …
- **Where in the UI this lives:** …
- **Any pre-conditions** (e.g. "had to upload PR first", "had to click 'Generate Recon' before download was offered"): …

---

## 7. Anything unexpected

Anything that broke the flow, surprised you, or felt wrong: re-auth prompts, captchas, error toasts, blank pages, slow loads, missing buttons, etc. Include the screenshot file name from `screenshots\99-anything-unexpected.png` if you took one.

- …

---

## 8. File checklist (tick when done)

- [ ] `inwards-walkthrough.har` saved to `discovery\` and is at least ~1 MB
- [ ] `NOTES.md` (this file) — all sections above filled in
- [ ] `screenshots\01-business-switcher.png`
- [ ] `screenshots\02-gstin-switcher.png`
- [ ] `screenshots\03-inwards-landing.png`
- [ ] `screenshots\04-period-picker.png`
- [ ] `screenshots\05-gstr2a-download-button.png`
- [ ] `screenshots\06-gstr2b-download-button.png`
- [ ] `screenshots\07-purchase-register-download.png` (or noted as N/A above)
- [ ] `screenshots\08-recon-download.png` (or noted as N/A above)
- [ ] `screenshots\09-downloads-tray.png` (if any report was queued)
- [ ] `screenshots\99-anything-unexpected.png` (if anything was unexpected)
