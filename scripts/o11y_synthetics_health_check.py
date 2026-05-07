#!/usr/bin/env python3
"""
Splunk Observability Cloud — Synthetics health check (read-only).

Uses REST on ``https://api.{realm}.signalfx.com/v2/synthetics`` (same base as
`splunk/syntheticsclient` v2: ``GET /v2/synthetics/tests``, ``GET /v2/synthetics/tests/{type}/{id}``;
**SignalFlow** on ``synthetics.run.count`` (failed vs total per test) for **Failing tests** (7-day rate);
aligned with ``Splunk-Observability-Health-Check.md`` **Synthetics Health Check**.

Detector linkage: scans ``programText`` from ``GET /v2/detector/{id}`` (capped) for synthetic test id
references (e.g. ``filter('test_id', …)``).

Environment: ``SPLUNK_ACCESS_TOKEN`` / profile ``access_token``; ``realm`` in profile or ``SPLUNK_REALM``.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import (  # noqa: E402
    aggregate_values,
    execute_signalflow_matrix,
    execute_signalflow_time_series,
    load_customer_profile_scalars,
    resolve_profile_path,
)
from o11y_script_logging import setup_script_logging  # noqa: E402

STRUCTURED_SCHEMA = "o11y_synthetics_health/v1"

logger = logging.getLogger(__name__)

# Same subscription metrics as ``License_utilizations.md`` / ``o11y_license_utilization.py`` (Synthetics).
SYNTH_SUB_BROWSER = "sf.org.synthetics.subscription.browser_tests"
SYNTH_SUB_API = "sf.org.synthetics.subscription.api_tests"
SYNTH_SUB_UPTIME = "sf.org.synthetics.subscription.uptime_tests"

# Average month length for extrapolation (matches prior ``runs_per_month`` heuristic).
_AVERAGE_MONTH_MINUTES = 30 * 24 * 60

SYNTHETICS_BASE = "/v2/synthetics"

# Checklist subsection titles — keep aligned with ``Splunk-Observability-Health-Check.md``.
SYNTHETICS_CHECKLIST: dict[str, dict[str, str]] = {
    "testUsage": {
        "title": "Test Usage Analysis",
        "description": "Detailed list of Synthetics tests.",
        "recommendation": (
            "Analyze the detailed test report to determine if each test is running at the correct frequency "
            "and using the right locations. This check is more for teams to have a good overview of the "
            "different tests running in their environment."
        ),
    },
    "failingTests": {
        "title": "Failing Tests",
        "description": (
            "Active tests that have a failure rate above 30% for the last 7 days. These may be tests that are "
            "running against old URLs, deprecated APIs, or retired services that for some reason are still enabled."
        ),
        "recommendation": "Update the test to point to the right URL/API, or delete/disable if not needed.",
    },
    "disabledTests": {
        "title": "Disabled Tests",
        "description": "List of tests that are disabled (not actively running).",
        "recommendation": "Review if the test is no longer needed; consider deleting.",
    },
    "similarTests": {
        "title": "Similar Tests",
        "description": (
            "List of tests that may be doing similar things, or could be duplicates (same or overlapping targets)."
        ),
        "recommendation": (
            "Review tests to see if they are redundant, can be merged, or should stay separate with clearer ownership."
        ),
    },
    "noDetectors": {
        "title": "Tests with No Detectors",
        "description": "List of tests that have no associated detector.",
        "recommendation": (
            "Create a new detector to ensure alerts are being generated for the test."
        ),
    },
}

_TEST_ID_RES = [
    re.compile(r"""filter\s*\(\s*['\"]test_id['\"]\s*,\s*['\"]?(\d+)""", re.I),
    re.compile(r"""filter\s*\(\s*['\"]testId['\"]\s*,\s*['\"]?(\d+)""", re.I),
    re.compile(r"""filter\s*\(\s*[^)]+['\"]synthetics\.run\.count['\"][^)]*['\"]test_id['\"]\s*,\s*['\"]?(\d+)""", re.I),
    # Legacy UI path (some detector deep links may still use this shape)
    re.compile(r"""#/synthetics/tests/[a-z]+/(\d+)""", re.I),
    re.compile(r"""/synthetics/tests/[a-z]+/(\d+)""", re.I),
    # Current UI: #/synthetics/tests/view/{type}/{id}/availability
    re.compile(r"""#/synthetics/tests/view/[a-z]+/(\d+)""", re.I),
    re.compile(r"""/synthetics/tests/view/[a-z]+/(\d+)""", re.I),
]


def api_base(realm: str) -> str:
    return f"https://api.{realm}.signalfx.com"


def app_base(realm: str) -> str:
    return f"https://app.{realm}.signalfx.com"


def synthetics_test_open_url(realm: str, test_type: str, test_id: int) -> str:
    """Splunk Observability UI URL for a synthetic test (opens in app).

    Uses the **Availability** view: ``#/synthetics/tests/view/{type}/{id}/availability``
    (browser, ``http``, ``api``, ``port``, etc.).
    """
    r = str(realm or "").strip()
    if not r or not test_id:
        return ""
    t = str(test_type or "browser").strip().lower()
    if t not in ("browser", "api", "http", "port"):
        t = "browser"
    tid = urllib.parse.quote(str(int(test_id)), safe="")
    return f"{app_base(r)}/#/synthetics/tests/view/{t}/{tid}/availability"


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _md_test_name_cell(name: str, realm: str, test_type: str, test_id: int) -> str:
    n = str(name or "—").replace("\n", " ")
    url = synthetics_test_open_url(realm, test_type, test_id) if realm and test_id else ""
    if not url:
        return _md_cell(n)
    esc_href = html.escape(url, quote=True)
    esc_text = html.escape(n, quote=False).replace("|", "&#124;")
    return f'<a href="{esc_href}" target="_blank" rel="noopener noreferrer">{esc_text}</a>'


