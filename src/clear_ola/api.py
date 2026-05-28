"""Thin typed wrapper around `requests.Session` for the ClearGST endpoints we
discovered in Phase 0 (see discovery/FINDINGS.md). Auth = Chrome session cookies
+ a small set of `x-cleartax-*` / `x-workspace-id` identifier headers."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

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
        extra_headers: dict[str, str] | None = None,
        stream: bool = False,
        expect_text: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else f"{BASE}{path}"
        headers = {"x-request-id": _nanoid()}
        if extra_headers:
            headers.update(extra_headers)
        logger.debug("HTTP {} {} params={} body_keys={}",
                     method, path,
                     params if isinstance(params, dict) else "[...]" if params else None,
                     list(json_body.keys()) if isinstance(json_body, dict) else None)
        resp = self.session.request(
            method, url,
            params=params, json=json_body,
            headers=headers, timeout=self.timeout, stream=stream,
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
            "metadata": {"reportLevel": "PAN"},
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
        periods: list[str],
        *,
        gstin_node_ids: list[str],
        workflow: str,
    ) -> str:
        """Get a short-lived RLS token. Required as `x-rls-token` on export/trigger.

        Clear gates this token by the GSTIN scope, which it reads from the
        `x-clear-node-id` / `x-clear-node-type` headers. Without those it
        returns a generic INTERNAL_SERVER 500."""
        # Build params with repeated returnPeriods= entries, then workFlow,
        # then an empty tableType= (matches the HAR exactly).
        params: list[tuple[str, str]] = [("returnPeriods", p) for p in periods]
        params.append(("workFlow", workflow))
        params.append(("tableType", ""))
        data = self._request(
            "POST", "/api/gst-auto-compute/public/rls/fetch-token",
            params=params,
            extra_headers=_node_headers(gstin_node_ids),
        )
        token = data["token"]
        logger.info("Got RLS token (expires {}) ", data.get("expiry"))
        return token

    def trigger_export(self, payload: dict, *, rls_token: str) -> str:
        """Submit the export. Returns the 24-hex-char export job ID (plain text)."""
        text = self._request(
            "POST", "/api/clear/data-browser/public/export/trigger",
            json_body=payload,
            extra_headers={
                "x-rls-token": rls_token,
                "x-tenant-name": "GST_REPORTS",
            },
            expect_text=True,
        )
        export_id = text.strip().strip('"')
        if len(export_id) < 12:
            raise ClearAPIError(f"Unexpected export/trigger response: {text!r}")
        logger.info("Triggered export, exportId={}", export_id)
        return export_id

    def get_export_status(self, export_id: str) -> dict:
        """One snapshot of the export job status."""
        return self._request(
            "GET", f"/api/clear/data-browser/public/export/download/{export_id}",
            extra_headers={"x-tenant-name": "GST_REPORTS"},
        )

    def wait_for_export(
        self,
        export_id: str,
        *,
        poll_seconds: int = 5,
        timeout_seconds: int = 900,
    ) -> ExportReady:
        """Poll `get_export_status` until `taskStatus == "SUCCESS"`.

        Tolerates intermediate states like PENDING/IN_PROGRESS/PROCESSING.
        Raises on FAILED-style states or on timeout.
        """
        deadline = time.monotonic() + timeout_seconds
        while True:
            data = self.get_export_status(export_id)
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


# ---- header helpers ----

def _node_headers(gstin_node_ids: Iterable[str]) -> dict[str, str]:
    return {
        "x-clear-node-id": ",".join(gstin_node_ids),
        "x-clear-node-type": "GSTIN",
    }
