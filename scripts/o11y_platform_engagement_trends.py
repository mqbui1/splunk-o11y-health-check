#!/usr/bin/env python3
"""
Splunk Observability Cloud — Platform engagement trends (read-only SignalFlow).

Aligned with ``Splunk-Observability-Health-Check.md`` **Platform engagement → Engagement Trends**:
org-level ``sf.org.*`` counters over a configurable lookback, with **current vs baseline** KPIs and
Mermaid ``xychart-beta`` trend blocks.

**Product gating (license JSON):**
- **Instrumented applications** (``service.request.count`` …) is queried and shown **only** when
  the license snapshot shows at least one **APM** entitlement with positive subscription allowance.
- **Custom metrics** (``sf.org.numCustomMetrics``) is shown **only** when entitlement ``im_custom_metrics``
  has positive subscription allowance.
- **RUM sessions (monthly)** (``sf.org.rum.numSessions``): **sum of daily rollup buckets** in each full UTC calendar
  month, after ``data(...).sum()`` across matching series (org-wide total per day → total sessions in the month).
  Default mode compares the previous full month to the month six months earlier; **month-vs-month** picks two
  ``YYYY-MM`` values and **always** assigns the **earlier** month to Baseline and the **later** to Comparison
  (chronological order, regardless of which flag came first); **custom**
  windows sum buckets in each selected period.
- **Synthetics test runs (monthly)** (``synthetics.run.count``): **sum of daily org-wide counts** in each calendar month
  (``data(...).sum()`` at 1d resolution, then sum all buckets in the month) — same idea as Chart Builder
  ``sum(cycle='month', cycle_start='1d', partial_values=False)``. Shown **only** when any of ``syn_browser_runs`` /
  ``syn_api_runs`` / ``syn_uptime_runs`` has allowance.

If ``--license-json`` is missing or unreadable, those gated KPIs are **omitted** (conservative default).
"""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import (  # noqa: E402
    execute_signalflow_matrix,
    execute_signalflow_time_series,
    filter_license_rows_for_subscription_report,
    load_customer_profile_scalars,
    resolve_profile_path,
    row_has_positive_subscription_allowance,
    utc_end_of_day_ms_inclusive,
)
from o11y_script_logging import setup_script_logging  # noqa: E402
from o11y_im_metrics_usage_breakdown import (  # noqa: E402
    _billing_class_from_row,
    _deep_extract_metric_rows,
    _metric_name,
    _mts_value,
    fetch_metrics_usage_payload,
)
from o11y_rum_health_check import _RUM_SESSION_PROGRAMS, _app_label_from_props  # noqa: E402

STRUCTURED_SCHEMA = "o11y_platform_engagement_trends/v1"

logger = logging.getLogger(__name__)

_MS_DAY = 86400000
_MS_WEEK = 7 * _MS_DAY


def _stream_url(realm: str) -> str:
    return f"https://stream.{(realm or '').strip()}.signalfx.com"


def _dedupe_ts_mean(points: list[tuple[int, float]]) -> list[tuple[int, float]]:
    buckets: dict[int, list[float]] = {}
    for ts, v in points:
        buckets.setdefault(int(ts), []).append(float(v))
    return sorted((ts, statistics.mean(vs)) for ts, vs in buckets.items())


def _mean_window(points: list[tuple[int, float]], *, last_ms: int, span_ms: int) -> float | None:
    """Mean of values with timestamp in (last_ms - span_ms, last_ms]."""
    lo = last_ms - span_ms
    vals = [v for ts, v in points if lo < ts <= last_ms]
    if not vals:
        return None
    return float(statistics.mean(vals))