def _md_similar_tests_list_cell(realm: str, row: dict[str, Any]) -> str:
    """Comma-separated linked test names for Similar Tests column (structured row or legacy string)."""
    dups = row.get("similarTests") or row.get("duplicateTests")
    if isinstance(dups, list) and dups:
        chunks: list[str] = []
        for d in dups:
            if not isinstance(d, dict):
                continue
            nm = str(d.get("testName") or "")
            tt = str(d.get("testType") or "")
            tid = int(d.get("testId") or 0)
            chunks.append(_md_test_name_cell(nm, realm, tt, tid))
        omitted = int(row.get("similarTestsOmitted") or row.get("duplicateTestsOmitted") or 0)
        if omitted > 0:
            chunks.append(_md_cell(f"+{omitted} more"))
        return ", ".join(chunks) if chunks else "—"
    legacy = row.get("duplicateTestNames")
    if legacy is not None and str(legacy).strip():
        return _md_cell(str(legacy))
    return "—"


def api_get(token: str, realm: str, path: str, params: dict[str, Any] | None = None) -> tuple[Any | None, str | None]:
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
    url = f"{api_base(realm).rstrip('/')}{path}"
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
        err = (e.read() or b"").decode("utf-8", errors="replace")
        return None, f"HTTP {e.code} {path}: {err[:1500]}"
    except OSError as e:
        return None, str(e)
    if not raw.strip():
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON {path}: {e}"


