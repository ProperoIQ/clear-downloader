# GSTR-6A vs PR Reconciliation (PAN-based)

Reconciles the ISD auto-drafted **GSTR-6A** (pulled from GSTN) against the
uploaded **Purchase Register (PR)**, producing one PAN-level XLSX per FY.

Backed by the same recon "matching task" pipeline as the 2A/2B-vs-PR reports
(`/api/recon/ultimatum/public/...`), differing only in the match type. Confirmed
from `discovery/app.clear.in.har__6A vs PR recon.har`:

- `matchType` = `MAX_ITC_6A_PR`
- UI path = `/reconciliation/idt/6aVsPr`
- rhs filter label = `6A Return Period`
- PAN-based (`nodeType: PAN`, `panNodeId`, `gstin: ""`)

## Pipeline (per PAN × FY)

1. `POST matching/v2/trigger` → taskId (also fetches 6A/PR data server-side)
2. `GET matching/current` (poll) → wait for `taskStatus == DATAVIEW_READY`
3. `POST workbench/report/v1/generate` → reportId
4. `GET workbench/report/v1/{id}/download` (poll) → presigned XLSX url → download

Both PR (lhs) and 6A (rhs) sides use the same period range, clipped to
`MIN_START_PERIOD = "072017"` (GST inception).

## Changes (all additive — existing PAN/GST/2A/2B flows untouched)

- `src/clear_ola/api.py` — added `"MAX_ITC_6A_PR": ("6A", "/reconciliation/idt/6aVsPr")`
  to `_RECON_VARIANTS`. No method signatures changed (already parameterized over
  `match_type`).
- `src/clear_ola/flows/gstr_6a_vs_pr_reconciliation.py` — new flow, faithful copy
  of `gstr_2a_vs_pr_reconciliation.py` with `REPORT_TYPE = "6A-vs-PR-Reconciliation"`
  and `MATCH_TYPE = "MAX_ITC_6A_PR"`.
- `src/clear_ola/cli.py` — import, `--report` choice `"6A-vs-PR-Reconciliation"`,
  and dispatch `elif` mirroring 2A/2B.
- `config.yaml` — added `"6A-vs-PR-Reconciliation"` to the `reports:` list.

## Verify

- `PYTHONPATH=src python -c "from clear_ola.flows import gstr_6a_vs_pr_reconciliation"` ✓
- `python -m py_compile src/clear_ola/cli.py src/clear_ola/api.py` ✓
- `_RECON_VARIANTS` now lists `MAX_ITC_6A_PR` alongside the unchanged 2A/2B keys ✓
- Live (user-run): `--report 6A-vs-PR-Reconciliation --pan AACCO4289J` → XLSX under
  `downloads/<PAN>/FY-<fy>/6A-vs-PR-Reconciliation/`, manifest marked done.
