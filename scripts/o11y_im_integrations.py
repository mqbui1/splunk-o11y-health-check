#!/usr/bin/env python3
"""
Splunk Observability Cloud — list org integrations for the **Analyze Integrations** IM health check.

Calls (same host as other ``app.{realm}.signalfx.com`` tools):

1. ``GET /v2/integration/_/info`` — summary / active flags (merged into list rows when helpful).
2. ``GET /v2/integration?limit=…`` — configured integrations with type and name.

Output is sorted by **Integration Type**, then name, for:

  | Integration Type | Integration Name | Active (T/F) |

Environment: ``SPLUNK_ACCESS_TOKEN`` / profile ``access_token``; ``SPLUNK_REALM`` / profile ``realm``.

Usage::

  python3 scripts/o11y_im_integrations.py --structured-json-out /tmp/im-int.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import load_customer_profile_scalars, resolve_profile_path  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

STRUCTURED_SCHEMA = "o11y_im_integrations/v1"

logger = logging.getLogger(__name__)

# Customer **Recommendation** only — match ``Splunk-Observability-Health-Check.md`` (Analyze Integrations).
_ANALYZE_INTEGRATIONS_RECOMMENDATION_CHECKLIST = (
    "Review the list of data being pulled in Splunk Observability Cloud and filter out any sources that may not be "
    "providing value."
)


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_analyze_integrations_markdown(int_report: dict[str, Any] | None) -> str:
    """
    **Analyze Integrations** subsection for the consolidated IM report (checklist-shaped).
    """
    lines: list[str] = [
        "### Analyze Integrations\n",
        "Analyze cloud integrations (AWS, GCP, Azure) to see if all services have been enabled. "
        "Breakdown of each integration with the sources of data being pulled.\n\n",
        "### Results\n",
        "",
        "| Integration Type | Integration Name | Active (T/F) |",
        "| --- | --- | --- |",
    ]

    if not int_report:
        lines.append("|  |  |  |")
        lines.append("")
        lines.append("### Recommendation\n")
        lines.append("")
        lines.append("*None — check not executed.*\n")
        return "\n".join(lines)

    err = int_report.get("error")
    items = int_report.get("integrations")
    if not isinstance(items, list):
        items = []

    if err or not items:
        lines.append("|  |  |  |")
        lines.append("")
        msg = str(err) if err else "No integration rows returned."
        lines.append("### Recommendation\n")
        lines.append("")
        lines.append(
            f"*Could not populate integrations table:* {_md_cell(msg)} "
            "Verify Splunk Observability access for this org.\n"
        )
        return "\n".join(lines)

    for it in items:
        if not isinstance(it, dict):
            continue
        t = _md_cell(str(it.get("integrationType") or "—"))
        n = _md_cell(str(it.get("integrationName") or "—"))
        a = it.get("active")
        if a is True:
            a_s = "True"
        elif a is False:
            a_s = "False"
        else:
            a_s = "—"
        lines.append(f"| {t} | {n} | {a_s} |")

    lines.append("")
    lines.append("### Recommendation\n")
    lines.append("")
    lines.append(_ANALYZE_INTEGRATIONS_RECOMMENDATION_CHECKLIST + "\n")
    return "\n".join(lines)


def _app_base(realm: str) -> str:
    return f"https://app.{realm}.signalfx.com"


def _get_json(url: str, token: str) -> tuple[Any | None, str | None]:
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
        return None, f"HTTP {e.code}: {err[:2000]}"
    except OSError as e:
        return None, str(e)
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"


def _extract_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "integrations", "data", "items", "rows"):
        v = payload.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    return []


def _info_active_map(payload: Any) -> dict[str, bool]:
    """
    Best-effort map integration id -> active (True/False) from ``/_/info`` response shapes.
    """
    out: dict[str, bool] = {}
    if not isinstance(payload, (dict, list)):
        return out
    if isinstance(payload, list):
        for x in payload:
            if not isinstance(x, dict):
                continue
            iid = x.get("id") or x.get("integrationId")
            if iid is None:
                continue
            a = _active_from_obj(x)
            if a is not None:
                out[str(iid)] = a
        return out
    # dict: try common containers
    for key in ("integrations", "results", "active", "inactive", "byId", "info"):
        v = payload.get(key)
        if isinstance(v, list):
            for x in v:
                if not isinstance(x, dict):
                    continue
                iid = x.get("id") or x.get("integrationId")
                if iid is None:
                    continue
                a = _active_from_obj(x)
                if a is not None:
                    out[str(iid)] = a
    # flat id -> bool
    for k, v in payload.items():
        if isinstance(k, str) and k not in ("results", "integrations") and isinstance(v, bool):
            out[k] = v
        if isinstance(v, dict) and "enabled" in v:
            iid = v.get("id") or k
            if iid is not None:
                a = _active_from_obj(v)
                if a is not None:
                    out[str(iid)] = a
    return out


def _active_from_obj(obj: dict[str, Any]) -> bool | None:
    if "enabled" in obj and isinstance(obj["enabled"], bool):
        return bool(obj["enabled"])
    if "active" in obj and isinstance(obj["active"], bool):
        return bool(obj["active"])
    if "paused" in obj and isinstance(obj["paused"], bool):
        return not bool(obj["paused"])
    if "inactive" in obj and isinstance(obj["inactive"], bool):
        return not bool(obj["inactive"])
    return None


def _row_type_name_active(
    row: dict[str, Any],
    info_map: dict[str, bool],
) -> tuple[str, str, bool | None]:
    itype = (
        row.get("type")
        or row.get("integrationType")
        or row.get("moduleType")
        or row.get("category")
        or "—"
    )
    if not isinstance(itype, str):
        itype = str(itype)
    name = row.get("name") or row.get("title") or row.get("label") or row.get("id") or "—"
    if not isinstance(name, str):
        name = str(name)
    active: bool | None = _active_from_obj(row)
    iid = row.get("id") or row.get("integrationId")
    if active is None and iid is not None and str(iid) in info_map:
        active = info_map[str(iid)]
    return itype.strip() or "—", name.strip() or "—", active


def build_integrations_report(
    *,
    realm: str,
    limit: int,
    list_payload: Any,
    info_payload: Any | None,
) -> dict[str, Any]:
    info_map = _info_active_map(info_payload) if info_payload is not None else {}
    raw_rows = _extract_list(list_payload)
    rows: list[dict[str, Any]] = []
    for r in raw_rows:
        itype, name, active = _row_type_name_active(r, info_map)
        rows.append(
            {
                "integrationType": itype,
                "integrationName": name,
                "active": active,
            }
        )
    rows.sort(key=lambda x: (str(x["integrationType"]).lower(), str(x["integrationName"]).lower()))
    return {
        "schema": STRUCTURED_SCHEMA,
        "realm": realm,
        "limit": limit,
        "integrationCount": len(rows),
        "integrations": rows,
        "note": "Active (T/F) uses integration list fields when present, else /_/info when mappable by id.",
    }


def main() -> int:
    p = argparse.ArgumentParser(description="List Splunk Observability integrations (IM health check).")
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--limit", type=int, default=10000, help="Max integrations (default 10000).")
    p.add_argument("--json-out", metavar="PATH", help="Write raw API JSON (list + info) for debugging.")
    p.add_argument(
        "--structured-json-out",
        metavar="PATH",
        help="Normalized report JSON for o11y_health_check_run.py.",
    )
    p.add_argument("--print-schema", action="store_true", help="Print keys from first list row (stderr).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("IM integrations list starting (GET /v2/integration)")

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

    logger.info("Realm: %s | limit: %s", realm, max(1, min(int(args.limit), 10000)))

    base = _app_base(realm)
    lim = max(1, min(int(args.limit), 10000))
    info_url = f"{base}/v2/integration/_/info"
    list_url = f"{base}/v2/integration?{urllib.parse.urlencode({'limit': str(lim)})}"

    info_payload, info_err = _get_json(info_url, token)
    list_payload, list_err = _get_json(list_url, token)

    if args.json_out:
        bundle = {"info": info_payload, "infoError": info_err, "list": list_payload, "listError": list_err}
        outp = Path(args.json_out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")

    if list_err:
        logger.error("Integration list request failed: %s", list_err[:800])
        structured = {
            "schema": STRUCTURED_SCHEMA,
            "realm": realm,
            "limit": lim,
            "error": list_err,
            "integrations": [],
        }
        if args.structured_json_out:
            Path(args.structured_json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.structured_json_out).write_text(json.dumps(structured, indent=2) + "\n", encoding="utf-8")
            logger.info("Wrote error structured JSON: %s", args.structured_json_out)
        return 1

    if info_err:
        # List is primary; continue without info map
        logger.warning("Integration /_/info unavailable (non-fatal): %s", info_err[:400])
        info_payload = None

    raw_rows = _extract_list(list_payload)
    if args.print_schema and raw_rows:
        print("First integration row keys:", sorted(raw_rows[0].keys()), file=sys.stderr)

    structured = build_integrations_report(
        realm=realm,
        limit=lim,
        list_payload=list_payload,
        info_payload=info_payload,
    )
    if info_err:
        structured["infoWarning"] = info_err

    if args.structured_json_out:
        outp = Path(args.structured_json_out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(structured, indent=2) + "\n", encoding="utf-8")
        logger.info("Wrote structured JSON: %s", args.structured_json_out)

    print(f"# Integrations (realm={realm}, n={structured['integrationCount']})")
    print("integration_type\tintegration_name\tactive")
    for row in structured["integrations"]:
        a = row.get("active")
        a_s = "True" if a is True else ("False" if a is False else "Unknown")
        print(f"{row['integrationType']}\t{row['integrationName']}\t{a_s}")

    logger.info("Done: %s integration row(s) on stdout (TSV)", structured["integrationCount"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
