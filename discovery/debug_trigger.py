"""One-shot debug: load cookies, look up the PAN's GSTIN nodes, then attempt the
pull/v2/trigger call with full visibility into what we send vs what Clear sees."""

import json
import logging
import sys
from pathlib import Path

# Enable urllib3 / http.client request-line logging
import http.client
http.client.HTTPConnection.debuglevel = 1
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("urllib3").setLevel(logging.DEBUG)
logging.getLogger("requests").setLevel(logging.DEBUG)

from clear_ola.api import ClearAPI, _node_headers
from clear_ola.config import AppConfig
from clear_ola.cookies import load_clear_cookies


def main():
    cfg = AppConfig.load(Path("config.yaml"))
    cookies = load_clear_cookies(cfg.chrome_profile)
    api = ClearAPI(workspace_id=cfg.workspace_id, cookies=cookies)

    nodes = api.user_gstins()
    pan = cfg.pans[0].pan  # AAGCP5410J
    gstins = [n for n in nodes if n.pan == pan]
    print(f"\n=== Found {len(gstins)} GSTINs for PAN {pan} ===")
    # Use exactly the 8 nodeIds from the HAR, to isolate "too many nodeIds" from
    # other potential issues.
    node_ids = [
        "01cbd28b-126b-46ed-bc85-b8aa5eb2fe83",
        "eb29a58f-3cd5-49d3-b00b-053e14d3cc21",
        "deac3f27-1a00-4810-ad38-2ef1a28aaea0",
        "6a4f6153-f035-4c4e-adbe-956dc8b07ae4",
        "bd0bc2d4-4f45-4b82-b332-898ba137204f",
        "7e1f2be2-f48e-4f8a-91de-f9a25d94c100",
        "c580313d-1e1d-405a-93c4-041ab29764e8",
        "b64933f5-5ea7-4a6d-a847-f49ae1383f46",
    ]
    print(f"Testing with HAR's exact 8 nodeIds (not all {len(gstins)})")

    # Build the exact body we send
    body = {
        "nodeType": "GSTIN",
        "nodeIds": node_ids,
        "startRange": "042025",
        "endRange": "032026",
        "pullType": "OPTIMIZED_PULL",
        "tenant": "GSTR2A_REPORTS",
        "dataSources": [],
        "pageIds": [],
        "nodeIdDataSourcesMap": {},
        "metadata": {"reportLevel": "PAN"},
        "gisDownloadBehaviour": "USE_EXISTING_DATA",
    }
    print("\n=== Body we're about to send (JSON) ===")
    print(json.dumps(body, indent=2)[:500])
    print("...")
    print(f"body size: {len(json.dumps(body))} bytes")

    # Make the call with explicit headers we can inspect
    url = "https://app.clear.in/api/data-pull/public/pull/v2/trigger"
    headers = {
        "x-request-id": "debug-trigger-001",
        "tenant": "GSTR2A_REPORTS",  # <-- the missing custom header from HAR
        **_node_headers(node_ids),
    }
    print("\n=== Per-request headers being added ===")
    for k, v in headers.items():
        print(f"  {k}: {v[:80]}{'...' if len(v) > 80 else ''}")

    print("\n=== Session headers ===")
    for k, v in api.session.headers.items():
        print(f"  {k}: {v[:80]}{'...' if len(v) > 80 else ''}")

    print("\n=== Sending request now ===")
    resp = api.session.post(url, json=body, headers=headers, timeout=30)
    print(f"\n=== Response: {resp.status_code} ===")
    print("response headers:", dict(resp.headers))
    print("response body:")
    print(resp.text[:2000])

    # As a sanity check, print what requests actually serialized
    print("\n=== What requests sent (req.body) ===")
    sent = resp.request.body
    if isinstance(sent, bytes):
        sent = sent.decode("utf-8", errors="replace")
    print(sent[:1000] if sent else "<empty>")
    print("\n=== Request headers actually sent ===")
    for k, v in resp.request.headers.items():
        if k.lower() == "cookie":
            print(f"  {k}: <{len(v)} chars>")
            continue
        print(f"  {k}: {v[:80]}{'...' if len(v) > 80 else ''}")


if __name__ == "__main__":
    main()