def pick(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def normalize_list_test(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one test object from GET /tests (snake_case or camelCase)."""
    tid = pick(row, "id", "ID")
    try:
        iid = int(tid) if tid is not None else 0
    except (TypeError, ValueError):
        iid = 0
    active = pick(row, "active", "Active")
    if not isinstance(active, bool):
        active = True
    freq = pick(row, "frequency", "Frequency")
    try:
        fq = int(freq) if freq is not None else 0
    except (TypeError, ValueError):
        fq = 0
    loc = pick(row, "location_ids", "locationIds") or []
    if not isinstance(loc, list):
        loc = []
    strat = str(pick(row, "scheduling_strategy", "schedulingStrategy") or "").strip()
    lrs = str(pick(row, "last_run_status", "lastRunStatus") or "").strip()
    lra = pick(row, "last_run_at", "lastRunAt")
    lra_s = ""
    if isinstance(lra, str):
        lra_s = lra
    elif isinstance(lra, (int, float)) and lra:
        lra_s = str(lra)
    return {
        "id": iid,
        "name": str(pick(row, "name", "Name") or "").strip() or f"test-{iid}",
        "type": str(pick(row, "type", "Type") or "").strip().lower() or "http",
        "active": active,
        "frequencyMinutes": fq,
        "locationIds": [str(x) for x in loc if x is not None],
        "schedulingStrategy": strat,
        "lastRunStatus": lrs,
        "lastRunAt": lra_s,
    }


def list_all_tests(
    token: str,
    realm: str,
    *,
    per_page: int,
    max_tests: int,
    sleep_s: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """Paginate GET /v2/synthetics/tests (see splunk/syntheticsclient get_checksv2)."""
    out: list[dict[str, Any]] = []
    page = 1
    total_count: int | None = None
    while len(out) < max_tests:
        params = {
            "testType": "",
            "page": str(page),
            "perPage": str(per_page),
            "orderBy": "id",
            "search": "",
        }
        payload, err = api_get(token, realm, f"{SYNTHETICS_BASE}/tests", params)
        if err:
            return [], err
        if not isinstance(payload, dict):
            return [], "unexpected /v2/synthetics/tests response (not an object)"
        raw_tests = pick(payload, "tests", "Tests")
        if not isinstance(raw_tests, list):
            return [], "unexpected /v2/synthetics/tests: missing tests[]"
        for r in raw_tests:
            if isinstance(r, dict):
                out.append(normalize_list_test(r))
        tc = pick(payload, "total_count", "totalCount")
        if isinstance(tc, int):
            total_count = tc
        if len(raw_tests) < per_page:
            break
        if total_count is not None and len(out) >= total_count:
            break
        npl = pick(payload, "next_page_link", "nextPageLink")
        if npl is None or npl == "" or npl == 0:
            if len(raw_tests) == 0:
                break
        page += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
        if page > 5000:
            break
    return out[:max_tests], None


def is_round_robin_scheduling(scheduling_strategy: str, num_locations: int) -> bool:
    """True when scheduling runs one location per interval (random location), not all locations each tick."""
    s = (scheduling_strategy or "").strip().lower()
    n = max(0, int(num_locations))
    if not s:
        # Unknown strategy: single-location tests behave like one run per tick; multi-location like parallel.
        return n <= 1
    return "round" in s


def runs_per_interval_multiplier(*, scheduling_strategy: str, num_locations: int) -> int:
    """
    Runs executed per frequency tick for this test.

    - **Round-robin:** one run per tick (one location chosen per execution).
    - **Not round-robin:** one run per location per tick (all locations each cycle).
    """
    n = max(0, int(num_locations))
    if is_round_robin_scheduling(scheduling_strategy, n):
        return 1
    return max(1, n)


def extrapolate_runs_per_month(
    *,
    frequency_minutes: int,
    scheduling_strategy: str,
    num_locations: int,
    active: bool,
) -> int:
    """Estimated runs in an average 30-day month (disabled tests → 0)."""
    if not active or frequency_minutes <= 0:
        return 0
    intervals = _AVERAGE_MONTH_MINUTES / float(frequency_minutes)
    mult = runs_per_interval_multiplier(scheduling_strategy=scheduling_strategy, num_locations=num_locations)
    return int(intervals * float(mult))


def subscription_key_for_test_type(test_type: str) -> str | None:
    """Maps API test ``type`` to ``fetch_synthetics_subscription_allowances`` keys."""
    t = (test_type or "").strip().lower()
    if t == "browser":
        return "browser_tests"
    if t == "api":
        return "api_tests"
    if t in ("http", "port"):
        return "uptime_tests"
    return None


def fetch_synthetics_subscription_allowances(
    token: str,
    realm: str,
    *,
    lookback_days: int,
    resolution_hours: int,
) -> tuple[dict[str, float | None], str | None]:
    """
    Mean monthly run **allowance** per Synthetics pool from org subscription metrics (SignalFlow).

    Returns keys ``browser_tests``, ``api_tests``, ``uptime_tests`` — values may be ``None`` if the query failed.
    """
    stream_url = f"https://stream.{realm}.signalfx.com"
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - max(1, lookback_days) * 24 * 3600 * 1000
    resolution_ms = max(3_600_000, max(1, resolution_hours) * 3600 * 1000)
    metrics = {
        "browser_tests": SYNTH_SUB_BROWSER,
        "api_tests": SYNTH_SUB_API,
        "uptime_tests": SYNTH_SUB_UPTIME,
    }
    out: dict[str, float | None] = {k: None for k in metrics}
    errs: list[str] = []
    for key, mname in metrics.items():
        prog = f"data('{mname}').mean().publish(label='s')"
        pts, err = execute_signalflow_time_series(
            stream_url=stream_url,
            token=token,
            program=prog,
            start_ms=start_ms,
            stop_ms=now_ms,
            resolution_ms=resolution_ms,
            wall_seconds=90.0,
            read_timeout=75.0,
        )
        if err:
            errs.append(f"{key}:{err}")
            continue
        agg = aggregate_values([v for _, v in pts])
        if agg is not None:
            out[key] = float(agg)
    return out, ("; ".join(errs) if errs else None)


def license_utilization_pct(
    estimated_monthly_runs: float,
    subscription_allowance: float | None,
) -> float | None:
    """Approximate % of monthly run allowance for this test's pool."""
    if subscription_allowance is None:
        return None
    try:
        cap = float(subscription_allowance)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    return round(100.0 * float(estimated_monthly_runs) / cap, 2)


def _sum_datapoints_per_tsid(data_points: list[dict[str, Any]]) -> dict[str, float]:
    acc: dict[str, float] = {}
    for dp in data_points:
        tid = dp.get("tsId")
        if not tid:
            continue
        acc[str(tid)] = acc.get(str(tid), 0.0) + float(dp["value"])
    return acc


def _test_key_from_metric_props(props: dict[str, Any]) -> tuple[str, str] | None:
    test = props.get("test") or props.get("sf_test") or props.get("synthetics_test")
    if test is None:
        return None
    tt = props.get("test_type") or props.get("testType") or ""
    return (str(test).strip(), str(tt).strip().lower())


def _rollup_run_counts_by_test(
    metadata: dict[str, dict[str, Any]],
    per_tsid: dict[str, float],
) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = defaultdict(float)
    for tsid, val in per_tsid.items():
        props = metadata.get(tsid) or {}
        key = _test_key_from_metric_props(props)
        if key is None:
            continue
        out[key] += val
    return dict(out)


def _lookup_metric_failed_total(
    failed_map: dict[tuple[str, str], float],
    total_map: dict[tuple[str, str], float],
    test_name: str,
    test_type: str,
) -> tuple[float | None, float | None]:
    """Match (test name, test_type) to metric rollup keys; allow single-name fallback."""
    name = str(test_name or "").strip()
    tt = str(test_type or "").strip().lower()
    k = (name, tt)
    f = failed_map.get(k)
    t = total_map.get(k)
    if f is not None or t is not None:
        return f, t
    # Uptime: API uses http or port; metric may tag either
    if tt in ("http", "port"):
        for alt in ("http", "port"):
            if alt == tt:
                continue
            k2 = (name, alt)
            f2 = failed_map.get(k2)
            t2 = total_map.get(k2)
            if f2 is not None or t2 is not None:
                return f2, t2
    keys = {x for x in failed_map.keys() if x[0] == name} | {x for x in total_map.keys() if x[0] == name}
    if len(keys) == 1:
        u = next(iter(keys))
        return failed_map.get(u), total_map.get(u)
    return None, None


def fetch_synthetics_run_count_metrics(
    token: str,
    realm: str,
    *,
    lookback_hours: int,
    resolution_ms: int,
) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], float], str | None]:
    """
    Sum ``synthetics.run.count`` over the wall-clock window from ``synthetics.run.count`` MTS,
    grouped by ``test`` + ``test_type`` when supported. Failed counts use ``filter('failed', 'true')``
    when the dimension exists; otherwise **failed ≈ total − success** with ``filter('failed', 'false')``.
    """
    stream_url = f"https://stream.{realm}.signalfx.com"
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - max(1, int(lookback_hours)) * 3600 * 1000
    res = max(60_000, int(resolution_ms))

    total_programs = [
        "data('synthetics.run.count').sum(by=['test', 'test_type']).publish(label='total')",
        "data('synthetics.run.count').sum(by=['test']).publish(label='total')",
    ]
    failed_direct_programs = [
        (
            "data('synthetics.run.count', filter=filter('failed', 'true'))"
            ".sum(by=['test', 'test_type']).publish(label='failed')"
        ),
        (
            "data('synthetics.run.count', filter=filter('failed', 'true'))"
            ".sum(by=['test']).publish(label='failed')"
        ),
    ]
    success_programs = [
        (
            "data('synthetics.run.count', filter=filter('failed', 'false'))"
            ".sum(by=['test', 'test_type']).publish(label='ok')"
        ),
        (
            "data('synthetics.run.count', filter=filter('failed', 'false'))"
            ".sum(by=['test']).publish(label='ok')"
        ),
    ]

    errs: list[str] = []

    def run_program(program: str) -> tuple[dict[tuple[str, str], float], str | None]:
        meta, dps, err, _sr = execute_signalflow_matrix(
            stream_url=stream_url,
            token=token,
            program=program,
            start_ms=start_ms,
            stop_ms=now_ms,
            resolution_ms=res,
            wall_seconds=150.0,
            read_timeout=120.0,
            max_data_points=200_000,
        )
        if err:
            return {}, err
        per_ts = _sum_datapoints_per_tsid(dps)
        return _rollup_run_counts_by_test(meta, per_ts), None

    total_map: dict[tuple[str, str], float] = {}
    for prog in total_programs:
        m, terr = run_program(prog)
        if terr:
            errs.append(f"total:{prog[:80]}:{terr}")
            continue
        total_map = m
        break

    failed_map: dict[tuple[str, str], float] = {}
    failed_direct_ok = False
    for prog in failed_direct_programs:
        m, ferr = run_program(prog)
        if ferr:
            errs.append(f"failed:{prog[:80]}:{ferr}")
            continue
        failed_map = m
        failed_direct_ok = True
        break

    if not failed_direct_ok and total_map:
        success_map: dict[tuple[str, str], float] = {}
        for prog in success_programs:
            m, serr = run_program(prog)
            if serr:
                errs.append(f"success:{prog[:80]}:{serr}")
                continue
            success_map = m
            break
        all_keys = set(total_map.keys()) | set(success_map.keys())
        for key in all_keys:
            tot = float(total_map.get(key, 0.0))
            ok = float(success_map.get(key, 0.0))
            diff = tot - ok
            if diff > 0:
                failed_map[key] = diff

    summary_err = "; ".join(errs) if errs else None
    return failed_map, total_map, summary_err


