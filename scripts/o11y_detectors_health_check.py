#!/usr/bin/env python3
"""
Splunk Observability Cloud — Detectors health check (read-only).

Uses REST on ``https://api.{realm}.signalfx.com`` (same family as MCP: ``/v2/detector``,
``/v2/detector/{id}/events``, ``/v2/metrictimeseries``, ``/v2/incident``, ``/v2/alertmuting``) plus optional
``GET https://app.{realm}.signalfx.com/v2/integration`` to flag inactive notification integrations.

**Redundant detectors:** detectors are grouped when they share at least one **MTS id** among the series sampled from
each ``programText`` (``data('…')`` / ``/v2/metrictimeseries``, heavily capped).

Per-detector work (detail + MTS sampling + events lookup) runs concurrently across a thread pool
(``--concurrency``, default 10) to keep runtime manageable for orgs with hundreds of detectors.

Outputs structured JSON aligned with ``Splunk-Observability-Health-Check.md`` Detectors section.
Detector names in tables render as HTML links (``target="_blank"``, ``rel="noopener noreferrer"``) to
``https://app.{realm}.signalfx.com/#/detector/v2/{id}/edit``.
Heavy orgs: use ``--max-detectors`` to cap per-detector calls (events + optional detail).

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
import http.client
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import load_customer_profile_scalars, resolve_profile_path  # noqa: E402
from o11y_report_format import fmt_table_number  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

logger = logging.getLogger(__name__)

STRUCTURED_SCHEMA = "o11y_detectors_health/v1"

_MS_DAY = 86400 * 1000

# Detectors subsection copy — keep in sync with ``Splunk-Observability-Health-Check.md`` (## Detectors Health Checks).
DETECTOR_HEALTH_CHECKLIST: dict[str, dict[str, str]] = {
    "noisy": {
        "title": "Noisy Detectors",
        "description": (
            "List of detectors that fire constantly with the number of times the detector has fired out in the last week."
        ),
        "recommendation": (
            "Review detector rules and adjust trigger configuration to reduce the alert noise."
        ),
    },
    "nonFiring": {
        "title": "Non-Firing Detectors",
        "description": "List of active detectors that have not triggered in the last 30 days.",
        "recommendation": (
            "Review detector rules, thresholds, and whether the signal is still relevant."
        ),
    },
    "redundant": {
        "title": "Redundant Detectors",
        "description": "Multiple teams may have created overlapping detectors for the same service, or metric.",
        "recommendation": "Review list of redundant detectors.",
    },
    "inactiveDestinations": {
        "title": "Inactive Alert Destinations",
        "description": (
            "Review integrations used for sending alerts to (Slack, Splunk, On-Call ..). "
            "Analyze what alert destinations are being used within detectors and create a list of detectors that are "
            "sending alerts to deactivated or deleted destinations or single e-mail addresses.\n\n"
            "Only **Red** and **Yellow** rows are listed; group/service addresses and active integrations are omitted."
        ),
        "recommendation": (
            "Review alert destinations and routing; remediate deactivated integrations and individual-email risks as "
            "appropriate."
        ),
    },
    "muted": {
        "title": "Muted Detectors",
        "description": (
            "List of detectors that have been muted for longer than 3 days. Someone may have forgotten to re-enable "
            "the detector. Check muting rules to determine if there is a rule in place for the detector"
        ),
        "recommendation": (
            "Review muted detectors and determine if they should be re-enabled. Assess if there is a muting rule that "
            "will re-enable the detector at a certain date"
        ),
    },
}


def _detector_subsection_markdown(
    checklist_key: str,
    block: dict[str, Any],
    table_lines: list[str],
) -> list[str]:
    """Health check name → description → Results → Recommendation (``Splunk-Observability-Health-Check.md``)."""
    c = DETECTOR_HEALTH_CHECKLIST[checklist_key]
    desc = str(block.get("description") or c["description"]).strip()
    rec = str(block.get("recommendation") or c["recommendation"]).strip()
    out: list[str] = [
        f"### {c['title']}\n",
        "",
        desc,
        "",
    ]
    out.extend(
        [
            "### Results",
            "",
            *table_lines,
            "",
            "### Recommendation",
            "",
            rec,
            "",
        ]
    )
    return out


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def api_base(realm: str) -> str:
    return f"https://api.{realm}.signalfx.com"


def app_base(realm: str) -> str:
    return f"https://app.{realm}.signalfx.com"


def detector_edit_url(realm: str, detector_id: str) -> str:
    """Splunk Observability UI edit URL for a detector (realm matches profile, e.g. us0, us1, eu0)."""
    r = str(realm or "").strip()
    if not r:
        return ""
    rid = urllib.parse.quote(str(detector_id).strip(), safe="")
    return f"{app_base(r)}/#/detector/v2/{rid}/edit"


def _md_detector_name_cell(row: dict[str, Any], realm: str) -> str:
    """Table cell: HTML link opening the detector editor in a new tab when URL/id + realm is available."""
    url = row.get("detectorUrl")
    if not url and row.get("detectorId") and realm:
        url = detector_edit_url(str(realm), str(row["detectorId"]))
    name = str(row.get("detectorName") or "").replace("\n", " ")
    if not url:
        return _md_cell(name)
    esc_href = html.escape(str(url), quote=True)
    esc_text = html.escape(name, quote=False).replace("|", "&#124;")
    return f'<a href="{esc_href}" target="_blank" rel="noopener noreferrer">{esc_text}</a>'


def _md_redundant_detector_ids_cell(row: dict[str, Any], realm: str) -> str:
    """Redundant Detector IDs column: comma-separated detector id links (new tab)."""
    ids = row.get("redundantDetectorIdList")
    if not isinstance(ids, list) or not ids:
        return _md_cell(str(row.get("redundantDetectorIds") or ""))
    parts: list[str] = []
    for oid in ids:
        o = str(oid).strip()
        if not o:
            continue
        url = detector_edit_url(realm, o) if realm else ""
        esc_text = html.escape(o, quote=False).replace("|", "&#124;")
        if not url:
            parts.append(esc_text)
            continue
        esc_href = html.escape(url, quote=True)
        parts.append(f'<a href="{esc_href}" target="_blank" rel="noopener noreferrer">{esc_text}</a>')
    if row.get("redundantDetectorIdsHasMore"):
        parts.append("…")
    return ", ".join(parts) if parts else "—"


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
    except (OSError, http.client.IncompleteRead) as e:
        return None, str(e)
    if not raw.strip():
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON {path}: {e}"


def app_get(token: str, realm: str, path: str, params: dict[str, Any] | None = None) -> tuple[Any | None, str | None]:
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}, doseq=True)
    url = f"{app_base(realm).rstrip('/')}{path}"
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
    except (OSError, http.client.IncompleteRead) as e:
        return None, str(e)
    if not raw.strip():
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON {path}: {e}"


def paginate_results(
    token: str,
    realm: str,
    path: str,
    *,
    page_size: int,
    extra_params: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Collect ``results`` arrays from paged GET responses (detector-style)."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = dict(extra_params or {})
        params["limit"] = page_size
        params["offset"] = offset
        payload, err = api_get(token, realm, path, params)
        if err:
            return [], err
        if payload is None:
            break
        if isinstance(payload, list):
            rows = [x for x in payload if isinstance(x, dict)]
            out.extend(rows)
            if len(rows) < page_size:
                break
            offset += page_size
            continue
        if isinstance(payload, dict):
            chunk = payload.get("results")
            if isinstance(chunk, list):
                rows = [x for x in chunk if isinstance(x, dict)]
                out.extend(rows)
                if len(rows) < page_size:
                    break
                offset += page_size
                continue
            return [], f"unexpected envelope keys: {list(payload.keys())[:20]}"
        break
    return out, None


def fetch_detector_events(
    token: str,
    realm: str,
    detector_id: str,
    start_ms: int,
    end_ms: int,
    *,
    page_size: int = 100,
) -> tuple[list[dict[str, Any]], str | None]:
    """GET /v2/detector/{id}/events — paginate by offset."""
    all_ev: list[dict[str, Any]] = []
    offset = 0
    path = f"/v2/detector/{urllib.parse.quote(detector_id, safe='')}/events"
    while True:
        params = {"from": start_ms, "to": end_ms, "limit": page_size, "offset": offset}
        payload, err = api_get(token, realm, path, params)
        if err:
            return [], err
        if payload is None:
            break
        if isinstance(payload, list):
            chunk = [x for x in payload if isinstance(x, dict)]
        else:
            chunk = []
        all_ev.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
        if offset > 100000:
            break
    return all_ev, None


def event_time_ms(ev: dict[str, Any]) -> int | None:
    for k in ("timestamp", "eventTime", "time", "triggeredOn", "createdOn", "startTime"):
        v = ev.get(k)
        if isinstance(v, (int, float)):
            ms = int(v)
            if ms < 10**12:
                ms *= 1000
            return ms
        if isinstance(v, str) and v.isdigit():
            ms = int(v)
            if ms < 10**12:
                ms *= 1000
            return ms
    return None


def count_events_in_windows(
    events: list[dict[str, Any]],
    *,
    now_ms: int,
    win7_ms: int,
    win30_ms: int,
) -> tuple[int, int]:
    """Returns (count_7d, count_30d) based on event timestamps."""
    c7 = c30 = 0
    for ev in events:
        t = event_time_ms(ev)
        if t is None:
            c30 += 1
            continue
        if t >= win30_ms:
            c30 += 1
        if t >= win7_ms:
            c7 += 1
    return c7, c30


def extract_program_text(d: dict[str, Any]) -> str:
    return str(d.get("programText") or d.get("program_text") or "")


def detector_is_disabled_or_inactive(detail: dict[str, Any], list_row: dict[str, Any] | None) -> bool:
    """True when the detector is paused/disabled in the UI (not part of the active non-firing set)."""
    for src in (detail, list_row or {}):
        if not isinstance(src, dict):
            continue
        if src.get("disabled") is True:
            return True
        if isinstance(src.get("enabled"), bool) and not src["enabled"]:
            return True
        if src.get("paused") is True:
            return True
    st = str(detail.get("status") or (list_row or {}).get("status") or "").strip().lower()
    if st in ("disabled", "inactive", "paused"):
        return True
    return False


def extract_metrics_from_program(program: str, *, max_metrics: int = 5) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"""data\(\s*['\"]([^'\"]+)['\"]""", program):
        out.append(m.group(1))
        if len(out) >= max_metrics:
            break
    return out


def _mts_search_query(metric_name: str) -> str:
    esc = metric_name.replace("\\", "\\\\").replace('"', '\\"')
    return f'metric:"{esc}"'


def search_metric_timeseries(
    token: str,
    realm: str,
    *,
    query: str,
    limit: int,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], str | None]:
    """GET /v2/metrictimeseries?query=...&limit=..."""
    payload, err = api_get(
        token,
        realm,
        "/v2/metrictimeseries",
        {"query": query, "limit": str(max(1, limit)), "offset": str(max(0, offset))},
    )
    if err:
        return [], err
    if not isinstance(payload, dict):
        return [], "unexpected metrictimeseries response"
    res = payload.get("results")
    if not isinstance(res, list):
        return [], None
    return [x for x in res if isinstance(x, dict)], None


