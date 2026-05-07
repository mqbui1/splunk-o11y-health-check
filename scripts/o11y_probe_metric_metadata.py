#!/usr/bin/env python3
"""
Probe Splunk Observability Metrics Metadata API for ``lastUpdated`` semantics.

Endpoints (api.{realm}.signalfx.com):
  GET /v2/metric?query=...&limit=...&offset=...  — search metric names
  GET /v2/metric/{metricName}                     — one metric metadata object
  GET /v2/metrictimeseries?query=...&limit=... — search MTS
  GET /v2/metrictimeseries/{mtsId}               — one MTS metadata object

OpenAPI (signalfx-go) documents:
  - Metric.lastUpdated: "The time that the metric was last updated" (may include metadata edits)
  - MetricTimeSeries.lastUpdated: "The time that the MTS was last updated" (often aligned with ingest)

Usage (repo root):
  SPLUNK_ACCESS_TOKEN=... SPLUNK_REALM=us1 python3 scripts/o11y_probe_metric_metadata.py
  python3 scripts/o11y_probe_metric_metadata.py --profile customer-profile.yaml
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import load_customer_profile_scalars, resolve_profile_path  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

logger = logging.getLogger(__name__)


def api_get(token: str, realm: str, path: str, params: dict[str, str] | None = None) -> tuple[Any, str | None]:
    """GET JSON from api.{realm}.signalfx.com (read-only diagnostic)."""
    q = urllib.parse.urlencode(params or {}, doseq=True)
    url = f"https://api.{realm}.signalfx.com{path}"
    if q:
        url = f"{url}?{q}"
    req = urllib.request.Request(
        url,
        headers={"X-SF-Token": token, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {(e.read() or b'').decode('utf-8', errors='replace')[:800]}"
    except OSError as e:
        return None, str(e)
    if not raw.strip():
        return None, "empty body"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, str(e)


def ms_to_iso(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    try:
        x = float(ms)
        sec = x / 1000.0 if x > 1e12 else x
        return datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError):
        return str(ms)


def main() -> int:
    # Human-readable investigation tool: stdout is probe output; stderr is logging only.
    setup_script_logging(__name__, verbose=False)
    logger.info("Metric/MTS metadata probe starting")

    profile_path = resolve_profile_path(None)
    profile: dict[str, str] = {}
    if profile_path and Path(profile_path).is_file():
        profile = load_customer_profile_scalars(profile_path)

    token = (
        os.environ.get("SPLUNK_ACCESS_TOKEN")
        or profile.get("access_token")
        or profile.get("ACCESS_TOKEN")
        or ""
    ).strip()
    realm = (
        os.environ.get("SPLUNK_REALM", "").strip()
        or (profile.get("realm") or "").strip()
        or "us0"
    )

    if not token:
        logger.error("Set SPLUNK_ACCESS_TOKEN or access_token in customer-profile*.yaml")
        return 1

    logger.info("Realm: %s (human-readable tables follow on stdout)", realm)

    now_ms = int(time.time() * 1000)

    # 1) Search ~20 metrics (try broad query)
    queries_to_try = ("*", "sf.org.*", "")
    payload = None
    err = None
    for q in queries_to_try:
        params: dict[str, str] = {"limit": "20", "offset": "0"}
        if q:
            params["query"] = q
        payload, err = api_get(token, realm, "/v2/metric", params)
        if err is None and isinstance(payload, dict) and payload.get("results"):
            print(f"Search query used: {q!r}\n")
            break
        print(f"Search with query={q!r} failed: {err}", file=sys.stderr)

    if err or not isinstance(payload, dict):
        print(f"Could not list metrics: {err}", file=sys.stderr)
        return 1

    results = payload.get("results") or []
    if not results:
        print("No results from /v2/metric search", file=sys.stderr)
        return 1

    print("=" * 72)
    print("A) GET /v2/metric (search) — per-metric row fields (sample)")
    print("=" * 72)
    for i, row in enumerate(results[:20]):
        if not isinstance(row, dict):
            continue
        name = row.get("name") or "—"
        lu = row.get("lastUpdated")
        cr = row.get("created")
        age_h = (now_ms - int(lu)) / 3600000.0 if isinstance(lu, (int, float)) else None
        print(f"{i+1:2}. {name[:64]}")
        print(f"    lastUpdated: {lu} → {ms_to_iso(lu)}  (≈{age_h:.1f}h ago)" if age_h is not None else f"    lastUpdated: {lu}")
        print(f"    created:     {cr} → {ms_to_iso(cr)}")
        keys = sorted(row.keys())
        print(f"    keys: {keys[:12]}{'…' if len(keys) > 12 else ''}")

    print()
    print("=" * 72)
    print("B) GET /v2/metric/{name} — detail for first metric (URL-encoded name)")
    print("=" * 72)
    first = results[0]
    if isinstance(first, dict) and first.get("name"):
        mname = str(first["name"])
        enc = urllib.parse.quote(mname, safe="")
        detail, derr = api_get(token, realm, f"/v2/metric/{enc}", None)
        if derr:
            print(f"Error: {derr}")
        elif isinstance(detail, dict):
            print(json.dumps(detail, indent=2)[:4000])
            if len(json.dumps(detail)) > 4000:
                print("… (truncated)")
        else:
            print(detail)

    print()
    print("=" * 72)
    print("C) GET /v2/metrictimeseries — search for MTS of first metric (limit 5)")
    print("=" * 72)
    if isinstance(first, dict) and first.get("name"):
        mname = str(first["name"])
        mq = f'metric:"{mname}"'
        mts_payload, merr = api_get(
            token,
            realm,
            "/v2/metrictimeseries",
            {"query": mq, "limit": "5", "offset": "0"},
        )
        if merr:
            print(f"Error: {merr}")
        elif isinstance(mts_payload, dict):
            mts_results = mts_payload.get("results") or []
            print(f"query={mq!r} → {len(mts_results)} MTS row(s)\n")
            for j, mrow in enumerate(mts_results[:5]):
                if not isinstance(mrow, dict):
                    continue
                mid = mrow.get("id") or mrow.get("Id") or "—"
                lu = mrow.get("lastUpdated")
                cr = mrow.get("created")
                dim = mrow.get("dimensions")
                dim_s = ""
                if isinstance(dim, dict):
                    dim_s = str(list(dim.items())[:2])
                age_h = None
                if isinstance(lu, (int, float)) and lu > 0:
                    age_h = (now_ms - int(lu)) / 3600000.0
                print(f"  MTS {j+1}: id={mid}")
                lu_note = ""
                if lu == 0 or lu is None:
                    lu_note = "  ← list view may omit this; use GET /v2/metrictimeseries/{id}"
                if age_h is not None:
                    print(f"    lastUpdated: {lu} → {ms_to_iso(lu)}  (≈{age_h:.1f}h ago){lu_note}")
                else:
                    print(f"    lastUpdated: {lu} → {ms_to_iso(lu) if lu else '—'}{lu_note}")
                print(f"    created:     {cr} → {ms_to_iso(cr)}")
                print(f"    dimensions sample: {dim_s[:120]}")
                print(f"    keys: {sorted(mrow.keys())[:14]}…")

            # Optional: GET by id for first MTS
            if mts_results and isinstance(mts_results[0], dict):
                mid0 = mts_results[0].get("id")
                if mid0:
                    print()
                    print("D) GET /v2/metrictimeseries/{id} — first MTS detail")
                    d2, e2 = api_get(token, realm, f"/v2/metrictimeseries/{urllib.parse.quote(str(mid0), safe='')}", None)
                    if e2:
                        print(f"Error: {e2}")
                    elif isinstance(d2, dict):
                        print(json.dumps(d2, indent=2)[:3500])

    print()
    print("=" * 72)
    print("Interpretation (verify against your org):")
    print("- Metric.lastUpdated: API text says \"metric was last updated\" (catalog/metadata churn possible).")
    print("- MetricTimeSeries.lastUpdated (GET by id): per-MTS; use for staleness vs 36h threshold.")
    print("- MTS *search* results sometimes return lastUpdated=0; do not trust until GET /v2/metrictimeseries/{id}.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
