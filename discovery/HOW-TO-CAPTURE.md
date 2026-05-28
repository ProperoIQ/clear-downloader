# Phase 0 — Portal Discovery Walkthrough

**Goal:** Capture one full manual inwards-download flow from ClearGST so we know exactly which URLs, endpoints, and DOM elements to automate. Once done, hand the artifacts back and we'll write the automation against real data instead of guesses.

**Time required:** ~15–20 minutes
**Prerequisites:** You are already logged into ClearGST in your daily-driver Chrome profile.

---

## Files you will produce

By the end of this walkthrough you should have:

```
D:\Projects\Claude-Data\clear-ola-data\discovery\
├── inwards-walkthrough.har        # the network capture (one file)
├── NOTES.md                       # you fill this in as you go (template provided)
└── screenshots\
    ├── 01-business-switcher.png
    ├── 02-gstin-switcher.png
    ├── 03-inwards-landing.png
    ├── 04-period-picker.png
    ├── 05-gstr2a-download-button.png
    ├── 06-gstr2b-download-button.png
    ├── 07-purchase-register-download.png
    ├── 08-recon-download.png
    ├── 09-downloads-tray.png      # only if any report queues asynchronously
    └── 99-anything-unexpected.png # any modal/error/captcha you hit
```

You can take screenshots on Windows with **Win + Shift + S**, then paste into Paint and save as PNG (or use the auto-save behavior if you have Snipping Tool set up).

---

## Step 1 — Prepare your scope (1 min)

Pick **one** combination to walk through. Smallest possible scope:

- **One PAN** (one business in your Clear account)
- **One GSTIN** under that PAN
- **One tax period** (one month — pick a recent month that definitely has data, e.g. `April 2024` or any month with filed returns)

Write these down in `NOTES.md` (Section 1 of the template) so we know what we're looking at when we analyze the HAR.

---

## Step 2 — Open Chrome and DevTools (1 min)

1. Open Chrome with your logged-in profile.
2. Navigate to `https://app.clear.in` (or whatever URL Clear lands you on after login — note the actual URL in `NOTES.md`).
3. Press **F12** to open DevTools.
4. Click the **Network** tab.
5. In the Network tab toolbar:
   - ✅ Check **Preserve log** (top of the panel — this is critical, otherwise the log clears on every navigation)
   - ✅ Check **Disable cache**
   - In the filter row, leave it set to **All** (not XHR/Fetch — we want to see everything including the file download responses)
6. Click the 🚫 **Clear** icon in the Network tab toolbar to start with a clean slate.

**Do not close DevTools at any point during the walkthrough. Do not navigate via tabs — stay in this one tab.**

---

## Step 3 — Business / PAN switch (2 min)

1. Find the **business / company switcher** in the top bar (usually shows current business name with a dropdown caret).
2. **Before clicking it**: take screenshot `01-business-switcher.png` showing the closed switcher.
3. Click it to open the dropdown.
4. Take screenshot again of the **open dropdown** showing the list of PANs/businesses available (this is `01-business-switcher.png` — overwrite the closed one; the open one is more useful).
5. Select the target PAN/business you chose in Step 1.
6. **In `NOTES.md` Section 2**, paste the URL from the address bar **after** the switch.

---

## Step 4 — GSTIN switch (2 min)

1. Find the **GSTIN switcher** (usually nearby — could be a separate dropdown or part of a settings/profile menu).
2. Take screenshot `02-gstin-switcher.png` with the dropdown open.
3. Select your target GSTIN.
4. **In `NOTES.md` Section 3**, paste the URL after the switch.

---

## Step 5 — Navigate to the inwards / purchase section (1 min)

1. From the main menu, navigate to the section that contains GSTR-2A / GSTR-2B / Purchase Register. Common labels: **GST Returns → Inwards**, or **Purchase**, or **2A/2B Recon**.
2. Take screenshot `03-inwards-landing.png` showing the section you're on.
3. **In `NOTES.md` Section 4**, paste the URL and write the exact menu path you clicked (e.g. "Sidebar → GST → Inwards → 2A/2B Download").

---

## Step 6 — Pick the FY and tax period (1 min)

1. Find the FY / financial year selector. Pick the FY containing your chosen month.
2. Find the tax period (month) selector. Pick the month.
3. Take screenshot `04-period-picker.png` showing both selectors visible (open one if needed).
4. **In `NOTES.md` Section 5**, paste any URL changes.

---

## Step 7 — Download GSTR-2A (3 min)

