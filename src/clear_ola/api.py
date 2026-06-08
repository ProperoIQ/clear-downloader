"""Thin typed wrapper around `requests.Session` for the ClearGST endpoints we
discovered in Phase 0 (see discovery/FINDINGS.md). Auth = Chrome session cookies
+ a small set of `x-cleartax-*` / `x-workspace-id` identifier headers."""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE = "https://app.clear.in"


class ClearSessionExpired(RuntimeError):
    """Raised when Clear returns 401/403 — the cookies in the Chrome profile
    are no longer valid and the user must log in via Chrome again."""


class ClearAPIError(RuntimeError):
    """Any other non-2xx from the Clear API."""


# ---- helpers ----

def _nanoid(n: int = 21) -> str:
    """Approximate the nanoid-shape strings Clear's frontend uses for x-request-id."""
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_-"
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ---- data types ----

@dataclass
class GstinNode:
    gstin: str
    gstin_node_id: str
    pan_node_id: str
    pan: str
    business_name: str
    state_name: str

    @classmethod
    def from_user_gstins_row(cls, row: dict) -> "GstinNode":
        gstin = row["gstin"]
        return cls(
            gstin=gstin,
            gstin_node_id=row["gstinNodeId"],
            pan_node_id=row["panNodeId"],
            pan=gstin[2:12],  # GSTIN positions 3-12 = PAN
            business_name=row.get("businessName", ""),
            state_name=row.get("displayName", ""),
        )


@dataclass
class ExportReady:
    file_name: str
    pre_signed_url: str


@dataclass
class Gstr3bReportReady:
    """Outcome of `wait_for_3b_report` — what `download_file` needs next."""
    file_name: str       # URL-decoded last path segment of reportUri (for logging)
    report_uri: str      # presigned storage.clear.in URL — pass straight to download_file


@dataclass
class ReconReportReady:
    """Outcome of `wait_for_recon_report` — what `download_file` needs next.

    Used by the recon/ultimatum matching-task pipeline (e.g. 2B-vs-PR), which
    is wholly separate from the data-browser export pipeline above.
    """
    file_name: str       # fileInfos[].filename (e.g. "GSTR 2B Vs PR_<name>_<uuid>.xlsx")
    pre_signed_url: str  # fileInfos[].url — presigned app.clear.in/storage URL


# ---- the client ----

