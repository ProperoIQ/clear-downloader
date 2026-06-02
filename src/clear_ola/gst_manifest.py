"""SQLite-backed manifest for the GST-based track — keyed by (GSTIN x FY x
report_type) instead of (PAN x FY x report_type). Used by GSTR-6A and any
future per-GSTIN reports.

Separate from `manifest.py` (and stored in a separate sqlite file) so the
PAN-based track stays untouched and the two manifests can't collide on
primary key shape."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


_SCHEMA = """
CREATE TABLE IF NOT EXISTS gst_downloads (
    gstin           TEXT NOT NULL,
    fy              TEXT NOT NULL,
    report_type     TEXT NOT NULL,
    status          TEXT NOT NULL,    -- pending | in_progress | done | no_data | failed
    file_path       TEXT,
    file_bytes      INTEGER,
    pull_request_id TEXT,
    export_id       TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    error_message   TEXT,
    PRIMARY KEY (gstin, fy, report_type)
);
CREATE INDEX IF NOT EXISTS idx_gst_status ON gst_downloads(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GstManifest:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        with self._conn() as cx:
            cx.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(self._db_path, isolation_level=None)  # autocommit
        cx.row_factory = sqlite3.Row
        try:
            yield cx
        finally:
            cx.close()

    # ---- queries ----

    def is_done(self, gstin: str, fy: str, report_type: str) -> bool:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT status FROM gst_downloads WHERE gstin=? AND fy=? AND report_type=?",
                (gstin, fy, report_type),
            ).fetchone()
        return bool(row) and row["status"] in ("done", "no_data")

    def get(self, gstin: str, fy: str, report_type: str) -> dict | None:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT * FROM gst_downloads WHERE gstin=? AND fy=? AND report_type=?",
                (gstin, fy, report_type),
            ).fetchone()
        return dict(row) if row else None

    def all_rows(self) -> list[dict]:
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT * FROM gst_downloads ORDER BY gstin, fy, report_type"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- mutations ----

    def mark_started(self, gstin: str, fy: str, report_type: str) -> None:
        with self._conn() as cx:
            cx.execute("""
                INSERT INTO gst_downloads (gstin, fy, report_type, status, started_at)
                VALUES (?, ?, ?, 'in_progress', ?)
                ON CONFLICT(gstin, fy, report_type) DO UPDATE SET
                    status='in_progress',
                    started_at=excluded.started_at,
                    error_message=NULL,
                    completed_at=NULL
            """, (gstin, fy, report_type, _now()))

    def set_pull_id(self, gstin: str, fy: str, report_type: str, pull_id: str) -> None:
        with self._conn() as cx:
            cx.execute(
                "UPDATE gst_downloads SET pull_request_id=? "
                "WHERE gstin=? AND fy=? AND report_type=?",
                (pull_id, gstin, fy, report_type),
            )

    def set_export_id(self, gstin: str, fy: str, report_type: str, export_id: str) -> None:
        with self._conn() as cx:
            cx.execute(
                "UPDATE gst_downloads SET export_id=? "
                "WHERE gstin=? AND fy=? AND report_type=?",
                (export_id, gstin, fy, report_type),
            )

    def mark_done(
        self, gstin: str, fy: str, report_type: str, *,
        file_path: str, file_bytes: int,
    ) -> None:
        with self._conn() as cx:
            cx.execute("""
                UPDATE gst_downloads
                SET status='done', file_path=?, file_bytes=?, completed_at=?,
                    error_message=NULL
                WHERE gstin=? AND fy=? AND report_type=?
            """, (file_path, file_bytes, _now(), gstin, fy, report_type))

    def mark_failed(self, gstin: str, fy: str, report_type: str, error: str) -> None:
        with self._conn() as cx:
            cx.execute("""
                UPDATE gst_downloads
                SET status='failed', error_message=?, completed_at=?
                WHERE gstin=? AND fy=? AND report_type=?
            """, (error[:2000], _now(), gstin, fy, report_type))

    def mark_no_data(self, gstin: str, fy: str, report_type: str) -> None:
        """Settle a (GSTIN, FY) combo as 'no data exists' — Clear's pull
        returned NOT_APPLICABLE (the GSTIN wasn't ISD-registered for this FY,
        or no inward distributions to report)."""
        with self._conn() as cx:
            cx.execute("""
                UPDATE gst_downloads
                SET status='no_data', completed_at=?,
                    error_message='No data: pull returned NOT_APPLICABLE'
                WHERE gstin=? AND fy=? AND report_type=?
            """, (_now(), gstin, fy, report_type))

    def recover_orphans(self) -> int:
        """Find any row stuck in 'in_progress' (left there by a previous run
        that was Ctrl-C'd or crashed) and transition it to 'failed' so the
        next run will retry it normally."""
        with self._conn() as cx:
            cur = cx.execute("""
                UPDATE gst_downloads
                SET status='failed',
                    error_message=COALESCE(error_message, '') ||
                                  ' [orphan: previous run interrupted]',
                    completed_at=?
                WHERE status='in_progress'
            """, (_now(),))
            return cur.rowcount

    def reset(
        self, gstin: str, fy: str | None = None, report_type: str | None = None,
    ) -> int:
        """Delete rows so the next run re-downloads them. Returns rows deleted."""
        clauses = ["gstin=?"]
        args: list = [gstin]
        if fy is not None:
            clauses.append("fy=?")
            args.append(fy)
        if report_type is not None:
            clauses.append("report_type=?")
            args.append(report_type)
        sql = f"DELETE FROM gst_downloads WHERE {' AND '.join(clauses)}"
        with self._conn() as cx:
            cur = cx.execute(sql, args)
            return cur.rowcount