def format_last_run_date(iso_or_ms: str) -> str:
    if not iso_or_ms:
        return "—"
    # ISO timestamps from API
    if "T" in iso_or_ms[:24]:
        return iso_or_ms.split("T")[0]
    return iso_or_ms[:10] if len(iso_or_ms) >= 10 else iso_or_ms


_MISSING_LAST_RUN_SORT_MS = 2**62


def last_run_at_sort_ms(raw: Any) -> int:
    """
    Epoch milliseconds for sorting disabled tests (oldest first).
    Missing or invalid → very large so those rows sort last.
    """
    if raw is None:
        return _MISSING_LAST_RUN_SORT_MS
    s = str(raw).strip()
    if not s:
        return _MISSING_LAST_RUN_SORT_MS
    if s.isdigit():
        n = int(s)
        if n < 10**11:
            n *= 1000
        return n
    try:
        ts = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return _MISSING_LAST_RUN_SORT_MS


def fetch_test_detail(
    token: str, realm: str, test_type: str, test_id: int
) -> tuple[dict[str, Any] | None, str | None]:
    t = test_type.strip().lower()
    if t == "browser":
        path = f"{SYNTHETICS_BASE}/tests/browser/{test_id}"
    elif t == "api":
        path = f"{SYNTHETICS_BASE}/tests/api/{test_id}"
    elif t == "http":
        path = f"{SYNTHETICS_BASE}/tests/http/{test_id}"
    elif t == "port":
        path = f"{SYNTHETICS_BASE}/tests/port/{test_id}"
    else:
        return None, None
    payload, err = api_get(token, realm, path, None)
    if err:
        return None, err
    if not isinstance(payload, dict):
        return None, None
    inner = payload.get("test")
    if isinstance(inner, dict):
        return inner, None
    return payload, None


def fingerprint_target(test_type: str, detail: dict[str, Any]) -> str:
    """Best-effort target key for duplicate detection."""
    tt = test_type.lower()
    url = pick(detail, "url", "URL")
    if isinstance(url, str) and url.strip():
        return f"{tt}|{url.strip().lower()}"
    su = pick(detail, "startUrl", "start_url", "starturl")
    if isinstance(su, str) and su.strip():
        return f"{tt}|{su.strip().lower()}"
    host = pick(detail, "host", "Host")
    port = pick(detail, "port", "Port")
    if host:
        return f"{tt}|{str(host).lower()}:{port!s}"
    reqs = pick(detail, "requests", "Requests")
    if isinstance(reqs, list) and reqs and isinstance(reqs[0], dict):
        cfg = reqs[0].get("configuration") or reqs[0].get("Configuration")
        if isinstance(cfg, dict):
            u = cfg.get("url") or cfg.get("URL")
            if isinstance(u, str) and u.strip():
                return f"{tt}|{u.strip().lower()}"
    return ""


def paginate_detectors(token: str, realm: str, *, page_size: int) -> tuple[list[dict[str, Any]], str | None]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload, err = api_get(
            token,
            realm,
            "/v2/detector",
            {"limit": str(page_size), "offset": str(offset)},
        )
        if err:
            return [], err
        if not isinstance(payload, dict):
            return [], "unexpected detector list"
        chunk = payload.get("results")
        if not isinstance(chunk, list):
            return [], "detector list missing results"
        rows = [x for x in chunk if isinstance(x, dict)]
        out.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return out, None


def extract_program_text(d: dict[str, Any]) -> str:
    return str(d.get("programText") or d.get("program_text") or "")


def extract_synthetics_test_ids_from_blob(blob: str) -> set[int]:
    found: set[int] = set()
    for rx in _TEST_ID_RES:
        for m in rx.finditer(blob or ""):
            try:
                found.add(int(m.group(1)))
            except (TypeError, ValueError):
                continue
    return found


def collect_detector_referenced_test_ids(
    token: str,
    realm: str,
    *,
    max_detectors: int,
    sleep_s: float,
) -> tuple[set[int], str | None]:
    rows, err = paginate_detectors(token, realm, page_size=100)
    if err:
        return set(), err
    rows.sort(key=lambda r: str(r.get("name") or r.get("id") or ""))
    ids_found: set[int] = set()
    n = 0
    for r in rows:
        if n >= max_detectors:
            break
        did = str(r.get("id") or "").strip()
        if not did:
            continue
        detail, gerr = api_get(token, realm, f"/v2/detector/{urllib.parse.quote(did, safe='')}", None)
        if gerr or not isinstance(detail, dict):
            detail = r
        blob = extract_program_text(detail)
        blob += "\n" + json.dumps(detail)[:8000]
        ids_found |= extract_synthetics_test_ids_from_blob(blob)
        n += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
    return ids_found, None


