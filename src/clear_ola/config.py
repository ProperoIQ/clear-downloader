"""Load and validate config.yaml. Uses dataclasses to keep the dependency surface
small (no pydantic / attrs dependency)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PanConfig:
    pan: str
    fys: list[str]                  # e.g. ["2024-25", "2025-26"]
    business_name: str = ""         # optional; auto-derived from Clear at run time if blank


@dataclass
class AppConfig:
    workspace_id: str
    chrome_profile: str
    poll_seconds_pull: int
    poll_seconds_export: int
    poll_timeout_pull_seconds: int
    poll_timeout_export_seconds: int
    inter_call_delay_seconds: float
    reports: list[str]
    pans: list[PanConfig]
    default_fys: list[str]  # global FYs applied when a PAN doesn't list its own
    downloads_dir: Path
    state_dir: Path

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        _require(raw, "workspace_id", str)
        _require(raw, "chrome_profile", str)
        _require(raw, "reports", list)
        _require(raw, "pans", list)
        default_fys = [fy.strip() for fy in (raw.get("fys") or [])]
        pans: list[PanConfig] = []
        for p in raw["pans"]:
            # Per-PAN `fys` overrides the global list. If neither exists, error.
            pan_fys = p.get("fys")
            if pan_fys is not None:
                resolved = [fy.strip() for fy in pan_fys]
            elif default_fys:
                resolved = list(default_fys)
            else:
                raise ValueError(
                    f"config.yaml: PAN {p.get('pan')!r} has no `fys:` and "
                    f"no top-level `fys:` is set. Add one or the other."
                )
            pans.append(PanConfig(
                pan=p["pan"].strip().upper(),
                business_name=(p.get("business_name") or "").strip(),
                fys=resolved,
            ))
        root = path.parent
        return cls(
            workspace_id=raw["workspace_id"].strip(),
            chrome_profile=raw["chrome_profile"].strip(),
            poll_seconds_pull=int(raw.get("poll_seconds_pull", 10)),
            poll_seconds_export=int(raw.get("poll_seconds_export", 5)),
            poll_timeout_pull_seconds=int(raw.get("poll_timeout_pull_seconds", 1800)),
            poll_timeout_export_seconds=int(raw.get("poll_timeout_export_seconds", 900)),
            inter_call_delay_seconds=float(raw.get("inter_call_delay_seconds", 1.0)),
            reports=[r.strip() for r in raw["reports"]],
            pans=pans,
            default_fys=default_fys,
            downloads_dir=(root / raw.get("downloads_dir", "downloads")).resolve(),
            state_dir=(root / raw.get("state_dir", "state")).resolve(),
        )


def _require(d: dict[str, Any], key: str, type_: type) -> None:
    if key not in d:
        raise ValueError(f"config.yaml missing required key: {key!r}")
    if not isinstance(d[key], type_):
        raise ValueError(
            f"config.yaml key {key!r} must be {type_.__name__}, got {type(d[key]).__name__}"
        )


# ---- FY <-> periods helpers ----

def fy_periods(fy: str, *, as_of=None) -> list[str]:
    """`'2025-26'` -> `['042025', '052025', ..., '032026']`.

    If `as_of` (a `datetime.date`) falls *inside* this FY, the list is
    truncated at that month — so for FY 2026-27 in May 2026, you get just
    `['042026', '052026']` instead of all 12 months. This avoids asking
    Clear/GSTN to fetch periods that don't exist yet (which would otherwise
    surface as `DOWNLOADED_PARTIALLY` indefinitely).

    `as_of=None` returns the full 12 months — useful for historical FYs and
    for testing.
    """
    start_yr, end_yr_short = fy.split("-")
    start_yr = int(start_yr)
    end_yr = 2000 + int(end_yr_short)
    if end_yr != start_yr + 1:
        raise ValueError(f"FY must span exactly one year: {fy!r}")
    months = []
    # Apr-Dec of start_yr
    for m in range(4, 13):
        months.append((m, start_yr))
    # Jan-Mar of end_yr
    for m in range(1, 4):
        months.append((m, end_yr))

    if as_of is not None:
        cutoff_yr, cutoff_m = as_of.year, as_of.month
        # Only truncate if `as_of` is within this FY's window
        if (start_yr, 4) <= (cutoff_yr, cutoff_m) <= (end_yr, 3):
            months = [(m, y) for (m, y) in months
                      if (y, m) <= (cutoff_yr, cutoff_m)]

    return [f"{m:02d}{y}" for (m, y) in months]


def fy_date_range(fy: str) -> tuple[str, str]:
    """`'2025-26'` -> (`'2025-04-01'`, `'2026-03-31'`)."""
    start_yr, end_yr_short = fy.split("-")
    end_yr = 2000 + int(end_yr_short)
    return f"{start_yr}-04-01", f"{end_yr}-03-31"


def fy_dmy_range(fy: str) -> tuple[str, str]:
    """`'2025-26'` -> (`'01-04-2025'`, `'31-03-2026'`). prefetchStatus uses this."""
    start_yr, end_yr_short = fy.split("-")
    end_yr = 2000 + int(end_yr_short)
    return f"01-04-{start_yr}", f"31-03-{end_yr}"


def fy_human(fy: str) -> str:
    """`'2025-26'` -> `'Apr 2025 - Mar 2026'`."""
    start_yr, end_yr_short = fy.split("-")
    end_yr = 2000 + int(end_yr_short)
    return f"Apr {start_yr} - Mar {end_yr}"


def validate_fy(fy: str) -> None:
    """Raise ValueError if `fy` isn't a well-formed Indian FY like '2025-26'."""
    try:
        fy_periods(fy)  # also validates "consecutive years" invariant
    except (ValueError, IndexError, AttributeError) as e:
        raise ValueError(
            f"FY must be 'YYYY-YY' with consecutive years (e.g. '2025-26'). Got: {fy!r}"
        ) from e


def current_fy(today=None) -> str:
    """Return the current Indian FY (April-March) given today's date.

    Apr-Dec → that calendar year is the start. Jan-Mar → previous calendar year.
    """
    from datetime import date
    if today is None:
        today = date.today()
    start = today.year if today.month >= 4 else today.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def recent_fys(n: int = 5, today=None) -> list[str]:
    """Return the current FY plus the last (n-1) prior FYs, most recent first."""
    cur = current_fy(today)
    start_yr = int(cur.split("-")[0])
    out = [cur]
    for i in range(1, n):
        s = start_yr - i
        out.append(f"{s}-{(s + 1) % 100:02d}")
    return out
