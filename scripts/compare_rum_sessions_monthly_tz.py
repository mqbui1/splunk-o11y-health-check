#!/usr/bin/env python3
"""
Side-by-side RUM sessions monthly totals: UTC calendar months vs America/Chicago.

Uses the same SignalFlow program as ``rum_sessions`` in ``o11y_license_utilization.py``:
``data('sf.org.rum.numSessions').sum().publish(label='usage')``.

The execute window is the **union** of:
  - UTC months ``--from-month`` … ``--to-month`` (inclusive), and
  - those same calendar months interpreted in ``America/Chicago``,

so edge buckets around New Year / month boundaries are included for both aggregations.

Environment: ``SPLUNK_ACCESS_TOKEN`` or profile ``access_token``; ``SPLUNK_REALM`` / profile ``realm``.

Pass multiple ``--resolution-minutes`` values (e.g. ``60 10`` for 1h vs 10m) to print one table comparing
UTC vs America/Chicago for each resolution, plus optional deltas between resolutions.
"""

from __future__ import annotations

import argparse
import calendar
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import o11y_license_utilization as lic  # noqa: E402

RUM_USAGE_PROGRAM = "data('sf.org.rum.numSessions').sum().publish(label='usage')"
CENTRAL_TZ = ZoneInfo("America/Chicago")


def _parse_month(s: str) -> tuple[int, int]:
    parts = s.strip().replace("/", "-").split("-")
    if len(parts) < 2:
        raise ValueError(f"Invalid month {s!r} (use YYYY-MM)")
    y, m = int(parts[0]), int(parts[1])
    if y < 1970 or m < 1 or m > 12:
        raise ValueError(f"Invalid month {s!r}")
    return y, m


def _utc_month_start_ms(y: int, mo: int) -> int:
    return lic.utc_start_of_day_ms(f"{y:04d}-{mo:02d}-01")


def _utc_month_end_ms_inclusive(y: int, mo: int) -> int:
    last = calendar.monthrange(y, mo)[1]
    return lic.utc_end_of_day_ms_inclusive(f"{y:04d}-{mo:02d}-{last:02d}")