def _subsection_markdown(checklist_key: str, block: dict[str, Any], table_lines: list[str]) -> list[str]:
    c = SYNTHETICS_CHECKLIST[checklist_key]
    desc = str(block.get("description") or c["description"]).strip()
    rec = str(block.get("recommendation") or c["recommendation"]).strip()
    out: list[str] = [
        f"### {c['title']}\n",
        "",
        desc,
        "",
        "### Results",
        "",
        *table_lines,
        "",
        "### Recommendation",
        "",
        rec,
        "",
    ]
    return out


@dataclass
class SyntheticsHealthConfig:
    realm: str
    max_tests: int
    per_page: int
    max_url_detail_for_dupes: int
    max_detectors_scan: int
    sleep_s: float
    fetch_subscription_allowances: bool = True
    subscription_lookback_days: int = 7
    subscription_resolution_hours: int = 6
    fetch_failure_metrics: bool = True
    failure_metrics_lookback_hours: int = 7 * 24
    failure_metrics_resolution_minutes: int = 60
    failing_rate_threshold_pct: float = 30.0


def _test_usage_row_sort_key(r: dict[str, Any]) -> tuple[str, float]:
    """Sort test usage rows: **Test Type** (A→Z), then **Total Runs per Month** (high→low)."""
    tt = str(r.get("testType") or "").lower()
    try:
        rpm = float(r.get("runsPerMonth") if r.get("runsPerMonth") is not None else 0)
    except (TypeError, ValueError):
        rpm = 0.0
    return (tt, -rpm)