def collect_mts_ids_for_program(
    token: str,
    realm: str,
    *,
    program: str,
    max_metrics: int,
    per_metric_search_limit: int,
    max_mts_total: int,
    sleep_s: float,
) -> tuple[set[str], str | None]:
    """
    Sample MTS ids from ``programText`` (``data('…')``) via ``/v2/metrictimeseries``. Used to find
    redundant-detector overlap (detectors sharing at least one sampled MTS id).
    """
    metrics = extract_metrics_from_program(program, max_metrics=max_metrics)
    if not metrics:
        return set(), None
    ids: set[str] = set()
    api_err: str | None = None
    remaining = max(1, max_mts_total)
    for metric in metrics:
        if remaining <= 0:
            break
        q = _mts_search_query(metric)
        lim = min(per_metric_search_limit, remaining)
        rows, serr = search_metric_timeseries(token, realm, query=q, limit=lim)
        if serr:
            api_err = api_err or serr
            continue
        for row in rows:
            if remaining <= 0:
                break
            remaining -= 1
            mid = row.get("id")
            if isinstance(mid, str) and mid:
                ids.add(mid)
            if sleep_s > 0:
                time.sleep(sleep_s)
    return ids, api_err


def redundant_detector_groups_from_shared_mts(
    detector_to_mts: dict[str, set[str]],
) -> list[list[str]]:
    """
    Group detector ids that share at least one MTS id in the per-detector samples (union-find).
    Returns sorted groups with length >= 2.
    """
    mts_to_dets: dict[str, list[str]] = {}
    for did, mids in detector_to_mts.items():
        for mid in mids:
            mts_to_dets.setdefault(mid, []).append(did)

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        if x not in parent:
            parent[x] = x
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for dets in mts_to_dets.values():
        if len(dets) < 2:
            continue
        base = dets[0]
        for other in dets[1:]:
            union(base, other)

    by_root: dict[str, list[str]] = {}
    for did in detector_to_mts:
        r = find(did)
        by_root.setdefault(r, []).append(did)

    groups = [sorted(v) for v in by_root.values() if len(v) >= 2]
    groups.sort(key=lambda g: g[0])
    return groups


