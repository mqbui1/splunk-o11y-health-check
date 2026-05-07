#!/usr/bin/env python3
"""
Run Splunk Observability health-check scripts in sequence and merge outcomes into one markdown
report aligned with ``Splunk-Observability-Health-Check.md`` (section order from that document).

Currently runs:
  - ``o11y_license_utilization.py`` → License utilization
  - ``o11y_platform_engagement_trends.py`` → Platform engagement (``sf.org`` trends; rolling 7d vs ~6mo for most KPIs; RUM/Synthetics monthly when entitled; APM/custom-metrics gated on license snapshot)
  - ``o11y_im_metrics_usage_breakdown.py`` → IM metrics table (Usage analytics)
  - ``o11y_im_integrations.py`` → IM integrations list (``GET /v2/integration``)
  - ``o11y_detectors_health_check.py`` → Detectors (API inventory, events, muting heuristics, optional inactive MTS sampling)
  - ``o11y_dashboards_health_check.py`` → Dashboards (API: dashboard groups, charts, detector links, duplicate names)
  - ``o11y_synthetics_health_check.py`` → Synthetics (API: ``GET /v2/synthetics/tests``, detector program scan)
  - ``o11y_token_health_check.py`` → Token health (``GET /v2/token``)
  - ``o11y_otel_collectors_health_check.py`` → OpenTelemetry Collectors (SignalFlow on ``otelcol_process_uptime``; optional GitHub ``splunk-otel-collector`` releases for Splunk-distro depreciation dates)
  - ``o11y_apm_health_check.py`` → APM health checks (default: full ``--checks all`` + ``--trace-checks``)
  - ``o11y_rum_health_check.py`` → RUM health checks (SignalFlow on ``sf.org`` RUM session / MMS metrics)

Other domains without a script still emit *None — check not executed.* per ``AGENTS.md``.

**Optional exports** (install ``requirements-presentation.txt``): ``--pptx-out`` writes an executive
``.pptx`` (highlights + sample tables) using the **dark** Splunk master in ``splunk-ppt-template/`` by default;
``--pptx-light`` or ``health_check_pptx_theme: light`` selects the light master. ``--pdf-out`` renders the
full markdown through ``web/o11y-health-report`` with headless Chromium.

**HTML viewer:** serve ``web/o11y-health-report/`` over HTTP. Auto-load uses ``report.md`` next to
``index.html`` (repo default is a demo file) or ``?report=../reports/…``; otherwise pass ``--out
web/o11y-health-report/report.md`` or drag/drop the generated ``reports/<title>.md``.

Usage (from repo root — uses ``customer-profile.yaml`` by default):

  python3 scripts/o11y_health_check_run.py

Writes ``reports/<report_title>.md`` (title from YAML; folder from ``health_check_output_dir``).
``access_token`` and ``realm`` are read from the profile (env ``SPLUNK_ACCESS_TOKEN`` overrides token).

Optional overrides: ``--out PATH``, ``--profile PATH``, ``--skip-license``, ``--skip-platform-engagement``, ``--skip-im``, ``--skip-detectors``, ``--skip-dashboards``, ``--skip-apm``, ``--skip-rum``, ``--skip-synthetics``, ``--skip-tokens``, ``--skip-otel-collectors``, ``--skip-inactive-mts``, etc.

Environment: ``SPLUNK_ACCESS_TOKEN`` overrides profile ``access_token``; child scripts receive ``--profile``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("o11y_health_check_run")

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_apm_health_check import render_apm_checks_markdown  # noqa: E402
from o11y_platform_engagement_trends import render_platform_engagement_markdown  # noqa: E402
from o11y_rum_health_check import render_rum_checks_markdown  # noqa: E402
from o11y_im_metrics_usage_breakdown import render_im_checks_markdown  # noqa: E402
from o11y_detectors_health_check import render_detectors_checks_markdown  # noqa: E402
from o11y_dashboards_health_check import render_dashboards_checks_markdown  # noqa: E402
from o11y_synthetics_health_check import render_synthetics_checks_markdown  # noqa: E402
from o11y_token_health_check import render_token_checks_markdown  # noqa: E402
from o11y_otel_collectors_health_check import render_otel_collectors_markdown  # noqa: E402
from o11y_license_utilization import (  # noqa: E402
    build_license_entitlement_utilization_chart_md,
    drop_im_hosts_if_zero_subscription,
    filter_license_rows_for_subscription_report,
    filter_monthly_to_complete_months,
    filter_rows_by_active_license_models,
    format_license_utilization_window_markdown,
    load_customer_profile_scalars,
    sort_license_rows_for_product,
    sort_monthly_rows_oldest_first,
)
from o11y_presentation_util import (  # noqa: E402
    default_splunk_pptx_template_path,
    parse_profile_aux_path,
    resolve_repo_relative_path,
)


def resolve_profile_path_repo(repo_root: Path, explicit: str | None) -> str | None:
    """Prefer ``customer-profile.local.yaml``, then ``customer-profile.yaml`` under repo root."""
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return str(p.resolve())
        return None
    for name in ("customer-profile.local.yaml", "customer-profile.yaml"):
        candidate = repo_root / name
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def sanitize_report_filename(title: str) -> str:
    """Filesystem-safe basename stem from report title."""
    bad = '<>:"/\\|?*'
    out = "".join(c if c not in bad else "-" for c in title)
    out = " ".join(out.split())
    out = out.strip().strip(".")
    if not out:
        out = "health-check-report"
    return out[:120]


def profile_int(raw: str | None, default: int) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        return default


def profile_bool(raw: str | None, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("true", "1", "yes", "y", "on")


def profile_float(raw: str | None, default: float) -> float:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except ValueError:
        return default


def default_report_output_path(repo_root: Path, profile: dict[str, str]) -> Path:
    title = (
        profile.get("report_title")
        or profile.get("company_or_purpose")
        or "Splunk Observability Health Check"
    )
    rel = (profile.get("health_check_output_dir") or "reports").strip()
    if not rel or ".." in rel or Path(rel).is_absolute():
        rel = "reports"
    out_dir = (repo_root / rel).resolve()
    safe = sanitize_report_filename(title)
    return out_dir / f"{safe}.md"


def default_json_output_path(md_path: Path) -> Path:
    return md_path.with_suffix(".json")


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def setup_logging(*, verbose: bool) -> None:
    """Log to stderr; safe to call more than once (e.g. after re-parse)."""
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
        logger.propagate = False
    for h in logger.handlers:
        h.setLevel(level)


def _truncate(s: str, max_len: int) -> str:
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def _fmt_duration_s(seconds: float) -> str:
    """Human-readable duration for logs (e.g. 12.34s or 1m 5.20s)."""
    if seconds < 0:
        return "0.00s"
    if seconds >= 60:
        m, s = divmod(seconds, 60.0)
        return f"{int(m)}m {s:.2f}s"
    return f"{seconds:.2f}s"


def _emit_hub_progress(payload: dict) -> None:
    """Machine-readable checkpoints for Health Check Hub progress UI (``O11Y_HUB_PROGRESS`` env)."""
    if not os.environ.get("O11Y_HUB_PROGRESS"):
        return
    try:
        logger.info("[HUB_PROGRESS] %s", json.dumps(payload, separators=(",", ":")))
    except Exception:
        pass


def _log_auth_resolution(
    *,
    profile_path: str,
    profile: dict[str, str],
    realm_effective: str,
    cli_realm: str | None,
) -> None:
    """
    Log where token and realm come from (no secret material). Helps debug HTTP 401:
    SPLUNK_ACCESS_TOKEN in the environment overrides profile tokens — a stale env value
    often causes 401 while the profile looks correct.
    """
    env_tok = (os.environ.get("SPLUNK_ACCESS_TOKEN") or "").strip()
    if env_tok:
        logger.info(
            "Auth: token source = SPLUNK_ACCESS_TOKEN (environment), length = %s — "
            "this overrides access_token in the profile; unset the env var to use the profile",
            len(env_tok),
        )
    else:
        ak = "access_token" if (profile.get("access_token") or "").strip() else None
        ak = ak or ("ACCESS_TOKEN" if (profile.get("ACCESS_TOKEN") or "").strip() else None)
        pt = (
            (profile.get("access_token") or profile.get("ACCESS_TOKEN") or "").strip()
        )
        if pt and ak:
            logger.info(
                "Auth: token source = profile %s (%s), length = %s",
                ak,
                profile_path,
                len(pt),
            )
        else:
            logger.error(
                "Auth: no token — set SPLUNK_ACCESS_TOKEN or access_token / ACCESS_TOKEN in %s",
                profile_path,
            )

    cli_r = (cli_realm or "").strip()
    prof_r = (profile.get("realm") or "").strip()
    env_r = (os.environ.get("SPLUNK_REALM") or "").strip()
    if cli_r:
        r_src = "--realm CLI"
    elif prof_r:
        r_src = "profile key `realm`"
    elif env_r:
        r_src = "SPLUNK_REALM environment"
    else:
        r_src = "default (us0)"
    logger.info("Auth: realm = %s (source: %s)", realm_effective, r_src)


def _run_child_script(
    argv: list[str],
    *,
    cwd: Path,
    step_label: str,
) -> int:
    """
    Run a child script with captured output. Logs progress and errors to stderr.
    Returns the child process exit code.
    """
    cmd_display = f"{sys.executable} {' '.join(shlex.quote(a) for a in argv)}"
    logger.info("▶ %s — starting", step_label)
    logger.debug("Command: %s", cmd_display)

    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [sys.executable, *argv],
            cwd=str(cwd),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=None,
        )
    except OSError as e:
        elapsed = time.monotonic() - t0
        logger.exception(
            "▶ %s — could not start subprocess after %s: %s",
            step_label,
            _fmt_duration_s(elapsed),
            e,
        )
        return 127

    elapsed = time.monotonic() - t0
    rc = int(r.returncode)
    err = (r.stderr or "").strip()
    out = (r.stdout or "").strip()

    if rc == 0:
        logger.info(
            "▶ %s — finished OK (exit 0) in %s",
            step_label,
            _fmt_duration_s(elapsed),
        )
        if err:
            for line in err.splitlines()[:30]:
                logger.info("    %s", line)
        if out:
            logger.debug("stdout: %s", _truncate(out, 800))
    else:
        logger.error(
            "▶ %s — finished with errors (exit %s) in %s",
            step_label,
            rc,
            _fmt_duration_s(elapsed),
        )
        if err:
            logger.error("    stderr:\n%s", _truncate(err, 4000))
        if out:
            logger.error("    stdout:\n%s", _truncate(out, 2000))

    return rc


def load_json_report(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """
    Load JSON written by a child script. Returns (data, error_reason).
    Detects HTML error pages (common when auth fails or an endpoint returns HTML).
    """
    if not path.is_file():
        return None, "output file was not created"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"cannot read file: {e}"

    raw_stripped = raw.strip()
    if not raw_stripped:
        return None, "output file is empty"

    head = raw_stripped[:200].lower()
    if raw_stripped.startswith("<") or "<!doctype html" in head or "<html" in head:
        return (
            None,
            "file contains HTML, not JSON — often an auth error, wrong realm, or proxy/login page; "
            "check token, realm, and network",
        )

    try:
        data = json.loads(raw_stripped)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON ({e}); start of file: {_truncate(raw_stripped, 200)!r}"

    if not isinstance(data, dict):
        return None, f"expected JSON object at root, got {type(data).__name__}"
    return data, None


def _product_order(rows: list[dict[str, Any]]) -> list[str]:
    order: list[str] = []
    for r in rows:
        p = str(r.get("product") or "")
        if p and p not in order:
            order.append(p)
    return order


def _severity_legend_markdown() -> str:
    return (
        "## Severity legend\n\n"
        "| Level | Meaning |\n"
        "| --- | --- |\n"
        "| Green | Healthy or normal |\n"
        "| Yellow | Warning or lower criticality |\n"
        "| Red | Critical or immediate attention |\n\n"
    )


def _section_automation_missing_md(title: str) -> str:
    """Short body when a checklist section was requested but no JSON/report was produced."""
    return f"{title}\n\n*Automation did not produce data for this section; see runner log.*\n\n"


def _render_license_capacity(report: dict[str, Any]) -> str:
    """License utilization: grouped by product; monthly UTC charts (viewer: usage bars vs subscription line)."""
    rows: list[dict[str, Any]] = list(report.get("rows") or [])
    # Legacy JSON may omit ``license_model_filter``; re-apply APM host vs TAPM filter for display.
    if not (report.get("license_model_filter") or {}).get("excluded_keys"):
        rows, _ = filter_rows_by_active_license_models(rows)
    rows = drop_im_hosts_if_zero_subscription(rows)
    rows_display = filter_license_rows_for_subscription_report(rows)

    stop_ms = int(report.get("stop_ms") or 0)
    if stop_ms <= 0:
        stop_ms = int(time.time() * 1000)

    lines = [
        "## License utilization",
        "",
        format_license_utilization_window_markdown(report),
        "",
        "- **Bars** = usage by month, with utilization %.",
        "- **Line** = subscription allowance.",
        "",
        "### Severity legend",
        "",
        "| Color | Criteria |",
        "| --- | --- |",
        "| Green | Utilization from 40% to 85% |",
        "| Yellow | Utilization less than 40% |",
        "| Orange | Utilization greater than 85% and up to 100% |",
        "| Red | Utilization greater than 100% |",
        "",
    ]

    if not rows:
        lines += ["*No entitlement rows in license snapshot.*", ""]
    elif not rows_display:
        lines += [
            "*No product families with a subscription allowance — unlicensed or zero-subscription products are omitted.*",
            "",
        ]
    else:
        rows_by_product: dict[str, list[dict[str, Any]]] = {}
        for r in rows_display:
            p = str(r.get("product") or "—")
            rows_by_product.setdefault(p, []).append(r)

        for product in _product_order(rows_display):
            lines.append(f"### {_md_cell(product)}")
            lines.append("")
            for r in sort_license_rows_for_product(rows_by_product.get(product, []), product):
                label = _md_cell(str(r.get("label") or r.get("key") or "—"))
                lines.append(f"#### Entitlement: {label}")
                lines.append("")
                if r.get("notes"):
                    lines.append(f"*{_md_cell(str(r['notes']))}*")
                    lines.append("")

                if r.get("usage_error"):
                    lines.append(
                        f"*Usage series unavailable:* `{_md_cell(str(r['usage_error'])[:300])}`"
                    )
                    lines.append("")
                if r.get("subscription_error"):
                    lines.append(
                        f"*Subscription series unavailable:* `{_md_cell(str(r['subscription_error'])[:300])}`"
                    )
                    lines.append("")

                fw = (report.get("fixed_window_utc") or {}) if isinstance(report.get("fixed_window_utc"), dict) else {}
                fw_end = str(fw.get("endDate") or "").strip() or None
                monthly = filter_monthly_to_complete_months(
                    list(r.get("monthly") or []),
                    stop_ms,
                    fixed_window_end_date=fw_end,
                )
                monthly_chart = sort_monthly_rows_oldest_first(monthly)
                if monthly_chart:
                    lines.extend(
                        build_license_entitlement_utilization_chart_md(
                            r,
                            monthly_chart,
                        )
                    )
                else:
                    lines.append(
                        "*No monthly samples in lookback* (usage/subscription series may have failed or "
                        "window too short)."
                    )
                lines.append("")
                lines.append("#### Recommendation")
                lines.append("")
                lines.append(
                    "Review utilization trends and subscription alignment for this entitlement; adjust capacity or "
                    "workloads as needed."
                )
                lines.append("")

    return "\n".join(lines) + "\n"


def _not_executed_test(
    title: str,
    table_header: str,
    table_sep: str,
    *,
    description: str = "",
) -> str:
    parts: list[str] = [f"### {title}\n\n"]
    if description.strip():
        parts.append(description.strip() + "\n\n")
    parts.extend(
        [
            "### Results\n\n",
            f"{table_header}\n{table_sep}\n\n",
            "### Recommendation\n\n",
            "*None — check not executed.*\n\n",
        ]
    )
    return "".join(parts)


def _placeholder_platform_engagement() -> str:
    return (
        "## Platform engagement\n\n"
        "### Engagement Trends\n\n"
        "Trend comparison of org-level usage signals over the last ~6 months (see checklist).\n\n"
        "### Results\n\n"
        "*None — check not executed.*\n\n"
        "### User Analysis\n\n"
        "### Results\n\n"
        "| Metric | Value |\n"
        "| --- | --- |\n"
        "| Directory / login analytics | *Not collected* |\n\n"
        "### Recommendation\n\n"
        "*None — check not executed.* Use Splunk Observability **administration / usage** views for login activity.\n\n"
    )


def _placeholder_dashboards() -> str:
    tests: list[tuple[str, str, str, str]] = [
        (
            "Links to Deleted/Inactive Detectors",
            "List of charts that have a link to a non-existing detector or an inactive detector.",
            "| Color | Dashboard Group | Dashboard Name | Chart Name | Detector Link |",
            "| --- | --- | --- | --- | --- |",
        ),
        (
            "Inactive Charts",
            "List of charts that are using inactive metrics.",
            "| Color | Dashboard Group | Dashboard Name | Chart Name |",
            "| --- | --- | --- | --- |",
        ),
        (
            "Duplicate Dashboards",
            "Teams may often clone dashboards. This can lead to many identical dashboards across the organization.",
            "| Color | Dashboard Group | Dashboard Name | Duplicate Dashboard IDs |",
            "| --- | --- | --- | --- |",
        ),
    ]
    body = "".join(_not_executed_test(title, h, s, description=desc) for title, desc, h, s in tests)
    return "## Dashboards health checks\n\n" + body


def _placeholder_rum() -> str:
    tests: list[tuple[str, str, str, str]] = [
        (
            "Volume by Application",
            "List of applications by total number of sessions.",
            "| Color | Application Name | Number of Sessions | % Utilization of License |",
            "| --- | --- | --- | --- |",
        ),
        (
            "Filter Synthetic/Bot traffic",
            "Ensure crawler and bot sessions aren't being ingested as real user sessions.",
            "| Color | Application Name | IP Addresses likely to be bots or crawlers |",
            "| --- | --- | --- |",
        ),
        (
            "Review Custom Events",
            "Are teams sending excessive custom RUM events that provide little analytical value. Compare this against your RUM session volume entitlement.",
            "| Color | Application Name | Custom Events | Cardinality of Event |",
            "| --- | --- | --- | --- |",
        ),
        (
            "RUM Troubleshooting Metrics Sets (TMS) Usage Analysis",
            "Provide a list of TMS by license usage %.",
            "| Color | Application Name | TMS Name | TMS Cardinality | % of License |",
            "| --- | --- | --- | --- | --- |",
        ),
        (
            "RUM Monitoring Metrics Sets (MMS) Usage Analysis",
            "Provide a list of MMS by license usage %, number of services, endpoints enabled.",
            "| Color | Application Name | MMS Name | MMS Cardinality | % of License |",
            "| --- | --- | --- | --- | --- |",
        ),
    ]
    body = "".join(_not_executed_test(title, h, s, description=desc) for title, desc, h, s in tests)
    return "## Real User Monitoring (RUM) health checks\n\n" + body


def _placeholder_synthetics() -> str:
    tests: list[tuple[str, str, str, str]] = [
        (
            "Test Usage Analysis",
            "Detailed list of Synthetics tests.",
            "| Test Name | Test Type | Frequency | # Locations | Round Robin (Y/N) | Total Runs per Month | % Utilization of License |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ),
        (
            "Failing Tests",
            "Active tests that have a failure rate above 30% for the last 7 days. These may be tests that are "
            "running against old URLs, deprecated APIs, or retired services that for some reason are still enabled.",
            "| Color | Test Name | Test Type | Frequency | Failure Rate % (7D) |",
            "| --- | --- | --- | --- | --- |",
        ),
        (
            "Disabled Tests",
            "List of tests that are disabled (not actively running).",
            "| Color | Test Name | Test Type | Last Run Date |",
            "| --- | --- | --- | --- |",
        ),
        (
            "Similar Tests",
            "List of tests that may be doing similar things, or could be duplicates (same or overlapping targets).",
            "| Color | Test Name | Test Type | List of Similar Test Names |",
            "| --- | --- | --- | --- |",
        ),
        (
            "Tests with No Detectors",
            "List of tests that have no associated detector.",
            "| Color | Test Name | Test Type | Frequency |",
            "| --- | --- | --- | --- |",
        ),
    ]
    body = "".join(_not_executed_test(title, h, s, description=desc) for title, desc, h, s in tests)
    return "## Synthetics health check\n\n" + body


def _placeholder_tokens() -> str:
    tests: list[tuple[str, str, str, str]] = [
        ("Expired Tokens", "List of expired tokens.", "| Token Name | Token Type | Expired Date |", "| --- | --- | --- |"),
        ("Near Expiration Tokens", "List of tokens within 90 days of expiration.", "| Token Name | Token Type | Expiration Date |", "| --- | --- | --- |"),
    ]
    body = "".join(_not_executed_test(title, h, s, description=desc) for title, desc, h, s in tests)
    return "## Token health check\n\n" + body


def _placeholder_otel_collectors() -> str:
    title = "List of Collectors by version"
    desc = (
        "List of deployed OpenTelemetry Collectors and their version.\n\n"
        "Yellow – Collector version 30 – 90 days from deprecation/support  \n"
        "Red – Collector version less than 30 days deprecation/support"
    )
    header = "| Color | Host Name | Host ID | Deployment context | OTel Collector Name | Version | Depreciation Date |"
    sep = "| --- | --- | --- | --- | --- | --- | --- |"
    body = _not_executed_test(title, header, sep, description=desc)
    return "## OpenTelemetry Collectors\n\n" + body


def _apm_any_result_rows(apm_report: dict[str, Any]) -> bool:
    checks = apm_report.get("checks") or {}
    for block in checks.values():
        if isinstance(block, dict) and (block.get("rows") or []):
            return True
    return False


def _rum_any_automated_rows(rum_report: dict[str, Any]) -> bool:
    """True if RUM script produced volume or MMS rows (SignalFlow-backed tables)."""
    chk = rum_report.get("checks") or {}
    vol = (chk.get("volumeByApplication") or {}).get("rows") or []
    mms = (chk.get("rumMms") or {}).get("rows") or []
    return bool(vol or mms)


def _license_any_utilization(rows: list[dict[str, Any]]) -> bool:
    """True if at least one entitlement has a computed window % or monthly rows."""
    for r in rows:
        if r.get("utilization_pct") is not None:
            return True
        for m in r.get("monthly") or []:
            if m.get("utilization_pct") is not None:
                return True
    return False


def _executive_summary(
    *,
    license_report: dict[str, Any] | None,
    platform_engagement_report: dict[str, Any] | None,
    im_report: dict[str, Any] | None,
    im_integrations_report: dict[str, Any] | None,
    detectors_report: dict[str, Any] | None,
    dashboards_report: dict[str, Any] | None,
    apm_report: dict[str, Any] | None,
    rum_report: dict[str, Any] | None,
    synthetics_report: dict[str, Any] | None,
    token_report: dict[str, Any] | None,
    otel_report: dict[str, Any] | None,
    scope_parts: list[str],
    include_license: bool,
    include_platform_engagement: bool,
    include_im: bool,
    include_detectors: bool,
    include_dashboards: bool,
    include_apm: bool,
    include_rum: bool,
    include_synthetics: bool,
    include_tokens: bool,
    include_otel_collectors: bool,
) -> str:
    bullets: list[str] = []
    if include_license and license_report:
        rows = license_report.get("rows") or []
        rows_display = filter_license_rows_for_subscription_report(rows)
        reds = sum(1 for r in rows_display if r.get("severity") == "Red")
        oranges = sum(1 for r in rows_display if r.get("severity") == "Orange")
        yellows = sum(1 for r in rows_display if r.get("severity") == "Yellow")
        if rows_display:
            bullets.append(
                f"License utilization: **{reds}** Red, **{oranges}** Orange, **{yellows}** Yellow entitlement rows "
                "(by utilization band)."
            )
            if not _license_any_utilization(rows_display):
                bullets.append(
                    "**License data:** no utilization % computed — SignalFlow usage or subscription series "
                    "did not return for these entitlements (see **License utilization** charts). Verify token "
                    "Stream ingest scope, `realm`, and that `sf.org.*` metrics exist for the org."
                )
        elif rows:
            bullets.append(
                "License utilization: **no** subscription allowance on any product family — unlicensed products "
                "omitted from license charts."
            )
        else:
            bullets.append("License utilization: no entitlement rows produced (verify SignalFlow access).")
    elif include_license:
        bullets.append(
            "**License utilization:** automation did not produce entitlement data (see runner log); "
            "no **License utilization** section in this report."
        )
    if include_platform_engagement:
        if platform_engagement_report is not None:
            if platform_engagement_report.get("error"):
                bullets.append("**Platform engagement:** trends automation did not complete (see that section).")
            else:
                nk = len(platform_engagement_report.get("kpis") or [])
                bullets.append(
                    f"**Platform engagement:** **{nk}** KPI trend series (``sf.org`` org counters; rolling windows "
                    "for most; RUM/Synthetics **calendar-month** pair when entitled; APM apps / custom metrics only "
                    "when licensed per snapshot)."
                )
        else:
            bullets.append(
                "**Platform engagement:** automation did not produce engagement data (see runner log)."
            )
    if include_im:
        if im_report is not None:
            im_err = im_report.get("error")
            im_n = len(im_report.get("metrics") or [])
            if im_err or im_n == 0:
                bullets.append(
                    "**Infrastructure monitoring (metrics):** Usage analytics table did not load or returned no rows "
                    "(see IM section)."
                )
            else:
                bullets.append(
                    f"**Infrastructure monitoring (metrics):** Usage analytics returned **{im_n}** metric row(s) "
                    "(average hourly MTS)."
                )
        else:
            bullets.append(
                "**Infrastructure monitoring (metrics):** automation did not produce Usage analytics output."
            )
        if im_integrations_report is not None:
            ie = im_integrations_report.get("error")
            in_n = len(im_integrations_report.get("integrations") or [])
            if ie or in_n == 0:
                bullets.append(
                    "**Infrastructure monitoring (integrations):** integration list did not load or returned no rows."
                )
            else:
                bullets.append(
                    f"**Infrastructure monitoring (integrations):** **{in_n}** integration(s) listed (sorted by type)."
                )
        else:
            bullets.append(
                "**Infrastructure monitoring (integrations):** automation did not produce integration inventory."
            )
    if include_detectors:
        if detectors_report is not None:
            if detectors_report.get("error"):
                bullets.append("**Detectors:** automation did not complete (see Detectors section).")
            else:
                da = detectors_report.get("detectorsAnalyzed")
                dt = detectors_report.get("detectorListTotal")
                bullets.append(
                    f"**Detectors:** analyzed **{da}** of **{dt}** listed detectors "
                    "(heuristic severities; confirm in UI)."
                )
        else:
            bullets.append("**Detectors:** automation did not produce detector inventory data.")
    if include_dashboards:
        if dashboards_report is not None:
            if dashboards_report.get("error"):
                bullets.append("**Dashboards:** automation did not complete (see Dashboards section).")
            else:
                da = dashboards_report.get("dashboardsAnalyzed")
                dt = dashboards_report.get("dashboardListTotal")
                bullets.append(
                    f"**Dashboards:** analyzed **{da}** of **{dt}** listed dashboards "
                    "(chart/detector heuristics; confirm in UI)."
                )
        else:
            bullets.append("**Dashboards:** automation did not produce dashboard inventory data.")
    if include_apm:
        if apm_report:
            bullets.append(
                f"APM: snapshot over **{apm_report.get('window_hours', '—')}** hour(s); "
                "review domain subsections for Yellow/Red rows."
            )
            if not _apm_any_result_rows(apm_report):
                bullets.append(
                    "**APM data:** result tables are empty or findings-only — `spans.count` / topology may have "
                    "no data for this window. Try a longer `--apm-hours` window or confirm APM ingest for this realm."
                )
        else:
            bullets.append("**APM:** automation did not produce APM health output.")
    if include_rum:
        if rum_report is not None:
            if rum_report.get("error"):
                bullets.append("**RUM:** automation did not complete (see RUM section).")
            else:
                lb = rum_report.get("lookbackHours", "—")
                bullets.append(
                    f"**RUM:** SignalFlow window **{lb}** hour(s); volume/MMS tables when `sf.org` series split by "
                    "application; bot/custom-event/TMS rows are UI-only in this automation (see Findings)."
                )
                if not _rum_any_automated_rows(rum_report):
                    bullets.append(
                        "**RUM data:** no volume or MMS rows — `sf.org.rum.numSessions` / "
                        "`sf.org.numRumMonitoringMetricSetMetrics` may lack by-app dimensions for this window. "
                        "Try `--rum-lookback-hours` or confirm org metrics in Chart Builder."
                    )
        else:
            bullets.append("**RUM:** automation did not produce RUM health output.")
    if include_synthetics:
        if synthetics_report is not None:
            if synthetics_report.get("error"):
                bullets.append("**Synthetics:** automation did not complete (see Synthetics section).")
            else:
                ta = synthetics_report.get("testsAnalyzed")
                bullets.append(
                    f"**Synthetics:** listed **{ta}** synthetic test(s) (heuristic severities; confirm in UI)."
                )
        else:
            bullets.append("**Synthetics:** automation did not produce Synthetics inventory data.")
    if include_tokens:
        if token_report is not None:
            if token_report.get("error"):
                bullets.append("**Tokens:** automation did not complete (see Token health check section).")
            else:
                tn = token_report.get("tokenListTotal")
                bullets.append(f"**Tokens:** listed **{tn}** org token(s) (name column only in report).")
        else:
            bullets.append("**Tokens:** automation did not produce token inventory data.")
    if include_otel_collectors:
        if otel_report is not None:
            if otel_report.get("error"):
                bullets.append("**OpenTelemetry Collectors:** automation did not complete (see that section).")
            else:
                n = len(
                    ((otel_report.get("checks") or {}).get("collectorsByVersion") or {}).get("rows") or []
                )
                bullets.append(f"**OpenTelemetry Collectors:** **{n}** collector instance row(s) from metrics.")
        else:
            bullets.append("**OpenTelemetry Collectors:** automation did not produce collector inventory data.")
    lines = ["## Executive summary", ""]
    for b in bullets:
        lines.append(f"- {b}")
    lines.append("")
    scope_txt = ", ".join(scope_parts) if scope_parts else "(no checklist domains selected for this run)"
    lines.append(f"- **Scope:** {scope_txt}")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_consolidated_report(
    *,
    title: str,
    customer: str,
    realm: str,
    scope: str,
    license_report: dict[str, Any] | None,
    platform_engagement_report: dict[str, Any] | None,
    im_report: dict[str, Any] | None,
    im_integrations_report: dict[str, Any] | None,
    detectors_report: dict[str, Any] | None,
    dashboards_report: dict[str, Any] | None,
    apm_report: dict[str, Any] | None,
    rum_report: dict[str, Any] | None,
    synthetics_report: dict[str, Any] | None,
    token_report: dict[str, Any] | None,
    otel_report: dict[str, Any] | None,
    include_license: bool = True,
    include_platform_engagement: bool = True,
    include_im: bool = True,
    include_detectors: bool = True,
    include_dashboards: bool = True,
    include_apm: bool = True,
    include_rum: bool = True,
    include_synthetics: bool = True,
    include_tokens: bool = True,
    include_otel_collectors: bool = True,
) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta_rows = [
        "| Field | Value |",
        "| --- | --- |",
        f"| Customer / purpose | {_md_cell(customer)} |",
        f"| Assessment date (UTC) | {now_utc} |",
        f"| Realm | {_md_cell(realm)} |",
        f"| Scope | {_md_cell(scope)} |",
    ]

    scope_parts: list[str] = []
    if include_license:
        scope_parts.append("License utilization")
    if include_platform_engagement:
        scope_parts.append("Platform engagement")
    if include_im:
        scope_parts.append("Infrastructure monitoring")
    if include_detectors:
        scope_parts.append("Detectors")
    if include_dashboards:
        scope_parts.append("Dashboards")
    if include_apm:
        scope_parts.append("APM")
    if include_rum:
        scope_parts.append("Real User Monitoring (RUM)")
    if include_synthetics:
        scope_parts.append("Synthetics")
    if include_tokens:
        scope_parts.append("Token")
    if include_otel_collectors:
        scope_parts.append("OpenTelemetry Collectors")
    any_product_module = any(
        (
            include_im,
            include_detectors,
            include_dashboards,
            include_apm,
            include_rum,
            include_synthetics,
            include_tokens,
            include_otel_collectors,
        )
    )
    show_severity_legend = any_product_module

    parts: list[str] = [
        f"# {title}",
        "",
        "\n".join(meta_rows),
        "",
    ]

    parts.append(
        _executive_summary(
            license_report=license_report,
            platform_engagement_report=platform_engagement_report,
            im_report=im_report,
            im_integrations_report=im_integrations_report,
            detectors_report=detectors_report,
            dashboards_report=dashboards_report,
            apm_report=apm_report,
            rum_report=rum_report,
            synthetics_report=synthetics_report,
            token_report=token_report,
            otel_report=otel_report,
            scope_parts=scope_parts,
            include_license=include_license,
            include_platform_engagement=include_platform_engagement,
            include_im=include_im,
            include_detectors=include_detectors,
            include_dashboards=include_dashboards,
            include_apm=include_apm,
            include_rum=include_rum,
            include_synthetics=include_synthetics,
            include_tokens=include_tokens,
            include_otel_collectors=include_otel_collectors,
        )
    )

    if include_license:
        if license_report is not None:
            parts.append(
                _render_license_capacity(license_report)
            )
        else:
            parts.append(_section_automation_missing_md("## License utilization"))

    if include_platform_engagement:
        if platform_engagement_report is not None:
            parts.append(render_platform_engagement_markdown(platform_engagement_report))
        else:
            parts.append(_section_automation_missing_md("## Platform engagement"))

    if show_severity_legend:
        parts.append(_severity_legend_markdown())

    if include_im:
        if im_report is not None or im_integrations_report is not None:
            parts.append(render_im_checks_markdown(im_report, im_integrations_report))
        else:
            parts.append(_section_automation_missing_md("## Infrastructure monitoring health checks"))

    if include_detectors:
        if detectors_report is not None:
            parts.append(render_detectors_checks_markdown(detectors_report))
        else:
            parts.append(_section_automation_missing_md("## Detectors health checks"))

    if include_dashboards:
        if dashboards_report is not None:
            parts.append(render_dashboards_checks_markdown(dashboards_report))
        else:
            parts.append(_section_automation_missing_md("## Dashboards health checks"))

    if include_apm:
        if apm_report is not None:
            checks = apm_report.get("checks") or {}
            parts.append("## APM health checks\n\n")
            parts.append(render_apm_checks_markdown(checks, heading_prefix="###"))
        else:
            parts.append(_section_automation_missing_md("## APM health checks"))

    if include_rum:
        if rum_report is not None:
            parts.append(render_rum_checks_markdown(rum_report))
        else:
            parts.append(_section_automation_missing_md("## Real User Monitoring (RUM) health checks"))

    if include_synthetics:
        if synthetics_report is not None:
            parts.append(render_synthetics_checks_markdown(synthetics_report))
        else:
            parts.append(_section_automation_missing_md("## Synthetics health check"))

    if include_tokens:
        if token_report is not None:
            parts.append(render_token_checks_markdown(token_report))
        else:
            parts.append(_section_automation_missing_md("## Token health check"))

    if include_otel_collectors:
        if otel_report is not None:
            parts.append(render_otel_collectors_markdown(otel_report))
        else:
            parts.append(_section_automation_missing_md("## OpenTelemetry Collectors"))

    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    repo_root = _SCRIPT_DIR.parent
    p = argparse.ArgumentParser(
        description="Run O11y health scripts and write one consolidated markdown report "
        "(defaults from customer-profile.yaml).",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Override output path for consolidated markdown (default: reports/<report_title>.md from profile)",
    )
    p.add_argument(
        "--customer",
        default=None,
        help="Override customer / purpose label (default: company_or_purpose in profile)",
    )
    p.add_argument(
        "--title",
        default=None,
        help="Override report H1 title (default: report_title in profile)",
    )
    p.add_argument(
        "--realm",
        default=None,
        help="Override realm (default: realm in profile, else SPLUNK_REALM, else us0)",
    )
    p.add_argument(
        "--profile",
        default=None,
        metavar="PATH",
        help="Profile YAML path (default: customer-profile.local.yaml or customer-profile.yaml in repo root)",
    )
    p.add_argument("--skip-license", action="store_true", help="Do not run license utilization script")
    p.add_argument(
        "--skip-platform-engagement",
        action="store_true",
        help="Do not run platform engagement trends (o11y_platform_engagement_trends.py)",
    )
    p.add_argument(
        "--skip-im",
        action="store_true",
        help="Do not run Infrastructure Monitoring metrics usage script (o11y_im_metrics_usage_breakdown.py)",
    )
    p.add_argument("--skip-detectors", action="store_true", help="Do not run detectors health script")
    p.add_argument(
        "--skip-dashboards",
        action="store_true",
        help="Do not run dashboards health script (o11y_dashboards_health_check.py)",
    )
    p.add_argument(
        "--skip-synthetics",
        action="store_true",
        help="Do not run Synthetics health script (o11y_synthetics_health_check.py)",
    )
    p.add_argument("--skip-apm", action="store_true", help="Do not run APM health script")
    p.add_argument(
        "--skip-rum",
        action="store_true",
        help="Do not run RUM health script (o11y_rum_health_check.py)",
    )
    p.add_argument(
        "--skip-tokens",
        action="store_true",
        help="Do not run token health script (o11y_token_health_check.py)",
    )
    p.add_argument(
        "--skip-otel-collectors",
        action="store_true",
        help="Do not run OpenTelemetry Collectors script (o11y_otel_collectors_health_check.py)",
    )
    p.add_argument(
        "--otel-lookback-hours",
        type=int,
        default=None,
        help="OTel SignalFlow window hours (default: health_check_otel_lookback_hours in profile, else 4)",
    )
    p.add_argument(
        "--otel-support-days",
        type=int,
        default=None,
        help="OTel: depreciation = GitHub release date + N days (default: health_check_otel_support_days in profile, else 730)",
    )
    p.add_argument(
        "--otel-skip-github-catalog",
        action="store_true",
        help="OTel: skip GitHub API for splunk-otel-collector releases (depreciation dates show as —)",
    )
    p.add_argument(
        "--license-days",
        type=int,
        default=None,
        help="Override license lookback days (default: health_check_license_days in profile, else 90)",
    )
    p.add_argument(
        "--pe-lookback-days",
        type=int,
        default=None,
        help="Platform engagement SignalFlow window days (default: health_check_pe_lookback_days in profile, else 180)",
    )
    p.add_argument(
        "--pe-resolution-hours",
        type=int,
        default=None,
        help="Platform engagement rollup hours for sf.org gauges (default: health_check_pe_resolution_hours, else 24)",
    )
    p.add_argument(
        "--pe-apps-resolution-hours",
        type=int,
        default=None,
        help="Platform engagement rollup hours for APM instrumented-apps matrix (default: health_check_pe_apps_resolution_hours, else 168)",
    )
    p.add_argument(
        "--license-start-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="License utilization: UTC window start (inclusive); requires --license-end-date (or profile pair)",
    )
    p.add_argument(
        "--license-end-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="License utilization: UTC window end (inclusive); requires --license-start-date",
    )
    p.add_argument(
        "--license-calendar-month",
        default=None,
        metavar="YYYY-MM",
        help="License utilization: single full UTC calendar month (overrides --license-start/end-date and lookback days)",
    )
    p.add_argument(
        "--pe-as-of-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Platform engagement: anchor rolling KPIs to end of this UTC day (ignored if all four "
        "--pe-compare-*-date values are set)",
    )
    p.add_argument(
        "--pe-compare-current-start-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="With the other three --pe-compare-*-date flags: rolling KPI current period start (UTC)",
    )
    p.add_argument(
        "--pe-compare-current-end-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Rolling KPI current period end (UTC, inclusive)",
    )
    p.add_argument(
        "--pe-compare-baseline-start-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Rolling KPI baseline period start (UTC, inclusive)",
    )
    p.add_argument(
        "--pe-compare-baseline-end-date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Rolling KPI baseline period end (UTC, inclusive)",
    )
    p.add_argument(
        "--pe-baseline-month",
        default=None,
        metavar="YYYY-MM",
        help="Platform engagement: one of two full UTC months (requires --pe-comparison-month; overrides "
        "as-of and four --pe-compare-*-date). The earlier month becomes Baseline and the later Comparison.",
    )
    p.add_argument(
        "--pe-comparison-month",
        default=None,
        metavar="YYYY-MM",
        help="Platform engagement: the other month in the pair (see --pe-baseline-month).",
    )
    p.add_argument(
        "--apm-hours",
        type=int,
        default=None,
        help="Override APM lookback hours (default: health_check_apm_hours in profile, else 24)",
    )
    p.add_argument(
        "--apm-signalflow-max-data-points",
        type=int,
        default=None,
        metavar="N",
        help="APM spans.count / traces.count SignalFlow datapoint cap (default: "
        "health_check_apm_signalflow_max_data_points in profile, else 300000)",
    )
    p.add_argument(
        "--rum-lookback-hours",
        type=int,
        default=None,
        help="RUM SignalFlow window hours (default: health_check_rum_lookback_hours in profile, else 168; max 720)",
    )
    p.add_argument(
        "--rum-resolution-minutes",
        type=int,
        default=None,
        help="RUM rollup resolution minutes (default: health_check_rum_resolution_minutes in profile, else 360)",
    )
    p.add_argument(
        "--rum-max-rows",
        type=int,
        default=None,
        help="RUM max rows per volumetric table (default: health_check_rum_max_rows in profile, else 200)",
    )
    p.add_argument(
        "--im-lookback",
        default=None,
        choices=("P1D", "P7D", "P30D"),
        help="IM Usage analytics lookback (default: health_check_im_lookback in profile, else P1D)",
    )
    p.add_argument(
        "--im-limit",
        type=int,
        default=None,
        help="IM API max metrics (default: health_check_im_limit in profile, else 10000)",
    )
    p.add_argument(
        "--im-top",
        type=int,
        default=None,
        help="IM table: only top N metrics by MTS in report (default: health_check_im_top in profile; omit = all)",
    )
    p.add_argument(
        "--max-detectors",
        type=int,
        default=None,
        help="Detectors: max detectors to analyze (default: health_check_detectors_max in profile, else 400)",
    )
    p.add_argument(
        "--max-dashboards",
        type=int,
        default=None,
        help="Dashboards: max dashboards to analyze (default: health_check_max_dashboards in profile, else 150)",
    )
    p.add_argument(
        "--max-synthetics-tests",
        type=int,
        default=None,
        help="Synthetics: max tests to list (default: health_check_max_synthetics_tests in profile, else 2000)",
    )
    p.add_argument(
        "--skip-inactive-mts",
        action="store_true",
        help="Detectors: pass --skip-inactive-mts to o11y_detectors_health_check.py (no MTS API sampling)",
    )
    p.add_argument(
        "--inactive-mts-hours",
        type=float,
        default=None,
        help="Detectors: stale threshold hours (default: health_check_inactive_mts_hours in profile, else 36)",
    )
    p.add_argument(
        "--inactive-mts-max-metrics",
        type=int,
        default=None,
        help="Detectors: max data('…') metrics per detector (default: profile health_check_inactive_mts_max_metrics, else 3)",
    )
    p.add_argument(
        "--inactive-mts-search-limit",
        type=int,
        default=None,
        help="Detectors: MTS rows per metric search (default: profile …search_limit, else 3)",
    )
    p.add_argument(
        "--inactive-mts-max-evaluations",
        type=int,
        default=None,
        help="Detectors: max MTS checks per detector (default: profile …max_evaluations, else 6)",
    )
    p.add_argument(
        "--no-apm-trace-checks",
        action="store_true",
        help="Omit --trace-checks for APM (overrides health_check_apm_trace_checks in profile)",
    )
    p.add_argument(
        "--keep-intermediate-json",
        action="store_true",
        help="Keep intermediate step JSON next to output (also set via health_check_keep_intermediate_json)",
    )
    p.add_argument(
        "--json-out",
        default=None,
        metavar="PATH",
        help="Write combined JSON to this path (default: if health_check_json_out is true in profile, "
        "writes <same basename as .md>.json)",
    )
    p.add_argument(
        "--pptx-out",
        default=None,
        metavar="PATH",
        help="Write executive PowerPoint deck (.pptx). Omit path and set health_check_pptx_out in profile "
        "to auto-use <report>.pptx. Requires: pip install python-pptx",
    )
    p.add_argument(
        "--pptx-template",
        default=None,
        metavar="PATH",
        help="Optional Splunk / customer .pptx; overrides default theme master. Template slide masters "
        "are kept; existing slides in the file are removed before adding generated content.",
    )
    p.add_argument(
        "--pptx-light",
        action="store_true",
        help="Use Splunk **light** template (FY27). Default is **dark** (splunk-ppt-template/…Dark…). "
        "Also: profile health_check_pptx_theme: light",
    )
    p.add_argument(
        "--pdf-out",
        default=None,
        metavar="PATH",
        help="Render detailed report PDF via Chromium + local viewer. Omit path with health_check_pdf_out "
        "in profile for <report>.pdf. Requires: pip install playwright && playwright install chromium",
    )
    p.add_argument(
        "--pdf-viewer-dir",
        default=None,
        metavar="PATH",
        help="Folder containing viewer index.html (default repo web/o11y-health-report or "
        "health_check_pdf_viewer_dir in profile).",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging (debug), including full subprocess command lines",
    )
    args = p.parse_args()

    setup_logging(verbose=bool(args.verbose))

    profile_path = resolve_profile_path_repo(repo_root, args.profile)
    if not profile_path:
        logger.error(
            "No profile YAML found. Add customer-profile.yaml or customer-profile.local.yaml "
            "to the repo root, or pass --profile /path/to/profile.yaml"
        )
        return 1

    logger.info("Splunk Observability consolidated health check — starting")
    logger.info("Profile: %s", profile_path)
    run_t0 = time.monotonic()

    profile: dict[str, str] = load_customer_profile_scalars(profile_path)

    skip_license = args.skip_license or profile_bool(profile.get("health_check_skip_license"), False)
    skip_im = args.skip_im or profile_bool(profile.get("health_check_skip_im"), False)
    skip_detectors = args.skip_detectors or profile_bool(profile.get("health_check_skip_detectors"), False)
    skip_dashboards = args.skip_dashboards or profile_bool(profile.get("health_check_skip_dashboards"), False)
    skip_synthetics = args.skip_synthetics or profile_bool(profile.get("health_check_skip_synthetics"), False)
    skip_apm = args.skip_apm or profile_bool(profile.get("health_check_skip_apm"), False)
    skip_rum = args.skip_rum or profile_bool(profile.get("health_check_skip_rum"), False)
    skip_platform_engagement = args.skip_platform_engagement or profile_bool(
        profile.get("health_check_skip_platform_engagement"), False
    )
    skip_tokens = args.skip_tokens or profile_bool(profile.get("health_check_skip_tokens"), False)
    skip_otel_collectors = args.skip_otel_collectors or profile_bool(
        profile.get("health_check_skip_otel_collectors"), False
    )
    otel_lookback_hours = (
        args.otel_lookback_hours
        if args.otel_lookback_hours is not None
        else profile_int(profile.get("health_check_otel_lookback_hours"), 4)
    )
    otel_lookback_hours = max(1, min(otel_lookback_hours, 168))
    otel_support_days = (
        args.otel_support_days
        if args.otel_support_days is not None
        else profile_int(profile.get("health_check_otel_support_days"), 730)
    )
    otel_support_days = max(0, min(otel_support_days, 3650))
    otel_skip_github_catalog = args.otel_skip_github_catalog or profile_bool(
        profile.get("health_check_otel_skip_github_catalog"), False
    )
    license_days = (
        args.license_days
        if args.license_days is not None
        else profile_int(profile.get("health_check_license_days"), 90)
    )
    pe_lookback_days = (
        args.pe_lookback_days
        if args.pe_lookback_days is not None
        else profile_int(profile.get("health_check_pe_lookback_days"), 180)
    )
    pe_lookback_days = max(90, min(pe_lookback_days, 366))
    pe_resolution_hours = (
        args.pe_resolution_hours
        if args.pe_resolution_hours is not None
        else profile_int(profile.get("health_check_pe_resolution_hours"), 24)
    )
    pe_resolution_hours = max(6, min(pe_resolution_hours, 168))
    pe_apps_resolution_hours = (
        args.pe_apps_resolution_hours
        if args.pe_apps_resolution_hours is not None
        else profile_int(profile.get("health_check_pe_apps_resolution_hours"), 168)
    )
    pe_apps_resolution_hours = max(24, min(pe_apps_resolution_hours, 168))

    def _profile_date(cli: str | None, key: str) -> str | None:
        v = (cli or "").strip() or (profile.get(key) or "").strip()
        return v or None

    def _profile_month(cli: str | None, key: str) -> str | None:
        v = _profile_date(cli, key)
        if not v:
            return None
        if len(v) != 7 or v[4] != "-":
            logger.error("Invalid calendar month %r (expected YYYY-MM).", v)
            return None
        try:
            y, m = int(v[:4]), int(v[5:7])
        except ValueError:
            logger.error("Invalid calendar month %r (expected YYYY-MM).", v)
            return None
        if y < 1970 or m < 1 or m > 12:
            logger.error("Invalid calendar month %r (expected YYYY-MM).", v)
            return None
        return v

    lic_cal = _profile_month(args.license_calendar_month, "health_check_license_calendar_month")
    lic_sd = _profile_date(args.license_start_date, "health_check_license_start_date")
    lic_ed = _profile_date(args.license_end_date, "health_check_license_end_date")
    if ((args.license_calendar_month or "").strip() or (profile.get("health_check_license_calendar_month") or "").strip()) and not lic_cal:
        return 1
    if lic_cal and (lic_sd or lic_ed):
        logger.error("Use either --license-calendar-month or start/end license dates, not both.")
        return 1
    if (lic_sd and not lic_ed) or (lic_ed and not lic_sd):
        logger.error(
            "License dates: provide both --license-start-date and --license-end-date "
            "(or profile health_check_license_start_date / health_check_license_end_date), or neither."
        )
        return 1

    pe_base_m = _profile_month(args.pe_baseline_month, "health_check_pe_baseline_month")
    pe_comp_m = _profile_month(args.pe_comparison_month, "health_check_pe_comparison_month")
    pe_mm_n = sum(1 for x in (pe_base_m, pe_comp_m) if x)
    if pe_mm_n == 1:
        logger.error("Platform engagement: set both --pe-baseline-month and --pe-comparison-month, or neither.")
        return 1
    if (
        (args.pe_baseline_month or "").strip()
        or (args.pe_comparison_month or "").strip()
        or (profile.get("health_check_pe_baseline_month") or "").strip()
        or (profile.get("health_check_pe_comparison_month") or "").strip()
    ) and pe_mm_n != 2:
        return 1

    if pe_mm_n == 2:
        if (args.pe_as_of_date or "").strip():
            logger.warning("Ignoring --pe-as-of-date because --pe-baseline-month/--pe-comparison-month are set.")
        if any(
            (getattr(args, name) or "").strip()
            for name in (
                "pe_compare_current_start_date",
                "pe_compare_current_end_date",
                "pe_compare_baseline_start_date",
                "pe_compare_baseline_end_date",
            )
        ):
            logger.warning(
                "Ignoring --pe-compare-*-date CLI values because month comparison months are set."
            )
        pe_as_of = None
        pe_c0 = pe_c1 = pe_b0 = pe_b1 = None
        pe_cmp_ct = 0
    else:
        pe_as_of = _profile_date(args.pe_as_of_date, "health_check_pe_as_of_date")
        pe_c0 = _profile_date(args.pe_compare_current_start_date, "health_check_pe_compare_current_start_date")
        pe_c1 = _profile_date(args.pe_compare_current_end_date, "health_check_pe_compare_current_end_date")
        pe_b0 = _profile_date(args.pe_compare_baseline_start_date, "health_check_pe_compare_baseline_start_date")
        pe_b1 = _profile_date(args.pe_compare_baseline_end_date, "health_check_pe_compare_baseline_end_date")
        pe_cmp_ct = sum(1 for x in (pe_c0, pe_c1, pe_b0, pe_b1) if x)
        if pe_cmp_ct not in (0, 4):
            logger.error(
                "Platform engagement: set all four --pe-compare-*-date options (or matching profile keys), or none."
            )
            return 1
        if pe_cmp_ct == 4 and pe_as_of:
            logger.warning("PE custom compare windows take precedence over --pe-as-of-date.")
            pe_as_of = None

    apm_hours = args.apm_hours if args.apm_hours is not None else profile_int(profile.get("health_check_apm_hours"), 24)
    apm_sf_max_pts = (
        args.apm_signalflow_max_data_points
        if args.apm_signalflow_max_data_points is not None
        else profile_int(profile.get("health_check_apm_signalflow_max_data_points"), 300_000)
    )
    apm_sf_max_pts = max(5_000, min(apm_sf_max_pts, 2_000_000))
    rum_lookback_h = (
        args.rum_lookback_hours
        if args.rum_lookback_hours is not None
        else profile_int(profile.get("health_check_rum_lookback_hours"), 168)
    )
    rum_lookback_h = max(1, min(rum_lookback_h, 720))
    rum_res_min = (
        args.rum_resolution_minutes
        if args.rum_resolution_minutes is not None
        else profile_int(profile.get("health_check_rum_resolution_minutes"), 360)
    )
    rum_res_min = max(1, min(rum_res_min, 1440))
    rum_max_rows = (
        args.rum_max_rows if args.rum_max_rows is not None else profile_int(profile.get("health_check_rum_max_rows"), 200)
    )
    rum_max_rows = max(10, min(rum_max_rows, 2000))
    im_lookback = (
        args.im_lookback
        if args.im_lookback is not None
        else (profile.get("health_check_im_lookback") or "P1D").strip().upper()
    )
    if im_lookback not in ("P1D", "P7D", "P30D"):
        im_lookback = "P1D"
    im_limit = args.im_limit if args.im_limit is not None else profile_int(profile.get("health_check_im_limit"), 10000)
    im_limit = max(1, min(im_limit, 10000))
    im_top_raw = profile.get("health_check_im_top")
    im_top_default: int | None = None
    if im_top_raw is not None and str(im_top_raw).strip() != "":
        im_top_default = profile_int(im_top_raw, 0)
        if im_top_default <= 0:
            im_top_default = None
    im_top = args.im_top if args.im_top is not None else im_top_default
    detectors_max = (
        args.max_detectors
        if args.max_detectors is not None
        else profile_int(profile.get("health_check_detectors_max"), 400)
    )
    detectors_max = max(1, min(detectors_max, 10000))
    dashboards_max = (
        args.max_dashboards
        if args.max_dashboards is not None
        else profile_int(profile.get("health_check_max_dashboards"), 150)
    )
    dashboards_max = max(1, min(dashboards_max, 5000))
    synthetics_max_tests = (
        args.max_synthetics_tests
        if args.max_synthetics_tests is not None
        else profile_int(profile.get("health_check_max_synthetics_tests"), 2000)
    )
    synthetics_max_tests = max(1, min(synthetics_max_tests, 10000))
    skip_synthetics_failure_metrics = profile_bool(
        profile.get("health_check_skip_synthetics_failure_metrics"), False
    )
    synthetics_failure_lookback_h = max(
        1,
        min(
            profile_int(profile.get("health_check_synthetics_failure_lookback_hours"), 168),
            168,
        ),
    )
    synthetics_failing_threshold_pct = max(
        0.0,
        min(
            profile_float(profile.get("health_check_synthetics_failing_rate_threshold_pct"), 30.0),
            100.0,
        ),
    )
    synthetics_failure_res_min = max(
        1,
        min(
            profile_int(profile.get("health_check_synthetics_failure_resolution_minutes"), 60),
            1440,
        ),
    )
    skip_inactive_mts = args.skip_inactive_mts or profile_bool(
        profile.get("health_check_skip_inactive_mts"), False
    )
    inactive_mts_hours = (
        args.inactive_mts_hours
        if args.inactive_mts_hours is not None
        else profile_float(profile.get("health_check_inactive_mts_hours"), 36.0)
    )
    inactive_mts_hours = max(1.0, float(inactive_mts_hours))
    inactive_mts_max_metrics = (
        args.inactive_mts_max_metrics
        if args.inactive_mts_max_metrics is not None
        else profile_int(profile.get("health_check_inactive_mts_max_metrics"), 3)
    )
    inactive_mts_max_metrics = max(1, min(inactive_mts_max_metrics, 20))
    inactive_mts_search_limit = (
        args.inactive_mts_search_limit
        if args.inactive_mts_search_limit is not None
        else profile_int(profile.get("health_check_inactive_mts_search_limit"), 3)
    )
    inactive_mts_search_limit = max(1, min(inactive_mts_search_limit, 50))
    inactive_mts_max_evaluations = (
        args.inactive_mts_max_evaluations
        if args.inactive_mts_max_evaluations is not None
        else profile_int(profile.get("health_check_inactive_mts_max_evaluations"), 6)
    )
    inactive_mts_max_evaluations = max(1, min(inactive_mts_max_evaluations, 50))
    apm_trace_checks = (not args.no_apm_trace_checks) and profile_bool(
        profile.get("health_check_apm_trace_checks"), True
    )
    keep_intermediate = args.keep_intermediate_json or profile_bool(
        profile.get("health_check_keep_intermediate_json"), False
    )

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    else:
        out_path = default_report_output_path(repo_root, profile).resolve()

    logger.info("Report markdown path: %s", out_path)

    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    pptx_out_path = parse_profile_aux_path(
        out_path,
        profile_val=profile.get("health_check_pptx_out"),
        arg_val=args.pptx_out,
        suffix=".pptx",
    )
    pdf_out_path = parse_profile_aux_path(
        out_path,
        profile_val=profile.get("health_check_pdf_out"),
        arg_val=args.pdf_out,
        suffix=".pdf",
    )
    pptx_tpl_s = ((args.pptx_template or "").strip() or (profile.get("health_check_pptx_template") or "").strip())
    pptx_explicit_template: Path | None = resolve_repo_relative_path(repo_root, pptx_tpl_s) if pptx_tpl_s else None

    pptx_theme_raw = (profile.get("health_check_pptx_theme") or "dark").strip().lower()
    if pptx_theme_raw not in ("light", "dark"):
        logger.warning("health_check_pptx_theme must be light or dark (got %r); using dark", pptx_theme_raw)
        pptx_theme_raw = "dark"
    pptx_use_light = bool(args.pptx_light) or (pptx_theme_raw == "light")

    pv_raw = ((args.pdf_viewer_dir or "").strip() or (profile.get("health_check_pdf_viewer_dir") or "").strip())
    if pv_raw:
        pdf_viewer_dir = Path(pv_raw).expanduser()
        pdf_viewer_dir = (repo_root / pdf_viewer_dir).resolve() if not pdf_viewer_dir.is_absolute() else pdf_viewer_dir.resolve()
    else:
        pdf_viewer_dir = (repo_root / "web/o11y-health-report").resolve()

    stamp = f"{os.getpid()}_{int(time.time() * 1000)}"
    if keep_intermediate:
        lic_json = out_dir / f".o11y_run_{stamp}_license.json"
        pe_json = out_dir / f".o11y_run_{stamp}_platform_engagement.json"
        apm_json = out_dir / f".o11y_run_{stamp}_apm.json"
        im_json = out_dir / f".o11y_run_{stamp}_im.json"
        im_int_json = out_dir / f".o11y_run_{stamp}_im_int.json"
        det_json = out_dir / f".o11y_run_{stamp}_detectors.json"
        dash_json = out_dir / f".o11y_run_{stamp}_dashboards.json"
        rum_json = out_dir / f".o11y_run_{stamp}_rum.json"
        syn_json = out_dir / f".o11y_run_{stamp}_synthetics.json"
        tok_json = out_dir / f".o11y_run_{stamp}_tokens.json"
        otel_json = out_dir / f".o11y_run_{stamp}_otel.json"
    else:
        lic_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_license.json"
        pe_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_platform_engagement.json"
        apm_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_apm.json"
        im_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_im.json"
        im_int_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_im_int.json"
        det_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_detectors.json"
        dash_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_dashboards.json"
        rum_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_rum.json"
        syn_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_synthetics.json"
        tok_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_tokens.json"
        otel_json = Path(tempfile.gettempdir()) / f"o11y_run_{stamp}_otel.json"

    license_report: dict[str, Any] | None = None
    platform_engagement_report: dict[str, Any] | None = None
    im_report: dict[str, Any] | None = None
    im_integrations_report: dict[str, Any] | None = None
    detectors_report: dict[str, Any] | None = None
    dashboards_report: dict[str, Any] | None = None
    apm_report: dict[str, Any] | None = None
    rum_report: dict[str, Any] | None = None
    synthetics_report: dict[str, Any] | None = None
    token_report: dict[str, Any] | None = None
    otel_report: dict[str, Any] | None = None
    license_step_ok = False
    platform_engagement_step_ok = False
    im_step_ok = False
    im_int_step_ok = False
    det_step_ok = False
    dashboards_step_ok = False
    apm_step_ok = False
    rum_step_ok = False
    synthetics_step_ok = False
    token_step_ok = False
    otel_step_ok = False

    common: list[str] = ["--profile", profile_path]
    realm_effective = (
        (args.realm or "").strip()
        or (profile.get("realm") or "").strip()
        or os.environ.get("SPLUNK_REALM", "").strip()
        or "us0"
    )
    common += ["--realm", realm_effective]
    _log_auth_resolution(
        profile_path=profile_path,
        profile=profile,
        realm_effective=realm_effective,
        cli_realm=args.realm,
    )

    if skip_license:
        logger.info("Skipping license utilization (--skip-license or profile health_check_skip_license)")
    else:
        lic_argv = [
            str(_SCRIPT_DIR / "o11y_license_utilization.py"),
            "--json-out",
            str(lic_json),
            *common,
        ]
        if lic_cal:
            lic_argv.extend(["--calendar-month", lic_cal])
        elif lic_sd and lic_ed:
            lic_argv.extend(["--start-date", lic_sd, "--end-date", lic_ed])
        else:
            lic_argv.extend(["--days", str(max(1, license_days))])
        lic_rc = _run_child_script(lic_argv, cwd=repo_root, step_label="License utilization (o11y_license_utilization.py)")
        if lic_json.is_file():
            license_report, lic_err = load_json_report(lic_json)
            if license_report is not None:
                license_step_ok = True
                rows_n = len(license_report.get("rows") or [])
                logger.info("Loaded license JSON OK (%s entitlement row(s))", rows_n)
                if lic_rc != 0:
                    logger.warning(
                        "License script exited %s but JSON parsed — section may be partial or stale",
                        lic_rc,
                    )
            else:
                logger.error("Could not use license JSON: %s", lic_err)
                logger.info("Continuing without license data — consolidated report will show license as not executed")
        else:
            logger.error("License script did not write JSON to %s", lic_json)
            logger.info("Continuing to next check…")

    if skip_platform_engagement:
        logger.info(
            "Skipping platform engagement trends (--skip-platform-engagement or profile "
            "health_check_skip_platform_engagement)"
        )
    else:
        pe_argv: list[str] = [
            str(_SCRIPT_DIR / "o11y_platform_engagement_trends.py"),
            "--lookback-days",
            str(pe_lookback_days),
            "--resolution-hours",
            str(pe_resolution_hours),
            "--apps-resolution-hours",
            str(pe_apps_resolution_hours),
            "--structured-json-out",
            str(pe_json),
            *common,
        ]
        if not skip_license and lic_json.is_file():
            pe_argv.extend(["--license-json", str(lic_json)])
        if pe_mm_n == 2:
            pe_argv.extend(
                [
                    "--baseline-calendar-month",
                    pe_base_m or "",
                    "--comparison-calendar-month",
                    pe_comp_m or "",
                ]
            )
        elif pe_cmp_ct == 4:
            pe_argv.extend(
                [
                    "--compare-current-start-date",
                    pe_c0,
                    "--compare-current-end-date",
                    pe_c1,
                    "--compare-baseline-start-date",
                    pe_b0,
                    "--compare-baseline-end-date",
                    pe_b1,
                ]
            )
        elif pe_as_of:
            pe_argv.extend(["--as-of-date", pe_as_of])
        pe_rc = _run_child_script(
            pe_argv, cwd=repo_root, step_label="Platform engagement (o11y_platform_engagement_trends.py)"
        )
        if pe_json.is_file():
            pe_data, pe_err = load_json_report(pe_json)
            if pe_data is not None:
                platform_engagement_report = pe_data
                if not platform_engagement_report.get("error"):
                    platform_engagement_step_ok = True
                    nk = len(platform_engagement_report.get("kpis") or [])
                    logger.info("Loaded platform engagement JSON OK (%s KPI series)", nk)
                else:
                    logger.warning(
                        "Platform engagement JSON reports error: %s",
                        platform_engagement_report.get("error"),
                    )
                if pe_rc != 0:
                    logger.warning(
                        "Platform engagement script exited %s — section may be partial or stale",
                        pe_rc,
                    )
            else:
                logger.error("Could not use platform engagement JSON: %s", pe_err)
                logger.info("Continuing — Platform engagement section will show placeholder or error state")
        else:
            logger.error("Platform engagement script did not write JSON to %s", pe_json)

    if skip_im:
        logger.info("Skipping IM metrics usage (--skip-im or profile health_check_skip_im)")
    else:
        im_argv = [
            str(_SCRIPT_DIR / "o11y_im_metrics_usage_breakdown.py"),
            "--lookback",
            im_lookback,
            "--limit",
            str(im_limit),
            "--structured-json-out",
            str(im_json),
            *common,
        ]
        if im_top is not None:
            im_argv.extend(["--top", str(max(1, im_top))])
        im_rc = _run_child_script(im_argv, cwd=repo_root, step_label="IM metrics usage (o11y_im_metrics_usage_breakdown.py)")
        if im_json.is_file():
            im_data, im_err = load_json_report(im_json)
            if im_data is not None:
                im_report = im_data
                if not im_report.get("error") and (im_report.get("metrics") or []):
                    im_step_ok = True
                    n_im = len(im_report.get("metrics") or [])
                    logger.info("Loaded IM metrics JSON OK (%s metric row(s))", n_im)
                else:
                    logger.warning(
                        "IM metrics JSON has error or empty metrics: %s",
                        im_report.get("error") or "empty",
                    )
                if im_rc != 0:
                    logger.warning(
                        "IM script exited %s — section may show error findings",
                        im_rc,
                    )
            else:
                logger.error("Could not use IM JSON: %s", im_err)
                logger.info("Continuing — IM section will show check not executed or error state")
        else:
            logger.error("IM script did not write JSON to %s", im_json)

        int_argv = [
            str(_SCRIPT_DIR / "o11y_im_integrations.py"),
            "--limit",
            "10000",
            "--structured-json-out",
            str(im_int_json),
            *common,
        ]
        int_rc = _run_child_script(int_argv, cwd=repo_root, step_label="IM integrations (o11y_im_integrations.py)")
        if im_int_json.is_file():
            int_data, int_err = load_json_report(im_int_json)
            if int_data is not None:
                im_integrations_report = int_data
                if not im_integrations_report.get("error") and (im_integrations_report.get("integrations") or []):
                    im_int_step_ok = True
                    n_int = len(im_integrations_report.get("integrations") or [])
                    logger.info("Loaded IM integrations JSON OK (%s integration(s))", n_int)
                else:
                    logger.warning(
                        "IM integrations JSON has error or empty list: %s",
                        im_integrations_report.get("error") or "empty",
                    )
                if int_rc != 0:
                    logger.warning(
                        "IM integrations script exited %s — section may show error findings",
                        int_rc,
                    )
            else:
                logger.error("Could not use IM integrations JSON: %s", int_err)
        else:
            logger.error("IM integrations script did not write JSON to %s", im_int_json)

    if skip_detectors:
        logger.info("Skipping detectors health check (--skip-detectors or profile health_check_skip_detectors)")
    else:
        det_argv = [
            str(_SCRIPT_DIR / "o11y_detectors_health_check.py"),
            "--max-detectors",
            str(detectors_max),
            "--structured-json-out",
            str(det_json),
            *common,
        ]
        if skip_inactive_mts:
            det_argv.append("--skip-inactive-mts")
        else:
            det_argv += [
                "--inactive-mts-hours",
                str(inactive_mts_hours),
                "--inactive-mts-max-metrics",
                str(inactive_mts_max_metrics),
                "--inactive-mts-search-limit",
                str(inactive_mts_search_limit),
                "--inactive-mts-max-evaluations",
                str(inactive_mts_max_evaluations),
            ]
        det_rc = _run_child_script(det_argv, cwd=repo_root, step_label="Detectors health (o11y_detectors_health_check.py)")
        if det_json.is_file():
            detectors_report, det_err = load_json_report(det_json)
            if detectors_report is not None:
                if not detectors_report.get("error"):
                    det_step_ok = True
                    da = detectors_report.get("detectorsAnalyzed")
                    dt = detectors_report.get("detectorListTotal")
                    logger.info("Loaded detectors JSON OK (analyzed %s of %s listed)", da, dt)
                else:
                    logger.warning(
                        "Detectors JSON reports error: %s",
                        detectors_report.get("error"),
                    )
                if det_rc != 0:
                    logger.warning(
                        "Detectors script exited %s — section may be partial or stale",
                        det_rc,
                    )
            else:
                logger.error("Could not use detectors JSON: %s", det_err)
                logger.info("Continuing — Detectors section will show placeholder or error state")
        else:
            logger.error("Detectors script did not write JSON to %s", det_json)

    if skip_dashboards:
        logger.info("Skipping dashboards health check (--skip-dashboards or profile health_check_skip_dashboards)")
    else:
        dash_argv = [
            str(_SCRIPT_DIR / "o11y_dashboards_health_check.py"),
            "--max-dashboards",
            str(dashboards_max),
            "--structured-json-out",
            str(dash_json),
            *common,
        ]
        dash_rc = _run_child_script(
            dash_argv, cwd=repo_root, step_label="Dashboards health (o11y_dashboards_health_check.py)"
        )
        if dash_json.is_file():
            dashboards_report, dash_err = load_json_report(dash_json)
            if dashboards_report is not None:
                if not dashboards_report.get("error"):
                    dashboards_step_ok = True
                    da = dashboards_report.get("dashboardsAnalyzed")
                    dt = dashboards_report.get("dashboardListTotal")
                    logger.info("Loaded dashboards JSON OK (analyzed %s of %s listed)", da, dt)
                else:
                    logger.warning(
                        "Dashboards JSON reports error: %s",
                        dashboards_report.get("error"),
                    )
                if dash_rc != 0:
                    logger.warning(
                        "Dashboards script exited %s — section may be partial or stale",
                        dash_rc,
                    )
            else:
                logger.error("Could not use dashboards JSON: %s", dash_err)
                logger.info("Continuing — Dashboards section will show placeholder or error state")
        else:
            logger.error("Dashboards script did not write JSON to %s", dash_json)

    if skip_apm:
        logger.info("Skipping APM health check (--skip-apm or profile health_check_skip_apm)")
    else:
        apm_argv = [
            str(_SCRIPT_DIR / "o11y_apm_health_check.py"),
            "--hours",
            str(max(1, apm_hours)),
            "--signalflow-max-data-points",
            str(apm_sf_max_pts),
            "--checks",
            "all",
            "--json-out",
            str(apm_json),
            *common,
        ]
        if apm_trace_checks:
            apm_argv.append("--trace-checks")
            logger.info("APM trace checks enabled (minimal_spans + large_span_sizes, etc.)")
        else:
            logger.info("APM trace checks disabled (faster run)")

        apm_rc = _run_child_script(apm_argv, cwd=repo_root, step_label="APM health (o11y_apm_health_check.py)")
        if apm_json.is_file():
            apm_report, apm_err = load_json_report(apm_json)
            if apm_report is not None:
                apm_step_ok = True
                checks_n = len((apm_report.get("checks") or {}).keys())
                logger.info("Loaded APM JSON OK (%s check block(s))", checks_n)
                if apm_rc != 0:
                    logger.warning(
                        "APM script exited %s but JSON parsed — section may be partial or stale",
                        apm_rc,
                    )
            else:
                logger.error("Could not use APM JSON: %s", apm_err)
                logger.info("Continuing — consolidated report will show APM as not executed where needed")
        else:
            logger.error("APM script did not write JSON to %s", apm_json)

    if skip_rum:
        logger.info("Skipping RUM health check (--skip-rum or profile health_check_skip_rum)")
    else:
        rum_argv = [
            str(_SCRIPT_DIR / "o11y_rum_health_check.py"),
            "--lookback-hours",
            str(rum_lookback_h),
            "--resolution-minutes",
            str(rum_res_min),
            "--max-rows",
            str(rum_max_rows),
            "--structured-json-out",
            str(rum_json),
            *common,
        ]
        rum_rc = _run_child_script(rum_argv, cwd=repo_root, step_label="RUM health (o11y_rum_health_check.py)")
        if rum_json.is_file():
            rum_data, rum_err = load_json_report(rum_json)
            if rum_data is not None:
                rum_report = rum_data
                if not rum_report.get("error"):
                    rum_step_ok = True
                    n_vol = len(((rum_report.get("checks") or {}).get("volumeByApplication") or {}).get("rows") or [])
                    n_mms = len(((rum_report.get("checks") or {}).get("rumMms") or {}).get("rows") or [])
                    logger.info("Loaded RUM JSON OK (volume_rows=%s mms_rows=%s)", n_vol, n_mms)
                else:
                    logger.warning(
                        "RUM JSON reports error: %s",
                        rum_report.get("error"),
                    )
                if rum_rc != 0:
                    logger.warning(
                        "RUM script exited %s — section may be partial or stale",
                        rum_rc,
                    )
            else:
                logger.error("Could not use RUM JSON: %s", rum_err)
                logger.info("Continuing — RUM section will show placeholder or error state")
        else:
            logger.error("RUM script did not write JSON to %s", rum_json)

    if skip_synthetics:
        logger.info("Skipping Synthetics health check (--skip-synthetics or profile health_check_skip_synthetics)")
    else:
        syn_argv = [
            str(_SCRIPT_DIR / "o11y_synthetics_health_check.py"),
            "--max-tests",
            str(synthetics_max_tests),
            "--structured-json-out",
            str(syn_json),
            *common,
        ]
        if skip_synthetics_failure_metrics:
            syn_argv.append("--skip-failure-metrics")
        else:
            syn_argv += [
                "--failure-metrics-lookback-hours",
                str(synthetics_failure_lookback_h),
                "--failure-metrics-resolution-minutes",
                str(synthetics_failure_res_min),
                "--failing-rate-threshold-pct",
                str(synthetics_failing_threshold_pct),
            ]
        syn_rc = _run_child_script(syn_argv, cwd=repo_root, step_label="Synthetics health (o11y_synthetics_health_check.py)")
        if syn_json.is_file():
            synthetics_report, syn_err = load_json_report(syn_json)
            if synthetics_report is not None:
                if not synthetics_report.get("error"):
                    synthetics_step_ok = True
                    ta = synthetics_report.get("testsAnalyzed")
                    logger.info("Loaded Synthetics JSON OK (%s test(s))", ta)
                else:
                    logger.warning(
                        "Synthetics JSON reports error: %s",
                        synthetics_report.get("error"),
                    )
                if syn_rc != 0:
                    logger.warning(
                        "Synthetics script exited %s — section may be partial or stale",
                        syn_rc,
                    )
            else:
                logger.error("Could not use Synthetics JSON: %s", syn_err)
                logger.info("Continuing — Synthetics section will show placeholder or error state")
        else:
            logger.error("Synthetics script did not write JSON to %s", syn_json)

    if skip_tokens:
        logger.info("Skipping Token health check (--skip-tokens or profile health_check_skip_tokens)")
    else:
        tok_argv = [
            str(_SCRIPT_DIR / "o11y_token_health_check.py"),
            "--structured-json-out",
            str(tok_json),
            *common,
        ]
        tok_rc = _run_child_script(tok_argv, cwd=repo_root, step_label="Token health (o11y_token_health_check.py)")
        if tok_json.is_file():
            token_report, tok_err = load_json_report(tok_json)
            if token_report is not None:
                if not token_report.get("error"):
                    token_step_ok = True
                    tn = token_report.get("tokenListTotal")
                    logger.info("Loaded Token health JSON OK (%s token(s) listed)", tn)
                else:
                    logger.warning(
                        "Token health JSON reports error: %s",
                        token_report.get("error"),
                    )
                if tok_rc != 0:
                    logger.warning(
                        "Token script exited %s — section may be partial or stale",
                        tok_rc,
                    )
            else:
                logger.error("Could not use Token health JSON: %s", tok_err)
                logger.info("Continuing — Token section will show placeholder or error state")
        else:
            logger.error("Token health script did not write JSON to %s", tok_json)

    if skip_otel_collectors:
        logger.info(
            "Skipping OpenTelemetry Collectors check (--skip-otel-collectors or profile health_check_skip_otel_collectors)"
        )
    else:
        otel_argv = [
            str(_SCRIPT_DIR / "o11y_otel_collectors_health_check.py"),
            "--lookback-hours",
            str(otel_lookback_hours),
            "--support-days",
            str(otel_support_days),
            "--structured-json-out",
            str(otel_json),
            *common,
        ]
        if otel_skip_github_catalog:
            otel_argv.append("--skip-github-catalog")
        otel_rc = _run_child_script(
            otel_argv, cwd=repo_root, step_label="OpenTelemetry Collectors (o11y_otel_collectors_health_check.py)"
        )
        if otel_json.is_file():
            otel_report, otel_err = load_json_report(otel_json)
            if otel_report is not None:
                if not otel_report.get("error"):
                    otel_step_ok = True
                    n_ot = len(
                        ((otel_report.get("checks") or {}).get("collectorsByVersion") or {}).get("rows") or []
                    )
                    logger.info("Loaded OpenTelemetry Collectors JSON OK (%s row(s))", n_ot)
                else:
                    logger.warning(
                        "OpenTelemetry Collectors JSON reports error: %s",
                        otel_report.get("error"),
                    )
                if otel_rc != 0:
                    logger.warning(
                        "OpenTelemetry Collectors script exited %s — section may be partial or stale",
                        otel_rc,
                    )
            else:
                logger.error("Could not use OpenTelemetry Collectors JSON: %s", otel_err)
                logger.info("Continuing — OpenTelemetry Collectors section will show placeholder or error state")
        else:
            logger.error("OpenTelemetry Collectors script did not write JSON to %s", otel_json)

    customer = (args.customer or profile.get("company_or_purpose") or profile.get("report_title") or "Organization").strip()
    title = (args.title or profile.get("report_title") or f"{customer} Splunk Observability Health Check").strip()
    realm = realm_effective

    scope_yaml = (profile.get("scope") or "").strip()
    auto_bits: list[str] = []
    if not skip_license:
        auto_bits.append("License")
    if not skip_platform_engagement:
        auto_bits.append("Platform engagement")
    if not skip_im:
        auto_bits.append("IM")
    if not skip_detectors:
        auto_bits.append("Detectors")
    if not skip_dashboards:
        auto_bits.append("Dashboards")
    if not skip_apm:
        auto_bits.append("APM")
    if not skip_rum:
        auto_bits.append("RUM")
    if not skip_synthetics:
        auto_bits.append("Synthetics")
    if not skip_tokens:
        auto_bits.append("Token")
    if not skip_otel_collectors:
        auto_bits.append("OpenTelemetry Collectors")
    if not auto_bits:
        scope_auto = "Placeholders only"
    elif len(auto_bits) == 1:
        scope_auto = f"{auto_bits[0]} only (automated)"
    else:
        scope_auto = " + ".join(auto_bits) + " (automated)"
    if scope_yaml:
        scope_display = f"{scope_yaml} · {scope_auto}"
    else:
        scope_display = scope_auto

    logger.info("Building consolidated markdown report…")
    _emit_hub_progress({"phase": "consolidate", "status": "start"})
    inclusion = dict(
        include_license=not skip_license,
        include_platform_engagement=not skip_platform_engagement,
        include_im=not skip_im,
        include_detectors=not skip_detectors,
        include_dashboards=not skip_dashboards,
        include_apm=not skip_apm,
        include_rum=not skip_rum,
        include_synthetics=not skip_synthetics,
        include_tokens=not skip_tokens,
        include_otel_collectors=not skip_otel_collectors,
    )
    report_sections_meta = {
        "license": inclusion["include_license"],
        "platformEngagement": inclusion["include_platform_engagement"],
        "im": inclusion["include_im"],
        "detectors": inclusion["include_detectors"],
        "dashboards": inclusion["include_dashboards"],
        "apm": inclusion["include_apm"],
        "rum": inclusion["include_rum"],
        "synthetics": inclusion["include_synthetics"],
        "tokens": inclusion["include_tokens"],
        "otelCollectors": inclusion["include_otel_collectors"],
        "severityLegend": any(
            (
                inclusion["include_im"],
                inclusion["include_detectors"],
                inclusion["include_dashboards"],
                inclusion["include_apm"],
                inclusion["include_rum"],
                inclusion["include_synthetics"],
                inclusion["include_tokens"],
                inclusion["include_otel_collectors"],
            )
        ),
    }

    try:
        md = build_consolidated_report(
            title=title,
            customer=customer,
            realm=realm,
            scope=scope_display,
            license_report=license_report,
            platform_engagement_report=platform_engagement_report,
            im_report=im_report,
            im_integrations_report=im_integrations_report,
            detectors_report=detectors_report,
            dashboards_report=dashboards_report,
            apm_report=apm_report,
            rum_report=rum_report,
            synthetics_report=synthetics_report,
            token_report=token_report,
            otel_report=otel_report,
            **inclusion,
        )
    except Exception:
        logger.exception("Failed to build consolidated report")
        _emit_hub_progress({"phase": "consolidate", "status": "done", "ok": False})
        return 1

    try:
        out_path.write_text(md, encoding="utf-8")
    except OSError as e:
        logger.exception("Could not write report file %s: %s", out_path, e)
        _emit_hub_progress({"phase": "consolidate", "status": "done", "ok": False})
        return 1

    logger.info("Wrote consolidated markdown: %s", out_path)
    _emit_hub_progress({"phase": "consolidate", "status": "done", "ok": True})

    json_path: Path | None = None
    if args.json_out:
        json_path = Path(args.json_out).expanduser().resolve()
    elif profile_bool(profile.get("health_check_json_out"), False):
        json_path = default_json_output_path(out_path)

    combined: dict[str, Any] | None = None
    if json_path or pptx_out_path or pdf_out_path:
        combined = {
            "schema": 1,
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "title": title,
            "customer": customer,
            "realm": realm,
            "reportSections": report_sections_meta,
            "license": license_report,
            "platformEngagement": platform_engagement_report,
            "im": im_report,
            "im_integrations": im_integrations_report,
            "detectors": detectors_report,
            "dashboards": dashboards_report,
            "apm": apm_report,
            "rum": rum_report,
            "synthetics": synthetics_report,
            "tokens": token_report,
            "otelCollectors": otel_report,
        }

    if json_path and combined is not None:
        try:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
            logger.info("Wrote combined JSON: %s", json_path)
        except OSError as e:
            logger.error("Could not write JSON output %s: %s — continuing", json_path, e)

    if pptx_out_path:
        _emit_hub_progress({"phase": "pptx", "status": "start"})
        pptx_ok = False
        try:
            from o11y_pptx_export import export_pptx  # noqa: WPS433,E402 — optional heavy dep

            if combined is None:
                logger.error("Skipping PowerPoint export — internal error (combined snapshot missing).")
            else:
                pptx_tpl_used: Path | None = None
                if pptx_explicit_template is not None:
                    if pptx_explicit_template.is_file():
                        pptx_tpl_used = pptx_explicit_template
                        if args.pptx_light:
                            logger.info(
                                "Note: --pptx-light is set but --pptx-template takes precedence (%s).",
                                pptx_explicit_template.name,
                            )
                    else:
                        logger.warning(
                            "PPTX template not found (%s); using blank master", pptx_explicit_template
                        )
                else:
                    branded = default_splunk_pptx_template_path(repo_root, use_light=pptx_use_light)
                    if branded.is_file():
                        pptx_tpl_used = branded
                        logger.info(
                            "PPTX: using bundled Splunk %s template (%s)",
                            "light" if pptx_use_light else "dark",
                            branded.name,
                        )
                    else:
                        logger.warning(
                            "Bundled Splunk template missing (%s); using blank master. "
                            "Add splunk-ppt-template/ masters or pass --pptx-template.",
                            branded,
                        )

                pptx_max_rows = profile_int(profile.get("health_check_pptx_max_table_rows"), 7)
                pptx_max_rows = max(1, min(pptx_max_rows, 20))
                pptx_exec_max = profile_int(profile.get("health_check_pptx_max_exec_bullets"), 14)
                pptx_exec_max = max(3, min(pptx_exec_max, 30))

                err_ppt = export_pptx(
                    combined=combined,
                    consolidated_markdown=md,
                    out_path=pptx_out_path,
                    template_path=pptx_tpl_used,
                    max_table_rows=pptx_max_rows,
                    max_exec_bullets=pptx_exec_max,
                )
                if err_ppt:
                    logger.error("%s", err_ppt)
                elif pptx_out_path.is_file():
                    pptx_ok = True
                    logger.info("Wrote executive PowerPoint: %s", pptx_out_path)
                else:
                    logger.warning("PowerPoint export returned no error but file missing: %s", pptx_out_path)
        except Exception:
            logger.exception("PowerPoint export raised an unexpected error")
        finally:
            _emit_hub_progress({"phase": "pptx", "status": "done", "ok": pptx_ok})

    if pdf_out_path:
        _emit_hub_progress({"phase": "pdf", "status": "start"})
        pdf_ok = False
        try:
            from o11y_pdf_export import export_pdf_via_viewer  # noqa: WPS433,E402

            err_pdf = export_pdf_via_viewer(
                markdown_path=out_path,
                pdf_out=pdf_out_path,
                viewer_dir=pdf_viewer_dir,
            )
            if err_pdf:
                logger.error("%s", err_pdf)
            elif pdf_out_path.is_file():
                pdf_ok = True
                logger.info("Wrote viewer PDF (detailed report): %s", pdf_out_path)
            else:
                logger.warning("PDF export returned no error but file missing: %s", pdf_out_path)
        except Exception:
            logger.exception("PDF export raised an unexpected error")
        finally:
            _emit_hub_progress({"phase": "pdf", "status": "done", "ok": pdf_ok})

    if not keep_intermediate:
        for tmp in (
            lic_json,
            pe_json,
            im_json,
            im_int_json,
            det_json,
            dash_json,
            apm_json,
            rum_json,
            syn_json,
            tok_json,
            otel_json,
        ):
            try:
                if tmp.is_file():
                    tmp.unlink()
                    logger.debug("Removed intermediate file %s", tmp)
            except OSError as e:
                logger.warning("Could not remove intermediate file %s: %s", tmp, e)

    # Run summary (stderr only — easy to scan)
    total_elapsed = time.monotonic() - run_t0
    logger.info("-" * 60)
    logger.info("RUN SUMMARY")
    logger.info("  Total wall time: %s (per-check timings logged above)", _fmt_duration_s(total_elapsed))
    if skip_license:
        logger.info("  License utilization: skipped (by config)")
    else:
        logger.info(
            "  License utilization: %s",
            "OK — data loaded" if license_step_ok else "FAILED or empty — see errors above",
        )
    if skip_platform_engagement:
        logger.info("  Platform engagement trends: skipped (by config)")
    else:
        logger.info(
            "  Platform engagement trends: %s",
            "OK — data loaded" if platform_engagement_step_ok else "FAILED or empty — see errors above",
        )
    if skip_im:
        logger.info("  IM metrics usage: skipped (by config)")
        logger.info("  IM integrations: skipped (by config)")
    else:
        logger.info(
            "  IM metrics usage: %s",
            "OK — table populated" if im_step_ok else "FAILED or empty — see errors above",
        )
        logger.info(
            "  IM integrations: %s",
            "OK — list populated" if im_int_step_ok else "FAILED or empty — see errors above",
        )
    if skip_detectors:
        logger.info("  Detectors health check: skipped (by config)")
    else:
        logger.info(
            "  Detectors health check: %s",
            "OK — data loaded" if det_step_ok else "FAILED or empty — see errors above",
        )
    if skip_dashboards:
        logger.info("  Dashboards health check: skipped (by config)")
    else:
        logger.info(
            "  Dashboards health check: %s",
            "OK — data loaded" if dashboards_step_ok else "FAILED or empty — see errors above",
        )
    if skip_apm:
        logger.info("  APM health check: skipped (by config)")
    else:
        logger.info(
            "  APM health check: %s",
            "OK — data loaded" if apm_step_ok else "FAILED or empty — see errors above",
        )
    if skip_rum:
        logger.info("  RUM health check: skipped (by config)")
    else:
        logger.info(
            "  RUM health check: %s",
            "OK — data loaded" if rum_step_ok else "FAILED or empty — see errors above",
        )
    if skip_synthetics:
        logger.info("  Synthetics health check: skipped (by config)")
    else:
        logger.info(
            "  Synthetics health check: %s",
            "OK — data loaded" if synthetics_step_ok else "FAILED or empty — see errors above",
        )
    if skip_tokens:
        logger.info("  Token health check: skipped (by config)")
    else:
        logger.info(
            "  Token health check: %s",
            "OK — data loaded" if token_step_ok else "FAILED or empty — see errors above",
        )
    if skip_otel_collectors:
        logger.info("  OpenTelemetry Collectors: skipped (by config)")
    else:
        logger.info(
            "  OpenTelemetry Collectors: %s",
            "OK — data loaded" if otel_step_ok else "FAILED or empty — see errors above",
        )
    logger.info("  Consolidated report: %s", out_path)
    if pptx_out_path:
        logger.info(
            "  Executive PowerPoint: %s — %s",
            pptx_out_path,
            "written" if pptx_out_path.is_file() else "not created",
        )
    if pdf_out_path:
        logger.info(
            "  Detailed PDF: %s — %s",
            pdf_out_path,
            "written" if pdf_out_path.is_file() else "not created",
        )

    im_any_ok = im_step_ok or im_int_step_ok
    partial = (
        (not skip_license and not license_step_ok)
        or (not skip_platform_engagement and not platform_engagement_step_ok)
        or (not skip_im and not im_any_ok)
        or (not skip_detectors and not det_step_ok)
        or (not skip_dashboards and not dashboards_step_ok)
        or (not skip_apm and not apm_step_ok)
        or (not skip_rum and not rum_step_ok)
        or (not skip_synthetics and not synthetics_step_ok)
        or (not skip_tokens and not token_step_ok)
        or (not skip_otel_collectors and not otel_step_ok)
    )
    if partial:
        logger.warning(
            "Finished with gaps — report file was still written; fix token/realm/network and re-run, "
            "or inspect child script messages above"
        )
        return 0

    logger.info("Finished successfully — all requested checks produced usable data")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