def _utc_start_of_day_ms(iso_date: str) -> int:
    d = datetime.strptime(iso_date.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _utc_end_of_day_exclusive_ms(iso_date: str) -> int:
    """First instant strictly after the calendar day ``iso_date`` (UTC)."""
    return _utc_start_of_day_ms(iso_date) + _MS_DAY


def _mean_in_half_open_range(
    points: list[tuple[int, float]], lo_ms: int, hi_exclusive_ms: int
) -> float | None:
    """Mean of points with ``lo_ms <= ts < hi_exclusive_ms``."""
    vals = [v for ts, v in points if lo_ms <= ts < hi_exclusive_ms]
    if not vals:
        return None
    return float(statistics.mean(vals))


def _baseline_mean(
    points: list[tuple[int, float]],
    *,
    last_ms: int,
    window_ms: int,
    offset_ms: int,
) -> float | None:
    """Mean over ``window_ms`` ending at ``last_ms - offset_ms``."""
    end = last_ms - offset_ms
    return _mean_window(points, last_ms=end, span_ms=window_ms)


def _utc_month_bounds_ms(year: int, month: int) -> tuple[int, int]:
    """Return ``[start_ms, end_ms)`` for the calendar month in UTC."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _month_end_date_iso(year: int, month: int) -> str:
    last_d = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_d:02d}"


def _add_calendar_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """``delta`` negative moves backward (e.g. ``-6`` → six months earlier)."""
    idx = (year * 12 + (month - 1)) + delta
    y = idx // 12
    m = idx % 12 + 1
    return y, m


def _previous_full_month_utc(*, now_ms: int | None = None) -> tuple[int, int]:
    """Calendar month immediately before ``now`` (UTC)."""
    if now_ms is None:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    y, m = dt.year, dt.month
    if m == 1:
        return y - 1, 12
    return y, m - 1


def _points_in_month(
    points: list[tuple[int, float]], year: int, month: int
) -> list[tuple[int, float]]:
    lo, hi = _utc_month_bounds_ms(year, month)
    return [(ts, v) for ts, v in points if lo <= ts < hi]


def _sum_in_half_open_range(
    points: list[tuple[int, float]], lo_ms: int, hi_exclusive_ms: int
) -> float | None:
    """Sum of all bucket values with ``lo_ms <= ts < hi_exclusive_ms`` (e.g. RUM / Synthetics daily totals in window)."""
    chunk = [v for ts, v in points if lo_ms <= ts < hi_exclusive_ms]
    if not chunk:
        return None
    return float(sum(chunk))


def _month_end_instant_iso_utc(year: int, month: int) -> str:
    """ISO-8601 instant at last millisecond of the calendar month (UTC)."""
    last_d = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_d:02d}T23:59:59.999Z"


def _month_sum_bucket_values(points: list[tuple[int, float]], year: int, month: int) -> float | None:
    """Sum of daily (or per-bucket) values in the UTC month — RUM sessions and Synthetics runs KPIs."""
    chunk = _points_in_month(points, year, month)
    if not chunk:
        return None
    return float(sum(v for _, v in chunk))


def _pct_change(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None:
        return None
    if baseline == 0:
        return None if current == 0 else float("inf")
    return (current - baseline) / baseline * 100.0


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return "—"
    if p == float("inf"):
        return "∞"
    return f"{p:+.2f}%"


def _fmt_num(x: float | None, *, max_decimals: int = 2) -> str:
    if x is None:
        return "—"
    if abs(x - round(x)) < 1e-6:
        return str(int(round(x)))
    return f"{x:.{max_decimals}f}".rstrip("0").rstrip(".")


def _read_entitlement_flags(license_json_path: str | None) -> tuple[bool, bool, bool, bool, str]:
    """
    Returns (show_apm_apps, show_custom_metrics, show_rum_monthly, show_synthetics_monthly, note).
    ``note`` is ``ok``, ``missing_file``, ``invalid_json``, or ``no_rows``.
    """
    if not license_json_path:
        return False, False, False, False, "missing_file"
    p = Path(license_json_path)
    if not p.is_file():
        return False, False, False, False, "missing_file"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, False, False, False, "invalid_json"
    rows = list(data.get("rows") or [])
    if not rows:
        return False, False, False, False, "no_rows"
    disp = filter_license_rows_for_subscription_report(rows)
    show_apm = any(
        str(r.get("product") or "").strip() == "APM" and row_has_positive_subscription_allowance(r) for r in disp
    )
    show_cm = any(
        str(r.get("key") or "").strip() == "im_custom_metrics" and row_has_positive_subscription_allowance(r)
        for r in disp
    )
    show_rum = any(
        str(r.get("key") or "").strip() == "rum_sessions" and row_has_positive_subscription_allowance(r)
        for r in disp
    )
    syn_keys = frozenset({"syn_browser_runs", "syn_api_runs", "syn_uptime_runs"})
    show_syn = any(
        str(r.get("key") or "").strip() in syn_keys and row_has_positive_subscription_allowance(r) for r in disp
    )
    return show_apm, show_cm, show_rum, show_syn, "ok"


def _fetch_gauge_series(
    *,
    stream_url: str,
    token: str,
    metric: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    wall_seconds: float = 150.0,
    read_timeout: float = 90.0,
    max_points: int = 25_000,
) -> tuple[list[tuple[int, float]], str | None]:
    program = f"data('{metric}').mean().publish(label='v')"
    pts, err = execute_signalflow_time_series(
        stream_url=stream_url,
        token=token,
        program=program,
        start_ms=start_ms,
        stop_ms=stop_ms,
        resolution_ms=resolution_ms,
        wall_seconds=wall_seconds,
        read_timeout=read_timeout,
        max_points=max_points,
    )
    if err:
        return [], err
    return _dedupe_ts_mean(pts), None


def _fetch_rum_sessions_sum_series(
    *,
    stream_url: str,
    token: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
    wall_seconds: float = 240.0,
    read_timeout: float = 120.0,
    max_points: int = 25_000,
) -> tuple[list[tuple[int, float]], str | None]:
    """Org-wide RUM session signal: sum across series so each bucket matches total sessions for that day."""
    program = "data('sf.org.rum.numSessions').sum().publish(label='rum_sessions')"
    pts, err = execute_signalflow_time_series(
        stream_url=stream_url,
        token=token,
        program=program,
        start_ms=start_ms,
        stop_ms=stop_ms,
        resolution_ms=resolution_ms,
        wall_seconds=wall_seconds,
        read_timeout=read_timeout,
        max_points=max_points,
    )
    if err:
        return [], err
    return _dedupe_ts_mean(pts), None


def _fetch_app_count_series(
    *,
    stream_url: str,
    token: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
) -> tuple[list[tuple[int, float]], str | None]:
    program = (
        "data('service.request.count').sum(by=['service.name', 'sf_environment']).publish(label='apps')"
    )
    _meta, dps, err, stop = execute_signalflow_matrix(
        stream_url=stream_url,
        token=token,
        program=program,
        start_ms=start_ms,
        stop_ms=stop_ms,
        resolution_ms=resolution_ms,
        wall_seconds=260.0,
        read_timeout=120.0,
        max_data_points=200_000,
    )
    if err:
        return [], err
    if stop and stop not in ("end_of_channel", None):
        logger.info("App-count matrix stop_reason=%s (points=%s)", stop, len(dps))
    by_ts: dict[int, set[str]] = {}
    for pt in dps:
        ts = pt.get("timestampMs")
        tid = pt.get("tsId")
        if ts is None or tid is None:
            continue
        by_ts.setdefault(int(ts), set()).add(str(tid))
    series = sorted((ts, float(len(ids))) for ts, ids in by_ts.items())
    return _dedupe_ts_mean(series), None


def _fetch_synthetics_run_sum_series(
    *,
    stream_url: str,
    token: str,
    start_ms: int,
    stop_ms: int,
    resolution_ms: int,
) -> tuple[list[tuple[int, float]], str | None]:
    """Org-wide synthetics run counter, one aggregate series (sum across tests/types)."""
    program = "data('synthetics.run.count').sum().publish(label='syn_runs')"
    pts, err = execute_signalflow_time_series(
        stream_url=stream_url,
        token=token,
        program=program,
        start_ms=start_ms,
        stop_ms=stop_ms,
        resolution_ms=resolution_ms,
        wall_seconds=240.0,
        read_timeout=120.0,
        max_points=20_000,
    )
    if err:
        return [], err
    return _dedupe_ts_mean(pts), None


# --- Platform engagement KPI drill-downs (viewer overlay contributors) -----------------


@dataclass(frozen=True)
class _PeGaugeWindows:
    """Time filters for service.request.count matrix sums (match gauge KPI windows)."""

    rolling: bool
    # rolling: ts with cmp_lo_exclusive < ts <= cmp_hi_inclusive
    cmp_lo_exclusive: int
    cmp_hi_inclusive: int
    base_lo_exclusive: int
    base_hi_inclusive: int
    # custom: cmp_lo_inclusive <= ts < cmp_hi_exclusive
    cmp_lo_inclusive: int
    cmp_hi_exclusive: int
    base_lo_inclusive: int
    base_hi_exclusive: int


def _pe_rum_syn_half_open_ranges(
    cfg: TrendConfig,
    *,
    custom_compare: bool,
    month_pair: bool,
    prev_y: int,
    prev_m: int,
    base_y: int,
    base_m: int,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """((c_lo, c_hi_excl), (b_lo, b_hi_excl)) for RUM / Synthetics bucket sums."""
    if custom_compare and not month_pair:
        c_lo = int(cfg.custom_current_start_ms or 0)
        c_hi = int(cfg.custom_current_end_exclusive_ms or 0)
        b_lo = int(cfg.custom_baseline_start_ms or 0)
        b_hi = int(cfg.custom_baseline_end_exclusive_ms or 0)
        return (c_lo, c_hi), (b_lo, b_hi)
    c_lo, c_hi = _utc_month_bounds_ms(prev_y, prev_m)
    b_lo, b_hi = _utc_month_bounds_ms(base_y, base_m)
    return (c_lo, c_hi), (b_lo, b_hi)


def _pe_gauge_windows(
    cfg: TrendConfig,
    stop_ms: int,
    *,
    custom_compare: bool,
    baseline_offset_ms: int,
) -> _PeGaugeWindows:
    last_ts = int(stop_ms)
    if custom_compare:
        return _PeGaugeWindows(
            rolling=False,
            cmp_lo_exclusive=0,
            cmp_hi_inclusive=0,
            base_lo_exclusive=0,
            base_hi_inclusive=0,
            cmp_lo_inclusive=int(cfg.custom_current_start_ms or 0),
            cmp_hi_exclusive=int(cfg.custom_current_end_exclusive_ms or 0),
            base_lo_inclusive=int(cfg.custom_baseline_start_ms or 0),
            base_hi_exclusive=int(cfg.custom_baseline_end_exclusive_ms or 0),
        )
    end_b = last_ts - baseline_offset_ms
    return _PeGaugeWindows(
        rolling=True,
        cmp_lo_exclusive=last_ts - int(cfg.current_window_ms),
        cmp_hi_inclusive=last_ts,
        base_lo_exclusive=end_b - int(cfg.baseline_window_ms),
        base_hi_inclusive=end_b,
        cmp_lo_inclusive=0,
        cmp_hi_exclusive=0,
        base_lo_inclusive=0,
        base_hi_exclusive=0,
    )


def _matrix_ts_ok_for_gauge(ts_ms: int, gw: _PeGaugeWindows, *, comparison: bool) -> bool:
    if gw.rolling:
        if comparison:
            return gw.cmp_lo_exclusive < ts_ms <= gw.cmp_hi_inclusive
        return gw.base_lo_exclusive < ts_ms <= gw.base_hi_inclusive
    if comparison:
        return gw.cmp_lo_inclusive <= ts_ms < gw.cmp_hi_exclusive
    return gw.base_lo_inclusive <= ts_ms < gw.base_hi_exclusive


def _matrix_sum_by_tsid(
    metadata: dict[str, dict[str, Any]],
    dps: list[dict[str, Any]],
    ts_ok,
) -> dict[str, float]:
    sums: dict[str, float] = {}
    for pt in dps:
        ts_raw = pt.get("timestampMs")
        if ts_raw is None:
            continue
        try:
            ts_ms = int(ts_raw)
        except (TypeError, ValueError):
            continue
        if not ts_ok(ts_ms):
            continue
        tid = pt.get("tsId")
        if tid is None:
            continue
        v = pt.get("value")
        if not isinstance(v, (int, float)):
            continue
        k = str(tid)
        sums[k] = sums.get(k, 0.0) + float(v)
    return sums


def _service_env_label(meta: dict[str, dict[str, Any]], tsid: str) -> str:
    props = meta.get(tsid) or {}
    if not isinstance(props, dict):
        props = {}
    svc = props.get("service.name") or props.get("sf_service") or "—"
    env = props.get("sf_environment") or props.get("deployment.environment") or "—"
    return f"{svc} · {env}"


def _synth_test_label(meta: dict[str, dict[str, Any]], tsid: str) -> str:
    props = meta.get(tsid) or {}
    if not isinstance(props, dict):
        props = {}
    name = props.get("test") or props.get("sf_test") or "—"
    tt = props.get("test_type") or props.get("testType") or ""
    return f"{name} ({tt})" if tt else str(name)


def _rows_top_added_removed_delta(
    cmp_sums: dict[str, float],
    base_sums: dict[str, float],
    label_fn,
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    keys_c = set(cmp_sums)
    keys_b = set(base_sums)
    added = sorted(((k, cmp_sums[k]) for k in keys_c - keys_b), key=lambda x: -x[1])[:top_n]
    removed = sorted(((k, base_sums[k]) for k in keys_b - keys_c), key=lambda x: -x[1])[:top_n]
    delta_rows = sorted(
        ((k, cmp_sums[k] - base_sums[k], base_sums[k], cmp_sums[k]) for k in keys_c & keys_b),
        key=lambda x: -abs(x[1]),
    )[:top_n]
    return {
        "added": [
            {"id": k, "label": label_fn(k), "baselineValue": 0.0, "comparisonValue": v, "delta": v}
            for k, v in added
        ],
        "removed": [
            {"id": k, "label": label_fn(k), "baselineValue": v, "comparisonValue": 0.0, "delta": -v}
            for k, v in removed
        ],
        "largestDelta": [
            {
                "id": k,
                "label": label_fn(k),
                "baselineValue": bv,
                "comparisonValue": cv,
                "delta": d,
            }
            for k, d, bv, cv in delta_rows
        ],
    }


def _drill_instrumented_apps(
    *,
    stream_url: str,
    token: str,
    fetch_lo: int,
    stop_ms: int,
    resolution_ms: int,
    gw: _PeGaugeWindows,
) -> dict[str, Any]:
    program = (
        "data('service.request.count').sum(by=['service.name', 'sf_environment']).publish(label='apps')"
    )
    meta, dps, err, stop = execute_signalflow_matrix(
        stream_url=stream_url,
        token=token,
        program=program,
        start_ms=fetch_lo,
        stop_ms=stop_ms,
        resolution_ms=resolution_ms,
        wall_seconds=320.0,
        read_timeout=150.0,
        max_data_points=400_000,
    )
    if err:
        return {"error": err[:500]}
    if stop and stop not in ("end_of_channel", None):
        logger.info("Drill instrumented_apps matrix stop_reason=%s", stop)
    cmp_sums = _matrix_sum_by_tsid(
        meta,
        dps,
        lambda ts: _matrix_ts_ok_for_gauge(ts, gw, comparison=True),
    )
    base_sums = _matrix_sum_by_tsid(
        meta,
        dps,
        lambda ts: _matrix_ts_ok_for_gauge(ts, gw, comparison=False),
    )
    tables = _rows_top_added_removed_delta(
        cmp_sums,
        base_sums,
        lambda tid: _service_env_label(meta, tid),
    )
    return {
        "methodology": (
            "Sums of ``service.request.count`` rollup bucket values per service×environment in the same "
            "UTC windows as the Instrumented Applications KPI (rolling means or custom ranges)."
        ),
        **tables,
    }


def _drill_rum_sessions(
    *,
    stream_url: str,
    token: str,
    fetch_lo: int,
    stop_ms: int,
    c_range: tuple[int, int],
    b_range: tuple[int, int],
) -> dict[str, Any]:
    c_lo, c_hi = c_range
    b_lo, b_hi = b_range
    last_err = None
    for program in _RUM_SESSION_PROGRAMS:
        meta, dps, err, stop = execute_signalflow_matrix(
            stream_url=stream_url,
            token=token,
            program=program,
            start_ms=fetch_lo,
            stop_ms=stop_ms,
            resolution_ms=_MS_DAY,
            wall_seconds=280.0,
            read_timeout=150.0,
            max_data_points=300_000,
        )
        if err:
            last_err = err
            continue
        if stop and stop not in ("end_of_channel", None):
            logger.info("Drill RUM matrix stop_reason=%s", stop)
        cmp_sums = _matrix_sum_by_tsid(meta, dps, lambda ts: c_lo <= ts < c_hi)
        base_sums = _matrix_sum_by_tsid(meta, dps, lambda ts: b_lo <= ts < b_hi)
        if len(meta) <= 1 and not any(cmp_sums.values()) and not any(base_sums.values()):
            last_err = "aggregate_only"
            continue
        tables = _rows_top_added_removed_delta(
            cmp_sums,
            base_sums,
            lambda tid: _app_label_from_props(meta.get(tid) or {}),
        )
        return {
            "methodology": (
                "Sum of daily ``sf.org.rum.numSessions`` buckets per application dimension in the same "
                "UTC windows as the RUM Sessions KPI (full calendar months or custom ranges)."
            ),
            **tables,
        }
    return {
        "error": (last_err or "no_program")[:500],
        "methodology": "Per-application RUM drill-down could not be computed.",
    }


_SYN_PROG_PRIMARY = (
    "data('synthetics.run.count').sum(by=['test', 'test_type']).publish(label='syn_drill')"
)
_SYN_PROG_FALLBACK = "data('synthetics.run.count').sum(by=['test']).publish(label='syn_drill')"


def _drill_synthetics(
    *,
    stream_url: str,
    token: str,
    fetch_lo: int,
    stop_ms: int,
    c_range: tuple[int, int],
    b_range: tuple[int, int],
) -> dict[str, Any]:
    c_lo, c_hi = c_range
    b_lo, b_hi = b_range
    for program in (_SYN_PROG_PRIMARY, _SYN_PROG_FALLBACK):
        meta, dps, err, stop = execute_signalflow_matrix(
            stream_url=stream_url,
            token=token,
            program=program,
            start_ms=fetch_lo,
            stop_ms=stop_ms,
            resolution_ms=_MS_DAY,
            wall_seconds=280.0,
            read_timeout=150.0,
            max_data_points=300_000,
        )
        if err:
            continue
        if stop and stop not in ("end_of_channel", None):
            logger.info("Drill synthetics matrix stop_reason=%s", stop)
        cmp_sums = _matrix_sum_by_tsid(meta, dps, lambda ts: c_lo <= ts < c_hi)
        base_sums = _matrix_sum_by_tsid(meta, dps, lambda ts: b_lo <= ts < b_hi)
        tables = _rows_top_added_removed_delta(
            cmp_sums,
            base_sums,
            lambda tid: _synth_test_label(meta, tid),
        )
        return {
            "methodology": (
                "Sum of daily ``synthetics.run.count`` buckets per test (and type when available) in the same "
                "UTC windows as the Synthetics KPI."
            ),
            **tables,
        }
    return {"error": "SignalFlow matrix failed for synthetics.run.count", "methodology": ""}


def _drill_custom_metrics_usage_api(*, token: str, realm: str) -> dict[str, Any]:
    """
    Usage analytics API only exposes fixed lookbacks (e.g. P30D) from query time — not arbitrary months.
    We return top Custom billing-class metrics for the rolling window with an explicit caveat.
    """
    payload, err = fetch_metrics_usage_payload(
        token,
        realm,
        lookback="P30D",
        limit=5000,
        billable=True,
        order_by="-averageHourlyMtsCount",
    )
    if err or payload is None:
        return {"error": (err or "empty")[:500], "methodology": "", "topRecent": []}
    rows = _deep_extract_metric_rows(payload)
    custom_rows: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        mname = _metric_name(r)
        if _billing_class_from_row(r, mname) != "Custom":
            continue
        mts = _mts_value(r)
        custom_rows.append(
            {
                "metricName": mname[:200],
                "averageHourlyMts": mts,
            }
        )
    custom_rows.sort(key=lambda x: -float(x.get("averageHourlyMts") or 0))
    top = custom_rows[:10]
    return {
        "methodology": (
            "Splunk Usage analytics **metrics** API supports fixed lookbacks only (here: **P30D** from report "
            "generation time). This is **not** a true baseline-vs-comparison period diff — use Observability "
            "**Analyze metric usage** for exact windows. Listed: top Custom-class metrics by estimated average "
            "hourly MTS in that rolling window."
        ),
        "topRecent": top,
        "periodOverPeriodUnavailable": True,
    }


def _compute_pe_drilldowns(
    *,
    token: str,
    cfg: TrendConfig,
    stream_url: str,
    fetch_start_ms: int,
    stop_ms: int,
    last_ts: int,
    baseline_offset_ms: int,
    show_apm: bool,
    show_cm: bool,
    show_rum: bool,
    show_syn: bool,
    month_pair: bool,
    custom_compare: bool,
    prev_y: int,
    prev_m: int,
    base_y: int,
    base_m: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    gw = _pe_gauge_windows(cfg, last_ts, custom_compare=custom_compare, baseline_offset_ms=baseline_offset_ms)
    (c_rng, b_rng) = _pe_rum_syn_half_open_ranges(
        cfg,
        custom_compare=custom_compare,
        month_pair=month_pair,
        prev_y=prev_y,
        prev_m=prev_m,
        base_y=base_y,
        base_m=base_m,
    )
    fetch_lo = min(fetch_start_ms, c_rng[0], b_rng[0]) - 7 * _MS_DAY
    fetch_lo = max(0, fetch_lo)

    if show_apm:
        try:
            out["instrumented_apps"] = _drill_instrumented_apps(
                stream_url=stream_url,
                token=token,
                fetch_lo=fetch_lo,
                stop_ms=stop_ms,
                resolution_ms=cfg.apps_resolution_ms,
                gw=gw,
            )
        except Exception as e:
            logger.exception("instrumented_apps drilldown")
            out["instrumented_apps"] = {"error": str(e)[:500]}
    if show_cm:
        try:
            out["custom_metrics"] = _drill_custom_metrics_usage_api(token=token, realm=cfg.realm)
        except Exception as e:
            logger.exception("custom_metrics drilldown")
            out["custom_metrics"] = {"error": str(e)[:500]}
    if show_rum:
        try:
            out["rum_sessions_monthly"] = _drill_rum_sessions(
                stream_url=stream_url,
                token=token,
                fetch_lo=fetch_lo,
                stop_ms=stop_ms,
                c_range=c_rng,
                b_range=b_rng,
            )
        except Exception as e:
            logger.exception("rum drilldown")
            out["rum_sessions_monthly"] = {"error": str(e)[:500]}
    if show_syn:
        try:
            out["synthetics_runs_monthly"] = _drill_synthetics(
                stream_url=stream_url,
                token=token,
                fetch_lo=fetch_lo,
                stop_ms=stop_ms,
                c_range=c_rng,
                b_range=b_rng,
            )
        except Exception as e:
            logger.exception("synthetics drilldown")
            out["synthetics_runs_monthly"] = {"error": str(e)[:500]}
    return out


def _parse_calendar_month_label(s: str) -> tuple[int, int]:
    """``YYYY-MM`` → (year, month)."""
    t = (s or "").strip()
    parts = t.split("-")
    if len(parts) != 2:
        raise ValueError("expected YYYY-MM")
    y, mo = int(parts[0]), int(parts[1])
    if y < 1970 or mo < 1 or mo > 12:
        raise ValueError("invalid month")
    return y, mo


def _month_compare_window_fields(cur_y: int, cur_m: int, base_y: int, base_m: int) -> dict[str, Any]:
    return {
        "comparisonKind": "full_calendar_month",
        "currentWindowStart": f"{cur_y:04d}-{cur_m:02d}-01",
        "currentWindowEnd": _month_end_date_iso(cur_y, cur_m),
        "baselineWindowStart": f"{base_y:04d}-{base_m:02d}-01",
        "baselineWindowEnd": _month_end_date_iso(base_y, base_m),
    }


@dataclass(frozen=True)
class TrendConfig:
    realm: str
    lookback_days: int
    resolution_ms: int
    apps_resolution_ms: int
    current_window_ms: int
    baseline_window_ms: int
    # If set, use as last_ts / monthly anchor instead of wall clock.
    stop_ms_override: int | None = None
    # When all four are set, rolling KPIs compare means over these UTC ranges (end exclusive ms).
    custom_current_start_ms: int | None = None
    custom_current_end_exclusive_ms: int | None = None
    custom_baseline_start_ms: int | None = None
    custom_baseline_end_exclusive_ms: int | None = None
    # Full UTC months: comparison (current column) vs baseline; implies custom_* are month bounds.
    month_vs_month: tuple[int, int, int, int] | None = None


def run_platform_engagement_trends(
    token: str,
    cfg: TrendConfig,
    *,
    license_json_path: str | None,
    skip_pe_drilldown: bool = False,
) -> dict[str, Any]:
    stream_url = _stream_url(cfg.realm)

    custom_compare = (
        cfg.custom_current_start_ms is not None
        and cfg.custom_current_end_exclusive_ms is not None
        and cfg.custom_baseline_start_ms is not None
        and cfg.custom_baseline_end_exclusive_ms is not None
    )
    month_pair = cfg.month_vs_month is not None

    if cfg.stop_ms_override is not None:
        stop_ms = int(cfg.stop_ms_override)
    elif custom_compare:
        stop_ms = max(cfg.custom_current_end_exclusive_ms, cfg.custom_baseline_end_exclusive_ms) - 1
    else:
        stop_ms = int(time.time() * 1000)

    if custom_compare:
        fetch_lo = min(cfg.custom_current_start_ms, cfg.custom_baseline_start_ms) - 7 * _MS_DAY
        start_ms = max(0, fetch_lo)
        baseline_offset_days = 0
        baseline_offset_ms = 0
    else:
        start_ms = stop_ms - cfg.lookback_days * _MS_DAY
        baseline_offset_days = min(180, max(14, cfg.lookback_days - 7))
        baseline_offset_ms = baseline_offset_days * _MS_DAY

    show_apm, show_cm, show_rum, show_syn, lic_note = _read_entitlement_flags(license_json_path)

    findings: list[str] = []
    if lic_note == "missing_file":
        findings.append(
            "APM **instrumented applications**, **custom metrics**, **RUM sessions (monthly)**, and "
            "**Synthetics test runs (monthly)** KPIs are omitted because no license snapshot was supplied — "
            "re-run with ``--license-json`` from ``o11y_license_utilization.py`` or run the consolidated "
            "health check so the license step precedes engagement."
        )
    elif lic_note in ("invalid_json", "no_rows"):
        findings.append(
            "License snapshot was missing or empty; APM apps, custom metrics, RUM monthly, and Synthetics "
            "monthly KPIs are omitted for safety."
        )

    if month_pair:
        pass  # Dates and methodology are shown in the report timeline + KPI rows; avoid long intro findings.
    elif custom_compare:
        pass
    elif cfg.stop_ms_override is not None:
        asof = datetime.fromtimestamp(stop_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        findings.append(f"Engagement trends anchored to **as-of** {asof} (rolling windows vs default baseline offset).")

    custom_win_fields: dict[str, Any] = {}
    if custom_compare:
        if month_pair:
            cy, cm, by, bm = cfg.month_vs_month  # type: ignore[misc]
            custom_win_fields = dict(_month_compare_window_fields(cy, cm, by, bm))
        else:
            custom_win_fields = {
                "comparisonKind": "custom_calendar_range",
                "currentWindowStart": datetime.fromtimestamp(
                    cfg.custom_current_start_ms / 1000.0, tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "currentWindowEnd": datetime.fromtimestamp(
                    (cfg.custom_current_end_exclusive_ms - 1) / 1000.0, tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "baselineWindowStart": datetime.fromtimestamp(
                    cfg.custom_baseline_start_ms / 1000.0, tz=timezone.utc
                ).strftime("%Y-%m-%d"),
                "baselineWindowEnd": datetime.fromtimestamp(
                    (cfg.custom_baseline_end_exclusive_ms - 1) / 1000.0, tz=timezone.utc
                ).strftime("%Y-%m-%d"),
            }

    kpi_defs: list[tuple[str, str, str, str | None]] = [
        ("org_users", "Total users", "sf.org.num.orguser", None),
        ("teams", "Total teams", "sf.org.num.team", None),
        ("dashboards", "Total dashboards", "sf.org.num.dashboard", None),
        ("detectors", "Total detectors", "sf.org.num.detector", None),
    ]
    if show_cm:
        kpi_defs.append(("custom_metrics", "Custom metrics (MTS)", "sf.org.numCustomMetrics", None))

    kpis: list[dict[str, Any]] = []
    last_ts = stop_ms

    for kid, label, metric, _ in kpi_defs:
        pts, err = _fetch_gauge_series(
            stream_url=stream_url,
            token=token,
            metric=metric,
            start_ms=start_ms,
            stop_ms=stop_ms,
            resolution_ms=cfg.resolution_ms,
        )
        if custom_compare and pts:
            cur = _mean_in_half_open_range(
                pts,
                int(cfg.custom_current_start_ms),
                int(cfg.custom_current_end_exclusive_ms),
            )
            base = _mean_in_half_open_range(
                pts,
                int(cfg.custom_baseline_start_ms),
                int(cfg.custom_baseline_end_exclusive_ms),
            )
        elif pts:
            cur = _mean_window(pts, last_ms=last_ts, span_ms=cfg.current_window_ms)
            base = _baseline_mean(
                pts,
                last_ms=last_ts,
                window_ms=cfg.baseline_window_ms,
                offset_ms=baseline_offset_ms,
            )
        else:
            cur = base = None
        pct = _pct_change(cur, base)
        pct_json = None if pct is None or pct == float("inf") else pct
        row_k: dict[str, Any] = {
            "id": kid,
            "label": label,
            "metric": metric,
            "points": [[ts, v] for ts, v in pts],
            "current": cur,
            "baseline": base,
            "pctChange": pct_json,
            "error": err,
        }
        if custom_compare and custom_win_fields:
            row_k.update(custom_win_fields)
        kpis.append(row_k)
        if err:
            findings.append(f"{label}: SignalFlow error ({metric}) — {_md_cell(err[:180])}.")

    if show_apm:
        pts, err = _fetch_app_count_series(
            stream_url=stream_url,
            token=token,
            start_ms=start_ms,
            stop_ms=stop_ms,
            resolution_ms=cfg.apps_resolution_ms,
        )
        if custom_compare and pts:
            cur = _mean_in_half_open_range(
                pts,
                int(cfg.custom_current_start_ms),
                int(cfg.custom_current_end_exclusive_ms),
            )
            base = _mean_in_half_open_range(
                pts,
                int(cfg.custom_baseline_start_ms),
                int(cfg.custom_baseline_end_exclusive_ms),
            )
        elif pts:
            cur = _mean_window(pts, last_ms=last_ts, span_ms=cfg.current_window_ms)
            base = _baseline_mean(
                pts,
                last_ms=last_ts,
                window_ms=cfg.baseline_window_ms,
                offset_ms=baseline_offset_ms,
            )
        else:
            cur = base = None
        pct = _pct_change(cur, base)
        pct_json = None if pct is None or pct == float("inf") else pct
        row_a: dict[str, Any] = {
            "id": "instrumented_apps",
            "label": "Instrumented Applications",
            "metric": "service.request.count → count of series after sum(by=[service.name, sf_environment])",
            "points": [[ts, v] for ts, v in pts],
            "current": cur,
            "baseline": base,
            "pctChange": pct_json,
            "error": err,
        }
        if custom_compare and custom_win_fields:
            row_a.update(custom_win_fields)
        kpis.append(row_a)
        if err:
            findings.append(
                "Instrumented applications: SignalFlow error — confirm APM ingest and entitlement."
            )
        elif not pts:
            findings.append(
                "Instrumented applications: no time series returned — verify ``service.request.count`` "
                "exists for this org."
            )
    else:
        findings.append(
            "**Instrumented applications** KPI is omitted — no **APM** entitlement with subscription "
            "allowance was found in the license snapshot."
        )

    if not show_cm:
        findings.append(
            "**Custom metrics** KPI is omitted — ``im_custom_metrics`` has no subscription allowance "
            "in the license snapshot (Infrastructure Monitoring custom metrics not on contract)."
        )

    if custom_compare:
        ms_fetch_lo = min(int(cfg.custom_current_start_ms), int(cfg.custom_baseline_start_ms)) - 7 * _MS_DAY
        if month_pair:
            cy, cm, by, bm = cfg.month_vs_month  # type: ignore[misc]
            prev_y, prev_m = cy, cm
            base_y, base_m = by, bm
        else:
            prev_y = prev_m = base_y = base_m = 0
    else:
        prev_y, prev_m = _previous_full_month_utc(now_ms=stop_ms)
        base_y, base_m = _add_calendar_months(prev_y, prev_m, -6)
        ms_fetch_lo = _utc_month_bounds_ms(base_y, base_m)[0] - 7 * _MS_DAY
    daily_res = _MS_DAY

    if show_rum:
        rum_pts, rum_err = _fetch_rum_sessions_sum_series(
            stream_url=stream_url,
            token=token,
            start_ms=ms_fetch_lo,
            stop_ms=stop_ms,
            resolution_ms=daily_res,
        )
        rum_pts = _dedupe_ts_mean(rum_pts) if not rum_err else []
        if custom_compare and not month_pair:
            cur_rum = _sum_in_half_open_range(
                rum_pts,
                int(cfg.custom_current_start_ms),
                int(cfg.custom_current_end_exclusive_ms),
            )
            base_rum = _sum_in_half_open_range(
                rum_pts,
                int(cfg.custom_baseline_start_ms),
                int(cfg.custom_baseline_end_exclusive_ms),
            )
            win_rum = dict(custom_win_fields) if custom_win_fields else {}
        elif month_pair:
            cur_rum = _month_sum_bucket_values(rum_pts, prev_y, prev_m)
            base_rum = _month_sum_bucket_values(rum_pts, base_y, base_m)
            win_rum = _month_compare_window_fields(prev_y, prev_m, base_y, base_m)
        else:
            cur_rum = _month_sum_bucket_values(rum_pts, prev_y, prev_m)
            base_rum = _month_sum_bucket_values(rum_pts, base_y, base_m)
            win_rum = _month_compare_window_fields(prev_y, prev_m, base_y, base_m)
        pct_rum = _pct_change(cur_rum, base_rum)
        kpis.append(
            {
                "id": "rum_sessions_monthly",
                "label": "RUM Sessions",
                "metric": (
                    "sf.org.rum.numSessions → data(...).sum(); KPI = sum(daily buckets) per UTC month or window"
                ),
                "points": [[ts, v] for ts, v in rum_pts],
                "current": cur_rum,
                "baseline": base_rum,
                "pctChange": None if pct_rum is None or pct_rum == float("inf") else pct_rum,
                "error": rum_err,
                **win_rum,
            }
        )
        if rum_err:
            findings.append(
                "RUM sessions (monthly): SignalFlow error — confirm ``sf.org.rum.numSessions`` for this org."
            )
        elif not rum_pts:
            findings.append(
                "RUM sessions (monthly): no series in the extended window — verify RUM entitlement data in "
                "Chart Builder."
            )
    elif lic_note == "ok":
        findings.append(
            "**RUM sessions (monthly)** KPI is omitted — ``rum_sessions`` has no subscription allowance in "
            "the license snapshot."
        )

    if show_syn:
        syn_pts, syn_err = _fetch_synthetics_run_sum_series(
            stream_url=stream_url,
            token=token,
            start_ms=ms_fetch_lo,
            stop_ms=stop_ms,
            resolution_ms=daily_res,
        )
        syn_pts = _dedupe_ts_mean(syn_pts) if not syn_err else []
        if custom_compare and not month_pair:
            cur_syn = _sum_in_half_open_range(
                syn_pts,
                int(cfg.custom_current_start_ms),
                int(cfg.custom_current_end_exclusive_ms),
            )
            base_syn = _sum_in_half_open_range(
                syn_pts,
                int(cfg.custom_baseline_start_ms),
                int(cfg.custom_baseline_end_exclusive_ms),
            )
            win_syn = dict(custom_win_fields) if custom_win_fields else {}
        elif month_pair:
            cur_syn = _month_sum_bucket_values(syn_pts, prev_y, prev_m)
            base_syn = _month_sum_bucket_values(syn_pts, base_y, base_m)
            win_syn = _month_compare_window_fields(prev_y, prev_m, base_y, base_m)
        else:
            cur_syn = _month_sum_bucket_values(syn_pts, prev_y, prev_m)
            base_syn = _month_sum_bucket_values(syn_pts, base_y, base_m)
            win_syn = _month_compare_window_fields(prev_y, prev_m, base_y, base_m)
        pct_syn = _pct_change(cur_syn, base_syn)
        kpis.append(
            {
                "id": "synthetics_runs_monthly",
                "label": "Synthetics Test Runs",
                "metric": (
                    "synthetics.run.count → sum(daily buckets) in each full UTC calendar month "
                    "(Chart Builder–style monthly total)"
                    if month_pair
                    else (
                        "synthetics.run.count → sum of daily buckets in each selected UTC window"
                        if custom_compare
                        else "synthetics.run.count → sum of daily buckets per UTC calendar month (default windows)"
                    )
                ),
                "points": [[ts, v] for ts, v in syn_pts],
                "current": cur_syn,
                "baseline": base_syn,
                "pctChange": None if pct_syn is None or pct_syn == float("inf") else pct_syn,
                "error": syn_err,
                **win_syn,
            }
        )
        if syn_err:
            findings.append(
                "Synthetics runs (monthly): SignalFlow error — confirm ``synthetics.run.count`` ingest."
            )
        elif not syn_pts:
            findings.append(
                "Synthetics runs (monthly): no series in the extended window — verify Synthetics usage metrics."
            )
    elif lic_note == "ok":
        findings.append(
            "**Synthetics test runs (monthly)** KPI is omitted — no synthetics run entitlement with "
            "allowance was found (``syn_browser_runs`` / ``syn_api_runs`` / ``syn_uptime_runs``)."
        )

    drilldowns: dict[str, Any] | None = None
    if not skip_pe_drilldown:
        try:
            drilldowns = _compute_pe_drilldowns(
                token=token,
                cfg=cfg,
                stream_url=stream_url,
                fetch_start_ms=start_ms,
                stop_ms=stop_ms,
                last_ts=last_ts,
                baseline_offset_ms=baseline_offset_ms,
                show_apm=show_apm,
                show_cm=show_cm,
                show_rum=show_rum,
                show_syn=show_syn,
                month_pair=month_pair,
                custom_compare=custom_compare,
                prev_y=prev_y,
                prev_m=prev_m,
                base_y=base_y,
                base_m=base_m,
            )
        except Exception:
            logger.exception("Platform engagement drilldowns failed")
            drilldowns = {"error": "drilldown computation raised an exception (see log)"}

    eff_lookback_days = max(1, int(round((stop_ms - start_ms) / float(_MS_DAY))))
    out: dict[str, Any] = {
        "schema": STRUCTURED_SCHEMA,
        "realm": cfg.realm,
        "lookbackDays": eff_lookback_days if custom_compare else cfg.lookback_days,
        "resolutionMs": cfg.resolution_ms,
        "appsResolutionMs": cfg.apps_resolution_ms,
        "currentWindowMs": cfg.current_window_ms,
        "baselineOffsetDays": None if custom_compare else baseline_offset_days,
        "baselineWindowMs": cfg.baseline_window_ms,
        "comparisonMode": (
            "month_vs_month"
            if month_pair
            else ("custom_calendar_range" if custom_compare else "rolling_default")
        ),
        "engagementStopMs": stop_ms,
        "monthlyKpiCalendar": (
            {
                "comparisonMonth": f"{prev_y:04d}-{prev_m:02d}",
                "baselineMonth": f"{base_y:04d}-{base_m:02d}",
                "comparisonMonthEndUtc": _month_end_instant_iso_utc(prev_y, prev_m),
                "baselineMonthEndUtc": _month_end_instant_iso_utc(base_y, base_m),
                "note": "RUM/Synthetics: sum of daily SignalFlow buckets in each full UTC calendar month.",
            }
            if month_pair
            else (
                {
                    "comparisonPeriodStart": custom_win_fields.get("currentWindowStart"),
                    "comparisonPeriodEnd": custom_win_fields.get("currentWindowEnd"),
                    "baselinePeriodStart": custom_win_fields.get("baselineWindowStart"),
                    "baselinePeriodEnd": custom_win_fields.get("baselineWindowEnd"),
                    "note": "RUM/Synthetics: sum of daily buckets inside each UTC comparison window.",
                }
                if custom_compare and custom_win_fields
                else {
                    "previousFullMonth": f"{prev_y:04d}-{prev_m:02d}",
                    "baselineFullMonth": f"{base_y:04d}-{base_m:02d}",
                    "note": "RUM/Synthetics: sum of daily buckets in each month (previous full month vs six months earlier).",
                }
            )
        ),
        "entitlements": {
            "showApmInstrumentedApps": show_apm,
            "showCustomMetrics": show_cm,
            "showRumMonthlyKpi": show_rum,
            "showSyntheticsMonthlyKpi": show_syn,
            "licenseJsonSource": lic_note,
        },
        "kpis": kpis,
        "findings": findings,
    }
    if drilldowns is not None:
        out["drilldowns"] = drilldowns
    if custom_compare:
        out["customCompare"] = {
            "currentStart": custom_win_fields.get("currentWindowStart"),
            "currentEnd": custom_win_fields.get("currentWindowEnd"),
            "baselineStart": custom_win_fields.get("baselineWindowStart"),
            "baselineEnd": custom_win_fields.get("baselineWindowEnd"),
        }
    return out


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


_VIEWER_PE_KPI_POINTS_CAP = 64


def _viewer_pe_kpi_payload(report: dict[str, Any]) -> dict[str, Any]:
    """
    JSON for the static web report: KPI cards + sparkline points (downsampled for payload size).
    """
    kpis_out: list[dict[str, Any]] = []
    last_ts = 0
    for k in report.get("kpis") or []:
        pts_raw = k.get("points") or []
        pts: list[list[int | float]] = []
        for row in pts_raw:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                try:
                    ts_i = int(row[0])
                    v_f = float(row[1])
                    pts.append([ts_i, v_f])
                    last_ts = max(last_ts, ts_i)
                except (TypeError, ValueError):
                    continue
        if len(pts) > _VIEWER_PE_KPI_POINTS_CAP:
            step = max(1, len(pts) // _VIEWER_PE_KPI_POINTS_CAP)
            pts = pts[::step][-_VIEWER_PE_KPI_POINTS_CAP:]
        entry: dict[str, Any] = {
            "id": k.get("id"),
            "label": k.get("label"),
            "current": k.get("current"),
            "baseline": k.get("baseline"),
            "pctChange": k.get("pctChange"),
            "error": k.get("error"),
            "points": pts,
        }
        for wk in (
            "comparisonKind",
            "currentWindowStart",
            "currentWindowEnd",
            "baselineWindowStart",
            "baselineWindowEnd",
        ):
            if k.get(wk) is not None:
                entry[wk] = k.get(wk)
        kpis_out.append(entry)
    if last_ts <= 0:
        last_ts = int(time.time() * 1000)
    cur_win = int(report.get("currentWindowMs") or 7 * _MS_DAY)
    base_win = int(report.get("baselineWindowMs") or 7 * _MS_DAY)
    off_days = int(report.get("baselineOffsetDays") or 180)
    off_ms = max(_MS_DAY, off_days * _MS_DAY)
    cur_end = last_ts
    cur_start_roll = cur_end - cur_win
    base_end = last_ts - off_ms
    base_start_roll = base_end - base_win

    def _iso(ms: int) -> str:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")

    mode = str(report.get("comparisonMode") or "")
    cc = report.get("customCompare") or {}
    mc = report.get("monthlyKpiCalendar") or {}
    timeline: dict[str, Any] | None = None
    cur_start_s: str
    cur_end_s: str
    base_start_s: str
    base_end_s: str

    if mode == "month_vs_month" and mc.get("comparisonMonth") and mc.get("baselineMonth"):
        try:
            cy, cm = _parse_calendar_month_label(str(mc["comparisonMonth"]))
            by, bm = _parse_calendar_month_label(str(mc["baselineMonth"]))
        except ValueError:
            cy = cm = by = bm = 1
        tl_b_end = mc.get("baselineMonthEndUtc") or _month_end_instant_iso_utc(by, bm)
        tl_c_end = mc.get("comparisonMonthEndUtc") or _month_end_instant_iso_utc(cy, cm)
        timeline = {
            "comparisonMode": "month_vs_month",
            "baseline": {
                "monthLabel": str(mc.get("baselineMonth")),
                "rangeStart": f"{by:04d}-{bm:02d}-01",
                "rangeEnd": _month_end_date_iso(by, bm),
                "endUtc": tl_b_end,
            },
            "comparison": {
                "monthLabel": str(mc.get("comparisonMonth")),
                "rangeStart": f"{cy:04d}-{cm:02d}-01",
                "rangeEnd": _month_end_date_iso(cy, cm),
                "endUtc": tl_c_end,
            },
            "rumSyntheticsNote": (
                "RUM & Synthetics KPIs **sum daily buckets** in each UTC month "
                "(same idea as ``sum(cycle='month')`` on ``synthetics.run.count`` in Chart Builder)."
            ),
        }
        cur_start_s = f"{cy:04d}-{cm:02d}-01"
        cur_end_s = _month_end_date_iso(cy, cm)
        base_start_s = f"{by:04d}-{bm:02d}-01"
        base_end_s = _month_end_date_iso(by, bm)
    elif mode == "custom_calendar_range" and cc.get("currentStart"):
        cur_start_s = str(cc.get("currentStart"))
        cur_end_s = str(cc.get("currentEnd"))
        base_start_s = str(cc.get("baselineStart"))
        base_end_s = str(cc.get("baselineEnd"))
        timeline = {
            "comparisonMode": "custom_calendar_range",
            "baseline": {
                "rangeStart": base_start_s,
                "rangeEnd": base_end_s,
                "endUtc": f"{base_end_s}T23:59:59.999Z",
            },
            "comparison": {
                "rangeStart": cur_start_s,
                "rangeEnd": cur_end_s,
                "endUtc": f"{cur_end_s}T23:59:59.999Z",
            },
            "rumSyntheticsNote": "RUM & Synthetics KPIs **sum daily buckets** inside each UTC window.",
        }
    else:
        cur_start_s = _iso(cur_start_roll)
        cur_end_s = _iso(cur_end)
        base_start_s = _iso(base_start_roll)
        base_end_s = _iso(base_end)
        timeline = {
            "comparisonMode": "rolling_default",
            "baseline": {"rangeStart": base_start_s, "rangeEnd": base_end_s},
            "comparison": {"rangeStart": cur_start_s, "rangeEnd": cur_end_s},
            "rumSyntheticsNote": (
                "Default run: gauges use rolling 7d means; "
                "RUM and Synthetics monthly KPIs **sum daily buckets** in each full UTC month vs six months prior."
            ),
        }

    out: dict[str, Any] = {
        "schema": "o11y_pe_viewer_kpi/v1",
        "currentWindowStart": cur_start_s,
        "currentWindowEnd": cur_end_s,
        "baselineWindowStart": base_start_s,
        "baselineWindowEnd": base_end_s,
        "timeline": timeline,
        "kpis": kpis_out,
    }
    return out


def _viewer_pe_kpi_comment(report: dict[str, Any]) -> str:
    """HTML comment consumed by web/o11y-health-report (strip before render); not visible in PDF."""
    payload = _viewer_pe_kpi_payload(report)
    raw = json.dumps(payload, separators=(",", ":"))
    b = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"<!-- O11Y_PE_KPI:{b} -->\n\n"


def _viewer_trim_drill_row(r: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in r.items():
        if k == "label" and isinstance(v, str):
            out[k] = v[:160]
        elif k == "metricName" and isinstance(v, str):
            out[k] = v[:160]
        else:
            out[k] = v
    return out


def _viewer_pe_drilldown_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Structured contributor tables for static viewer (cap list lengths)."""
    raw = report.get("drilldowns")
    if not isinstance(raw, dict):
        return {"schema": "o11y_pe_viewer_drilldown/v1", "drilldowns": {}}
    out_dd: dict[str, Any] = {}
    top_n = 10
    for key, block in raw.items():
        if not isinstance(block, dict):
            continue
        slim: dict[str, Any] = {
            "methodology": str(block.get("methodology") or "")[:1200],
        }
        if block.get("error"):
            slim["error"] = str(block.get("error"))[:500]
        for lst_key in ("added", "removed", "largestDelta", "topRecent"):
            rows = block.get(lst_key)
            if isinstance(rows, list):
                slim[lst_key] = [
                    _viewer_trim_drill_row(x) if isinstance(x, dict) else x
                    for x in rows[:top_n]
                ]
        if block.get("periodOverPeriodUnavailable") is True:
            slim["periodOverPeriodUnavailable"] = True
        out_dd[str(key)] = slim
    return {"schema": "o11y_pe_viewer_drilldown/v1", "drilldowns": out_dd}


def _viewer_pe_drilldown_comment(report: dict[str, Any]) -> str:
    if not report.get("drilldowns"):
        return ""
    payload = _viewer_pe_drilldown_payload(report)
    if not payload.get("drilldowns"):
        return ""
    raw = json.dumps(payload, separators=(",", ":"))
    b = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"<!-- O11Y_PE_DRILLDOWN:{b} -->\n\n"


def _mermaid_xychart(title: str, points: list[tuple[int, float]], *, max_points: int = 16) -> str:
    if len(points) < 2:
        return ""
    if len(points) > max_points:
        step = max(1, len(points) // max_points)
        sp = points[::step][-max_points:]
    else:
        sp = points
    labels: list[str] = []
    vals: list[int] = []
    for ts, v in sp:
        dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        labels.append(dt.strftime("%Y-%m-%d"))
        vals.append(int(round(max(0.0, float(v)))))
    ymax = max(vals + [1])
    ymax = int(ymax * 1.1) + 1
    title_esc = _md_cell(title).replace('"', "'")[:80]
    x_part = "[" + ", ".join(labels) + "]"
    y_part = f'"Count" 0 --> {ymax}'
    v_part = "[" + ", ".join(str(v) for v in vals) + "]"
    return (
        f"```mermaid\nxychart-beta\n"
        f'    title "{title_esc}"\n'
        f"    x-axis {x_part}\n"
        f"    y-axis {y_part}\n"
        f"    line {v_part}\n```\n\n"
    )


def render_platform_engagement_markdown(report: dict[str, Any] | None) -> str:
    """Full ``## Platform engagement`` section for the consolidated report."""
    if report is None or report.get("error"):
        err = str(report.get("error") or "") if report is not None else ""
        msg = "*Platform engagement trends were not run.*"
        if err.strip():
            msg = f"*Platform engagement trends failed ({_md_cell(err[:220])}).*"
        return (
            "## Platform engagement\n\n"
            "### Engagement Trends\n\n"
            f"{msg}\n\n"
            "### User Analysis\n\n"
            "### Results\n\n"
            "| Metric | Value |\n"
            "| --- | --- |\n"
            "| Directory / login analytics | *Not collected in this automation* |\n\n"
            "### Recommendation\n\n"
            "Use Splunk Observability **administration and usage** views for per-user login activity; "
            "directory counts alone do not prove engagement.\n\n"
        )

    lines: list[str] = [
        "## Platform engagement\n\n",
        "### Engagement Trends\n\n",
    ]
    if report.get("kpis"):
        lines.append(_viewer_pe_kpi_comment(report))
        lines.append(_viewer_pe_drilldown_comment(report))

    cc = report.get("customCompare") or {}
    mc = report.get("monthlyKpiCalendar") or {}
    if report.get("comparisonMode") == "month_vs_month" and mc.get("comparisonMonth"):
        comp_m = _md_cell(str(mc.get("comparisonMonth")))
        base_m = _md_cell(str(mc.get("baselineMonth")))
        tl_b = _md_cell(str(mc.get("baselineMonthEndUtc") or ""))
        tl_c = _md_cell(str(mc.get("comparisonMonthEndUtc") or ""))
        trend_intro = (
            "**Comparison periods (UTC, full calendar months)**\n\n"
            "| | Baseline month | Comparison month |\n"
            "| --- | --- | --- |\n"
            f"| Month | `{base_m}` | `{comp_m}` |\n"
            f"| Ends (UTC snapshot) | `{tl_b}` | `{tl_c}` |\n\n"
            "**Gauge KPIs:** mean of rollup points within each month. **RUM** and **Synthetics:** "
            "**sum of daily buckets** in each month (org-wide ``sum()`` on the metric, then add each day in the month).\n\n"
        )
    elif report.get("comparisonMode") == "custom_calendar_range" and cc.get("currentStart"):
        trend_intro = (
            "**Comparison periods (UTC)**\n\n"
            "| | Baseline | Comparison |\n"
            "| --- | --- | --- |\n"
            "| Range | "
            f"`{_md_cell(str(cc.get('baselineStart')))}`–`{_md_cell(str(cc.get('baselineEnd')))}` | "
            f"`{_md_cell(str(cc.get('currentStart')))}`–`{_md_cell(str(cc.get('currentEnd')))}` |\n\n"
            "**Gauge KPIs:** mean of rollup points in each window. **RUM** and **Synthetics:** "
            "**sum of daily buckets** inside each window.\n\n"
        )
    else:
        trend_intro = (
            "**Rolling windows (default)** — Most gauge KPIs: **latest 7-day mean** vs **~6 months earlier** "
            "(configurable offset). "
            "**RUM** and **Synthetics:** previous full UTC month vs six months earlier — **sum of daily buckets** in each "
            "month on ``sf.org.rum.numSessions`` (after ``sum()`` across series) and ``synthetics.run.count``.\n\n"
        )

    lines.extend(
        [
        trend_intro,
        "### Results\n\n",
        "#### KPI summary\n\n",
        "| KPI | Current | Baseline | Δ% |\n",
        "| --- | ---: | ---: | ---: |\n",
        ]
    )

    for k in report.get("kpis") or []:
        err = k.get("error")
        cur_s = "—" if err else _fmt_num(k.get("current"))
        base_s = "—" if err else _fmt_num(k.get("baseline"))
        pct_s = "—" if err else _fmt_pct(k.get("pctChange"))
        note = f" ({_md_cell(str(err)[:80])})" if err else ""
        lines.append(
            f"| {_md_cell(str(k.get('label')))}{note} | {cur_s} | {base_s} | {pct_s} |\n"
        )
    has_monthly_kpi = any(
        (k.get("comparisonKind") == "full_calendar_month") for k in (report.get("kpis") or [])
    )
    if report.get("comparisonMode") == "month_vs_month" and mc.get("comparisonMonth") and mc.get("baselineMonth"):
        lines.append(
            f"\n*RUM/Synthetics rows: **comparison month** `{_md_cell(str(mc['comparisonMonth']))}` "
            f"(sum of daily buckets through `{_md_cell(str(mc.get('comparisonMonthEndUtc') or ''))}` UTC); "
            f"**baseline month** `{_md_cell(str(mc['baselineMonth']))}` "
            f"(sum of daily buckets through `{_md_cell(str(mc.get('baselineMonthEndUtc') or ''))}` UTC).*\n\n"
        )
    elif (
        has_monthly_kpi
        and mc.get("previousFullMonth")
        and mc.get("baselineFullMonth")
    ):
        lines.append(
            f"\n*Monthly KPI rows: **current** = `{_md_cell(str(mc['previousFullMonth']))}` (previous full UTC "
            f"month); **baseline** = `{_md_cell(str(mc['baselineFullMonth']))}` (six months earlier). "
            "Other rows: default rolling windows or custom periods (see intro).*\n\n"
        )
    elif (
        report.get("comparisonMode") == "custom_calendar_range"
        and mc.get("comparisonPeriodStart")
        and mc.get("baselinePeriodStart")
    ):
        lines.append(
            f"\n*RUM / Synthetics rows (sum of daily buckets in window): **comparison** `{_md_cell(str(mc['comparisonPeriodStart']))}`–"
            f"`{_md_cell(str(mc['comparisonPeriodEnd']))}`; **baseline** `{_md_cell(str(mc['baselinePeriodStart']))}`–"
            f"`{_md_cell(str(mc['baselinePeriodEnd']))}`.*\n\n"
        )
    else:
        lines.append("\n")

    lines.append("#### Trend charts\n\n")
    for k in report.get("kpis") or []:
        if k.get("error"):
            lines.append(f"##### {_md_cell(str(k.get('label')))}\n\n")
            lines.append(f"*SignalFlow error:* `{_md_cell(str(k.get('error'))[:200])}`\n\n")
            continue
        pts_raw = k.get("points") or []
        pts: list[tuple[int, float]] = []
        for row in pts_raw:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                try:
                    pts.append((int(row[0]), float(row[1])))
                except (TypeError, ValueError):
                    continue
        chart = _mermaid_xychart(str(k.get("label") or k.get("id")), pts)
        lines.append(f"##### {_md_cell(str(k.get('label')))}\n\n")
        if chart:
            lines.append(chart)
        else:
            lines.append("*Not enough points for a chart.*\n\n")

    lines.append("### Findings\n\n")
    for f in report.get("findings") or []:
        lines.append(f"- {_md_cell(str(f))}\n")
    if not (report.get("findings") or []):
        lines.append("- *No findings.*\n")
    lines.append("\n")

    lines.append(
        "### Recommendation\n\n"
        "Use these trends with **license** and **usage** sections: sustained drops in users or teams "
        "may reflect identity cleanup; rises in dashboards/detectors may warrant governance reviews. "
        "Validate sharp moves in **Chart Builder** before acting.\n\n"
    )

    lines.append(
        "### User Analysis\n\n"
        "### Results\n\n"
        "| Metric | Value |\n"
        "| --- | --- |\n"
        "| Directory / 30-day unique logins | *Not collected here — requires admin / usage reporting* |\n\n"
        "### Recommendation\n\n"
        "Treat org **user directory** size as membership, not login frequency, unless a supported "
        "usage export is available.\n\n"
    )

    return "".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Splunk Observability Platform engagement trends (sf.org + optional APM series)."
    )
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument(
        "--license-json",
        metavar="PATH",
        help="License utilization JSON (o11y_license_utilization.py) for APM / custom-metrics gating.",
    )
    p.add_argument(
        "--lookback-days",
        type=int,
        default=180,
        help="SignalFlow window in days (default 180; clamped 90–366).",
    )
    p.add_argument(
        "--resolution-hours",
        type=int,
        default=24,
        help="Rollup resolution in hours for sf.org gauges (default 24).",
    )
    p.add_argument(
        "--apps-resolution-hours",
        type=int,
        default=168,
        help="Rollup resolution in hours for instrumented-apps matrix (default 168 = weekly).",
    )
    p.add_argument(
        "--baseline-calendar-month",
        default=None,
        metavar="YYYY-MM",
        help="With --comparison-calendar-month: one of two **full UTC months** to compare. Together they define a "
        "pair; the **earlier** month is always Baseline and the **later** Comparison (KPI baseline vs current). "
        "Takes precedence over --compare-*-date and --as-of-date.",
    )
    p.add_argument(
        "--comparison-calendar-month",
        default=None,
        metavar="YYYY-MM",
        help="With --baseline-calendar-month: the other month in the pair (see --baseline-calendar-month).",
    )
    p.add_argument(
        "--as-of-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Anchor rolling KPIs to end of this UTC day (instead of now). Ignored when all four "
        "--compare-*-date arguments are set or when month calendar args are set.",
    )
    p.add_argument(
        "--compare-current-start-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="With the other three --compare-*-date flags: UTC start of **current** period (inclusive).",
    )
    p.add_argument(
        "--compare-current-end-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC end of **current** period (inclusive).",
    )
    p.add_argument(
        "--compare-baseline-start-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC start of **baseline** period (inclusive).",
    )
    p.add_argument(
        "--compare-baseline-end-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="UTC end of **baseline** period (inclusive).",
    )
    p.add_argument("--structured-json-out", metavar="PATH", help="Write normalized report JSON.")
    p.add_argument("--md-out", metavar="PATH", help="Write markdown section.")
    p.add_argument(
        "--skip-pe-drilldown",
        action="store_true",
        help="Skip contributor drill-down queries (faster; viewer overlay tables omitted).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("Platform engagement trends starting")

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

    lb = max(90, min(int(args.lookback_days), 366))
    res_h = max(6, min(int(args.resolution_hours), 168))
    apps_res_h = max(24, min(int(args.apps_resolution_hours), 168))

    bl_ym = (args.baseline_calendar_month or "").strip() or None
    cp_ym = (args.comparison_calendar_month or "").strip() or None
    n_ym = sum(1 for x in (bl_ym, cp_ym) if x)
    if n_ym == 1:
        logger.error("Set both --baseline-calendar-month and --comparison-calendar-month, or neither.")
        return 1

    month_vs_month: tuple[int, int, int, int] | None = None
    stop_ov: int | None = None
    csm = cee = bsm = bee = None

    if n_ym == 2:
        if any(
            (getattr(args, name) or "").strip()
            for name in (
                "compare_current_start_date",
                "compare_current_end_date",
                "compare_baseline_start_date",
                "compare_baseline_end_date",
            )
        ):
            logger.warning("Ignoring --compare-*-date; using baseline/comparison calendar months.")
        if (args.as_of_date or "").strip():
            logger.warning("Ignoring --as-of-date; using baseline/comparison calendar months.")
        try:
            raw_b = _parse_calendar_month_label(bl_ym or "")
            raw_c = _parse_calendar_month_label(cp_ym or "")
        except ValueError:
            logger.error("Invalid --baseline-calendar-month or --comparison-calendar-month (use YYYY-MM).")
            return 1
        # Chronological semantics: Baseline = earlier calendar month, Comparison = later (matches growth-over-time).
        by, bm0 = min(raw_b, raw_c, key=lambda t: (t[0], t[1]))
        cy, cm0 = max(raw_b, raw_c, key=lambda t: (t[0], t[1]))
        if (raw_c[0], raw_c[1]) < (raw_b[0], raw_b[1]):
            logger.info(
                "Month-vs-month: profile/CLI had comparison month before baseline month; "
                "normalized to baseline %04d-%02d, comparison %04d-%02d.",
                by,
                bm0,
                cy,
                cm0,
            )
        csm, cee = _utc_month_bounds_ms(cy, cm0)
        bsm, bee = _utc_month_bounds_ms(by, bm0)
        month_vs_month = (cy, cm0, by, bm0)
    else:
        c0 = (args.compare_current_start_date or "").strip() or None
        c1 = (args.compare_current_end_date or "").strip() or None
        b0 = (args.compare_baseline_start_date or "").strip() or None
        b1 = (args.compare_baseline_end_date or "").strip() or None
        cmp_dates = [c0, c1, b0, b1]
        n_cmp = sum(1 for x in cmp_dates if x)
        if n_cmp not in (0, 4):
            logger.error(
                "Provide all four --compare-current-start-date, --compare-current-end-date, "
                "--compare-baseline-start-date, --compare-baseline-end-date, or omit custom compare entirely."
            )
            return 1

        if n_cmp == 4:
            try:
                csm = _utc_start_of_day_ms(c0)
                cee = _utc_end_of_day_exclusive_ms(c1)
                bsm = _utc_start_of_day_ms(b0)
                bee = _utc_end_of_day_exclusive_ms(b1)
            except ValueError:
                logger.error("Invalid --compare-*-date (use YYYY-MM-DD).")
                return 1
            if csm >= cee or bsm >= bee:
                logger.error("Each compare period needs start date on or before end date.")
                return 1
        else:
            asof = (args.as_of_date or "").strip() or None
            if asof:
                try:
                    stop_ov = utc_end_of_day_ms_inclusive(asof)
                except ValueError:
                    logger.error("Invalid --as-of-date (use YYYY-MM-DD).")
                    return 1

    cfg = TrendConfig(
        realm=realm,
        lookback_days=lb,
        resolution_ms=res_h * 3600 * 1000,
        apps_resolution_ms=apps_res_h * 3600 * 1000,
        current_window_ms=7 * _MS_DAY,
        baseline_window_ms=7 * _MS_DAY,
        stop_ms_override=stop_ov,
        custom_current_start_ms=csm,
        custom_current_end_exclusive_ms=cee,
        custom_baseline_start_ms=bsm,
        custom_baseline_end_exclusive_ms=bee,
        month_vs_month=month_vs_month,
    )

    exit_code = 0
    try:
        report = run_platform_engagement_trends(
            token,
            cfg,
            license_json_path=args.license_json,
            skip_pe_drilldown=bool(args.skip_pe_drilldown),
        )
    except Exception:
        logger.exception("Platform engagement trends crashed")
        report = {
            "schema": STRUCTURED_SCHEMA,
            "realm": realm,
            "error": "run_platform_engagement_trends raised an exception (see stderr log)",
            "kpis": [],
            "findings": [],
        }
        exit_code = 1

    if args.structured_json_out:
        Path(args.structured_json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote structured JSON: %s", args.structured_json_out)
    if args.md_out:
        Path(args.md_out).write_text(render_platform_engagement_markdown(report), encoding="utf-8")
        logger.info("Wrote markdown: %s", args.md_out)

    n_ok = sum(1 for k in (report.get("kpis") or []) if not k.get("error"))
    logger.info("Done: kpi_series=%s exit=%s", n_ok, exit_code)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
