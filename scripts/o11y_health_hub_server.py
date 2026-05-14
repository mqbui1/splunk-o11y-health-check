#!/usr/bin/env python3
"""
Splunk Observability — local Health Check Hub (web UI).

Runs ``o11y_health_check_run.py`` from the repo with user-supplied parameters.
**Security:** writes a short-lived profile YAML containing the API token under
``data/o11y-health-hub/jobs/<id>/``. Use only on trusted networks; do not expose
this service to the public internet without authentication and TLS.

Usage (from repo root, after ``pip install -r requirements-health-hub.txt``):

  python3 scripts/o11y_health_hub_server.py

Open http://127.0.0.1:8766
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path

# Repo root = parent of scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
HUB_STATIC = REPO_ROOT / "web" / "o11y-health-hub"
JOBS_ROOT = REPO_ROOT / "data" / "o11y-health-hub" / "jobs"
RUNNER = SCRIPTS_DIR / "o11y_health_check_run.py"
VIEWER_DIR = REPO_ROOT / "web" / "o11y-health-report"

# Optional: python-pptx / playwright must be installed for those artifacts (runner logs errors).

_jobs_lock = threading.Lock()
_running_procs: dict[str, "subprocess.Popen[str]"] = {}  # job_id -> live Popen

# Ordered checklist steps shown in the Hub progress UI (IDs must match parser + runner [HUB_PROGRESS] phases).
_HUB_PROGRESS_SPEC: list[tuple[str, str, str | None]] = [
    ("license", "License utilization", "skip_license"),
    ("engagement", "Platform engagement", "skip_platform_engagement"),
    ("im_metrics", "Infrastructure monitoring — metrics usage", "skip_im"),
    ("im_integrations", "Infrastructure monitoring — integrations", "skip_im"),
    ("detectors", "Detectors", "skip_detectors"),
    ("dashboards", "Dashboards", "skip_dashboards"),
    ("apm", "APM", "skip_apm"),
    ("rum", "RUM", "skip_rum"),
    ("synthetics", "Synthetics", "skip_synthetics"),
    ("tokens", "Token health", "skip_tokens"),
    ("otel", "OpenTelemetry collectors", "skip_otel_collectors"),
    ("consolidate", "Build web report (markdown)", None),
    ("pptx", "Executive PowerPoint", None),
    ("pdf", "Detailed PDF (Chromium)", None),
]

_STEP_HINTS: dict[str, str] = {
    "license": "Subscription and utilization from billing APIs.",
    "engagement": "Org-wide sf.org trend KPIs (may wait on license snapshot).",
    "im_metrics": "Top metrics usage / cardinality signals.",
    "im_integrations": "Cloud integrations inventory via API.",
    "detectors": "Often slow for large orgs (inactive MTS sampling, etc.).",
    "dashboards": "Dashboard groups, charts, and detector links.",
    "apm": "Typically one of the longest steps (metrics + traces).",
    "rum": "Session and MMS metrics via SignalFlow.",
    "synthetics": "Synthetic tests and usage.",
    "tokens": "Org access token inventory and expiry.",
    "otel": "Collector versions and depreciation (may call GitHub).",
    "consolidate": "Merging all sections into one markdown report.",
    "pptx": "python-pptx executive deck from combined results.",
    "pdf": "Headless Chromium render; first run can be slower.",
}

_RE_HUB_PROGRESS = re.compile(r"\[HUB_PROGRESS\]\s*(\{.*\})\s*$")
_RE_SUB_START = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[INFO\] ▶ (.+?) — starting\s*$"
)
_RE_SUB_OK = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[INFO\] ▶ (.+?) — finished OK \(exit 0\) in (.+?)\s*$"
)
_RE_SUB_ERR = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[ERROR\] ▶ (.+?) — finished with errors \(exit \d+\) in (.+?)\s*$"
)
_RE_SUB_CANT = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[ERROR\] ▶ (.+?) — could not start subprocess after"
)


def _subprocess_label_to_step_id(label: str) -> str | None:
    l = label.lower()
    if "license utilization" in l:
        return "license"
    if "platform engagement" in l:
        return "engagement"
    if "im metrics usage" in l:
        return "im_metrics"
    if "im integrations" in l:
        return "im_integrations"
    if "detectors health" in l:
        return "detectors"
    if "dashboards health" in l:
        return "dashboards"
    if "apm health" in l:
        return "apm"
    if "rum health" in l:
        return "rum"
    if "synthetics health" in l:
        return "synthetics"
    if "token health" in l:
        return "tokens"
    if "opentelemetry collectors" in l:
        return "otel"
    return None


def _fmt_elapsed_hum(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _opt_api_date(val: object) -> str | None:
    """Return ``YYYY-MM-DD`` or ``None``; raises ``ValueError`` if non-empty but invalid."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise ValueError("Dates must use YYYY-MM-DD (UTC).")
    datetime.strptime(s, "%Y-%m-%d")
    return s