def walk_notifications(obj: Any, emails: list[str], integration_ids: list[str]) -> None:
    if isinstance(obj, dict):
        t = str(obj.get("type") or "").lower()
        if "email" in t or obj.get("email"):
            e = obj.get("email") or obj.get("address")
            if isinstance(e, str) and "@" in e:
                emails.append(e)
        iid = obj.get("integrationId") or obj.get("integration_id") or obj.get("id")
        if isinstance(iid, str) and len(iid) > 8:
            integration_ids.append(iid)
        for v in obj.values():
            walk_notifications(v, emails, integration_ids)
    elif isinstance(obj, list):
        for x in obj:
            walk_notifications(x, emails, integration_ids)


def parse_notifications(det: dict[str, Any]) -> tuple[list[str], list[str]]:
    emails: list[str] = []
    iids: list[str] = []
    for key in ("notifications", "notification", "teams", "recipients"):
        walk_notifications(det.get(key), emails, iids)
    return emails, iids


# Heuristics: group / service mailboxes are not flagged as individual-risk (Red).
_GROUP_EMAIL_SUBSTRINGS: tuple[str, ...] = (
    "alerts@",
    "alert@",
    "team@",
    "group@",
    "groups@",
    "devops@",
    "sre@",
    "oncall@",
    "on-call",
    "pager",
    "splunk.com",
    "noreply",
    "no-reply",
    "support@",
    "notify@",
    "incidents@",
    "escalation@",
    "monitoring@",
    "observability@",
    "dl-",
    "list@",
    "mailer@",
    "helpdesk@",
    "soc@",
    "noc@",
    "@groups.",
    "mailinglist",
)