def run_synthetics_health(token: str, cfg: SyntheticsHealthConfig) -> dict[str, Any]:
    tests, terr = list_all_tests(
        token,
        cfg.realm,
        per_page=cfg.per_page,
        max_tests=cfg.max_tests,
        sleep_s=cfg.sleep_s,
    )
    if terr:
        return {"schema": STRUCTURED_SCHEMA, "realm": cfg.realm, "error": terr, "checks": {}}

    total_listed = len(tests)

    subscription_allowances: dict[str, float | None] = {
        "browser_tests": None,
        "api_tests": None,
        "uptime_tests": None,
    }
    subscription_fetch_error: str | None = None
    if cfg.fetch_subscription_allowances:
        subscription_allowances, subscription_fetch_error = fetch_synthetics_subscription_allowances(
            token,
            cfg.realm,
            lookback_days=max(1, cfg.subscription_lookback_days),
            resolution_hours=max(1, cfg.subscription_resolution_hours),
        )

    usage_rows: list[dict[str, Any]] = []
    failing_rows: list[dict[str, Any]] = []
    disabled_rows: list[dict[str, Any]] = []
    dup_rows: list[dict[str, Any]] = []
    no_det_rows: list[dict[str, Any]] = []

    # --- Usage table (all tests)
    for t in tests:
        tid = int(t["id"])
        nloc = len(t.get("locationIds") or [])
        strat = str(t.get("schedulingStrategy") or "")
        active = bool(t.get("active", True))
        est_runs = extrapolate_runs_per_month(
            frequency_minutes=int(t["frequencyMinutes"]),
            scheduling_strategy=strat,
            num_locations=nloc,
            active=active,
        )
        sub_key = subscription_key_for_test_type(str(t.get("type") or ""))
        cap = subscription_allowances.get(sub_key) if sub_key else None
        lic_pct = license_utilization_pct(float(est_runs), cap) if sub_key else None
        usage_rows.append(
            {
                "testName": t["name"],
                "testType": t["type"],
                "frequency": t["frequencyMinutes"],
                "numLocations": nloc,
                "roundRobin": "Y" if is_round_robin_scheduling(strat, nloc) else "N",
                "runsPerMonth": est_runs,
                "licenseUtilizationPct": lic_pct,
                "testId": tid,
                "active": active,
                "subscriptionPool": sub_key,
            }
        )

    usage_rows.sort(key=_test_usage_row_sort_key)

    # --- Failing tests: synthetics.run.count over lookback (default 7d); active tests with failure rate > threshold
    failed_metrics_map: dict[tuple[str, str], float] = {}
    total_metrics_map: dict[tuple[str, str], float] = {}
    metrics_err: str | None = None
    if cfg.fetch_failure_metrics:
        failed_metrics_map, total_metrics_map, metrics_err = fetch_synthetics_run_count_metrics(
            token,
            cfg.realm,
            lookback_hours=max(1, min(cfg.failure_metrics_lookback_hours, 168)),
            resolution_ms=max(60_000, cfg.failure_metrics_resolution_minutes * 60_000),
        )

    thr = float(cfg.failing_rate_threshold_pct)
    for t in tests:
        if not t.get("active", True):
            continue
        tid = int(t["id"])
        tt = str(t.get("type") or "")
        nm = str(t.get("name") or "")
        mf, mt = _lookup_metric_failed_total(failed_metrics_map, total_metrics_map, nm, tt)
        if mt is None or float(mt) <= 0:
            continue
        mf_f = float(mf) if mf is not None else 0.0
        rate = 100.0 * mf_f / float(mt)
        if rate > thr:
            failing_rows.append(
                {
                    "color": "Red",
                    "testName": t["name"],
                    "testType": tt,
                    "frequency": t["frequencyMinutes"],
                    "testId": tid,
                    "metricLookbackHours": cfg.failure_metrics_lookback_hours,
                    "metricFailedRuns": round(mf_f, 2),
                    "metricTotalRuns": round(float(mt), 2),
                    "metricFailureRatePct": round(rate, 2),
                }
            )

    failing_rows.sort(key=lambda r: float(r.get("metricFailureRatePct") or 0.0), reverse=True)

    # --- Disabled
    for t in tests:
        if t.get("active", True):
            continue
        disabled_rows.append(
            {
                "color": "Yellow",
                "testName": t["name"],
                "testType": t["type"],
                "lastRunDate": format_last_run_date(str(t.get("lastRunAt") or "")),
                "lastRunAtSortMs": last_run_at_sort_ms(t.get("lastRunAt")),
                "testId": int(t["id"]),
            }
        )

    disabled_rows.sort(
        key=lambda r: (
            int(r.get("lastRunAtSortMs") or _MISSING_LAST_RUN_SORT_MS),
            str(r.get("testName") or "").lower(),
        )
    )

    # --- Duplicates: URL fingerprint when detail fetch succeeds; fallback name+type
    fp_map: dict[str, list[dict[str, Any]]] = {}
    detail_cap = max(0, cfg.max_url_detail_for_dupes)
    for i, t in enumerate(tests):
        tt = str(t.get("type") or "").lower()
        fp = ""
        if i < detail_cap:
            dres, _ = fetch_test_detail(token, cfg.realm, tt, int(t["id"]))
            if cfg.sleep_s > 0:
                time.sleep(cfg.sleep_s)
            if isinstance(dres, dict):
                fp = fingerprint_target(tt, dres)
        if not fp:
            fp = f"name|{tt}|{t.get('name', '').strip().lower()}"
        fp_map.setdefault(fp, []).append(t)

    for fp, group in fp_map.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: int(x["id"]))
        canonical = group[0]
        dup_rest = group[1:]
        dup_entries = [
            {"testName": g["name"], "testType": str(g.get("type") or ""), "testId": int(g["id"])}
            for g in dup_rest[:20]
        ]
        dup_rows.append(
            {
                "color": "Yellow",
                "testName": canonical["name"],
                "testType": canonical["type"],
                "testId": int(canonical["id"]),
                "similarTests": dup_entries,
                "similarTestsOmitted": max(0, len(dup_rest) - 20),
            }
        )

    # --- Tests with no detectors
    ref_ids, _ = collect_detector_referenced_test_ids(
        token,
        cfg.realm,
        max_detectors=cfg.max_detectors_scan,
        sleep_s=cfg.sleep_s,
    )
    for t in tests:
        if not t.get("active", True):
            continue
        tid = int(t["id"])
        if tid not in ref_ids:
            no_det_rows.append(
                {
                    "color": "Red",
                    "testName": t["name"],
                    "testType": t["type"],
                    "frequency": t["frequencyMinutes"],
                    "testId": tid,
                }
            )

    checks = {
        "testUsage": {
            "rows": usage_rows[:5000],
            "description": SYNTHETICS_CHECKLIST["testUsage"]["description"],
            "recommendation": SYNTHETICS_CHECKLIST["testUsage"]["recommendation"],
        },
        "failingTests": {
            "rows": failing_rows[:500],
            "description": SYNTHETICS_CHECKLIST["failingTests"]["description"],
            "recommendation": SYNTHETICS_CHECKLIST["failingTests"]["recommendation"],
        },
        "disabledTests": {
            "rows": disabled_rows[:500],
            "description": SYNTHETICS_CHECKLIST["disabledTests"]["description"],
            "recommendation": SYNTHETICS_CHECKLIST["disabledTests"]["recommendation"],
        },
        "similarTests": {
            "rows": dup_rows[:500],
            "description": SYNTHETICS_CHECKLIST["similarTests"]["description"],
            "recommendation": SYNTHETICS_CHECKLIST["similarTests"]["recommendation"],
        },
        "noDetectors": {
            "rows": no_det_rows[:500],
            "description": SYNTHETICS_CHECKLIST["noDetectors"]["description"],
            "recommendation": SYNTHETICS_CHECKLIST["noDetectors"]["recommendation"],
        },
    }

    return {
        "schema": STRUCTURED_SCHEMA,
        "realm": cfg.realm,
        "testListTotal": total_listed,
        "testsAnalyzed": total_listed,
        "maxTestsCap": cfg.max_tests,
        "syntheticsSubscriptionAllowances": subscription_allowances,
        "syntheticsSubscriptionMetrics": {
            "browser_tests": SYNTH_SUB_BROWSER,
            "api_tests": SYNTH_SUB_API,
            "uptime_tests": SYNTH_SUB_UPTIME,
        },
        "syntheticsSubscriptionFetchError": subscription_fetch_error,
        "syntheticsFailureMetricsLookbackHours": (
            cfg.failure_metrics_lookback_hours if cfg.fetch_failure_metrics else None
        ),
        "syntheticsFailingRateThresholdPct": cfg.failing_rate_threshold_pct,
        "syntheticsFailureMetricsError": metrics_err,
        "checks": checks,
    }


