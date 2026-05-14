#!/usr/bin/env python3
"""
Splunk Observability Cloud — Token health check (read-only).

Uses ``GET https://api.{realm}.signalfx.com/v2/token`` (paginated) aligned with
``Splunk-Observability-Health-Check.md`` **Token Health Check**.

**Customer reports:** token **names** only — never write secret strings or full token values.

Environment: ``SPLUNK_ACCESS_TOKEN`` / profile ``access_token``; ``realm`` in profile or ``SPLUNK_REALM``.

Optional UI host for **token detail** links (defaults to ``https://app.{realm}.signalfx.com``):
``SIGNALFX_APP_BASE`` or profile ``signalfx_app_base`` (e.g. ``https://us-splunk-shw.signalfx.com``).
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import http.client
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_license_utilization import load_customer_profile_scalars, resolve_profile_path  # noqa: E402
from o11y_script_logging import setup_script_logging  # noqa: E402

STRUCTURED_SCHEMA = "o11y_token_health/v1"

logger = logging.getLogger(__name__)

TOKEN_BASE = "/v2/token"

TOKEN_HEALTH_CHECKLIST: dict[str, dict[str, str]] = {
    "expiredTokens": {
        "title": "Expired Tokens",
        "description": "List of expired tokens.",
        "recommendation": "Delete tokens if no longer needed.",
    },
    "nearExpiration": {
        "title": "Near Expiration Tokens",
        "description": "List of tokens within 90 days of expiration.",
        "recommendation": "Assess whether the token should be extended or rotated.",
    },
}


def api_base(realm: str) -> str:
    return f"https://api.{realm}.signalfx.com"


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


def pick(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _token_types_list(row: dict[str, Any]) -> list[str]:
    """
    Token type labels from ``GET /v2/token`` — prefer ``authScopes`` (e.g. ``API``, ``INGEST``, ``RUM``).

    Falls back to ``permissions`` / ``scopes`` only if ``authScopes`` is absent or empty.
    """
    auth = pick(row, "authScopes", "AuthScopes")
    if isinstance(auth, list) and auth:
        out = [str(p).strip() for p in auth if str(p).strip()]
        if out:
            return out

    perms = pick(row, "permissions", "Permissions", "scopes", "Scopes")
    if isinstance(perms, list):
        return [str(p).strip() for p in perms if str(p).strip()]
    if perms is not None:
        s = str(perms).strip()
        return [s] if s else []
    return []


def normalize_token_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map API token object to display fields (no secrets)."""
    name = str(pick(row, "name", "Name") or "").strip()
    if not name:
        name = "—"
    types_list = _token_types_list(row)
    disabled = pick(row, "disabled", "Disabled")
    if not isinstance(disabled, bool):
        disabled = False

    exp_raw = pick(row, "expiry", "Expiry", "expiresAt", "expires_at", "expirationTime", "expirationMs")
    exp_ms: int | None = None
    if isinstance(exp_raw, (int, float)) and exp_raw > 0:
        e = int(exp_raw)
        if e < 10**11:
            e *= 1000
        exp_ms = e
    elif isinstance(exp_raw, str) and exp_raw.strip():
        try:
            exp_ms = int(exp_raw)
            if exp_ms < 10**11:
                exp_ms *= 1000
        except ValueError:
            try:
                dt = datetime.fromisoformat(exp_raw.replace("Z", "+00:00"))
                exp_ms = int(dt.timestamp() * 1000)
            except (OSError, ValueError, TypeError):
                exp_ms = None

    last_raw = pick(row, "lastAccessTime", "last_access_time", "lastUsed", "last_used", "lastUsedTime")
    last_ms: int | None = None
    if isinstance(last_raw, (int, float)) and last_raw > 0:
        e = int(last_raw)
        if e < 10**11:
            e *= 1000
        last_ms = e
    elif isinstance(last_raw, str) and last_raw.strip():
        try:
            dt = datetime.fromisoformat(last_raw.replace("Z", "+00:00"))
            last_ms = int(dt.timestamp() * 1000)
        except (OSError, ValueError, TypeError):
            last_ms = None

    return {
        "name": name,
        "tokenTypes": types_list,
        "disabled": disabled,
        "expiryMs": exp_ms,
        "lastAccessMs": last_ms,
    }