def email_looks_like_group_or_service_address(em: str) -> bool:
    """True when the address is treated as a group or service mailbox (omit from findings)."""
    el = em.strip().lower()
    if "@" not in el:
        return False
    local, _, domain = el.partition("@")
    if any(s in el for s in _GROUP_EMAIL_SUBSTRINGS):
        return True
    if local in ("admin", "root", "postmaster", "hostmaster", "abuse", "security"):
        return True
    if len(local) >= 22:
        return True
    if any(x in domain for x in ("jira", "servicenow", "zendesk", "pagerduty", "atlassian")):
        return True
    return False


def detector_state_label(muted: bool, disabled: bool) -> str:
    """UI state for Inactive Alert Destinations column (mutually exclusive)."""
    if muted:
        return "Muted"
    if disabled:
        return "Disabled"
    return "Active"


def classify_inactive_alert_destination_row(
    emails: list[str],
    n_iids: list[str],
    *,
    muted: bool,
    disabled: bool,
    integ_active: set[str],
    integ_err: str | None,
) -> str | None:
    """
    Returns ``Red``, ``Yellow``, or ``None`` (healthy — row omitted from Results).

    Rules (aligned with health-check criteria):
    - No destinations: **Red** if detector is **Active**; **Yellow** if **Disabled** or **Muted**.
    - Individual (non–group/service) email → **Red**.
    - Referenced integration id not in the org's active integration list → **Red** (when list loaded).
    - Group/service email and active integrations only → **None** (do not list).
    - Integration list unavailable but integration ids present → **Yellow** (unknown).
    """
    emails_clean = [e.strip() for e in emails if isinstance(e, str) and e.strip()]
    iids_clean = [str(i).strip() for i in n_iids if i and str(i).strip()]
    has_no_dest = not emails_clean and not iids_clean

    if has_no_dest:
        if detector_state_label(muted, disabled) == "Active":
            return "Red"
        return "Yellow"

    for em in emails_clean:
        if not email_looks_like_group_or_service_address(em):
            return "Red"

    if not integ_err:
        for iid in iids_clean:
            if iid not in integ_active:
                return "Red"

    if integ_err and iids_clean:
        return "Yellow"

    return None