def render_synthetics_checks_markdown(report: dict[str, Any] | None) -> str:
    """Full ``## Synthetics health check`` section."""
    if not report or report.get("error"):
        return _synthetics_placeholder_markdown(str(report.get("error") or "") if report else "")

    realm = str(report.get("realm") or "")
    chk = report.get("checks") or {}

    parts: list[str] = ["## Synthetics health check\n"]

    # Test usage
    tu = chk.get("testUsage") or {}
    tu_rows = sorted(tu.get("rows") or [], key=_test_usage_row_sort_key)
    tu_lines = [
        "| Test Name | Test Type | Frequency | # Locations | Round Robin (Y/N) | Total Runs per Month | % Utilization of License |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in tu_rows:
        tid = int(r.get("testId") or 0)
        tname = _md_test_name_cell(str(r.get("testName") or "—"), realm, str(r.get("testType") or ""), tid)
        lic = r.get("licenseUtilizationPct")
        if lic is None:
            lic_s = "—"
        else:
            try:
                lic_s = f"{float(lic):.2f}%"
            except (TypeError, ValueError):
                lic_s = "—"
        tu_lines.append(
            f"| {tname} | {_md_cell(str(r.get('testType') or ''))} | {_md_cell(str(r.get('frequency') or ''))} | "
            f"{_md_cell(str(r.get('numLocations') or ''))} | {_md_cell(str(r.get('roundRobin') or ''))} | "
            f"{_md_cell(str(r.get('runsPerMonth') or ''))} | {_md_cell(lic_s)} |"
        )
    if not tu_rows:
        tu_lines.append("|  |  |  |  |  |  |  |")
    parts.extend(_subsection_markdown("testUsage", tu, tu_lines))

    # Failing (sorted by failure rate descending)
    ft = chk.get("failingTests") or {}
    ft_rows = sorted(
        ft.get("rows") or [],
        key=lambda r: float(r.get("metricFailureRatePct") or 0.0),
        reverse=True,
    )
    ft_lines = [
        "| Color | Test Name | Test Type | Frequency | Failure Rate % (7D) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in ft_rows:
        tid = int(r.get("testId") or 0)
        nm = _md_test_name_cell(str(r.get("testName") or ""), realm, str(r.get("testType") or ""), tid)
        rate = r.get("metricFailureRatePct")
        if rate is None:
            rate_s = "—"
        else:
            try:
                rate_s = f"{float(rate):.2f}%"
            except (TypeError, ValueError):
                rate_s = "—"
        ft_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {nm} | {_md_cell(str(r.get('testType')))} | "
            f"{_md_cell(str(r.get('frequency')))} | {_md_cell(rate_s)} |"
        )
    if not ft_rows:
        ft_lines.append("|  |  |  |  |  |")
    parts.extend(_subsection_markdown("failingTests", ft, ft_lines))

    # Disabled (oldest last run first; tie-break test name)
    dt = chk.get("disabledTests") or {}

    def _disabled_row_sort_key(r: Any) -> tuple[int, str]:
        if not isinstance(r, dict):
            return (_MISSING_LAST_RUN_SORT_MS, "")
        ms = r.get("lastRunAtSortMs")
        if isinstance(ms, (int, float)):
            return (int(ms), str(r.get("testName") or "").lower())
        ld = str(r.get("lastRunDate") or "").strip()
        if ld and ld not in ("—", "-") and len(ld) >= 10:
            try:
                d0 = datetime.strptime(ld[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                return (int(d0.timestamp() * 1000), str(r.get("testName") or "").lower())
            except ValueError:
                pass
        return (_MISSING_LAST_RUN_SORT_MS, str(r.get("testName") or "").lower())

    dt_rows = sorted(dt.get("rows") or [], key=_disabled_row_sort_key)
    dt_lines = ["| Color | Test Name | Test Type | Last Run Date |", "| --- | --- | --- | --- |"]
    for r in dt_rows:
        tid = int(r.get("testId") or 0)
        nm = _md_test_name_cell(str(r.get("testName") or ""), realm, str(r.get("testType") or ""), tid)
        dt_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {nm} | {_md_cell(str(r.get('testType')))} | "
            f"{_md_cell(str(r.get('lastRunDate')))} |"
        )
    if not dt_rows:
        dt_lines.append("|  |  |  |  |")
    parts.extend(_subsection_markdown("disabledTests", dt, dt_lines))

    # Duplicates
    dp = chk.get("similarTests") or chk.get("duplicateTests") or {}
    dp_rows = sorted(dp.get("rows") or [], key=lambda r: str(r.get("testName") or ""))
    dp_lines = ["| Color | Test Name | Test Type | List of Similar Test Names |", "| --- | --- | --- | --- |"]
    for r in dp_rows:
        tid = int(r.get("testId") or 0)
        nm = _md_test_name_cell(str(r.get("testName") or ""), realm, str(r.get("testType") or ""), tid)
        dup_cell = _md_similar_tests_list_cell(realm, r)
        dp_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {nm} | {_md_cell(str(r.get('testType')))} | {dup_cell} |"
        )
    if not dp_rows:
        dp_lines.append("|  |  |  |  |")
    parts.extend(_subsection_markdown("similarTests", dp, dp_lines))

    # No detectors
    nd = chk.get("noDetectors") or {}
    nd_rows = sorted(nd.get("rows") or [], key=lambda r: str(r.get("testName") or ""))
    nd_lines = ["| Color | Test Name | Test Type | Frequency |", "| --- | --- | --- | --- |"]
    for r in nd_rows:
        tid = int(r.get("testId") or 0)
        nm = _md_test_name_cell(str(r.get("testName") or ""), realm, str(r.get("testType") or ""), tid)
        nd_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {nm} | {_md_cell(str(r.get('testType')))} | "
            f"{_md_cell(str(r.get('frequency')))} |"
        )
    if not nd_rows:
        nd_lines.append("|  |  |  |  |")
    parts.extend(_subsection_markdown("noDetectors", nd, nd_lines))

    return "\n".join(parts)


def _synthetics_placeholder_markdown(err_hint: str) -> str:
    msg = "*None — check not executed.*"
    if err_hint.strip():
        msg = f"*Synthetics automation failed ({_md_cell(err_hint[:200])}).*"
    empty7 = "|  |  |  |  |  |  |  |"
    empty4 = "|  |  |  |  |"
    empty5 = "|  |  |  |  |  |"
    layout: list[tuple[str, list[str]]] = [
        (
            "testUsage",
            [
                "| Test Name | Test Type | Frequency | # Locations | Round Robin (Y/N) | Total Runs per Month | % Utilization of License |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                empty7,
            ],
        ),
        (
            "failingTests",
            [
                "| Color | Test Name | Test Type | Frequency | Failure Rate % (7D) |",
                "| --- | --- | --- | --- | --- |",
                empty5,
            ],
        ),
        ("disabledTests", ["| Color | Test Name | Test Type | Last Run Date |", "| --- | --- | --- | --- |", "|  |  |  |  |"]),
        (
            "similarTests",
            ["| Color | Test Name | Test Type | List of Similar Test Names |", "| --- | --- | --- | --- |", "|  |  |  |  |"],
        ),
        ("noDetectors", ["| Color | Test Name | Test Type | Frequency |", "| --- | --- | --- | --- |", empty4]),
    ]
    parts: list[str] = ["## Synthetics health check\n"]
    for key, lines in layout:
        c = SYNTHETICS_CHECKLIST[key]
        block = {"description": c["description"], "recommendation": msg}
        parts.extend(_subsection_markdown(key, block, lines))
    return "\n".join(parts)


