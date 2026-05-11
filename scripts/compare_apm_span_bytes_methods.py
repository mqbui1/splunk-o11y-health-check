#!/usr/bin/env python3
"""
Side-by-side APM trace volume (sf.org.apm.numSpanBytesReceived) for one or more UTC calendar months.

Examples:
  python3 compare_apm_span_bytes_methods.py --calendar-month 2026-04 --profile ../customer-profile.yaml
  python3 compare_apm_span_bytes_methods.py --from-month 2026-01 --to-month 2026-04 --profile ../customer-profile.yaml

Compares:
  - Chart Builder–aligned Mean(monthly) (extended stop + boundary month mapping)
  - Hourly rate + scale(60): **mean** of all bucket values in the month (not Σ v×Δt)

Optional third row (single month only): daily mean of points.

Requires access_token (or SPLUNK_ACCESS_TOKEN) and realm in a customer profile or env.
"""

from __future__ import annotations

import argparse
import calendar
import os
import sys

import o11y_license_utilization as lic


def _load_profile(path: str) -> dict[str, str]:
    if not os.path.isfile(path):
        print(f"Profile not found: {path}", file=sys.stderr)
        sys.exit(1)
    return lic.load_customer_profile_scalars(path)


def _iter_months_inclusive(start_ym: str, end_ym: str) -> list[str]:
    p0 = start_ym.split("-")
    p1 = end_ym.split("-")
    if len(p0) != 2 or len(p1) != 2:
        raise ValueError("Use YYYY-MM for range bounds")
    y1, m1 = int(p0[0]), int(p0[1])
    y2, m2 = int(p1[0]), int(p1[1])
    if (y1, m1) > (y2, m2):
        raise ValueError("from-month must be <= to-month")
    out: list[str] = []
    cy, cm = y1, m1
    while (cy, cm) <= (y2, m2):
        out.append(f"{cy:04d}-{cm:02d}")
        cm += 1
        if cm > 12:
            cm = 1
            cy += 1
    return out


def _mib(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x / (1024 * 1024):.4f}"