class ClearAPI:
    """All Clear endpoint calls go through here. One instance per script run.

    Args:
        workspace_id: Clear workspace UUID (from the URL after login).
        cookies: a CookieJar holding Chrome's session cookies for *.clear.in.
        timeout: per-request socket timeout in seconds.
    """

    def __init__(
        self,
        workspace_id: str,
        cookies: requests.cookies.RequestsCookieJar,
        timeout: float = 30.0,
    ) -> None:
        self.workspace_id = workspace_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.cookies = cookies
        # Transient network blips (DNS hiccup, connection reset, 5xx, 429)
        # are common during a long batch run. Auto-retry a few times with
        # exponential backoff so a 2-second Wi-Fi drop doesn't fail a combo.
        retry = Retry(
            total=4,
            connect=4,           # retry DNS / connection failures
            read=2,              # retry read timeouts (sparingly — could mask real issues)
            backoff_factor=1.5,  # delays: 1.5s, 3s, 6s, 12s
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset(("GET", "POST", "HEAD", "PUT", "DELETE")),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
            "origin": BASE,
            # Generic in-app referer. Some Clear endpoints route deserialization
            # based on this; without it, you get cryptic "tenant is null" NPEs.
            "referer": f"{BASE}/gst/reports?section=ALL",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0.0.0 Safari/537.36"
            ),
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-fetch-dest": "empty",
            "x-cleartax-country": "in",
            "x-cleartax-product": "GST",
            "x-cleartax-source": "APPCLEAR",
            "x-cleartax-orgunit": workspace_id,
            "x-workspace-id": workspace_id,
            "x-organisation-id": workspace_id,
            # x-ct-source identifies which Clear module the call originates from.
            # The reports flow uses GST_REPORTS. Carrying this on every request
            # is benign on endpoints that don't need it.
            "x-ct-source": "GST_REPORTS",
        })

    # ---- low-level request with auth-error handling ----

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | list | None = None,
        json_body: Any | None = None,
        extra_headers: Mapping[str, str | None] | None = None,
        stream: bool = False,
        expect_text: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{BASE}{path}"
        # `requests` interprets a None header value as "drop this header from
        # the merged session+request set" — useful for suppressing a session
        # default (e.g. x-ct-source) on endpoints that reject it.
        headers: dict[str, str | None] = {"x-request-id": _nanoid()}
        if extra_headers:
            for k, v in extra_headers.items():
                headers[k] = v
        logger.debug("HTTP {} {} params={} body_keys={}",
                     method, path,
                     params if isinstance(params, dict) else "[...]" if params else None,
                     list(json_body.keys()) if isinstance(json_body, dict) else None)
        resp = self.session.request(
            method, url,
            params=params, json=json_body,
            headers=headers,  # type: ignore[arg-type]  # requests drops None values at merge-time
            timeout=self.timeout, stream=stream,
        )
        if resp.status_code in (401, 403):
            raise ClearSessionExpired(
                f"Clear returned {resp.status_code} on {method} {path}. "
                "Your Chrome session for ClearGST has expired or you were logged out. "
                "Open Chrome, log into ClearGST again, then re-run."
            )
        if stream:
            # caller will iterate response
            resp.raise_for_status()
            return resp
        if resp.status_code >= 400:
            preview = resp.text[:500]
            raise ClearAPIError(
                f"{method} {path} -> HTTP {resp.status_code}. Body preview: {preview!r}"
            )
        if expect_text:
            return resp.text
        # default: assume JSON
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            return resp.json()
        return resp.text

    # ---- endpoints ----

    def user_gstins(self) -> list[GstinNode]:
        """List of all GSTINs the user can access, with their node IDs."""
        data = self._request(
            "GET",
            "/api/enterprise-orchestrator/public/business-hierarchy/v1/user_gstins",
            params={
                "isTaxpayerTypeRequired": "true",
                "isTokenInfoRequired": "true",
                "pageId": "LP2_REPORTS",
            },
        )
        return [GstinNode.from_user_gstins_row(r) for r in data["userGstinDto"]]

    def trigger_pull(
        self,
        *,
        gstin_node_ids: list[str],
        start_period: str,  # MMYYYY
        end_period: str,    # MMYYYY
        tenant: str = "GSTR2A_REPORTS",
        gis_download_behaviour: str = "USE_EXISTING_DATA",
        report_level: str = "PAN",
    ) -> str:
        """Kick off a fresh data pull from GSTN for the given GSTINs / period range.

        Returns the `pullRequestId` (we don't strictly need it — we poll by
        node_ids, not by request id — but log it for traceability).

        `gis_download_behaviour` corresponds to the UI's data-availability
        choice. Values confirmed against captured HARs:
          - "USE_EXISTING_DATA" (default) — use cached data, only pull what's
            missing. Maps to the page's first auto-trigger.
          - "DOWNLOAD_COMPLETE_DATA" — force a fresh full pull from GSTN for
            every (GSTIN, period) in scope. Maps to the "Download all data
            again" button in the partial-data modal.

        `report_level` switches the pull scope between PAN-level aggregation
        (default — what GSTR-2A/2B/1/8 use, even though the underlying
        `nodeType` is still "GSTIN") and per-GSTIN scope (what GSTR-6A uses).
        Confirmed from `discovery/app.clear.in.har_GSTR-6A Report.har`:
        the 6A flow sends `metadata.reportLevel: "GSTIN"` with one GSTIN id.

        IMPORTANT: Clear reads the tenant from the `tenant` HEADER, not from
        the JSON body. Omitting it produces a server-side NPE
        ("DataPullRequest.getTenant() is null").
        """
        body = {
            "nodeType": "GSTIN",
            "nodeIds": gstin_node_ids,
            "startRange": start_period,
            "endRange": end_period,
            "pullType": "OPTIMIZED_PULL",
            "tenant": tenant,
            "dataSources": [],
            "pageIds": [],
            "nodeIdDataSourcesMap": {},
            "metadata": {"reportLevel": report_level},
            "gisDownloadBehaviour": gis_download_behaviour,
        }
        data = self._request(
            "POST", "/api/data-pull/public/pull/v2/trigger",
            json_body=body,
            extra_headers={"tenant": tenant, **_node_headers(gstin_node_ids)},
        )
        pull_id = data.get("requestId") or data.get("pullRequestInfo", {}).get("pullRequestId")
        logger.info("Triggered pull, requestId={}", pull_id)
        return pull_id

    def poll_pull_status(
        self,
        gstin_node_ids: list[str],
        *,
        start_period: str,
        end_period: str,
        tenant: str = "GSTR2A_REPORTS",
    ) -> list[dict]:
        """One snapshot of the per-GSTIN download status. The body mirrors the
        trigger payload's scope (nodeType, range, pullType, tenant) — without
        those, Clear's status endpoint rejects with "Invalid node type"."""
        body = {
            "nodeIds": gstin_node_ids,
            "nodeType": "GSTIN",
            "startRange": start_period,
            "endRange": end_period,
            "pullType": "OPTIMIZED_PULL",
            "tenant": tenant,
            "dataSources": [],
        }
        data = self._request(
            "POST", "/api/data-pull/public/pull/v3/status",
            json_body=body,
            extra_headers={"tenant": tenant, **_node_headers(gstin_node_ids)},
        )
        return data["statusResponses"]

    # States that mean "Clear has stopped doing work on this pull" — either
    # success (DOWNLOADED), expected-no-op (NOT_APPLICABLE — GSTIN didn't yet
    # exist in this FY), or various outcomes the caller has to handle:
    #   - DOWNLOADED_PARTIALLY: Clear got some data but not all; auto-retry
    #     with DOWNLOAD_COMPLETE_DATA may help.
    #   - NOT_DOWNLOADED: Clear couldn't even start, almost always because
    #     its stored GSTN session for that GSTIN has expired and needs OTP
    #     re-authentication. No automated retry can fix this.
    #   - FAILED / ERROR: hard server-side failure.
    # We bail out of the poll loop on any of these.
    _SETTLED_STATUSES = (
        "DOWNLOADED",
        "NOT_APPLICABLE",
        "DOWNLOADED_PARTIALLY",
        "NOT_DOWNLOADED",
        "FAILED",
        "ERROR",
    )

    def wait_for_pull(
        self,
        gstin_node_ids: list[str],
        *,
        start_period: str,
        end_period: str,
        tenant: str = "GSTR2A_REPORTS",
        poll_seconds: int = 10,
        timeout_seconds: int = 1800,
    ) -> list[dict]:
        """Poll `poll_pull_status` until every entry is in a settled state.

        Returns the final status snapshot — the *caller* decides whether
        the outcome is acceptable (e.g. all DOWNLOADED vs some
        DOWNLOADED_PARTIALLY). Raises `TimeoutError` only if the polls never
        settle within `timeout_seconds`. Raises `ClearAPIError` only on
        outright FAILED / ERROR states.
        """
        deadline = time.monotonic() + timeout_seconds
        last_snapshot: list[dict] = []
        while True:
            last_snapshot = self.poll_pull_status(
                gstin_node_ids,
                start_period=start_period,
                end_period=end_period,
                tenant=tenant,
            )
            counts: dict[str, int] = {}
            for s in last_snapshot:
                counts[s.get("downloadStatus", "?")] = counts.get(s.get("downloadStatus", "?"), 0) + 1
            logger.info("Pull status: {}", counts)
            terminal_bad = [s for s in last_snapshot
                            if s.get("downloadStatus") in ("FAILED", "ERROR")]
            if terminal_bad:
                names = ", ".join(s.get("nodeName", "?") for s in terminal_bad)
                raise ClearAPIError(f"Pull failed for GSTINs: {names}")
            if all(s.get("downloadStatus") in self._SETTLED_STATUSES
                   for s in last_snapshot):
                logger.info("Pull settled: {}", counts)
                return last_snapshot
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Pull did not settle within {timeout_seconds}s. "
                    f"Last counts: {counts}"
                )
            time.sleep(poll_seconds)

    def fetch_rls_token(
        self,
        periods: list[str] | None = None,
        *,
        gstin_node_ids: list[str],
        workflow: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> str:
        """Get a short-lived RLS token. Required as `x-rls-token` on export/trigger.

        Clear gates this token by the GSTIN scope, which it reads from the
        `x-clear-node-id` / `x-clear-node-type` headers. Without those it
        returns a generic INTERNAL_SERVER 500.

        Two URL-param modes — pass one or the other (not both):

          - **Period mode** (default; GSTR-1/2A/2B/3B/8 and their reconciliation
            variants): pass `periods=["MMYYYY", ...]`. URL has repeated
            `returnPeriods=` entries.

          - **Date-range mode** (PAN Cash Ledger and other range-scoped reports):
            pass `from_date="DD-MM-YYYY", to_date="DD-MM-YYYY"`. URL has
            `fromDate=` and `toDate=` instead of `returnPeriods=`. Captured
            param order matches the HAR exactly.
        """
        if from_date is not None and to_date is not None:
            params: list[tuple[str, str]] = [
                ("workFlow", workflow),
                ("tableType", ""),
                ("fromDate", from_date),
                ("toDate", to_date),
            ]
        elif periods:
            params = [("returnPeriods", p) for p in periods]
            params.append(("workFlow", workflow))
            params.append(("tableType", ""))
        else:
            raise ValueError(
                "fetch_rls_token requires either `periods=[...]` or "
                "`from_date=..., to_date=...` (got neither)."
            )
        data = self._request(
            "POST", "/api/gst-auto-compute/public/rls/fetch-token",
            params=params,
            extra_headers=_node_headers(gstin_node_ids),
        )
        token = data["token"]
        logger.info("Got RLS token (expires {}) ", data.get("expiry"))
        return token

    def fetch_recon_rls_token(
        self,
        *,
        table_type: str,
        task_id: str,
        tenant: str = "IDT",
    ) -> str:
        """Get a short-lived RLS token for a recon matching task's data view.

        This is a SEPARATE endpoint from `fetch_rls_token` above — the recon
        (ultimatum) pipeline issues its tokens via
        `/api/recon/ultimatum/public/rls/fetch-token` keyed by the matching
        `taskId` (header `x-matching-task-id`) and the `tableType` query param,
        under tenant IDT. Used by the E-Invoice-vs-SR flow, which finishes via
        the data-browser export pipeline rather than workbench/report. Confirmed
        from discovery/app.clear.in.har__GSTR1 vs SR.har.
        """
        data = self._request(
            "POST", "/api/recon/ultimatum/public/rls/fetch-token",
            params=[("tableType", table_type)],
            extra_headers={
                "x-matching-task-id": task_id,
                "x-cleartax-tenant": tenant,
                "x-clear-node-type": "GSTIN",
            },
        )
        token = data["token"]
        logger.info("Got recon RLS token (expires {}) ", data.get("expiry"))
        return token

    def trigger_export(
        self, payload: dict, *, rls_token: str,
        referer_override: str | None = None,
        header_overrides: Mapping[str, str | None] | None = None,
    ) -> str:
        """Submit the export. Returns the 24-hex-char export job ID (plain text).

        referer_override: when set, replaces the session-default Referer header
        on this call. Required for panG3bvs1vsBooks (and likely any other newer
        data-browser report), which parses `reportType=` from the Referer query
        string and 500s with "Unknown error occurred." otherwise. Older flows
        (GSTR-2A/2B/1) tolerate the generic referer.

        header_overrides: per-call header additions/suppressions. A value of
        None for a key tells `requests` to drop that header (used to suppress
        session defaults like `x-ct-source` that some newer Clear endpoints
        reject). Values are merged after referer_override so they win.
        """
        extra_headers: dict[str, str | None] = {
            "x-rls-token": rls_token,
            "x-tenant-name": "GST_REPORTS",
        }
        if referer_override:
            extra_headers["referer"] = referer_override
        if header_overrides:
            extra_headers.update(header_overrides)

        text = self._request(
            "POST", "/api/clear/data-browser/public/export/trigger",
            json_body=payload,
            extra_headers=extra_headers,
            expect_text=True,
        )
        export_id = text.strip().strip('"')
        if len(export_id) < 12:
            raise ClearAPIError(f"Unexpected export/trigger response: {text!r}")
        logger.info("Triggered export, exportId={}", export_id)
        return export_id

    def run_data_browser_query(
        self, payload: dict, *, rls_token: str,
        referer_override: str | None = None,
        header_overrides: Mapping[str, str | None] | None = None,
    ) -> None:
        """Prime Clear's data-browser cube before an export trigger.

        The response is intentionally discarded — we want only the side effect
        of materializing the result set server-side so the subsequent
        `trigger_export` reads from a populated cube. Without this priming
        call, observed for GSTR-8 in `discovery/app.clear.in.har_GSTR-8.har`
        (UI line 68898), the export downloads a valid-shape-but-empty file.

        `payload` is the same SELECT statement passed to `trigger_export`,
        but with `limit: 1000` (a pageable preview) instead of `limit: 0`.
        """
        extra_headers: dict[str, str | None] = {
            "x-rls-token": rls_token,
            "x-tenant-name": "GST_REPORTS",
        }
        if referer_override:
            extra_headers["referer"] = referer_override
        if header_overrides:
            extra_headers.update(header_overrides)

        self._request(
            "POST", "/api/clear/data-browser/public/v2/query",
            json_body=payload,
            extra_headers=extra_headers,
        )
        logger.info("Primed data-browser cube via /v2/query")

    def get_export_status(
        self, export_id: str, *, tenant_name: str = "GST_REPORTS"
    ) -> dict:
        """One snapshot of the export job status.

        `tenant_name` sets the `x-tenant-name` header. Defaults to GST_REPORTS
        (every existing data-browser report); the E-Invoice-vs-SR recon export
        runs under IDT.
        """
        return self._request(
            "GET", f"/api/clear/data-browser/public/export/download/{export_id}",
            extra_headers={"x-tenant-name": tenant_name},
        )

    def wait_for_export(
        self,
        export_id: str,
        *,
        tenant_name: str = "GST_REPORTS",
        poll_seconds: int = 5,
        timeout_seconds: int = 900,
    ) -> ExportReady:
        """Poll `get_export_status` until `taskStatus == "SUCCESS"`.

        Tolerates intermediate states like PENDING/IN_PROGRESS/PROCESSING.
        Raises on FAILED-style states or on timeout. `tenant_name` is forwarded
        to `get_export_status` (default GST_REPORTS; IDT for the recon export).
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            data = self.get_export_status(export_id, tenant_name=tenant_name)
            status = (data.get("taskStatus") or "").upper()
            logger.info("Export {} status={}", export_id, status or "<empty>")
            if status == "SUCCESS":
                url = data.get("preSignedUrl") or (data.get("preSignedUrls") or [None])[0]
                fn = data.get("fileName")
                if not url or not fn:
                    raise ClearAPIError(
                        f"Export {export_id} reports SUCCESS but no preSignedUrl/fileName: {data!r}"
                    )
                return ExportReady(file_name=fn, pre_signed_url=url)
            if status in ("FAILED", "FAILURE", "ERROR", "CANCELLED"):
                raise ClearAPIError(f"Export {export_id} failed: {data!r}")
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Export {export_id} did not complete within {timeout_seconds}s "
                    f"(last status: {status!r})"
                )
            time.sleep(poll_seconds)

    def download_file(
        self,
        presigned_url: str,
        dest: Path,
        *,
        gstin_node_ids: list[str] | None = None,
    ) -> int:
        """Stream the presigned-URL response body to `dest`. Returns bytes written.

        The URL is on `app.clear.in/storage/v1/...` (Clear's S3 proxy), not S3
        directly. The proxy checks BOTH the AWS signature in the URL AND your
        ClearGST session cookies + x-* headers — so we have to use the
        authenticated session and pass node-id headers, otherwise the proxy
        returns 401 even though the AWS sig is valid.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"x-request-id": _nanoid()}
        if gstin_node_ids:
            headers.update(_node_headers(gstin_node_ids))
        with self.session.get(
            presigned_url, stream=True, timeout=self.timeout, headers=headers,
        ) as resp:
            resp.raise_for_status()
            written = 0
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
        logger.info("Wrote {} bytes -> {}", written, dest)
        return written

    # ---- GSTR-3B endpoints ----------------------------------------------
    #
    # GSTR-3B uses a completely separate backend from 2A/2B/1: instead of
    # `data-browser/export/trigger` it goes through `/api/gst-reports/...`,
    # there is no RLS token, and the trigger body for a specific variant
    # (Combined / Filed / etc.) is only 4 small fields. The 5 variants share
    # a single data-pull job; only the per-variant `reportDownload` POST and
    # its `ledgers/report/<id>/status` polls vary. See discovery/app.clear.in.har
    # (request bodies captured at lines 48828 and 104888) for the verbatim
    # captures these methods were modelled on.

    # x-job-type for every 3B call; lifted from the HAR header.
    _3B_JOB_TYPE = "PAN_MM3B_REPORT"

    def _3b_headers(self, gstin_node_ids: list[str]) -> dict[str, str]:
        """Headers required on every GSTR-3B endpoint.

        The orgunit/product/workspace headers are already set as session
        defaults; we only need x-job-type and the per-call node-id list.
        Sending an empty x-clear-node-id (matches the HAR for `reportPoller`)
        is intentional and accepted — the pull state is keyed by jobId, not
        by node-id, so the header just needs to exist.
        """
        return {
            "x-job-type": self._3B_JOB_TYPE,
            **_node_headers(gstin_node_ids),
        }

    def trigger_3b_data_pull(
        self,
        *,
        pan_node_id: str,
        gstin_node_ids: list[str],
        return_periods: list[str],   # ["MMYYYY", ...]
        workspace_id: str,
    ) -> str:
        """Kick off the shared GSTR-3B data pull for a (PAN, FY).

        Returns the `jobId` to feed into `wait_for_3b_data_pull` and into
        every subsequent `trigger_3b_report_download` for the same combo —
        one pull powers all 5 variants.
        """
        # workspace_id arg is currently unused (session already carries it
        # in default headers), but accepted to keep the call sites readable.
        del workspace_id
        body = {
            "panNodeId": pan_node_id,
            "gstinNodeIds": gstin_node_ids,
            "returnPeriods": return_periods,
            "reportType": "PAN_MM3B_REPORT",
            "triggerSource": "REPORTS_UI",
            "outputType": "DATA",
            "fyLevelReport": True,
        }
        data = self._request(
            "POST", "/api/gst-reports/reports/v1.0/trigger/report/data/pull",
            json_body=body,
            extra_headers=self._3b_headers(gstin_node_ids),
        )
        job_id = data.get("jobId") or (data.get("data") or {}).get("jobId")
        if not job_id:
            raise ClearAPIError(
                f"3B data-pull trigger returned no jobId: {data!r}"
            )
        logger.info("Triggered 3B data pull, jobId={}", job_id)
        return job_id

    # Per-GSTIN download_status values observed (HAR) or extrapolated.
    # "COMPLETED" replaces 2A/2B/1's "DOWNLOADED" in this backend's vocabulary;
    # the others are the same shape so partials.py logic still applies.
    _3B_SETTLED_STATUSES = (
        "COMPLETED",
        "DOWNLOADED",            # not observed for 3B but harmless to accept
        "NOT_APPLICABLE",
        "PARTIALLY_COMPLETED",   # ✓ observed live — Clear's actual per-GSTIN partial state
        "DOWNLOADED_PARTIALLY",  # legacy/defensive synonym (never observed)
        "NOT_DOWNLOADED",
        "FAILED",
        "ERROR",
    )

    # When the report-status endpoint flips to COMPLETED / PARTIALLY_COMPLETED
    # but hasn't yet exposed `reportUri`, keep polling this many more times
    # before declaring the missing URL a hard failure. 5 polls x ~5s each =
    # 25s of grace, which has been enough in live testing for the URI to
    # appear (usually it shows up on the very next poll).
    _3B_URI_GRACE_POLLS = 5

    def _normalise_3b_snapshot(self, poller_response: dict) -> list[dict]:
        """Flatten the nested `reportPoller` shape into the per-GSTIN list
        the 2A/2B/1 flow code already understands (`nodeName`, `downloadStatus`).

        3B response shape (from HAR):
            {"jobs": [{"downloadPercentage": 99.0,
                       "gstinData": [
                           {"gstin": "27...", "gstinState": "Maharashtra",
                            "downloadStatus": "COMPLETED", ...},
                           ...]}]}

        Output rows mirror what `poll_pull_status` returns for 2A/2B/1 so
        downstream code (`_any_partial`, `_summarize_issues`, `log_partial_items`)
        can be reused unchanged.
        """
        rows: list[dict] = []
        for job in poller_response.get("jobs") or []:
            for g in job.get("gstinData") or []:
                rows.append({
                    # `nodeName` / `nodeValue` are the field names 2A/2B/1's
                    # status snapshot uses; mirror them so partials.py and
                    # status_report.py work on 3B rows unchanged.
                    "nodeName": g.get("gstinState") or g.get("gstin", "?"),
                    "nodeValue": g.get("gstin", ""),
                    "gstin": g.get("gstin"),
                    "downloadStatus": g.get("downloadStatus"),
                    "otpStatus": g.get("otpStatus"),
                    "userName": g.get("userName"),
                    "lastRefreshDate": g.get("lastRefreshDate"),
                })
        return rows

    def wait_for_3b_data_pull(
        self,
        job_id: str,
        *,
        gstin_node_ids: list[str],
        workspace_id: str,
        poll_seconds: int = 10,
        timeout_seconds: int = 1800,
    ) -> list[dict]:
        """Poll `reportPoller?jobId=<id>` until every GSTIN settles.

        Returns the normalised per-GSTIN snapshot (same dict shape 2A/2B/1
        use) so the flow's partial / no-data handling can stay generic.
        """
        del workspace_id  # already in session headers
        deadline = time.monotonic() + timeout_seconds
        last_rows: list[dict] = []
        while True:
            raw = self._request(
                "GET", "/api/gst-reports/reports/v1.0/reportPoller",
                params={"jobId": job_id},
                extra_headers=self._3b_headers(gstin_node_ids),
            )
            # Defensive: if Clear has a transient hiccup and returns
            # {"errorCode": "...", "jobs": null}, treat as still-running.
            err_code = raw.get("errorCode")
            if err_code and not raw.get("jobs"):
                logger.warning(
                    "3B reportPoller returned errorCode={!r}: {}; "
                    "treating as transient and continuing to poll.",
                    err_code, raw.get("errorMessage"),
                )
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"3B data pull {job_id} did not settle within "
                        f"{timeout_seconds}s; last errorCode={err_code!r}"
                    )
                time.sleep(poll_seconds)
                continue

            last_rows = self._normalise_3b_snapshot(raw)
            counts: dict[str, int] = {}
            for r in last_rows:
                counts[r.get("downloadStatus") or "?"] = counts.get(
                    r.get("downloadStatus") or "?", 0,
                ) + 1
            pct = (raw.get("jobs") or [{}])[0].get("downloadPercentage")
            logger.info("3B pull {}%: {}", pct, counts)

            # Per-GSTIN FAILED on this backend is the analog of 2A/2B/1's
            # NOT_DOWNLOADED — Clear's stored GSTN session for that GSTIN
            # has expired or it lost the connection. The caller decides
            # whether to log + bail or proceed with partial data, so we
            # return the snapshot like 2A/2B/1's wait_for_pull does, NOT
            # raise here. (Hard pull-level errors surface via errorCode at
            # the top of the response, handled above.)
            if (last_rows
                    and all(r.get("downloadStatus") in self._3B_SETTLED_STATUSES
                            for r in last_rows)):
                logger.info("3B pull settled: {}", counts)
                return last_rows

            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"3B pull {job_id} did not settle within {timeout_seconds}s. "
                    f"Last counts: {counts}"
                )
            time.sleep(poll_seconds)

    def fetch_3b_summary(
        self,
        *,
        data_pull_job_id: str,
        gstin_node_ids: list[str],
        workspace_id: str,
    ) -> dict:
        """Fetch the on-screen 3B summary tables (the same call Clear's UI
        makes when it renders the report page after a pull settles).

        Required between `wait_for_3b_data_pull` and `trigger_3b_report_download`.
        Without it, Clear's reportDownload endpoint will accept the trigger,
        return COMPLETED in seconds, and serve a presigned URL pointing at a
        valid-shape-but-empty XLSX (all cells zero). The call seems to
        materialize Clear's per-period data from the upstream pull into
        the downstream report-builder's working store.

        Returns the parsed summary dict (large — ~120 KB in the captured
        HAR) but the flow only needs it for the side effect.
        """
        del workspace_id
        body = {
            "gstinNodeIds": gstin_node_ids,
            "jobId": data_pull_job_id,
        }
        data = self._request(
            "POST", "/api/gst-reports/reports/v1.0/fetch/3BSummary",
            json_body=body,
            extra_headers=self._3b_headers(gstin_node_ids),
        )
        # Log a rough size hint without dumping the whole payload — useful
        # for debugging "is the data actually there?" questions.
        if isinstance(data, dict):
            top_keys = list(data.keys())[:5]
            logger.info(
                "Fetched 3B summary (top-level keys: {}{})",
                top_keys, "..." if len(data) > 5 else "",
            )
        return data if isinstance(data, dict) else {}

    def trigger_3b_report_download(
        self,
        *,
        data_pull_job_id: str,
        sheet_type: str,     # e.g. "GSTR_3B_COMBINED_REPORT"
        output_type: str,    # "EXCEL" or "PDF"
        gstin_node_ids: list[str],
        workspace_id: str,
    ) -> str:
        """Trigger one variant's report download. Returns the per-variant jobId
        for `wait_for_3b_report`."""
        del workspace_id
        body = {
            "jobId": data_pull_job_id,
            "reportType": "PAN_MM3B_REPORT",
            "sheetType": sheet_type,
            "outputType": output_type,
            "triggerSource": "REPORT_UI",
        }
        data = self._request(
            "POST", "/api/gst-reports/reports/v1.0/reportDownload",
            json_body=body,
            extra_headers=self._3b_headers(gstin_node_ids),
        )
        report_job_id = data.get("jobId")
        if not report_job_id:
            raise ClearAPIError(
                f"3B reportDownload returned no jobId: {data!r}"
            )
        # If Clear echoes the data-pull jobId back as the "report" jobId AND
        # omits `referenceJobId`, it didn't actually create a new report job.
        # That happens when the requested variant isn't producible for this
        # (PAN, FY) — observed in the wild for Combined Report on FYs where
        # some monthly 3Bs haven't been filed. Fail fast with an actionable
        # message instead of polling a phantom job for 30+ seconds before
        # giving up on the missing reportUri.
        if (report_job_id == data_pull_job_id
                and not data.get("referenceJobId")):
            raise ClearAPIError(
                f"3B reportDownload for sheet_type={sheet_type!r} didn't "
                f"create a new report job (Clear returned the data-pull "
                f"jobId, ref=None). This variant likely isn't available "
                f"for this PAN+FY — common when not all monthly 3Bs in the "
                f"FY have been filed, or the report type doesn't apply to "
                f"this taxpayer. Try the other variants or a different FY."
            )
        logger.info(
            "Triggered 3B {} report, reportJobId={} (ref={})",
            sheet_type, report_job_id, data.get("referenceJobId"),
        )
        return report_job_id

    def wait_for_3b_report(
        self,
        report_job_id: str,
        *,
        gstin_node_ids: list[str],
        workspace_id: str,
        poll_seconds: int = 5,
        timeout_seconds: int = 900,
    ) -> Gstr3bReportReady:
        """Poll `ledgers/report/<jobId>/status` until `status == "COMPLETED"`.

        Intermediate state observed in HAR: `"ACCEPTED"` (returns until the
        report is built). On COMPLETED the response carries a `reportUri`
        pointing at a presigned `storage.clear.in/.../ct-document-service-prod/`
        URL — pass that straight to `download_file`.
        """
        del workspace_id
        deadline = time.monotonic() + timeout_seconds
        terminal_no_uri_polls = 0
        while True:
            data = self._request(
                "GET",
                f"/api/gst-reports/reports/v1.0/ledgers/report/{report_job_id}/status",
                extra_headers=self._3b_headers(gstin_node_ids),
            )
            status = (data.get("status") or "").upper()
            logger.info("3B report {} status={}", report_job_id, status or "<empty>")
            # COMPLETED / PARTIALLY_COMPLETED: the report is finished. Two
            # subtleties observed in live testing:
            #
            #   - Clear sometimes flips status to COMPLETED a few seconds
            #     before populating `reportUri` (eventual consistency on
            #     their side). We tolerate up to `_3B_URI_GRACE_POLLS`
            #     follow-up polls without a URI before giving up — usually
            #     the URI appears on the very next poll.
            #
            #   - PARTIALLY_COMPLETED with no URI is a *persistent* terminal
            #     state, not eventual: Clear gave up because too many GSTINs
            #     failed the upstream pull. We let the grace counter run
            #     down and then surface an actionable OTP message.
            if status in ("COMPLETED", "PARTIALLY_COMPLETED"):
                report_uri = data.get("reportUri")
                if report_uri:
                    if status == "PARTIALLY_COMPLETED":
                        logger.warning(
                            "3B report {} settled PARTIALLY_COMPLETED — the "
                            "downloaded file will be missing rows for any "
                            "GSTINs that failed the upstream pull. See "
                            "state/partial-items.csv for which states need "
                            "OTP re-auth in ClearGST's UI.",
                            report_job_id,
                        )
                    from urllib.parse import unquote, urlparse
                    last = urlparse(report_uri).path.rsplit("/", 1)[-1]
                    file_name = unquote(last) or "3b-report"
                    return Gstr3bReportReady(file_name=file_name, report_uri=report_uri)

                terminal_no_uri_polls += 1
                if terminal_no_uri_polls > self._3B_URI_GRACE_POLLS:
                    if status == "PARTIALLY_COMPLETED":
                        raise ClearAPIError(
                            f"3B report {report_job_id} settled "
                            f"PARTIALLY_COMPLETED with no reportUri — Clear "
                            f"couldn't build the report because too many "
                            f"GSTINs failed the upstream pull. Open ClearGST "
                            f"-> this PAN+FY's report page -> 'Generate OTP "
                            f"to connect GSTINs' for the FAILED states "
                            f"(listed in state/partial-items.csv), then re-run."
                        )
                    raise ClearAPIError(
                        f"3B report {report_job_id} stayed COMPLETED with no "
                        f"reportUri across {self._3B_URI_GRACE_POLLS + 1} polls "
                        f"— Clear's report-status endpoint isn't exposing the "
                        f"download URL: {data!r}"
                    )
                logger.info(
                    "3B report {} {} but reportUri not yet populated "
                    "(grace poll {}/{})",
                    report_job_id, status,
                    terminal_no_uri_polls, self._3B_URI_GRACE_POLLS,
                )
                # Fall through to the inter-poll sleep below.
            elif status in ("FAILED", "FAILURE", "ERROR", "CANCELLED"):
                raise ClearAPIError(f"3B report {report_job_id} failed: {data!r}")
            # ACCEPTED / IN_PROGRESS / PROCESSING / empty → keep polling
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"3B report {report_job_id} did not complete within "
                    f"{timeout_seconds}s (last status: {status!r})"
                )
            time.sleep(poll_seconds)

    # ---- recon/ultimatum endpoints (2B-vs-PR reconciliation) -------------
    #
    # The 2B-vs-PR report does NOT use the data-browser export pipeline. It
    # uses Clear's recon "matching task" backend under
    # /api/recon/ultimatum/public/... Flow (per PAN x FY), all confirmed from
    # discovery/app.clear.in.har__2B vs PR Reconciliation*.har:
    #   1. POST matching/v2/trigger  -> requestId (== matching taskId)
    #   2. GET  matching/current     -> poll until taskStatus == DATAVIEW_READY
    #   3. POST workbench/report/v1/generate -> reportId (plain UUID text)
    #   4. GET  workbench/report/v1/{reportId}/download -> presigned XLSX url
    # Step 1 also fetches the underlying 2B/PR data server-side, so there is no
    # separate data-pull. matchType is fixed: MAX_ITC_2B_PR.

    _RECON_TENANT = "IDT"
    _RECON_READY_STATUS = "DATAVIEW_READY"
    # Per-variant cosmetic bits used in the trigger taskContext. The matchType
    # itself ("MAX_ITC_2B_PR" / "MAX_ITC_2A_PR") is what actually selects the
    # report; these strings just mirror what the UI sends. (side_label, ui_path).
    _RECON_VARIANTS = {
        "MAX_ITC_2B_PR": ("2B", "/reconciliation/idt/2bVsPr"),
        "MAX_ITC_2A_PR": ("2A", "/reconciliation/idt/2aVsPr"),
        "MAX_ITC_6A_PR": ("6A", "/reconciliation/idt/6aVsPr"),
        "MAX_ITC_8A_PR": ("8A", "/reconciliation/idt/8aVsPr"),
        # E-Invoice (GSTR-1) vs Sales Register. Unlike the *-vs-PR variants
        # above (which finish via workbench/report/v1/generate), this one is a
        # hybrid: the match runs here but the report is downloaded via the
        # data-browser export pipeline under tenant IDT. See
        # flows/gstr_1_einvoice_vs_sr.py. Confirmed from
        # discovery/app.clear.in.har__GSTR1 vs SR.har.
        "MAX_ITC_G1EInv_SR": ("G1", "/reconciliation/idt/G1EInvVsSr"),
    }

    def _recon_pan_headers(
        self, pan_node_id: str, match_type: str = "MAX_ITC_2B_PR"
    ) -> dict[str, str]:
        """Node headers for a PAN-scoped recon call (matching/current)."""
        return {
            "x-clear-node-id": pan_node_id,
            "x-clear-node-type": "PAN",
            "x-cleartax-matching-type": match_type,
        }

    @staticmethod
    def _recon_period_filter(label: str, start_rp: str, end_rp: str) -> dict:
        """A single RETURN_PERIOD_RANGE filter block (PR or 2B side)."""
        return {
            "filters": [
                {
                    "filterKey": "returnPeriod",
                    "operatorType": "OR",
                    "filterType": "RETURN_PERIOD_RANGE",
                    "customFieldFilter": False,
                    "label": label,
                    "required": True,
                    "commaSeparated": False,
                    "filterValueStart": start_rp,
                    "filterValueEnd": end_rp,
                }
            ]
        }

    def recon_matching_trigger(
        self,
        *,
        pan_node_id: str,
        pan: str,
        start_rp: str,  # MMYYYY
        end_rp: str,    # MMYYYY
        match_type: str = "MAX_ITC_2B_PR",
        lhs_label: str = "PR Return Period",
        rhs_label: str | None = None,
        wait_if_busy: bool = False,
        busy_poll_seconds: int = 10,
        busy_timeout_seconds: int = 1800,
    ) -> str:
        """Trigger a fresh recon match for one PAN over [start_rp, end_rp].

        `match_type` selects the report variant (MAX_ITC_2B_PR / MAX_ITC_2A_PR /
        ... / MAX_ITC_G1EInv_SR); it defaults to 2B so existing callers are
        unchanged. Both the lhs and rhs sides use the same period range —
        confirmed against every HAR capture.

        `lhs_label` / `rhs_label` are the (cosmetic) filter labels the UI sends.
        They default to the *-vs-PR shape ("PR Return Period" / "<side> Return
        Period") so the PR flows are byte-identical; the E-Invoice-vs-SR flow
        overrides them to "SR Return Period" / "G1 Return Period".

        Returns the trigger's `requestId` (Clear's response field is
        `payload.requestId`). NOTE: for some variants (e.g. G1EInv_SR) this
        requestId differs from the `taskId` later reported by matching/current —
        callers that need the matching-task id should read it from
        `wait_for_recon_matching`'s returned payload, not from this value.

        `wait_if_busy` (default False, so the PR flows are byte-identical):
        Clear's recon backend allows only ONE matching task in progress per PAN
        (per matchType) at a time. While one is running, a fresh trigger is
        rejected with HTTP 400 `payload="Duplicate Request"` — it is a
        concurrency lock, NOT a content-level dedup (verified live: the same
        trigger that 400s while a match is DATA_FETCH_INITIATED succeeds once
        the prior match reaches DATAVIEW_READY). When `wait_if_busy=True` we
        treat that 400 as "lock held" and retry the trigger every
        `busy_poll_seconds` until it is accepted or `busy_timeout_seconds`
        elapses. This makes batch runs (many FYs, or a stale match left by an
        interrupted prior run / the Clear UI open on the recon page) self-heal
        instead of cascading into per-FY failures.
        """
        side_label, ui_path = self._RECON_VARIANTS[match_type]
        if rhs_label is None:
            rhs_label = f"{side_label} Return Period"
        # Built to match the verbatim taskContext the UI sends on
        # matching/v2/trigger (json.dumps keeps Clear's exact key order).
        task_context = json.dumps({
            "xClearNodeId": pan_node_id,
            "xClearNodeType": "PAN",
            "xCleartaxProduct": "GST",
            "xCleartaxMatchingType": match_type,
            "xReconMode": None,
            "xOrganisationId": self.workspace_id,
            "xWorkspaceId": self.workspace_id,
            "path": ui_path,
            "gstin": "",
            "inception": "framework",
        }, separators=(",", ":"))
        body = {
            "panNodeId": pan_node_id,
            "matchType": match_type,
            "nodeName": pan,
            "lhsDocumentFilter": self._recon_period_filter(
                lhs_label, start_rp, end_rp
            ),
            "rhsDocumentFilter": self._recon_period_filter(
                rhs_label, start_rp, end_rp
            ),
            "taskContext": task_context,
            # The PAN-scope selector. Omitting this 400s with "Invalid request."
            "commonDocumentFilter": {
                "filters": [
                    {
                        "filterKey": "nodeId",
                        "operatorType": "OR",
                        "filterType": "BH_SELECTOR",
                        "filterValues": [pan_node_id],
                        "customFieldFilter": False,
                        "label": "Business (PAN/GSTIN)",
                        "required": True,
                        "commaSeparated": False,
                    }
                ]
            },
        }
        trigger_headers = {
            # The trigger identifies the entity via x-node-id/x-node-type (PAN)
            # plus the matching type; x-clear-node-type stays GSTIN (verbatim
            # from the HAR). Missing these also yields a 400.
            "x-node-id": pan_node_id,
            "x-node-type": "PAN",
            "x-clear-node-type": "GSTIN",
            "x-cleartax-matching-type": match_type,
        }
        busy_deadline = time.monotonic() + busy_timeout_seconds
        while True:
            try:
                data = self._request(
                    "POST", "/api/recon/ultimatum/public/matching/v2/trigger",
                    json_body=body,
                    extra_headers=trigger_headers,
                )
                break
            except ClearAPIError as e:
                # "Duplicate Request" == another match is already in progress for
                # this PAN. With wait_if_busy, wait for that lock to free and
                # retry rather than failing this (PAN, FY).
                if not (wait_if_busy and "Duplicate Request" in str(e)):
                    raise
                if time.monotonic() > busy_deadline:
                    raise TimeoutError(
                        f"Recon trigger for PAN {pan} ({start_rp}..{end_rp}) "
                        f"kept getting 'Duplicate Request' (another match in "
                        f"progress for this PAN) for {busy_timeout_seconds}s"
                    ) from e
                logger.info(
                    "Recon busy for PAN {} (another match in progress); "
                    "retrying trigger in {}s...", pan, busy_poll_seconds,
                )
                time.sleep(busy_poll_seconds)
        payload = data.get("payload") or {}
        task_id = payload.get("requestId")
        if not task_id:
            raise ClearAPIError(
                f"matching/v2/trigger returned no requestId: {data!r}"
            )
        logger.info(
            "Triggered {}-vs-PR match for PAN {} ({}..{}), taskId={} status={}",
            side_label, pan, start_rp, end_rp, task_id, payload.get("status"),
        )
        return task_id

    def recon_matching_current(
        self, pan_node_id: str, match_type: str = "MAX_ITC_2B_PR"
    ) -> dict:
        """One snapshot of the current matching task for this PAN (the recon
        'current' state). Returns the `payload` dict (taskId, taskStatus,
        returnPeriodRange, corrupted, errorInfo, ...)."""
        data = self._request(
            "GET", "/api/recon/ultimatum/public/matching/current",
            extra_headers=self._recon_pan_headers(pan_node_id, match_type),
        )
        return data.get("payload") or {}

    def wait_for_recon_matching(
        self,
        *,
        pan_node_id: str,
        task_id: str | None = None,
        match_type: str = "MAX_ITC_2B_PR",
        expected_range: tuple[str, str] | None = None,
        poll_seconds: int = 10,
        timeout_seconds: int = 1800,
    ) -> dict:
        """Poll `matching/current` until the match reaches DATAVIEW_READY.

        Two acceptance modes:
          - **By task id** (default; the *-vs-PR flows): pass `task_id`. The
            ready payload is accepted only when `payload.taskId == task_id`.
            This is correct for those variants because their trigger's
            `requestId` *equals* the matching/current `taskId`.
          - **By period range** (the E-Invoice-vs-SR flow): pass `task_id=None`
            and `expected_range=(start_rp, end_rp)`. There, the trigger's
            `requestId` differs from the matching/current `taskId`, so we can't
            match on id; instead we accept the first DATAVIEW_READY whose
            `returnPeriodRange` matches the requested range. The caller reads
            the real matching-task id from the returned payload's `taskId`.

        Raises on a corrupted task, an errorInfo payload, or timeout. Returns
        the ready payload.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            payload = self.recon_matching_current(pan_node_id, match_type)
            cur_task = payload.get("taskId")
            status = (payload.get("taskStatus") or "").upper()
            logger.info(
                "Recon task {} status={} (current taskId={})",
                task_id or "<by-range>", status or "<empty>", cur_task,
            )
            if payload.get("corrupted"):
                raise ClearAPIError(
                    f"Recon task {task_id} reported corrupted=true: {payload!r}"
                )
            if payload.get("errorInfo"):
                raise ClearAPIError(
                    f"Recon task {task_id} failed: {payload.get('errorInfo')!r}"
                )
            if status == self._RECON_READY_STATUS and self._recon_match_accepted(
                payload, task_id=task_id, expected_range=expected_range,
            ):
                return payload
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Recon match {task_id or expected_range} did not reach "
                    f"{self._RECON_READY_STATUS} within {timeout_seconds}s "
                    f"(last status: {status!r})"
                )
            time.sleep(poll_seconds)

    @staticmethod
    def _recon_match_accepted(
        payload: dict,
        *,
        task_id: str | None,
        expected_range: tuple[str, str] | None,
    ) -> bool:
        """Decide whether a DATAVIEW_READY payload is the one we're waiting for.

        If `task_id` is set, require an exact taskId match. Otherwise require the
        payload's `returnPeriodRange` to match `expected_range` on both sides —
        a staleness guard so a leftover prior-period match isn't accepted.
        """
        if task_id is not None:
            return payload.get("taskId") == task_id
        if expected_range is None:
            # No id and no range to verify against — accept any ready match.
            return True
        start_rp, end_rp = expected_range
        rng = payload.get("returnPeriodRange") or {}
        return (
            rng.get("fromLhsRp") == start_rp
            and rng.get("toLhsRp") == end_rp
            and rng.get("fromRhsRp") == start_rp
            and rng.get("toRhsRp") == end_rp
        )

    def recon_report_generate(
        self,
        *,
        task_id: str,
        pan_node_id: str,
        pan: str,
        business_name: str,
        start_rp: str,  # MMYYYY
        end_rp: str,    # MMYYYY
        match_type: str = "MAX_ITC_2B_PR",
    ) -> str:
        """Request the EXCEL recon report for a ready matching task.

        Returns the `reportId` (Clear returns it as a bare UUID, not JSON).
        govt* fields are the GSTN (2B/2A) side, purchase* fields the PR side —
        both equal the same period range here.
        """
        side_label, _ = self._RECON_VARIANTS[match_type]
        callback_metadata = {
            "notificationType": "REPORT_GENERATION_V2",
            "product": "GST",
            "tenant": self._RECON_TENANT,
            "nodeId": [pan_node_id],
            "nodeType": "PAN",
            "xMatchingTaskId": task_id,
            "orgId": self.workspace_id,
            "workspaceId": self.workspace_id,
        }
        body = {
            "filter": {"dslFilters": []},
            "format": "EXCEL",
            "matchingReportGenerationType": match_type,
            "metadata": {
                "businessName": business_name,
                "govtFromRp": start_rp,
                "govtToRp": end_rp,
                "gstinNumber": "",
                "panNumber": pan,
                "purchaseFromRp": start_rp,
                "purchaseToRp": end_rp,
                "xMatchingTaskId": task_id,
            },
            "version": "V2",
            "reportTypes": ["DOCUMENT"],
            "onStartCallback": {
                "callbackType": "KRAMER",
                "metadata": dict(callback_metadata),
            },
            "onFinishCallback": {
                "callbackType": "KRAMER",
                "metadata": dict(callback_metadata),
            },
        }
        text = self._request(
            "POST", "/api/recon/ultimatum/public/workbench/report/v1/generate",
            json_body=body,
            extra_headers={
                "x-matching-task-id": task_id,
                "x-clear-node-type": "GSTIN",
            },
            expect_text=True,
        )
        report_id = text.strip().strip('"')
        if len(report_id) < 12:
            raise ClearAPIError(
                f"Unexpected report/v1/generate response: {text!r}"
            )
        logger.info(
            "Triggered {}-vs-PR report generation, reportId={}",
            side_label, report_id,
        )
        return report_id

    def recon_report_download(self, report_id: str, *, task_id: str) -> dict:
        """One snapshot of the recon report's generation status. Returns the
        raw dict ({status, fileInfos:[...]})."""
        return self._request(
            "GET",
            f"/api/recon/ultimatum/public/workbench/report/v1/{report_id}/download",
            extra_headers={
                "x-matching-task-id": task_id,
                "x-clear-node-type": "GSTIN",
            },
        )

    def wait_for_recon_report(
        self,
        report_id: str,
        *,
        task_id: str,
        poll_seconds: int = 5,
        timeout_seconds: int = 900,
    ) -> ReconReportReady:
        """Poll the recon report endpoint until `status == "SUCCESS"` with a
        non-empty `fileInfos`. Tolerates intermediate IN_PROGRESS/PENDING.
        Raises on FAILED-style states or timeout."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            data = self.recon_report_download(report_id, task_id=task_id)
            status = (data.get("status") or "").upper()
            file_infos = data.get("fileInfos") or []
            logger.info(
                "Recon report {} status={} files={}",
                report_id, status or "<empty>", len(file_infos),
            )
            if status == "SUCCESS" and file_infos:
                fi = file_infos[0]
                url = fi.get("url")
                fn = fi.get("filename")
                if not url or not fn:
                    raise ClearAPIError(
                        f"Recon report {report_id} SUCCESS but no url/filename: {data!r}"
                    )
                return ReconReportReady(file_name=fn, pre_signed_url=url)
            if status in ("FAILED", "FAILURE", "ERROR", "CANCELLED"):
                raise ClearAPIError(f"Recon report {report_id} failed: {data!r}")
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Recon report {report_id} did not complete within "
                    f"{timeout_seconds}s (last status: {status!r})"
                )
            time.sleep(poll_seconds)


# ---- header helpers ----

def _node_headers(gstin_node_ids: Iterable[str]) -> dict[str, str]:
    return {
        "x-clear-node-id": ",".join(gstin_node_ids),
        "x-clear-node-type": "GSTIN",
    }