1. Take screenshot `05-gstr2a-download-button.png` showing **the actual download button** for GSTR-2A, before clicking. Include some surrounding context so we can identify it.
2. **In the Network tab, click 🚫 Clear once more** so what follows is just the download traffic. (Optional but makes the HAR easier to read.)
3. Click the GSTR-2A download button.
4. **Watch what happens:**
   - **Case A (immediate):** A file downloads in Chrome's downloads bar right away. Note the file name in `NOTES.md`.
   - **Case B (queued):** A toast/message appears like *"Report is being prepared, you'll be notified"*. The download does NOT start immediately. → This is normal for large reports. Wait for the "ready" notification (could be 10s–2min). When it appears, take screenshot `09-downloads-tray.png` showing the tray. Then click the tray entry to download.
   - **Case C (modal):** A modal opens asking about options (e.g. "Download as Excel / JSON / both"). Take screenshot `99-anything-unexpected.png` and pick Excel.
5. **In `NOTES.md` Section 6 (GSTR-2A subsection)**: record which case you hit (A/B/C), the file name that got downloaded, and any visible status/IDs along the way.

---

## Step 8 — Repeat for GSTR-2B, Purchase Register, Recon (5 min)

Same drill as Step 7, with their own screenshots:

- **GSTR-2B** → screenshot `06-gstr2b-download-button.png` → record in NOTES.md Section 6.
- **Purchase Register** → screenshot `07-purchase-register-download.png` → record in NOTES.md Section 6.
- **2A/2B vs PR Recon** → screenshot `08-recon-download.png` → record in NOTES.md Section 6.

If any of these aren't available in the inwards section for the period you picked, skip and note it in NOTES.md ("Recon not available for April 2024 because…").

---

## Step 9 — Save the HAR file (2 min)

This is the most important artifact.

1. Stay in the DevTools **Network** tab.
2. Right-click anywhere in the **list of requests** (the rows, not the empty space).
3. From the context menu, select **Save all as HAR with content**.
   - Note: must be **"with content"**, not just "as HAR". The "with content" version includes response bodies, which is essential.
4. Save the file as: `D:\Projects\Claude-Data\clear-ola-data\discovery\inwards-walkthrough.har`
5. Confirm the file size — should be at least 1–2 MB if you captured the full flow (probably bigger). If it's only a few KB, something went wrong (Preserve log probably wasn't checked); redo from Step 2.

---

## Step 10 — Finish NOTES.md and verify (2 min)

1. Open `D:\Projects\Claude-Data\clear-ola-data\discovery\NOTES.md` and complete any sections you skipped.
2. Confirm all files are in place by checking the folder:
   ```
   discovery\
   ├── inwards-walkthrough.har        ← must exist, > 1 MB
   ├── NOTES.md                       ← all sections filled
   └── screenshots\
       └── <at least 5 PNGs>
   ```
3. Tell me you're done and I'll start parsing the HAR.

---

## Troubleshooting

**"Save all as HAR with content" isn't in the right-click menu**
→ You're not in the Network tab, or no requests have been captured. Make sure Preserve log was checked before you started.

**The HAR file is tiny (under 100 KB)**
→ Preserve log wasn't on, or you cleared the log after captures. Redo from Step 2.

**Chrome opened a captcha / re-auth prompt during the walkthrough**
→ Take a screenshot (`99-anything-unexpected.png`), solve the captcha / re-MFA, then continue. Mention it in NOTES.md — this tells us Clear has anti-bot heuristics we'll need to account for.

**A download triggered but the file is 0 bytes / corrupt**
→ Note it in NOTES.md (with the file name) and move on. The HAR still captured the request — we'll see what went wrong server-side.

**You accidentally closed DevTools mid-walkthrough**
→ Reopen DevTools, re-enable Preserve log, re-clear the log, and restart from Step 3. Don't try to merge partial captures.

---

## What I'll do with these artifacts

Once you have the four things in `discovery\` (HAR, NOTES.md, screenshots, optionally the tray screenshot):

1. Parse the HAR to enumerate every URL Clear hits, separate them by domain/path pattern (`/api/...`, `/portal/...`).
2. For each of the 4 inwards reports, identify the request that initiated the download and the request that returned the file bytes.
3. Classify each report as **(a)** direct GET, **(b)** queued async, or **(c)** UI-only.
4. Write `discovery\FINDINGS.md` with the per-report URL + selector spec.
5. Use FINDINGS to write the actual scraper.

That's the entire Phase 0. After that we're writing code against real, verified URLs and selectors — not guesses.