def main() -> int:
    p = argparse.ArgumentParser(description="Synthetics health check (Splunk Observability API).")
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--max-tests", type=int, default=2000, help="Max tests to list (default 2000).")
    p.add_argument("--per-page", type=int, default=50, help="Page size for GET /tests (default 50).")
    p.add_argument(
        "--max-detail-for-dupes",
        type=int,
        default=150,
        help="Max tests to fetch detail for similar-target detection (default 150).",
    )
    p.add_argument(
        "--max-detectors-scan",
        type=int,
        default=300,
        help="Max detectors to load program text from for test id extraction (default 300).",
    )
    p.add_argument("--sleep", type=float, default=0.05, help="Seconds between API calls (default 0.05).")
    p.add_argument(
        "--skip-subscription-signalflow",
        action="store_true",
        help="Do not query sf.org.synthetics.subscription.* via SignalFlow (omit %% utilization column data).",
    )
    p.add_argument(
        "--subscription-lookback-days",
        type=int,
        default=7,
        help="SignalFlow window for subscription metric means (default 7).",
    )
    p.add_argument(
        "--subscription-resolution-hours",
        type=int,
        default=6,
        help="SignalFlow resolution for subscription series in hours (default 6).",
    )
    p.add_argument(
        "--skip-failure-metrics",
        action="store_true",
        help="Do not query synthetics.run.count via SignalFlow (Failing tests table will be empty).",
    )
    p.add_argument(
        "--failure-metrics-lookback-hours",
        type=int,
        default=168,
        help="Wall-clock window for synthetics.run.count sums (default 168 = 7 days; max 168).",
    )
    p.add_argument(
        "--failing-rate-threshold-pct",
        type=float,
        default=30.0,
        help="List active tests with failure rate **strictly above** this %% (default 30).",
    )
    p.add_argument(
        "--failure-metrics-resolution-minutes",
        type=int,
        default=60,
        help="SignalFlow resolution for run-count rollups in minutes (default 60).",
    )
    p.add_argument("--structured-json-out", metavar="PATH", help="Write normalized report JSON for o11y_health_check_run.py.")
    p.add_argument("--md-out", metavar="PATH", help="Write markdown section (## Synthetics health check).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("Synthetics health check starting (tests API + optional SignalFlow)")

    token = (os.environ.get("SPLUNK_ACCESS_TOKEN") or "").strip()
    realm = (args.realm or os.environ.get("SPLUNK_REALM") or "").strip()
    profile_path = resolve_profile_path(args.profile)
    if profile_path:
        prof = load_customer_profile_scalars(profile_path)
        token = token or (prof.get("access_token") or "").strip()
        realm = realm or (prof.get("realm") or "").strip()
    if not token:
        logger.error("No API token: set SPLUNK_ACCESS_TOKEN or access_token in profile.")
        return 1
    if not realm:
        realm = "us0"

    logger.info(
        "Realm: %s | max_tests: %s | subscription SF: %s | failure SF: %s",
        realm,
        max(1, min(args.max_tests, 10000)),
        "off" if args.skip_subscription_signalflow else "on",
        "off" if args.skip_failure_metrics else "on",
    )

    cfg = SyntheticsHealthConfig(
        realm=realm,
        max_tests=max(1, min(args.max_tests, 10000)),
        per_page=max(1, min(args.per_page, 200)),
        max_url_detail_for_dupes=max(0, min(args.max_detail_for_dupes, 5000)),
        max_detectors_scan=max(1, min(args.max_detectors_scan, 5000)),
        sleep_s=max(0.0, float(args.sleep)),
        fetch_subscription_allowances=not bool(args.skip_subscription_signalflow),
        subscription_lookback_days=max(1, min(args.subscription_lookback_days, 90)),
        subscription_resolution_hours=max(1, min(args.subscription_resolution_hours, 168)),
        fetch_failure_metrics=not bool(args.skip_failure_metrics),
        failure_metrics_lookback_hours=max(1, min(args.failure_metrics_lookback_hours, 168)),
        failure_metrics_resolution_minutes=max(1, min(args.failure_metrics_resolution_minutes, 1440)),
        failing_rate_threshold_pct=max(0.0, min(float(args.failing_rate_threshold_pct), 100.0)),
    )
    report = run_synthetics_health(token, cfg)

    if args.structured_json_out:
        Path(args.structured_json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote structured JSON: %s", args.structured_json_out)
    if args.md_out:
        Path(args.md_out).write_text(render_synthetics_checks_markdown(report), encoding="utf-8")
        logger.info("Wrote markdown: %s", args.md_out)

    if report.get("error"):
        logger.error("Report error: %s", str(report.get("error"))[:800])
        print(json.dumps(report, indent=2))
        return 1
    brief = {k: report[k] for k in ("schema", "realm", "testListTotal", "testsAnalyzed", "maxTestsCap") if k in report}
    if "syntheticsSubscriptionAllowances" in report:
        brief["syntheticsSubscriptionAllowances"] = report["syntheticsSubscriptionAllowances"]
    if report.get("syntheticsSubscriptionFetchError"):
        brief["syntheticsSubscriptionFetchError"] = report["syntheticsSubscriptionFetchError"]
    print(json.dumps(brief, indent=2))
    logger.info("Done: testsAnalyzed=%s", brief.get("testsAnalyzed"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