def active_integration_ids(token: str, realm: str) -> tuple[set[str], str | None]:
    """IDs from ``GET /v2/integration`` on app host (enabled integrations)."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload, err = app_get(token, realm, "/v2/integration", {"limit": 1000, "offset": offset})
        if err:
            return set(), err
        if not isinstance(payload, dict):
            break
        chunk = payload.get("results")
        if not isinstance(chunk, list):
            break
        for x in chunk:
            if isinstance(x, dict) and x.get("id"):
                rows.append(x)
        if len(chunk) < 1000:
            break
        offset += 1000
    active: set[str] = set()
    for r in rows:
        iid = str(r.get("id") or "")
        en = r.get("enabled", True)
        if isinstance(en, bool) and en and iid:
            active.add(iid)
        elif en is not False and iid:
            active.add(iid)
    return active, None


def paginate_alertmuting(token: str, realm: str) -> tuple[list[dict[str, Any]], str | None]:
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload, err = api_get(
            token,
            realm,
            "/v2/alertmuting",
            {"limit": 100, "offset": offset},
        )
        if err:
            return [], err
        if not isinstance(payload, dict):
            break
        res = payload.get("results")
        if not isinstance(res, list):
            break
        for x in res:
            if isinstance(x, dict):
                out.append(x)
        if len(res) < 100:
            break
        offset += 100
    return out, None


def muting_rules_for_detector(
    rules: list[dict[str, Any]],
    detector_id: str,
    detector_name: str,
) -> list[str]:
    """Return rule names/ids that might apply to this detector (best-effort string match)."""
    matched: list[str] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or r.get("name") or "")
        desc = json.dumps(r)[:4000]
        if detector_id and detector_id in desc:
            matched.append(rid or "rule")
        elif detector_name and detector_name[:40] in desc:
            matched.append(rid or "rule")
    return matched[:5]


@dataclass
class DetectorHealthConfig:
    realm: str
    max_detectors: int
    noisy_7d_red: int
    noisy_7d_yellow: int
    sleep_s: float
    concurrency: int = 10
    mts_max_metrics: int = 3
    mts_search_limit: int = 3
    mts_max_samples: int = 6


def _process_detector(
    token: str,
    realm: str,
    did: str,
    name: str,
    det_rows: list[dict[str, Any]],
    integ_active: set[str],
    integ_err: str | None,
    muting_rules: list[dict[str, Any]],
    *,
    now_ms: int,
    win7: int,
    win30: int,
    mts_max_metrics: int,
    mts_search_limit: int,
    mts_max_samples: int,
    sleep_s: float,
    noisy_7d_red: int,
    noisy_7d_yellow: int,
) -> dict[str, Any]:
    """Fetch and evaluate everything for a single detector. Safe to run concurrently — only reads shared inputs."""
    detail, gerr = api_get(token, realm, f"/v2/detector/{urllib.parse.quote(did, safe='')}", None)
    if gerr or not isinstance(detail, dict):
        detail = next((r for r in det_rows if str(r.get("id")) == did), {})

    program = extract_program_text(detail)
    sampled_mts: set[str] = set()
    if program:
        sampled_mts, _cid_err = collect_mts_ids_for_program(
            token,
            realm,
            program=program,
            max_metrics=mts_max_metrics,
            per_metric_search_limit=mts_search_limit,
            max_mts_total=mts_max_samples,
            sleep_s=sleep_s,
        )

    events, everr = fetch_detector_events(token, realm, did, win30, now_ms)
    if everr:
        c7, c30 = 0, 0
    else:
        c7, c30 = count_events_in_windows(events, now_ms=now_ms, win7_ms=win7, win30_ms=win30)

    list_row = next((r for r in det_rows if str(r.get("id")) == did), None)
    lr = list_row if isinstance(list_row, dict) else None
    muted = bool(detail.get("muted")) if isinstance(detail.get("muted"), bool) else False
    disabled_ui = detector_is_disabled_or_inactive(detail, lr)
    det_url = detector_edit_url(realm, did)

    noisy_row = None
    if c7 >= noisy_7d_red:
        noisy_row = {"color": "Red", "detectorId": did, "detectorName": name, "detectorUrl": det_url, "triggers7d": c7}
    elif c7 >= noisy_7d_yellow:
        noisy_row = {"color": "Yellow", "detectorId": did, "detectorName": name, "detectorUrl": det_url, "triggers7d": c7}

    non_firing_row = None
    if c30 == 0 and not muted and not disabled_ui:
        non_firing_row = {
            "color": "Yellow", "detectorId": did, "detectorName": name, "detectorUrl": det_url, "triggers30d": c30,
        }

    emails, n_iids = parse_notifications(detail)
    dest_str = "; ".join(emails + [f"integration:{x}" for x in n_iids[:3]]) or "—"
    color_d = classify_inactive_alert_destination_row(
        emails, n_iids, muted=muted, disabled=disabled_ui, integ_active=integ_active, integ_err=integ_err,
    )
    dest_row = None
    if color_d in ("Red", "Yellow"):
        dest_row = {
            "color": color_d,
            "detectorId": did,
            "detectorName": name,
            "detectorUrl": det_url,
            "detectorState": detector_state_label(muted, disabled_ui),
            "alertSentTo": dest_str[:500],
        }

    muted_row = None
    if muted:
        muted_ts = detail.get("muteEndTime") or detail.get("mutedUntil") or detail.get("muteStartDate")
        mute_label = str(muted_ts) if muted_ts else "—"
        rules_h = muting_rules_for_detector(muting_rules or [], did, name)
        rule_s = ", ".join(rules_h) if rules_h else "—"
        muted_row = {
            "color": "Yellow", "detectorId": did, "detectorName": name, "detectorUrl": det_url,
            "mutedDate": mute_label, "mutingRule": rule_s,
        }

    if sleep_s > 0:
        time.sleep(sleep_s)

    return {
        "noisy": noisy_row,
        "nonFiring": non_firing_row,
        "dest": dest_row,
        "muted": muted_row,
        "mts_ids": sampled_mts,
        "events_error": everr,
    }


def run_detector_health(
    token: str,
    cfg: DetectorHealthConfig,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    win7 = now_ms - 7 * _MS_DAY
    win30 = now_ms - 30 * _MS_DAY

    det_rows, err = paginate_results(token, cfg.realm, "/v2/detector", page_size=100)
    if err:
        return {"schema": STRUCTURED_SCHEMA, "realm": cfg.realm, "error": err, "checks": {}}

    # Sort stable: name
    det_rows.sort(key=lambda r: str(r.get("name") or r.get("id") or ""))

    ids_in_order: list[str] = []
    id_to_name: dict[str, str] = {}
    for r in det_rows:
        did = str(r.get("id") or "")
        if not did:
            continue
        ids_in_order.append(did)
        id_to_name[did] = str(r.get("name") or did)

    if len(ids_in_order) > cfg.max_detectors:
        ids_in_order = ids_in_order[: cfg.max_detectors]

    integ_active, integ_err = active_integration_ids(token, cfg.realm)

    muting_rules, muting_err = paginate_alertmuting(token, cfg.realm)

    incidents_note: str | None = None
    noisy_rows: list[dict[str, Any]] = []
    non_firing_rows: list[dict[str, Any]] = []
    dest_rows: list[dict[str, Any]] = []
    muted_rows: list[dict[str, Any]] = []

    # MTS ids sampled per detector (from programText / metrictimeseries); used for redundant overlap.
    detector_to_mts: dict[str, set[str]] = {}

    # Per-detector work (detail + MTS sampling + events lookup) is independent across detectors —
    # run it concurrently so orgs with hundreds of detectors don't blow past the caller's timeout.
    with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:
        futures = {
            pool.submit(
                _process_detector,
                token,
                cfg.realm,
                did,
                id_to_name.get(did, did),
                det_rows,
                integ_active,
                integ_err,
                muting_rules,
                now_ms=now_ms,
                win7=win7,
                win30=win30,
                mts_max_metrics=cfg.mts_max_metrics,
                mts_search_limit=cfg.mts_search_limit,
                mts_max_samples=cfg.mts_max_samples,
                sleep_s=cfg.sleep_s,
                noisy_7d_red=cfg.noisy_7d_red,
                noisy_7d_yellow=cfg.noisy_7d_yellow,
            ): did
            for did in ids_in_order
        }
        results_by_id: dict[str, dict[str, Any]] = {}
        for future in as_completed(futures):
            did = futures[future]
            try:
                results_by_id[did] = future.result()
            except Exception as exc:
                results_by_id[did] = {
                    "noisy": None, "nonFiring": None, "dest": None, "muted": None,
                    "mts_ids": set(), "events_error": str(exc),
                }

    # Aggregate in stable (name-sorted) order even though execution above was concurrent.
    for did in ids_in_order:
        res = results_by_id.get(did) or {}
        if res.get("events_error") and incidents_note is None:
            incidents_note = res["events_error"]
        if res.get("noisy"):
            noisy_rows.append(res["noisy"])
        if res.get("nonFiring"):
            non_firing_rows.append(res["nonFiring"])
        if res.get("dest"):
            dest_rows.append(res["dest"])
        if res.get("muted"):
            muted_rows.append(res["muted"])
        if res.get("mts_ids"):
            detector_to_mts[did] = res["mts_ids"]

    # Redundant: detectors that share at least one sampled MTS id (capped sample per detector).
    redundant_rows: list[dict[str, Any]] = []
    for group in redundant_detector_groups_from_shared_mts(detector_to_mts):
        sorted_g = sorted(group, key=lambda x: (str(id_to_name.get(x, x)).lower(), x))
        cid = sorted_g[0]
        canonical = id_to_name.get(cid, cid)
        others_ids = sorted_g[1:9]
        has_more = len(sorted_g) > 9
        others_plain = ", ".join(others_ids) + ("…" if has_more else "")
        redundant_rows.append(
            {
                "color": "Yellow",
                "detectorId": cid,
                "detectorName": canonical,
                "detectorUrl": detector_edit_url(cfg.realm, cid),
                "redundantDetectorIds": others_plain,
                "redundantDetectorIdList": others_ids,
                "redundantDetectorIdsHasMore": has_more,
            }
        )

    noisy_rows.sort(key=lambda r: -float(r.get("triggers7d") or 0))

    def _chk(key: str, rows: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        base = DETECTOR_HEALTH_CHECKLIST[key]
        out: dict[str, Any] = {
            "rows": rows,
            "description": base["description"],
            "recommendation": base["recommendation"],
        }
        out.update(extra)
        return out

    checks = {
        "noisy": _chk("noisy", noisy_rows[:200]),
        "nonFiring": _chk("nonFiring", non_firing_rows[:200]),
        "redundant": _chk("redundant", redundant_rows[:200]),
        "inactiveDestinations": _chk("inactiveDestinations", dest_rows[:500]),
        "muted": _chk("muted", muted_rows[:200]),
    }

    return {
        "schema": STRUCTURED_SCHEMA,
        "realm": cfg.realm,
        "detectorListTotal": len(det_rows),
        "detectorsAnalyzed": len(ids_in_order),
        "maxDetectorsCap": cfg.max_detectors,
        "windows": {
            "nowMs": now_ms,
            "last7dMs": win7,
            "last30dMs": win30,
        },
        "integrationListError": integ_err,
        "mutingRulesError": muting_err,
        "incidentsNote": incidents_note,
        "checks": checks,
    }


def _detectors_placeholder_markdown() -> str:
    """Checklist-shaped placeholder when automation is skipped or failed (no import from runner — avoids cycles)."""
    layout: list[tuple[str, str, str, str]] = [
        ("noisy", "| Color | Detector Name | Number of Triggers (7 Days) |", "| --- | --- | --- |", "|  |  |  |"),
        ("nonFiring", "| Color | Detector Name | Number of Triggers (30 Days) |", "| --- | --- | --- |", "|  |  |  |"),
        ("redundant", "| Color | Detector Name | Redundant Detector IDs |", "| --- | --- | --- |", "|  |  |  |"),
        (
            "inactiveDestinations",
            "| Color | Detector Name | Detector state | Alert Sent to: |",
            "| --- | --- | --- | --- |",
            "|  |  |  |  |",
        ),
        ("muted", "| Color | Detector Name | Muted Date | Muting Rule |", "| --- | --- | --- | --- |", "|  |  |  |  |"),
    ]
    parts: list[str] = ["## Detectors health checks\n"]
    for ck, h, sep, empty in layout:
        c = DETECTOR_HEALTH_CHECKLIST[ck]
        block = {
            "description": c["description"],
            "recommendation": "*None — check not executed.*",
        }
        parts.extend(
            _detector_subsection_markdown(
                ck,
                block,
                [h, sep, empty],
            )
        )
    return "\n".join(parts)


def render_detectors_checks_markdown(report: dict[str, Any] | None) -> str:
    """Full ``## Detectors health checks`` section for the consolidated report."""
    if not report or report.get("error"):
        return _detectors_placeholder_markdown()

    parts: list[str] = ["## Detectors health checks\n"]
    chk = report.get("checks") or {}
    realm = str(report.get("realm") or "")

    # Noisy
    noisy = chk.get("noisy") or {}
    nrows = sorted(noisy.get("rows") or [], key=lambda r: -float(r.get("triggers7d") or 0))
    n_lines = [
        "| Color | Detector Name | Number of Triggers (7 Days) |",
        "| --- | --- | --- |",
    ]
    for r in nrows:
        n_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_detector_name_cell(r, realm)} | "
            f"{fmt_table_number(r.get('triggers7d'))} |"
        )
    if not nrows:
        n_lines.append("|  |  |  |")
    parts.extend(_detector_subsection_markdown("noisy", noisy, n_lines))

    # Non-firing
    nf = chk.get("nonFiring") or {}
    nfrows = sorted(nf.get("rows") or [], key=lambda r: str(r.get("detectorName") or ""))
    nf_lines = [
        "| Color | Detector Name | Number of Triggers (30 Days) |",
        "| --- | --- | --- |",
    ]
    for r in nfrows:
        nf_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_detector_name_cell(r, realm)} | "
            f"{fmt_table_number(r.get('triggers30d'))} |"
        )
    if not nfrows:
        nf_lines.append("|  |  |  |")
    parts.extend(_detector_subsection_markdown("nonFiring", nf, nf_lines))

    # Redundant
    red = chk.get("redundant") or {}
    rrows = red.get("rows") or []
    r_lines = [
        "| Color | Detector Name | Redundant Detector IDs |",
        "| --- | --- | --- |",
    ]
    for r in rrows:
        r_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_detector_name_cell(r, realm)} | "
            f"{_md_redundant_detector_ids_cell(r, realm)} |"
        )
    if not rrows:
        r_lines.append("|  |  |  |")
    parts.extend(_detector_subsection_markdown("redundant", red, r_lines))

    # Destinations
    dst = chk.get("inactiveDestinations") or {}
    drows = sorted(
        list(dst.get("rows") or []),
        key=lambda r: (0 if str(r.get("color")) == "Red" else 1, str(r.get("detectorName") or "").lower()),
    )
    d_lines = [
        "| Color | Detector Name | Detector state | Alert Sent to: |",
        "| --- | --- | --- | --- |",
    ]
    for r in drows:
        st = str(r.get("detectorState") or "—")
        d_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_detector_name_cell(r, realm)} | "
            f"{_md_cell(st)} | {_md_cell(str(r.get('alertSentTo')))} |"
        )
    if not drows:
        d_lines.append("|  |  |  |  |")
    parts.extend(_detector_subsection_markdown("inactiveDestinations", dst, d_lines))

    # Muted
    mu = chk.get("muted") or {}
    murows = mu.get("rows") or []
    mu_lines = [
        "| Color | Detector Name | Muted Date | Muting Rule |",
        "| --- | --- | --- | --- |",
    ]
    for r in murows:
        mu_lines.append(
            f"| {_md_cell(str(r.get('color')))} | {_md_detector_name_cell(r, realm)} | "
            f"{_md_cell(str(r.get('mutedDate')))} | {_md_cell(str(r.get('mutingRule')))} |"
        )
    if not murows:
        mu_lines.append("|  |  |  |  |")
    parts.extend(_detector_subsection_markdown("muted", mu, mu_lines))

    return "\n".join(parts)