def fmt_date(ms: int | None) -> str:
    if ms is None:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError, TypeError):
        return "—"


def list_all_tokens(
    token: str,
    realm: str,
    *,
    per_page: int,
    sleep_s: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Paginate ``GET /v2/token`` until a short page or empty batch; throttle with ``sleep_s`` between pages.
    """
    out: list[dict[str, Any]] = []
    offset = 0
    page = 0
    while True:
        payload, err = api_get(
            token,
            realm,
            TOKEN_BASE,
            {"limit": str(per_page), "offset": str(offset)},
        )
        if err:
            return [], err
        if not isinstance(payload, dict):
            return [], "unexpected /v2/token response"
        batch = pick(payload, "results", "Results", "tokens", "data", "items")
        if not isinstance(batch, list):
            return [], "unexpected /v2/token: missing results[]"
        for row in batch:
            if isinstance(row, dict):
                out.append(normalize_token_row(row))
        page += 1
        logger.debug("Token list page %s: %s row(s) (offset=%s)", page, len(batch), offset)

        if len(batch) < per_page:
            break
        offset += per_page
        if sleep_s > 0:
            time.sleep(sleep_s)
    logger.info("Token inventory: %s API row(s) after %s page(s)", len(out), page)
    return out, None


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def resolved_signalfx_app_base(realm: str, override: str | None) -> str:
    """Splunk Observability UI origin for token detail links (hash-route SPA)."""
    o = (override or "").strip()
    if o:
        return o.rstrip("/")
    r = str(realm or "us0").strip() or "us0"
    return f"https://app.{r}.signalfx.com"


def token_detail_ui_url(app_base: str, token_name: str) -> str:
    """
    Opens the token detail screen, e.g. ``…/#/token/detail?tokenName=Name%20Here``.
    Query string is part of the fragment route (same pattern as the Splunk Observability UI).
    """
    base = app_base.rstrip("/")
    enc = urllib.parse.quote(str(token_name), safe="")
    return f"{base}/#/token/detail?tokenName={enc}"


def _md_token_name_link_cell(name: str, app_base: str) -> str:
    """Token Name column: HTML link to token detail (new tab), detector-style."""
    nm = str(name or "").replace("\n", " ").strip()
    if not nm or nm == "—":
        return "—"
    url = token_detail_ui_url(app_base, nm)
    esc_href = html.escape(url, quote=True)
    esc_text = html.escape(nm, quote=False).replace("|", "&#124;")
    return f'<a href="{esc_href}" target="_blank" rel="noopener noreferrer">{esc_text}</a>'


def _md_token_types_cell(types_list: list[Any]) -> str:
    """Single table cell: comma-separated list (multiple permission / scope values)."""
    if not types_list:
        return "—"
    parts = [str(x).strip() for x in types_list if str(x).strip()]
    if not parts:
        return "—"
    return _md_cell(", ".join(parts))


def _subsection_md(key: str, block: dict[str, Any], table_lines: list[str]) -> list[str]:
    c = TOKEN_HEALTH_CHECKLIST[key]
    desc = str(block.get("description") or c["description"]).strip()
    rec = str(block.get("recommendation") or c["recommendation"]).strip()
    return [
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


def render_token_checks_markdown(report: dict[str, Any] | None) -> str:
    """Full ``## Token health check`` section."""
    if not report or report.get("error"):
        return _token_placeholder(str(report.get("error") or "") if report else "")

    chk = report.get("checks") or {}
    parts: list[str] = ["## Token health check\n"]

    realm = str(report.get("realm") or "us0")
    app_base = str(report.get("signalfxAppBase") or "").strip() or resolved_signalfx_app_base(realm, None)

    # Expired: Token Name | Token Type | Expired Date (YYYY-MM-DD)
    ex = chk.get("expiredTokens") or {}
    ex_rows = list(ex.get("rows") or [])
    ex_lines = ["| Token Name | Token Type | Expired Date |", "| --- | --- | --- |"]
    for r in ex_rows:
        name_cell = _md_token_name_link_cell(str(r.get("name") or "—"), app_base)
        tt_cell = _md_token_types_cell(list(r.get("tokenTypes") or []))
        ex_lines.append(
            f"| {name_cell} | {tt_cell} | {_md_cell(str(r.get('expiredDate') or '—'))} |"
        )
    if not ex_rows:
        ex_lines.append("|  |  |  |")
    parts.extend(_subsection_md("expiredTokens", ex, ex_lines))

    # Near expiration: Token Name | Token Type | Expiration Date — sorted by date ascending in data
    ne = chk.get("nearExpiration") or {}
    ne_rows = list(ne.get("rows") or [])
    ne_lines = ["| Token Name | Token Type | Expiration Date |", "| --- | --- | --- |"]
    for r in ne_rows:
        name_cell = _md_token_name_link_cell(str(r.get("name") or "—"), app_base)
        tt_cell = _md_token_types_cell(list(r.get("tokenTypes") or []))
        ne_lines.append(
            f"| {name_cell} | {tt_cell} | {_md_cell(str(r.get('expirationDate') or '—'))} |"
        )
    if not ne_rows:
        ne_lines.append("|  |  |  |")
    parts.extend(_subsection_md("nearExpiration", ne, ne_lines))

    return "\n".join(parts)


def _token_placeholder(err_hint: str) -> str:
    msg = "*None — check not executed.*"
    if err_hint.strip():
        msg = f"*Token automation failed ({_md_cell(err_hint[:200])}).*"
    empty = "|  |  |  |"
    layout: list[tuple[str, list[str]]] = [
        (
            "expiredTokens",
            ["| Token Name | Token Type | Expired Date |", "| --- | --- | --- |", empty],
        ),
        (
            "nearExpiration",
            ["| Token Name | Token Type | Expiration Date |", "| --- | --- | --- |", empty],
        ),
    ]
    parts: list[str] = ["## Token health check\n"]
    for key, lines in layout:
        c = TOKEN_HEALTH_CHECKLIST[key]
        block = {"description": c["description"], "recommendation": msg}
        parts.extend(_subsection_md(key, block, lines))
    return "\n".join(parts)


@dataclass
class TokenHealthConfig:
    realm: str
    per_page: int
    sleep_s: float
    near_expiry_days: int = 90
    #: Optional Splunk Observability UI origin (e.g. ``https://us-splunk-shw.signalfx.com``); default ``app.{realm}``.
    signalfx_app_base: str | None = None


def _expired_sort_key(r: dict[str, Any]) -> tuple[bool, str, str]:
    d = str(r.get("expiredDate") or "—")
    return (d == "—", d, str(r.get("name") or ""))


def run_token_health(token: str, cfg: TokenHealthConfig) -> dict[str, Any]:
    """Classify tokens into checklist tables (names only; no secret values)."""
    tokens, terr = list_all_tokens(token, cfg.realm, per_page=cfg.per_page, sleep_s=cfg.sleep_s)
    if terr:
        logger.error("Token list failed: %s", terr)
        return {"schema": STRUCTURED_SCHEMA, "realm": cfg.realm, "error": terr, "checks": {}}

    now_ms = int(time.time() * 1000)
    near_ms = now_ms + cfg.near_expiry_days * 24 * 3600 * 1000

    expired_rows: list[dict[str, Any]] = []
    near_pending: list[tuple[int, dict[str, Any]]] = []

    for t in tokens:
        nm = t["name"]
        tt: list[str] = list(t.get("tokenTypes") or [])
        exp_ms = t.get("expiryMs")

        if t.get("disabled"):
            expired_rows.append({"name": nm, "tokenTypes": tt, "expiredDate": "—"})
            continue

        if exp_ms is not None and exp_ms < now_ms:
            expired_rows.append(
                {"name": nm, "tokenTypes": tt, "expiredDate": fmt_date(exp_ms)}
            )
        elif exp_ms is not None and now_ms < exp_ms <= near_ms:
            near_pending.append(
                (
                    exp_ms,
                    {"name": nm, "tokenTypes": tt, "expirationDate": fmt_date(exp_ms)},
                )
            )

    expired_rows.sort(key=_expired_sort_key)

    near_pending.sort(key=lambda x: x[0])
    near_rows = [x[1] for x in near_pending]

    checks = {
        "expiredTokens": {
            "rows": expired_rows[:500],
            "description": TOKEN_HEALTH_CHECKLIST["expiredTokens"]["description"],
            "recommendation": TOKEN_HEALTH_CHECKLIST["expiredTokens"]["recommendation"],
        },
        "nearExpiration": {
            "rows": near_rows[:500],
            "description": TOKEN_HEALTH_CHECKLIST["nearExpiration"]["description"],
            "recommendation": TOKEN_HEALTH_CHECKLIST["nearExpiration"]["recommendation"],
        },
    }

    return {
        "schema": STRUCTURED_SCHEMA,
        "realm": cfg.realm,
        "signalfxAppBase": resolved_signalfx_app_base(cfg.realm, cfg.signalfx_app_base),
        "tokenListTotal": len(tokens),
        "checks": checks,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Token health check (Splunk Observability API).")
    p.add_argument("--realm", default=None)
    p.add_argument("--profile", default=None)
    p.add_argument("--per-page", type=int, default=100, help="Page size for GET /v2/token (default 100).")
    p.add_argument("--sleep", type=float, default=0.05, help="Seconds between pages (default 0.05).")
    p.add_argument("--structured-json-out", metavar="PATH", help="Write normalized report JSON.")
    p.add_argument("--md-out", metavar="PATH", help="Write markdown section (## Token health check).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging on stderr.")
    args = p.parse_args()

    setup_script_logging(__name__, verbose=args.verbose)
    logger.info("Token health check starting (GET /v2/token)")

    token = (os.environ.get("SPLUNK_ACCESS_TOKEN") or "").strip()
    realm = (args.realm or os.environ.get("SPLUNK_REALM") or "").strip()
    profile_path = resolve_profile_path(args.profile)
    prof: dict[str, str] = {}
    if profile_path:
        prof = load_customer_profile_scalars(profile_path)
        token = token or (prof.get("access_token") or "").strip()
        realm = realm or (prof.get("realm") or "").strip()
    signalfx_app_override = (os.environ.get("SIGNALFX_APP_BASE") or (prof.get("signalfx_app_base") or "")).strip()
    if not token:
        logger.error("No API token: set SPLUNK_ACCESS_TOKEN or access_token in profile.")
        return 1
    if not realm:
        realm = "us0"

    logger.info("Using realm: %s", realm)

    cfg = TokenHealthConfig(
        realm=realm,
        per_page=max(1, min(args.per_page, 500)),
        sleep_s=max(0.0, float(args.sleep)),
        signalfx_app_base=signalfx_app_override or None,
    )
    report = run_token_health(token, cfg)

    if args.structured_json_out:
        Path(args.structured_json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Wrote structured JSON: %s", args.structured_json_out)
    if args.md_out:
        Path(args.md_out).write_text(render_token_checks_markdown(report), encoding="utf-8")
        logger.info("Wrote markdown: %s", args.md_out)

    if report.get("error"):
        print(json.dumps(report, indent=2))
        logger.error("Report contains error field; exiting non-zero")
        return 1
    brief = {"schema": report["schema"], "realm": report["realm"], "tokenListTotal": report.get("tokenListTotal")}
    print(json.dumps(brief, indent=2))
    logger.info("Done: tokenListTotal=%s", brief.get("tokenListTotal"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