def _opt_api_month(val: object) -> str | None:
    """Return ``YYYY-MM`` (UTC calendar month) or ``None``; raises if non-empty but invalid."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if not re.match(r"^\d{4}-\d{2}$", s):
        raise ValueError("Calendar months must use YYYY-MM (UTC).")
    y, m = int(s[:4]), int(s[5:7])
    if y < 1970 or m < 1 or m > 12:
        raise ValueError("Calendar months must use YYYY-MM (UTC).")
    return s


def _license_month_span_to_utc_dates(start_ym: str, end_ym: str) -> tuple[str, str]:
    """Inclusive UTC window: first day of ``start_ym`` through last day of ``end_ym`` (``YYYY-MM``)."""
    sy, sm = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    start_d = f"{sy:04d}-{sm:02d}-01"
    last = calendar.monthrange(ey, em)[1]
    end_d = f"{ey:04d}-{em:02d}-{last:02d}"
    return start_d, end_d


def _parse_log_ts(line: str) -> datetime | None:
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def compute_job_progress(meta: dict[str, object], log_text: str, *, job_status: str) -> dict[str, object]:
    """Derive step list + overall percent from hub job meta, run.log, and job status."""
    steps: dict[str, dict[str, object]] = {}
    for sid, label, skip_key in _HUB_PROGRESS_SPEC:
        skipped = bool(skip_key and meta.get(skip_key))
        steps[sid] = {
            "id": sid,
            "label": label,
            "hint": _STEP_HINTS.get(sid, ""),
            "status": "skipped" if skipped else "pending",
            "detail": None,
        }

    if not meta.get("output_pptx", True):
        steps["pptx"]["status"] = "skipped"
        steps["pptx"]["detail"] = "Omitted (deliverables option)"
    if not meta.get("output_pdf", True):
        steps["pdf"]["status"] = "skipped"
        steps["pdf"]["detail"] = "Omitted (deliverables option)"

    running_step: str | None = None
    last_start_ts: dict[str, datetime] = {}

    def set_running(sid: str | None, line: str) -> None:
        nonlocal running_step
        running_step = sid
        if sid:
            ts = _parse_log_ts(line)
            if ts:
                last_start_ts[sid] = ts

    for line in log_text.splitlines():
        mhp = _RE_HUB_PROGRESS.search(line)
        if mhp:
            try:
                ev = json.loads(mhp.group(1))
            except json.JSONDecodeError:
                continue
            phase = str(ev.get("phase") or "")
            status = str(ev.get("status") or "")
            if phase not in ("consolidate", "pptx", "pdf"):
                continue
            if status == "start":
                steps[phase]["status"] = "running"
                steps[phase]["detail"] = "In progress…"
                set_running(phase, line)
            elif status == "done":
                ok = bool(ev.get("ok"))
                steps[phase]["status"] = "done" if ok else "error"
                steps[phase]["detail"] = "Completed" if ok else "Failed — see log"
                if running_step == phase:
                    set_running(None, "")
            continue

        ms = _RE_SUB_START.match(line)
        if ms:
            sid = _subprocess_label_to_step_id(ms.group(2))
            if sid and steps[sid]["status"] != "skipped":
                steps[sid]["status"] = "running"
                steps[sid]["detail"] = "Running subprocess…"
                set_running(sid, line)
            continue

        mo = _RE_SUB_OK.match(line)
        if mo:
            sid = _subprocess_label_to_step_id(mo.group(2))
            dur = mo.group(3).strip()
            if sid and steps[sid]["status"] != "skipped":
                steps[sid]["status"] = "done"
                steps[sid]["detail"] = f"Finished in {dur}"
            if sid and running_step == sid:
                set_running(None, "")
            continue

        me = _RE_SUB_ERR.match(line)
        if me:
            sid = _subprocess_label_to_step_id(me.group(2))
            dur = me.group(3).strip()
            if sid and steps[sid]["status"] != "skipped":
                steps[sid]["status"] = "error"
                steps[sid]["detail"] = f"Exited with errors ({dur}) — run continued"
            if sid and running_step == sid:
                set_running(None, "")
            continue

        mc = _RE_SUB_CANT.match(line)
        if mc:
            sid = _subprocess_label_to_step_id(mc.group(1))
            if sid and steps[sid]["status"] != "skipped":
                steps[sid]["status"] = "error"
                steps[sid]["detail"] = "Could not start subprocess"
            if sid and running_step == sid:
                set_running(None, "")
            continue

        if "Skipping license utilization" in line:
            steps["license"]["status"] = "skipped"
            steps["license"]["detail"] = "Skipped by configuration"
        elif "Skipping platform engagement" in line:
            steps["engagement"]["status"] = "skipped"
            steps["engagement"]["detail"] = "Skipped by configuration"
        elif "Skipping IM metrics usage" in line:
            steps["im_metrics"]["status"] = "skipped"
            steps["im_metrics"]["detail"] = "Skipped by configuration"
            steps["im_integrations"]["status"] = "skipped"
            steps["im_integrations"]["detail"] = "Skipped by configuration"
        elif "Skipping detectors health check" in line:
            steps["detectors"]["status"] = "skipped"
            steps["detectors"]["detail"] = "Skipped by configuration"
        elif "Skipping dashboards health check" in line:
            steps["dashboards"]["status"] = "skipped"
            steps["dashboards"]["detail"] = "Skipped by configuration"
        elif "Skipping APM health check" in line:
            steps["apm"]["status"] = "skipped"
            steps["apm"]["detail"] = "Skipped by configuration"
        elif "Skipping RUM health check" in line:
            steps["rum"]["status"] = "skipped"
            steps["rum"]["detail"] = "Skipped by configuration"
        elif "Skipping Synthetics health check" in line:
            steps["synthetics"]["status"] = "skipped"
            steps["synthetics"]["detail"] = "Skipped by configuration"
        elif "Skipping Token health check" in line:
            steps["tokens"]["status"] = "skipped"
            steps["tokens"]["detail"] = "Skipped by configuration"
        elif "Skipping OpenTelemetry Collectors" in line or "Skipping OpenTelemetry collectors" in line:
            steps["otel"]["status"] = "skipped"
            steps["otel"]["detail"] = "Skipped by configuration"

    if job_status == "completed":
        for row in steps.values():
            if row["status"] == "running":
                row["status"] = "done"
                row["detail"] = row["detail"] or "Done"
            elif row["status"] == "pending":
                # Log may be truncated or export phases omitted from tail; runner still exited 0.
                row["status"] = "done"
                row["detail"] = row["detail"] or "Completed"
    elif job_status == "failed":
        for row in steps.values():
            if row["status"] == "running":
                row["status"] = "error"
                row["detail"] = row["detail"] or "Interrupted"
            elif row["status"] == "pending":
                row["status"] = "cancelled"
                row["detail"] = "Not reached — run failed"

    active = [sid for sid, row in steps.items() if row["status"] != "skipped"]
    n_active = len(active) or 1
    acc = 0.0
    has_running = False
    for sid in active:
        st = str(steps[sid]["status"])
        if st in ("done", "error", "cancelled"):
            acc += 1.0
        elif st == "running":
            has_running = True
            acc += 0.45

    if job_status == "queued" and not log_text.strip():
        overall = 0
    elif job_status == "completed":
        overall = 100
    else:
        overall = int(min(99 if job_status == "running" and has_running else 100, 100 * acc / n_active))

    current_id: str | None = None
    for sid in active:
        if steps[sid]["status"] == "running":
            current_id = sid
            break

    running_elapsed_sec: float | None = None
    if current_id and current_id in last_start_ts:
        running_elapsed_sec = max(0.0, (datetime.now() - last_start_ts[current_id]).total_seconds())

    if current_id is None and job_status == "running":
        for sid, _label, sk in _HUB_PROGRESS_SPEC:
            if sk and meta.get(sk):
                continue
            if steps[sid]["status"] == "pending":
                current_id = sid
                break

    step_list = [steps[sid] for sid, _l, _k in _HUB_PROGRESS_SPEC]

    status_line = ""
    if job_status == "completed":
        status_line = "All checklist steps finished — opening the report may take a moment."
    elif job_status == "failed":
        status_line = "The run stopped with an error. Review the technical log and partial outputs below."
    elif job_status == "queued":
        status_line = "Queued — starting soon."
    elif current_id:
        row = steps[current_id]
        lbl = str(row["label"])
        if row["status"] == "running":
            if running_elapsed_sec is not None and running_elapsed_sec >= 2:
                status_line = f"Now running: {lbl} (about {_fmt_elapsed_hum(running_elapsed_sec)} so far)"
            else:
                status_line = f"Now running: {lbl}"
        elif row["status"] == "pending":
            status_line = f"Next up: {lbl}"
        else:
            status_line = lbl

    return {
        "steps": step_list,
        "currentStepId": current_id,
        "overallPercent": overall,
        "runningElapsedSec": running_elapsed_sec,
        "statusLine": status_line,
        "footnote": "Durations depend on org size, API latency, and optional PDF/Chromium cold start. "
        "Long-running steps are normal.",
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_bool(val: object, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if val in (1, "1", "true", "yes", "on"):
        return True
    if val in (0, "0", "false", "no", "off"):
        return False
    if val is None:
        return default
    return default


def _yaml_scalar(s: str) -> str:
    """JSON double-quoted string is valid YAML for scalars with special chars."""
    return json.dumps(s, ensure_ascii=False)


def _build_metric_row(im_mod: Any, r: dict, mname: str, bc: str, util: str, mts: float, pct: float) -> dict:
    """Build a metric row dict with all available governance columns."""
    intish = im_mod._intish
    row_get = im_mod._row_get_ci
    return {
        "metricName": mname,
        "billingClass": bc,
        "utilization": util,
        "averageHourlyMts": round(mts, 2),
        "pctOfOrgTotal": round(pct, 4),
        "source": im_mod._optional_source(r),
        "detectors": intish(row_get(r, "usedInDetectors", "detectorCount", "detectorsCount", "numDetectors")) or 0,
        "activeCharts": intish(row_get(r, "usedInActiveCharts", "activeChartCount", "activeChartsCount", "numActiveCharts")) or 0,
        "inactiveCharts": intish(row_get(r, "usedInInactiveCharts", "inactiveChartCount", "inactiveChartsCount", "numInactiveCharts")) or 0,
        "apiQueries": intish(row_get(r, "usedInApi", "apiQueryCount", "numApiQueries", "apiCount")) or 0,
        "dimensionCount": intish(row_get(r, "dimensionCount", "numDimensions", "dimensions", "cardinalityCount")) or 0,
        "creator": str(row_get(r, "creator", "createdBy", "owner", "creatorEmail") or ""),
        "lastUpdated": str(row_get(r, "lastUpdated", "updatedAt", "lastSeen", "lastActiveTime") or ""),
    }


def _write_ephemeral_profile(
    job_dir: Path,
    *,
    title: str,
    company: str,
    realm: str,
    token: str,
    license_start_date: str | None = None,
    license_end_date: str | None = None,
) -> Path:
    p = job_dir / "_ephemeral_profile.yaml"
    body = (
        f"report_title: {_yaml_scalar(title)}\n"
        f"company_or_purpose: {_yaml_scalar(company)}\n"
        f"realm: {_yaml_scalar(realm)}\n"
        f"access_token: {_yaml_scalar(token)}\n"
        "health_check_json_out: \"false\"\n"
    )
    if license_start_date and license_end_date:
        body += f"health_check_license_start_date: {_yaml_scalar(license_start_date)}\n"
        body += f"health_check_license_end_date: {_yaml_scalar(license_end_date)}\n"
    p.write_text(body, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def _build_runner_argv(
    job_dir: Path,
    profile_path: Path,
    *,
    skip_apm: bool,
    skip_im: bool,
    skip_rum: bool,
    skip_synthetics: bool,
    skip_license: bool = False,
    skip_platform_engagement: bool = False,
    skip_detectors: bool = False,
    skip_dashboards: bool = False,
    skip_tokens: bool = False,
    skip_otel_collectors: bool = False,
    output_json: bool = True,
    output_pptx: bool = True,
    output_pdf: bool = True,
) -> list[str]:
    md = job_dir / "report.md"
    pptx = job_dir / "report.pptx"
    pdf = job_dir / "report.pdf"
    js = job_dir / "report.json"
    argv = [
        sys.executable,
        str(RUNNER),
        "--profile",
        str(profile_path),
        "--out",
        str(md),
        "--pdf-viewer-dir",
        str(VIEWER_DIR.resolve()),
    ]
    if output_json:
        argv.extend(["--json-out", str(js)])
    if output_pptx:
        argv.extend(["--pptx-out", str(pptx)])
    if output_pdf:
        argv.extend(["--pdf-out", str(pdf)])
    if skip_apm:
        argv.append("--skip-apm")
    if skip_im:
        argv.append("--skip-im")
    if skip_rum:
        argv.append("--skip-rum")
    if skip_synthetics:
        argv.append("--skip-synthetics")
    if skip_license:
        argv.append("--skip-license")
    if skip_platform_engagement:
        argv.append("--skip-platform-engagement")
    if skip_detectors:
        argv.append("--skip-detectors")
    if skip_dashboards:
        argv.append("--skip-dashboards")
    if skip_tokens:
        argv.append("--skip-tokens")
    if skip_otel_collectors:
        argv.append("--skip-otel-collectors")
    return argv


def _run_job_thread(job_id: str) -> None:
    job_dir = JOBS_ROOT / job_id
    meta_path = job_dir / "meta.json"
    log_path = job_dir / "run.log"
    profile_path = job_dir / "_ephemeral_profile.yaml"

    def update_meta(**kwargs: object) -> None:
        with _jobs_lock:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            data.update(kwargs)
            meta_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError:
        return

    skip_apm = bool(meta.get("skip_apm", False))
    skip_im = bool(meta.get("skip_im", False))
    skip_rum = bool(meta.get("skip_rum", False))
    skip_synthetics = bool(meta.get("skip_synthetics", False))
    skip_license = bool(meta.get("skip_license", False))
    skip_platform_engagement = bool(meta.get("skip_platform_engagement", False))
    skip_detectors = bool(meta.get("skip_detectors", False))
    skip_dashboards = bool(meta.get("skip_dashboards", False))
    skip_tokens = bool(meta.get("skip_tokens", False))
    skip_otel_collectors = bool(meta.get("skip_otel_collectors", False))
    output_json = bool(meta.get("output_json", True))
    output_pptx = bool(meta.get("output_pptx", True))
    output_pdf = bool(meta.get("output_pdf", True))

    update_meta(status="running", started=_utc_now_iso(), error=None)

    argv = _build_runner_argv(
        job_dir,
        profile_path,
        skip_apm=skip_apm,
        skip_im=skip_im,
        skip_rum=skip_rum,
        skip_synthetics=skip_synthetics,
        skip_license=skip_license,
        skip_platform_engagement=skip_platform_engagement,
        skip_detectors=skip_detectors,
        skip_dashboards=skip_dashboards,
        skip_tokens=skip_tokens,
        skip_otel_collectors=skip_otel_collectors,
        output_json=output_json,
        output_pptx=output_pptx,
        output_pdf=output_pdf,
    )

    extra: list[str] = []
    if not skip_license:
        lic_sm = meta.get("license_utilization_start_month")
        lic_em = meta.get("license_utilization_end_month")
        if lic_sm and lic_em:
            ls, le = _license_month_span_to_utc_dates(str(lic_sm), str(lic_em))
            extra.extend(["--license-start-date", ls, "--license-end-date", le])
        else:
            lic_m = meta.get("license_calendar_month")
            if lic_m:
                extra.extend(["--license-calendar-month", str(lic_m)])
            else:
                ls = meta.get("license_start_date")
                le = meta.get("license_end_date")
                if ls and le:
                    extra.extend(["--license-start-date", str(ls), "--license-end-date", str(le)])
    pe_bm = meta.get("pe_baseline_month")
    pe_cm = meta.get("pe_comparison_month")
    if pe_bm and pe_cm:
        extra.extend(
            [
                "--pe-baseline-month",
                str(pe_bm),
                "--pe-comparison-month",
                str(pe_cm),
            ]
        )
    else:
        pc0, pc1, pb0, pb1 = (
            meta.get("pe_compare_current_start"),
            meta.get("pe_compare_current_end"),
            meta.get("pe_compare_baseline_start"),
            meta.get("pe_compare_baseline_end"),
        )
        if pc0 and pc1 and pb0 and pb1:
            extra.extend(
                [
                    "--pe-compare-current-start-date",
                    str(pc0),
                    "--pe-compare-current-end-date",
                    str(pc1),
                    "--pe-compare-baseline-start-date",
                    str(pb0),
                    "--pe-compare-baseline-end-date",
                    str(pb1),
                ]
            )
        elif meta.get("pe_as_of_date"):
            extra.extend(["--pe-as-of-date", str(meta["pe_as_of_date"])])
    argv = argv + extra

    log_path.write_text("", encoding="utf-8")
    t0 = time.monotonic()
    try:
        with open(log_path, "a", encoding="utf-8", buffering=1) as logf:
            logf.write(f"[hub] command: {' '.join(argv[:6])} ... (truncated)\n")
            logf.write(f"[hub] repo_root={REPO_ROOT}\n")
            logf.flush()
            proc = subprocess.Popen(
                argv,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "O11Y_HUB_PROGRESS": "1"},
            )
            _running_procs[job_id] = proc
            assert proc.stdout is not None
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
            rc = proc.wait()
            _running_procs.pop(job_id, None)
        elapsed = time.monotonic() - t0
        artifacts = {
            "hasMarkdown": (job_dir / "report.md").is_file(),
            "hasPptx": (job_dir / "report.pptx").is_file(),
            "hasPdf": (job_dir / "report.pdf").is_file(),
        }
        if rc == 0:
            update_meta(
                status="completed",
                finished=_utc_now_iso(),
                returncode=rc,
                elapsed_sec=round(elapsed, 1),
                **artifacts,
            )
        else:
            update_meta(
                status="failed",
                finished=_utc_now_iso(),
                returncode=rc,
                elapsed_sec=round(elapsed, 1),
                error=f"Runner exited with code {rc}",
                **artifacts,
            )
    except Exception as e:
        update_meta(
            status="failed",
            finished=_utc_now_iso(),
            error=str(e),
            hasMarkdown=(job_dir / "report.md").is_file(),
            hasPptx=(job_dir / "report.pptx").is_file(),
            hasPdf=(job_dir / "report.pdf").is_file(),
        )
    finally:
        _running_procs.pop(job_id, None)
        try:
            if profile_path.is_file():
                profile_path.unlink()
        except OSError:
            pass


def create_app():
    from flask import Flask, jsonify, redirect, request, send_file, send_from_directory
    from urllib.parse import quote

    app = Flask(__name__, static_folder=str(HUB_STATIC), static_url_path="")
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    viewer_root = VIEWER_DIR.resolve()

    @app.route("/")
    def index():
        return send_from_directory(HUB_STATIC, "index.html")

    @app.route("/report")
    def report_page():
        job_id = (request.args.get("id") or "").strip()
        if job_id and _safe_job_id(job_id):
            report_q = quote(f"/api/jobs/{job_id}/markdown", safe="/")
            return redirect(f"/health-report/?report={report_q}&hubJob={job_id}", HTTPStatus.FOUND)
        return redirect("/", HTTPStatus.FOUND)

    @app.get("/health-report")
    def health_report_redirect_slash():
        # Preserve ?report=…&hubJob=… — a bare redirect("/health-report/") drops the query string.
        q = request.query_string.decode("utf-8") if request.query_string else ""
        dest = "/health-report/" + (f"?{q}" if q else "")
        return redirect(dest, HTTPStatus.PERMANENT_REDIRECT)

    @app.get("/health-report/")
    def health_report_index():
        return send_from_directory(str(VIEWER_DIR), "index.html")

    @app.get("/health-report/<path:filename>")
    def health_report_static(filename: str):
        candidate = (VIEWER_DIR / filename).resolve()
        try:
            candidate.relative_to(viewer_root)
        except ValueError:
            return jsonify({"error": "not found"}), HTTPStatus.NOT_FOUND
        if not candidate.is_file():
            return jsonify({"error": "not found"}), HTTPStatus.NOT_FOUND
        return send_from_directory(str(VIEWER_DIR), filename)

    @app.get("/api/jobs")
    def list_jobs():
        rows: list[dict[str, object]] = []
        if not JOBS_ROOT.is_dir():
            return jsonify({"jobs": []})
        for d in sorted(JOBS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            mp = d / "meta.json"
            if not mp.is_file():
                continue
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                # Never expose token
                m.pop("token", None)
                rows.append(m)
            except json.JSONDecodeError:
                continue
        return jsonify({"jobs": rows})

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        if not _safe_job_id(job_id):
            return jsonify({"error": "invalid id"}), HTTPStatus.BAD_REQUEST
        mp = JOBS_ROOT / job_id / "meta.json"
        if not mp.is_file():
            return jsonify({"error": "not found"}), HTTPStatus.NOT_FOUND
        m = json.loads(mp.read_text(encoding="utf-8"))
        m.pop("token", None)
        log_path = JOBS_ROOT / job_id / "run.log"
        log_full = ""
        if log_path.is_file():
            raw = log_path.read_bytes()
            if len(raw) > 5_000_000:
                raw = raw[-5_000_000:]
            log_full = raw.decode("utf-8", errors="replace")
        lines = log_full.splitlines()
        m["logTail"] = "\n".join(lines[-400:])
        m["progress"] = compute_job_progress(
            m,
            log_full,
            job_status=str(m.get("status") or "queued"),
        )
        return jsonify(m)

    @app.get("/api/jobs/<job_id>/markdown")
    def get_markdown(job_id: str):
        if not _safe_job_id(job_id):
            return jsonify({"error": "invalid id"}), HTTPStatus.BAD_REQUEST
        p = JOBS_ROOT / job_id / "report.md"
        if not p.is_file():
            return jsonify({"error": "report not ready"}), HTTPStatus.NOT_FOUND
        return send_file(p, mimetype="text/markdown; charset=utf-8", as_attachment=False)

    @app.get("/api/jobs/<job_id>/download/<kind>")
    def download(job_id: str, kind: str):
        if not _safe_job_id(job_id):
            return jsonify({"error": "invalid id"}), HTTPStatus.BAD_REQUEST
        names = {"pptx": "report.pptx", "pdf": "report.pdf"}
        if kind not in names:
            return jsonify({"error": "bad kind"}), HTTPStatus.BAD_REQUEST
        p = JOBS_ROOT / job_id / names[kind]
        if not p.is_file():
            return jsonify({"error": "file not found"}), HTTPStatus.NOT_FOUND
        return send_file(
            p,
            as_attachment=True,
            download_name=f"health-check-{job_id[:8]}.{kind}",
        )

    @app.post("/api/jobs")
    def post_job():
        data = request.get_json(silent=True) or {}
        company = str(data.get("company") or "").strip()
        report_title = str(data.get("reportTitle") or "").strip()
        token = str(data.get("token") or "").strip()
        realm = str(data.get("realm") or "").strip() or "us0"
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$", realm):
            return jsonify({"error": "invalid realm"}), HTTPStatus.BAD_REQUEST
        products = data.get("products")

        if not company or not report_title or not token:
            return jsonify({"error": "company, reportTitle, and token are required"}), HTTPStatus.BAD_REQUEST

        if products == "all" or products is None:
            skip_apm = skip_im = skip_rum = skip_synthetics = False
        elif isinstance(products, list):
            if len(products) == 0:
                skip_apm = skip_im = skip_rum = skip_synthetics = True
            else:
                pset = {str(x).lower() for x in products}
                skip_apm = "apm" not in pset
                skip_im = "im" not in pset
                skip_rum = "rum" not in pset
                skip_synthetics = "synthetics" not in pset
        else:
            return jsonify({"error": "products must be 'all' or a list"}), HTTPStatus.BAD_REQUEST

        output_json = _json_bool(data.get("outputJson"), True)
        output_pptx = _json_bool(data.get("outputPptx"), True)
        output_pdf = _json_bool(data.get("outputPdf"), True)
        skip_license = _json_bool(data.get("skipLicense"), False)
        skip_platform_engagement = _json_bool(data.get("skipPlatformEngagement"), False)
        skip_detectors = _json_bool(data.get("skipDetectors"), False)
        skip_dashboards = _json_bool(data.get("skipDashboards"), False)
        skip_tokens = _json_bool(data.get("skipTokens"), False)
        skip_otel_collectors = _json_bool(data.get("skipOtelCollectors"), False)

        lic_sm: str | None = None
        lic_em: str | None = None
        lic_um_n = 0
        if not skip_license:
            try:
                lic_sm = _opt_api_month(data.get("licenseUtilizationStartMonth"))
                lic_em = _opt_api_month(data.get("licenseUtilizationEndMonth"))
            except ValueError as e:
                return jsonify({"error": str(e)}), HTTPStatus.BAD_REQUEST

            lic_um_n = sum(1 for x in (lic_sm, lic_em) if x)
            if lic_um_n == 1:
                return jsonify(
                    {
                        "error": "licenseUtilizationStartMonth and licenseUtilizationEndMonth must both be set, "
                        "or both omitted."
                    }
                ), HTTPStatus.BAD_REQUEST
            if lic_sm and lic_em and lic_sm > lic_em:
                return jsonify(
                    {"error": "License utilization start month must not be after end month."}
                ), HTTPStatus.BAD_REQUEST

        pe_comparison_month: str | None = None
        pe_baseline_month: str | None = None
        if skip_platform_engagement:
            pe_um_n = 0
        else:
            try:
                pe_comparison_month = _opt_api_month(data.get("platformEngagementComparisonMonth"))
                pe_baseline_month = _opt_api_month(data.get("platformEngagementBaselineMonth"))
            except ValueError as e:
                return jsonify({"error": str(e)}), HTTPStatus.BAD_REQUEST

            pe_um_n = sum(1 for x in (pe_comparison_month, pe_baseline_month) if x)
            if pe_um_n not in (0, 2):
                return jsonify(
                    {
                        "error": "platformEngagementComparisonMonth and platformEngagementBaselineMonth must "
                        "both be set, or both omitted."
                    }
                ), HTTPStatus.BAD_REQUEST

        job_id = uuid.uuid4().hex
        job_dir = JOBS_ROOT / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "id": job_id,
            "created": _utc_now_iso(),
            "company": company,
            "reportTitle": report_title,
            "realm": realm,
            "products": products if isinstance(products, list) else "all",
            "skip_apm": skip_apm,
            "skip_im": skip_im,
            "skip_rum": skip_rum,
            "skip_synthetics": skip_synthetics,
            "output_json": output_json,
            "output_pptx": output_pptx,
            "output_pdf": output_pdf,
            "skip_license": skip_license,
            "skip_platform_engagement": skip_platform_engagement,
            "skip_detectors": skip_detectors,
            "skip_dashboards": skip_dashboards,
            "skip_tokens": skip_tokens,
            "skip_otel_collectors": skip_otel_collectors,
            "license_utilization_start_month": lic_sm if lic_um_n == 2 else None,
            "license_utilization_end_month": lic_em if lic_um_n == 2 else None,
            "license_calendar_month": None,
            "license_start_date": None,
            "license_end_date": None,
            "pe_baseline_month": pe_baseline_month if pe_um_n == 2 else None,
            "pe_comparison_month": pe_comparison_month if pe_um_n == 2 else None,
            "pe_as_of_date": None,
            "pe_compare_current_start": None,
            "pe_compare_current_end": None,
            "pe_compare_baseline_start": None,
            "pe_compare_baseline_end": None,
            "status": "queued",
            "hasMarkdown": False,
            "hasPptx": False,
            "hasPdf": False,
        }
        (job_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

        lic_prof_start: str | None = None
        lic_prof_end: str | None = None
        if not skip_license and lic_um_n == 2 and lic_sm and lic_em:
            lic_prof_start, lic_prof_end = _license_month_span_to_utc_dates(str(lic_sm), str(lic_em))
        _write_ephemeral_profile(
            job_dir,
            title=report_title,
            company=company,
            realm=realm,
            token=token,
            license_start_date=lic_prof_start,
            license_end_date=lic_prof_end,
        )

        th = threading.Thread(target=_run_job_thread, args=(job_id,), daemon=True)
        th.start()

        return jsonify({"job": meta}), HTTPStatus.CREATED

    @app.get("/api/drilldown/custom-metrics")
    def drilldown_custom_metrics_direct():
        """
        Stateless custom-metrics drill-down: caller provides realm + token via header.
        Used when job session has expired.
        """
        token = (request.headers.get("X-CM-Token") or "").strip()
        realm = str(request.args.get("realm") or "us0").strip()
        if not token:
            return jsonify({"error": "X-CM-Token header required"}), HTTPStatus.BAD_REQUEST
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$", realm):
            return jsonify({"error": "invalid realm"}), HTTPStatus.BAD_REQUEST

        lookback = str(request.args.get("lookback") or "P7D").strip().upper()
        if lookback not in ("P1D", "P7D", "P30D"):
            lookback = "P7D"
        try:
            limit = max(100, min(10000, int(request.args.get("limit") or "2000")))
        except (ValueError, TypeError):
            limit = 2000

        import importlib
        im_mod = importlib.import_module("o11y_im_metrics_usage_breakdown")

        payload, err = im_mod.fetch_metrics_usage_payload(
            token, realm,
            lookback=lookback,
            limit=limit,
            billable=True,
            order_by="-averageHourlyMtsCount",
        )
        if err:
            return jsonify({"error": f"Usage API error: {err}"}), HTTPStatus.BAD_GATEWAY

        rows = im_mod._extract_row_list(payload)
        rows = im_mod._sort_rows(rows)
        total_mts = sum(im_mod._mts_value(r) for r in rows)
        all_rows = []
        for r in rows:
            mname = im_mod._metric_name(r)
            bc = im_mod._billing_class_from_row(r, mname)
            mts = im_mod._mts_value(r)
            pct = (100.0 * mts / total_mts) if total_mts > 0 else 0.0
            util = im_mod._utilization_from_row(r)
            all_rows.append(_build_metric_row(im_mod, r, mname, bc, util, mts, pct))

        custom_rows = [r for r in all_rows if r["billingClass"] == "Custom"]
        total_custom_mts = sum(r["averageHourlyMts"] for r in custom_rows)
        for r in all_rows:
            r["pctOfCustomTotal"] = round(
                100.0 * r["averageHourlyMts"] / total_custom_mts if total_custom_mts > 0 else 0.0, 4
            )
        util_summary: dict[str, float] = {}
        for r in custom_rows:
            util_summary[r["utilization"]] = util_summary.get(r["utilization"], 0) + r["averageHourlyMts"]
        billing_classes = sorted({r["billingClass"] for r in all_rows if r["billingClass"]})

        return jsonify({
            "realm": realm,
            "lookback": lookback,
            "totalOrgMts": round(total_mts, 2),
            "totalCustomMts": round(total_custom_mts, 2),
            "customMetricCount": len(custom_rows),
            "utilizationSummary": util_summary,
            "billingClasses": billing_classes,
            "metrics": all_rows,
        })

    @app.get("/api/jobs/<job_id>/drilldown/custom-metrics")
    def drilldown_custom_metrics(job_id: str):
        """
        On-demand drill-down: return the top custom-billing-class metrics for this job's org.
        Calls /v2/metrics-usage/metrics/_ and filters to Custom billing class.
        Query params: lookback (P1D|P7D|P30D, default P7D), limit (default 1000).
        """
        if not _safe_job_id(job_id):
            return jsonify({"error": "invalid id"}), HTTPStatus.BAD_REQUEST
        job_dir = JOBS_ROOT / job_id
        if not job_dir.is_dir():
            return jsonify({"error": "job not found"}), HTTPStatus.NOT_FOUND
        profile_path = job_dir / "_ephemeral_profile.yaml"
        if not profile_path.is_file():
            # Ephemeral profile was deleted — return realm from meta so frontend can prefill
            meta_path = job_dir / "meta.json"
            realm_hint = "us0"
            lookback_hint = "P7D"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    realm_hint = str(meta.get("realm") or "us0").strip()
                except Exception:
                    pass
            report_path = job_dir / "report.json"
            if report_path.is_file():
                try:
                    rd = json.loads(report_path.read_text(encoding="utf-8"))
                    lb = str(rd.get("im", {}).get("lookbackPeriod") or "P7D").strip().upper()
                    if lb in ("P1D", "P7D", "P30D"):
                        lookback_hint = lb
                except Exception:
                    pass
            return jsonify({"error": "token_required", "realm": realm_hint, "lookback": lookback_hint}), HTTPStatus.UNAUTHORIZED

        # Load realm + token from the ephemeral profile
        try:
            import yaml as _yaml  # type: ignore[import-untyped]
        except ImportError:
            _yaml = None
        try:
            raw_profile = profile_path.read_text(encoding="utf-8")
            if _yaml is not None:
                prof = _yaml.safe_load(raw_profile) or {}
            else:
                # Minimal YAML scalar parser for the two fields we need
                prof = {}
                for line in raw_profile.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        prof[k.strip()] = v.strip().strip("\"'")
        except OSError as e:
            return jsonify({"error": f"could not read profile: {e}"}), HTTPStatus.INTERNAL_SERVER_ERROR

        token = str(prof.get("access_token") or "").strip()
        realm = str(prof.get("realm") or "us0").strip()
        if not token:
            return jsonify({"error": "no token in job profile"}), HTTPStatus.INTERNAL_SERVER_ERROR

        # Use lookback from original report run; fall back to query param, then P7D
        report_lookback = "P7D"
        report_path = job_dir / "report.json"
        if report_path.is_file():
            try:
                report_data = json.loads(report_path.read_text(encoding="utf-8"))
                report_lookback = str(report_data.get("im", {}).get("lookbackPeriod") or "P7D").strip().upper()
                if report_lookback not in ("P1D", "P7D", "P30D"):
                    report_lookback = "P7D"
            except Exception:
                pass
        lookback = str(request.args.get("lookback") or report_lookback).strip().upper()
        if lookback not in ("P1D", "P7D", "P30D"):
            lookback = report_lookback
        try:
            limit = max(100, min(10000, int(request.args.get("limit") or "2000")))
        except (ValueError, TypeError):
            limit = 2000

        # Import the IM breakdown helpers
        import importlib
        im_mod = importlib.import_module("o11y_im_metrics_usage_breakdown")

        payload, err = im_mod.fetch_metrics_usage_payload(
            token, realm,
            lookback=lookback,
            limit=limit,
            billable=True,
            order_by="-averageHourlyMtsCount",
        )
        if err:
            return jsonify({"error": f"Usage API error: {err}"}), HTTPStatus.BAD_GATEWAY

        rows = im_mod._extract_row_list(payload)
        rows = im_mod._sort_rows(rows)

        total_mts = sum(im_mod._mts_value(r) for r in rows)
        all_rows = []
        for r in rows:
            mname = im_mod._metric_name(r)
            bc = im_mod._billing_class_from_row(r, mname)
            mts = im_mod._mts_value(r)
            pct = (100.0 * mts / total_mts) if total_mts > 0 else 0.0
            util = im_mod._utilization_from_row(r)
            all_rows.append(_build_metric_row(im_mod, r, mname, bc, util, mts, pct))

        custom_rows = [r for r in all_rows if r["billingClass"] == "Custom"]
        total_custom_mts = sum(r["averageHourlyMts"] for r in custom_rows)
        for r in all_rows:
            r["pctOfCustomTotal"] = round(
                100.0 * r["averageHourlyMts"] / total_custom_mts if total_custom_mts > 0 else 0.0, 4
            )

        util_summary: dict[str, float] = {}
        for r in custom_rows:
            util_summary[r["utilization"]] = util_summary.get(r["utilization"], 0) + r["averageHourlyMts"]
        billing_classes = sorted({r["billingClass"] for r in all_rows if r["billingClass"]})

        return jsonify({
            "realm": realm,
            "lookback": lookback,
            "totalOrgMts": round(total_mts, 2),
            "totalCustomMts": round(total_custom_mts, 2),
            "customMetricCount": len(custom_rows),
            "utilizationSummary": util_summary,
            "billingClasses": billing_classes,
            "metrics": all_rows,
        })

    def _mts_services_for_metric(token: str, realm: str, metric_name: str) -> tuple[list[dict], list[dict], str | None]:
        """Query /v2/metrictimeseries and return per-service and per-environment MTS counts."""
        import urllib.request, urllib.parse, urllib.error
        query = f'sf_metric:"{metric_name}"'
        url = (
            f"https://api.{realm}.signalfx.com/v2/metrictimeseries"
            f"?query={urllib.parse.quote(query)}&limit=200"
        )
        try:
            req = urllib.request.Request(url, headers={"X-SF-TOKEN": token, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            import sys
            print(f"[metric-services] HTTP {e.code} for metric={metric_name!r} realm={realm!r} token_prefix={token[:8]!r}", file=sys.stderr)
            if e.code in (401, 403):
                return [], [], "token_required"
            return [], [], f"HTTP {e.code}"
        except Exception as e:
            import sys
            print(f"[metric-services] Exception for metric={metric_name!r}: {e}", file=sys.stderr)
            return [], [], str(e)

        results = body.get("results") or []
        service_counts: dict[str, int] = {}
        env_counts: dict[str, int] = {}
        for mts in results:
            dims = mts.get("dimensions") or {}
            svc = None
            for key in ("service", "sf_service", "deployment.service", "k8s.deployment.name"):
                val = dims.get(key)
                if isinstance(val, str) and val.strip():
                    svc = val.strip()
                    break
            if svc:
                service_counts[svc] = service_counts.get(svc, 0) + 1
            for key in ("deployment.environment", "sf_environment", "environment"):
                val = dims.get(key)
                if isinstance(val, str) and val.strip():
                    env_counts[val.strip()] = env_counts.get(val.strip(), 0) + 1
                    break

        services = sorted([{"name": k, "mts": v} for k, v in service_counts.items()], key=lambda x: -x["mts"])
        environments = sorted([{"name": k, "mts": v} for k, v in env_counts.items()], key=lambda x: -x["mts"])
        return services, environments, None

    @app.get("/api/drilldown/metric-services")
    def drilldown_metric_services_direct():
        """Stateless: get services for a metric. Token via X-CM-Token header, realm + metric via query params."""
        token = (request.headers.get("X-CM-Token") or "").strip()
        realm = str(request.args.get("realm") or "us0").strip()
        metric_name = str(request.args.get("metric") or "").strip()
        if not token:
            return jsonify({"error": "X-CM-Token header required"}), HTTPStatus.BAD_REQUEST
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$", realm):
            return jsonify({"error": "invalid realm"}), HTTPStatus.BAD_REQUEST
        if not metric_name:
            return jsonify({"error": "metric param required"}), HTTPStatus.BAD_REQUEST
        services, environments, err = _mts_services_for_metric(token, realm, metric_name)
        if err == "token_required":
            return jsonify({"error": "token_required", "realm": realm}), HTTPStatus.UNAUTHORIZED
        if err:
            return jsonify({"error": err}), HTTPStatus.BAD_GATEWAY
        return jsonify({"metric": metric_name, "services": services, "environments": environments})

    @app.get("/api/jobs/<job_id>/drilldown/metric-services")
    def drilldown_metric_services(job_id: str):
        """Job-bound: get services for a metric using the job's stored credentials."""
        if not _safe_job_id(job_id):
            return jsonify({"error": "invalid id"}), HTTPStatus.BAD_REQUEST
        job_dir = JOBS_ROOT / job_id
        profile_path = job_dir / "_ephemeral_profile.yaml"
        token, realm = "", "us0"
        # Always load realm from meta.json first as the authoritative source
        meta_path = job_dir / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                realm = str(meta.get("realm") or "us0").strip()
            except Exception:
                pass
        if profile_path.is_file():
            try:
                import yaml as _yaml  # type: ignore[import-untyped]
            except ImportError:
                _yaml = None
            try:
                raw = profile_path.read_text(encoding="utf-8")
                prof = _yaml.safe_load(raw) if _yaml else {}
                token = str(prof.get("access_token") or "").strip()
                # Profile realm overrides meta.json if present
                if prof.get("realm"):
                    realm = str(prof["realm"]).strip()
            except Exception:
                pass
        if not token:
            # Fall back to token from request header
            token = (request.headers.get("X-CM-Token") or "").strip()
            if not token:
                return jsonify({"error": "token_required", "realm": realm}), HTTPStatus.UNAUTHORIZED

        metric_name = str(request.args.get("metric") or "").strip()
        if not metric_name:
            return jsonify({"error": "metric param required"}), HTTPStatus.BAD_REQUEST
        services, environments, err = _mts_services_for_metric(token, realm, metric_name)
        if err == "token_required":
            return jsonify({"error": "token_required", "realm": realm}), HTTPStatus.UNAUTHORIZED
        if err:
            return jsonify({"error": err}), HTTPStatus.BAD_GATEWAY
        return jsonify({"metric": metric_name, "services": services, "environments": environments})

    # ── MetricSet breakdown (MMS / TMS by service + environment) ─────────────

    def _run_metricset_breakdown(token: str, realm: str, hours: int = 1) -> tuple[dict, str | None]:
        """
        Run SignalFlow for MMS + TMS org metrics broken down by sf_service + sf_environment.
        Returns ({rows, totals, ...}, error_str | None).
        """
        import importlib as _imp

        try:
            apm_mod = _imp.import_module("o11y_apm_health_check")
            sf_collect = apm_mod.signalflow_matrix_collect
        except Exception as e:
            return {}, f"Cannot import apm module: {e}"

        stream_url = f"https://stream.{realm}.signalfx.com"
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - hours * 3600 * 1000
        resolution_ms = 60_000  # 1-min rollup

        # (service, env) -> {mms, tms}
        results: dict[tuple, dict] = {}

        for metric_key, label in [
            ("sf.org.apm.numMonitoringMetricSets", "mms"),
            ("sf.org.apm.numTroubleshootingMetricSets", "tms"),
        ]:
            program = (
                f"data('{metric_key}')"
                ".mean(by=['sf_service','sf_environment'])"
                f".publish(label='{label}')"
            )
            try:
                meta, pts, err, _ = sf_collect(
                    stream_url=stream_url,
                    token=token,
                    program=program,
                    start_ms=start_ms,
                    stop_ms=now_ms,
                    resolution_ms=resolution_ms,
                    wall_seconds=30.0,
                    read_timeout=30.0,
                    max_data_points=20_000,
                )
            except Exception as e:
                return {}, f"SignalFlow error for {metric_key}: {e}"
            if err:
                return {}, f"SignalFlow error for {metric_key}: {err}"

            # Average value per tsid across all time buckets
            tsid_vals: dict[str, list[float]] = {}
            for pt in pts:
                tsid_vals.setdefault(pt["tsId"], []).append(pt["value"])

            for tsid, vals in tsid_vals.items():
                props = meta.get(tsid, {})
                svc = str(props.get("sf_service") or props.get("service") or "unknown")
                env = str(props.get("sf_environment") or props.get("environment") or "unknown")
                key = (svc, env)
                if key not in results:
                    results[key] = {"service": svc, "environment": env, "mms": 0.0, "tms": 0.0}
                results[key][label] = round(sum(vals) / len(vals), 1)

        rows = sorted(results.values(), key=lambda r: -(r["mms"] + r["tms"]))
        total_mms = round(sum(r["mms"] for r in rows), 1)
        total_tms = round(sum(r["tms"] for r in rows), 1)

        # Org-level totals (no by-dimension — single series per metric)
        org_mms, org_tms = total_mms, total_tms
        for metric_key, field in [
            ("sf.org.apm.numMonitoringMetricSets", "org_mms"),
            ("sf.org.apm.numTroubleshootingMetricSets", "org_tms"),
        ]:
            program = f"data('{metric_key}').mean().publish(label='v')"
            try:
                _, pts, _, _ = sf_collect(
                    stream_url=stream_url,
                    token=token,
                    program=program,
                    start_ms=start_ms,
                    stop_ms=now_ms,
                    resolution_ms=resolution_ms,
                    wall_seconds=20.0,
                    read_timeout=20.0,
                    max_data_points=500,
                )
                if pts:
                    vals2 = [p["value"] for p in pts]
                    v = round(sum(vals2) / len(vals2), 1)
                    if field == "org_mms":
                        org_mms = v
                    else:
                        org_tms = v
            except Exception:
                pass

        return {
            "rows": rows,
            "totalMms": total_mms,
            "totalTms": total_tms,
            "orgMms": org_mms,
            "orgTms": org_tms,
            "hours": hours,
            "realm": realm,
            "rowCount": len(rows),
        }, None

    def _load_job_credentials(job_id: str) -> tuple[str, str]:
        """Return (token, realm) from a job's ephemeral profile + meta.json. token='' if unavailable."""
        job_dir = JOBS_ROOT / job_id
        realm = "us0"
        token = ""
        meta_path = job_dir / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                realm = str(meta.get("realm") or "us0").strip()
            except Exception:
                pass
        profile_path = job_dir / "_ephemeral_profile.yaml"
        if profile_path.is_file():
            try:
                import yaml as _yaml  # type: ignore[import-untyped]
            except ImportError:
                _yaml = None
            try:
                raw = profile_path.read_text(encoding="utf-8")
                prof = _yaml.safe_load(raw) if _yaml else {}
                token = str(prof.get("access_token") or "").strip()
                if prof.get("realm"):
                    realm = str(prof["realm"]).strip()
            except Exception:
                pass
        if not token:
            token = (request.headers.get("X-CM-Token") or "").strip()
        return token, realm

    @app.get("/api/drilldown/metricsets")
    def drilldown_metricsets_direct():
        """Stateless: MMS + TMS breakdown by service + environment. Token via X-CM-Token header."""
        token = (request.headers.get("X-CM-Token") or "").strip()
        realm = str(request.args.get("realm") or "us0").strip()
        if not token:
            return jsonify({"error": "token_required", "realm": realm}), HTTPStatus.UNAUTHORIZED
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$", realm):
            return jsonify({"error": "invalid realm"}), HTTPStatus.BAD_REQUEST
        try:
            hours = max(1, min(24, int(request.args.get("hours") or "1")))
        except (ValueError, TypeError):
            hours = 1
        data, err = _run_metricset_breakdown(token, realm, hours=hours)
        if err:
            return jsonify({"error": err}), HTTPStatus.BAD_GATEWAY
        return jsonify(data)

    @app.get("/api/jobs/<job_id>/drilldown/metricsets")
    def drilldown_metricsets(job_id: str):
        """Job-bound: MMS + TMS breakdown by service + environment using stored credentials."""
        if not _safe_job_id(job_id):
            return jsonify({"error": "invalid id"}), HTTPStatus.BAD_REQUEST
        token, realm = _load_job_credentials(job_id)
        if not token:
            return jsonify({"error": "token_required", "realm": realm}), HTTPStatus.UNAUTHORIZED
        try:
            hours = max(1, min(24, int(request.args.get("hours") or "1")))
        except (ValueError, TypeError):
            hours = 1
        data, err = _run_metricset_breakdown(token, realm, hours=hours)
        if err:
            return jsonify({"error": err}), HTTPStatus.BAD_GATEWAY
        return jsonify(data)

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id: str):
        if not _safe_job_id(job_id):
            return jsonify({"error": "invalid id"}), HTTPStatus.BAD_REQUEST
        job_dir = JOBS_ROOT / job_id
        if not job_dir.is_dir():
            return jsonify({"error": "not found"}), HTTPStatus.NOT_FOUND
        # Kill running subprocess if present
        proc = _running_procs.pop(job_id, None)
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        with _jobs_lock:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"ok": True}), HTTPStatus.OK

    return app


def _safe_job_id(job_id: str) -> bool:
    return bool(job_id) and all(c in "0123456789abcdef" for c in job_id) and len(job_id) == 32


def main() -> int:
    try:
        from flask import Flask  # noqa: F401
    except ImportError:
        print("Install Flask: pip install -r requirements-health-hub.txt", file=sys.stderr)
        return 1

    if not RUNNER.is_file():
        print(f"Runner not found: {RUNNER}", file=sys.stderr)
        return 1

    import argparse

    p = argparse.ArgumentParser(description="Health Check Hub web server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    args = p.parse_args()

    app = create_app()
    print(f"Health Check Hub: http://{args.host}:{args.port}/", flush=True)
    print("Trusted network only — job tokens are written under data/o11y-health-hub/jobs/", flush=True)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