def run_one_month(
    *,
    target: str,
    profile: dict[str, str],
    token: str,
    realm: str,
    stream_url: str,
    plat: str,
    hourly: str,
    cycle_res: int,
    wall_s: float,
    read_to: float,
    max_pts: int,
    include_daily: bool,
) -> dict[str, object]:
    parts = target.split("-")
    y, mo = int(parts[0]), int(parts[1])
    start_d = f"{y:04d}-{mo:02d}-01"
    last_d = calendar.monthrange(y, mo)[1]
    end_d = f"{y:04d}-{mo:02d}-{last_d:02d}"
    start_ms = lic.utc_start_of_day_ms(start_d)
    stop_ms = lic.utc_end_of_day_ms_inclusive(end_d)
    usage_stop_ext = lic.extend_stop_ms_for_splunk_monthly_cycle(stop_ms)

    def run(program: str, stop: int, res_ms: int):
        return lic.execute_signalflow_time_series(
            stream_url=stream_url,
            token=token,
            program=program,
            start_ms=start_ms,
            stop_ms=stop,
            resolution_ms=res_ms,
            wall_seconds=wall_s,
            read_timeout=read_to,
            max_points=max_pts,
        )

    pts_plat, err_plat = run(plat, usage_stop_ext, cycle_res)
    pts_h, err_h = run(hourly, stop_ms, 3600 * 1000)

    v_plat = v_hourly = v_daily = None
    err_d = None
    n_h = n_d = 0

    if not err_plat:
        bym = lic.monthly_usage_from_platform_cycle_mean(pts_plat, scale=1.0)
        v_plat = bym.get(target)

    if not err_h:
        byh = lic.monthly_mean_of_point_values_by_utc_month(pts_h)
        v_hourly = byh.get(target)
        n_h = sum(1 for ts, _ in pts_h if lic.month_key_utc(ts) == target)

    if include_daily:
        pts_d, err_d = run(hourly, stop_ms, 24 * 3600 * 1000)
        if not err_d:
            byd = lic.monthly_mean_of_point_values_by_utc_month(pts_d)
            v_daily = byd.get(target)
            n_d = sum(1 for ts, _ in pts_d if lic.month_key_utc(ts) == target)

    return {
        "target": target,
        "window": f"{start_d}..{end_d}",
        "v_plat": v_plat,
        "v_hourly": v_hourly,
        "v_daily": v_daily,
        "err_plat": err_plat,
        "err_h": err_h,
        "err_d": err_d,
        "n_plat": len(pts_plat),
        "n_h": n_h,
        "n_d": n_d,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--calendar-month", metavar="YYYY-MM", help="Single UTC month")
    g.add_argument(
        "--from-month",
        metavar="YYYY-MM",
        help="Start month (use with --to-month)",
    )
    p.add_argument("--to-month", metavar="YYYY-MM", help="End month inclusive (use with --from-month)")
    p.add_argument(
        "--profile",
        default=None,
        help="customer-profile.yaml path (default: repo root customer-profile.yaml)",
    )
    p.add_argument(
        "--no-daily",
        action="store_true",
        help="For single --calendar-month, omit the daily mean row (default: show A/B/C)",
    )
    args = p.parse_args()

    if args.from_month and not args.to_month:
        p.error("--from-month requires --to-month")
    if args.to_month and not args.from_month:
        p.error("--to-month requires --from-month")

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    profile_path = args.profile or os.path.join(root, "customer-profile.yaml")
    profile = _load_profile(profile_path)

    token = (
        (profile.get("access_token") or profile.get("ACCESS_TOKEN") or "").strip()
        or (os.environ.get("SPLUNK_ACCESS_TOKEN") or "").strip()
    )
    realm = (profile.get("realm") or os.environ.get("SPLUNK_REALM") or "us0").strip()
    if not token:
        print("Set access_token in profile or SPLUNK_ACCESS_TOKEN.", file=sys.stderr)
        return 1

    if args.calendar_month:
        months = [args.calendar_month.strip()]
        include_daily = not args.no_daily
    else:
        try:
            months = _iter_months_inclusive(args.from_month.strip(), args.to_month.strip())
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        include_daily = False

    stream_url = f"https://stream.{realm}.signalfx.com"
    cycle_res = lic.apm_span_platform_cycle_resolution_ms(profile)

    plat = (
        "data('sf.org.apm.numSpanBytesReceived', rollup='rate').scale(60)"
        ".mean(cycle='month', cycle_start='1d', partial_values=False).publish(label='usage')"
    )
    hourly = (
        "data('sf.org.apm.numSpanBytesReceived', rollup='rate').scale(60).mean().publish(label='usage')"
    )

    print(f"realm={realm}  months={', '.join(months)}")
    print(f"cycle execute resolution: {cycle_res // 1000 // 3600}h\n")

    results: list[dict[str, object]] = []
    for m in months:
        # wall/max scale with this month's span
        parts = m.split("-")
        y, mo = int(parts[0]), int(parts[1])
        start_d = f"{y:04d}-{mo:02d}-01"
        last_d = calendar.monthrange(y, mo)[1]
        end_d = f"{y:04d}-{mo:02d}-{last_d:02d}"
        start_ms = lic.utc_start_of_day_ms(start_d)
        stop_ms = lic.utc_end_of_day_ms_inclusive(end_d)
        usage_stop_ext = lic.extend_stop_ms_for_splunk_monthly_cycle(stop_ms)
        wall_s, read_to, max_pts = lic._license_signalflow_client_limits(start_ms, usage_stop_ext)
        max_pts = min(250_000, max(max_pts, 12_000))

        results.append(
            run_one_month(
                target=m,
                profile=profile,
                token=token,
                realm=realm,
                stream_url=stream_url,
                plat=plat,
                hourly=hourly,
                cycle_res=cycle_res,
                wall_s=wall_s,
                read_to=read_to,
                max_pts=max_pts,
                include_daily=include_daily,
            )
        )

    if len(months) > 1:
        print(
            f"{'Month':<10} | {'Platform cycle':>18} | {'Hourly mean':>18} | {'H/P ratio':>10} | "
            f"{'MiB plat':>10} | {'MiB h':>10}"
        )
        print("-" * 88)
        for r in results:
            vp = r["v_plat"]
            vh = r["v_hourly"]
            ep, eh = r["err_plat"], r["err_h"]
            if ep or eh:
                err = (ep or eh) if isinstance((ep or eh), str) else ""
                print(f"{r['target']:<10} | {'ERR':>18} | {'ERR':>18} | {'—':>10} | {'—':>10} | {'—':>10}  {err[:40]}")
                continue
            ratio = (vh / vp) if isinstance(vp, (int, float)) and vp else None
            rs = f"{ratio:.6f}" if isinstance(ratio, (int, float)) and ratio == ratio else "—"
            print(
                f"{r['target']:<10} | {float(vp):>18.4f} | {float(vh):>18.4f} | {rs:>10} | "
                f"{_mib(float(vp) if vp is not None else None):>10} | {_mib(float(vh) if vh is not None else None):>10}"
            )
        return 0

    # Single month: detailed 2–3 row table (original style)
    r = results[0]
    target = r["target"]
    print(f"month={target}  window {r['window']}")
    print(f"platform usage stop: extended +3d past inclusive end")
    print(f"cycle execute resolution: {cycle_res // 1000 // 3600}h\n")

    w = 52
    rows: list[tuple[str, str, float | None]] = []
    if r["err_plat"]:
        rows.append(("A. Mean(cycle=month) + extended stop (license default)", str(r["err_plat"])[:40], None))
    else:
        rows.append(
            (
                "A. Mean(cycle=month) + extended stop (license default)",
                f"points={r['n_plat']}",
                float(r["v_plat"]) if r["v_plat"] is not None else None,
            )
        )
    if r["err_h"]:
        rows.append(("B. Hourly mean of points (rate+scale60+mean)", str(r["err_h"])[:40], None))
    else:
        rows.append(
            (
                "B. Hourly mean of points (rate+scale60+mean)",
                f"points={r['n_h']} in {target}",
                float(r["v_hourly"]) if r["v_hourly"] is not None else None,
            )
        )
    if include_daily:
        if r["err_d"]:
            rows.append(("C. Daily mean of points", str(r["err_d"])[:40], None))
        else:
            rows.append(
                (
                    "C. Daily mean of points (rate+scale60+mean)",
                    f"points={r['n_d']} in {target}",
                    float(r["v_daily"]) if r["v_daily"] is not None else None,
                )
            )

    col = f"bytes ({target})"
    print(f"{'Method':<{w}} | {'detail':<22} | {col:>28} | MiB")
    print("-" * (w + 22 + 28 + 10))
    for name, detail, val in rows:
        if val is None:
            print(f"{name:<{w}} | {detail:<22} | {'—':>28} | —")
        else:
            print(f"{name:<{w}} | {detail:<22} | {val:>28.6f} | {_mib(val)}")

    base = rows[0][2]
    if isinstance(base, (int, float)) and base:
        print("\nRatios vs row A (platform cycle):")
        for name, _, val in rows[1:]:
            if isinstance(val, (int, float)) and val:
                print(f"  {name.split('.')[0]}: {val / base:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