def main() -> int:
    p = argparse.ArgumentParser(description="Detectors health check (Splunk Observability API).")
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--max-detectors", type=int, default=400, help="Max detectors to analyze (default 400).")
    p.add_argument("--noisy-red", type=int, default=100, help="7d event count >= this → Red noisy.")
    p.add_argument("--noisy-yellow", type=int, default=25, help="7d event count >= this → Yellow noisy.")
    p.add_argument("--sleep", type=float, default=0.05, help="Seconds each worker sleeps between its API bursts.")
    p.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Number of detectors processed in parallel via a thread pool (default 10).",
    )
    p.add_argument(
        "--mts-max-metrics",
        type=int,
        default=3,
        help="Max data('metric') names to parse per detector for redundant-detector MTS sampling (default 3).",
    )
    p.add_argument(
        "--mts-search-limit",
        type=int,
        default=3,
        help="Max MTS rows per metric search (default 3).",
    )
    p.add_argument(
        "--mts-max-samples",
        type=int,
        default=6,
        help="Max MTS ids sampled per detector across all metrics (default 6).",
    )
    p.add_argument("--structured-json-out", metavar="PATH", required=False)
    p.add_argument("--md-out", metavar="PATH", required=False)
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("Detectors health check starting (detector list + per-detector analysis)")

    profile_path = resolve_profile_path(args.profile)
    profile: dict[str, str] = {}
    if profile_path and os.path.isfile(profile_path):
        profile = load_customer_profile_scalars(profile_path)

    token = (
        os.environ.get("SPLUNK_ACCESS_TOKEN")
        or profile.get("access_token")
        or profile.get("ACCESS_TOKEN")
        or ""
    ).strip()
    if not token:
        logger.error("No API token: set SPLUNK_ACCESS_TOKEN or access_token in profile.")
        return 1

    realm = (
        args.realm
        or profile.get("realm")
        or os.environ.get("SPLUNK_REALM")
        or "us0"
    ).strip()

    logger.info(
        "Realm: %s | max_detectors: %s | concurrency: %s",
        realm,
        max(1, min(args.max_detectors, 5000)),
        max(1, args.concurrency),
    )

    ny = max(1, args.noisy_yellow)
    nr = max(2, args.noisy_red)
    if ny >= nr:
        ny = nr - 1
    cfg = DetectorHealthConfig(
        realm=realm,
        max_detectors=max(1, min(args.max_detectors, 5000)),
        noisy_7d_red=nr,
        noisy_7d_yellow=ny,
        sleep_s=max(0.0, args.sleep),
        concurrency=max(1, args.concurrency),
        mts_max_metrics=max(1, min(args.mts_max_metrics, 20)),
        mts_search_limit=max(1, min(args.mts_search_limit, 50)),
        mts_max_samples=max(1, min(args.mts_max_samples, 50)),
    )

    report = run_detector_health(token, cfg)
    if args.structured_json_out:
        outp = Path(args.structured_json_out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        logger.info("Wrote structured JSON: %s", args.structured_json_out)

    if args.md_out:
        Path(args.md_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.md_out).write_text(render_detectors_checks_markdown(report), encoding="utf-8")
        logger.info("Wrote markdown: %s", args.md_out)

    err = report.get("error")
    if err:
        logger.error("Detector run finished with error: %s", str(err)[:800])
    else:
        logger.info("Done: analyzed %s detector(s)", report.get("detectorsAnalyzed"))
    print(json.dumps({"ok": not bool(err), "error": err, "detectorsAnalyzed": report.get("detectorsAnalyzed")}))
    return 0 if not err else 1


if __name__ == "__main__":
    raise SystemExit(main())