def _tz_month_start_ms(y: int, mo: int, tz: ZoneInfo) -> int:
    dt = datetime(y, mo, 1, 0, 0, 0, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def _tz_month_end_ms_inclusive(y: int, mo: int, tz: ZoneInfo) -> int:
    """Last millisecond of calendar month ``mo`` in ``tz``."""
    if mo == 12:
        ny, nmo = y + 1, 1
    else:
        ny, nmo = y, mo + 1
    next_start = datetime(ny, nmo, 1, 0, 0, 0, tzinfo=tz)
    return int(next_start.timestamp() * 1000) - 1


def _union_query_window_ms(
    from_month: str, to_month: str, tz: ZoneInfo
) -> tuple[int, int, tuple[int, int], tuple[int, int]]:
    y0, m0 = _parse_month(from_month)
    y1, m1 = _parse_month(to_month)
    if (y0, m0) > (y1, m1):
        raise ValueError("--from-month must be on or before --to-month")

    utc_start = _utc_month_start_ms(y0, m0)
    utc_stop = _utc_month_end_ms_inclusive(y1, m1)
    tz_start = _tz_month_start_ms(y0, m0, tz)
    tz_stop = _tz_month_end_ms_inclusive(y1, m1, tz)

    start_ms = min(utc_start, tz_start)
    stop_ms = max(utc_stop, tz_stop)
    return start_ms, stop_ms, (y0, m0), (y1, m1)


def month_key_utc(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def month_key_central(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=CENTRAL_TZ)
    return f"{dt.year:04d}-{dt.month:02d}"


def _sum_by_month_key(
    points: list[tuple[int, float]], key_fn, lo_month: tuple[int, int], hi_month: tuple[int, int]
) -> dict[str, float]:
    """Sum values per month label; only include months in [lo_month, hi_month] inclusive (by label sort)."""
    lo_s = f"{lo_month[0]:04d}-{lo_month[1]:02d}"
    hi_s = f"{hi_month[0]:04d}-{hi_month[1]:02d}"
    out: dict[str, float] = {}
    for ts_ms, v in points:
        label = key_fn(ts_ms)
        if label < lo_s or label > hi_s:
            continue
        out[label] = out.get(label, 0.0) + float(v)
    return dict(sorted(out.items()))


def _month_sequence(lo: tuple[int, int], hi: tuple[int, int]) -> list[str]:
    y, m = lo
    end_y, end_m = hi
    out: list[str] = []
    while (y, m) <= (end_y, end_m):
        out.append(f"{y:04d}-{m:02d}")
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def _resolution_label(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        h = minutes // 60
        return f"{h}h" if h != 1 else "1h"
    return f"{minutes}m"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from-month", default="2026-01", metavar="YYYY-MM", help="First month (default 2026-01)")
    p.add_argument("--to-month", default="2026-04", metavar="YYYY-MM", help="Last month (default 2026-04)")
    p.add_argument(
        "--resolution-hours",
        type=int,
        default=None,
        help="Single resolution in hours (overrides --resolution-minutes to one bucket size)",
    )
    p.add_argument(
        "--resolution-minutes",
        type=int,
        nargs="+",
        default=None,
        metavar="M",
        help="SignalFlow resolution(s) in minutes (e.g. 60 10 for 1h and 10m). Default: 60 10",
    )
    p.add_argument("--realm", default=None, help="Splunk realm (default profile / SPLUNK_REALM / us0)")
    p.add_argument("--profile", default=None, help="Customer YAML profile path")
    args = p.parse_args()

    profile_path = lic.resolve_profile_path(args.profile)
    profile: dict[str, str] = {}
    if profile_path and os.path.isfile(profile_path):
        profile = lic.load_customer_profile_scalars(profile_path)

    token = (
        os.environ.get("SPLUNK_ACCESS_TOKEN")
        or profile.get("access_token")
        or profile.get("ACCESS_TOKEN")
        or ""
    ).strip()
    if not token:
        print("Set SPLUNK_ACCESS_TOKEN or access_token in profile.", file=sys.stderr)
        return 1

    realm = (args.realm or profile.get("realm") or os.environ.get("SPLUNK_REALM") or "us0").strip()
    stream_url = f"https://stream.{realm}.signalfx.com"

    try:
        start_ms, stop_ms, lo_m, hi_m = _union_query_window_ms(
            args.from_month, args.to_month, CENTRAL_TZ
        )
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1

    if args.resolution_hours is not None:
        res_minutes_list = [max(1, int(args.resolution_hours)) * 60]
    elif args.resolution_minutes is not None:
        res_minutes_list = [max(1, int(m)) for m in args.resolution_minutes]
    else:
        res_minutes_list = [60, 10]

    wall_s, read_to, max_pts = lic._license_signalflow_client_limits(start_ms, stop_ms)

    # Per resolution: (utc_by_month, ct_by_month)
    aggregates: dict[str, tuple[dict[str, float], dict[str, float]]] = {}
    for mins in res_minutes_list:
        resolution_ms = mins * 60 * 1000
        label = _resolution_label(mins)
        points, err = lic.execute_signalflow_time_series(
            stream_url=stream_url,
            token=token,
            program=RUM_USAGE_PROGRAM,
            start_ms=start_ms,
            stop_ms=stop_ms,
            resolution_ms=resolution_ms,
            wall_seconds=wall_s,
            read_timeout=read_to,
            max_points=max_pts,
        )
        if err:
            print(f"SignalFlow error ({label}): {err}", file=sys.stderr)
            return 1
        by_utc = _sum_by_month_key(points, month_key_utc, lo_m, hi_m)
        by_ct = _sum_by_month_key(points, month_key_central, lo_m, hi_m)
        aggregates[label] = (by_utc, by_ct)

    months = _month_sequence(lo_m, hi_m)
    res_labels = [_resolution_label(m) for m in res_minutes_list]

    print()
    print("## RUM sessions — monthly sum of bucket values (UTC vs America/Chicago)")
    print()
    print(f"- **Program:** `{RUM_USAGE_PROGRAM}`")
    print(f"- **Resolutions:** {', '.join(res_labels)} ({', '.join(str(m) + ' min' for m in res_minutes_list)})")
    print(f"- **Realm:** {realm}")
    print(
        f"- **Query window (ms):** `{start_ms}` → `{stop_ms}` "
        "(union of UTC and America/Chicago month ranges for the selected months)"
    )
    print(f"- **Rows:** calendar months **{months[0]}** through **{months[-1]}**")
    print()

    # Header: for each res, UTC | CT | ΔTZ
    head = "| Month |"
    sep = "| --- |"
    for lab in res_labels:
        head += f" UTC ({lab}) | CT ({lab}) | CT−UTC ({lab}) |"
        sep += " ---: | ---: | ---: |"
    pair_delta = len(res_labels) == 2
    if pair_delta:
        head += f" UTC Δ ({res_labels[1]}−{res_labels[0]}) | CT Δ ({res_labels[1]}−{res_labels[0]}) |"
        sep += " ---: | ---: |"
    print(head)
    print(sep)

    for m in months:
        row = f"| {m} |"
        for lab in res_labels:
            bu, bc = aggregates[lab]
            u = bu.get(m, 0.0)
            c = bc.get(m, 0.0)
            row += f" {u:,.2f} | {c:,.2f} | {c - u:+,.2f} |"
        if pair_delta:
            bu0, bc0 = aggregates[res_labels[0]]
            bu1, bc1 = aggregates[res_labels[1]]
            u_a, u_b = bu0.get(m, 0.0), bu1.get(m, 0.0)
            c_a, c_b = bc0.get(m, 0.0), bc1.get(m, 0.0)
            row += f" {u_b - u_a:+,.2f} | {c_b - c_a:+,.2f} |"
        print(row)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
