#!/usr/bin/env python3
"""
Submit APM tagsAdd for an indexed tag (re-analysis) and poll getTags until analysisJob is READY.
Uses the same GraphQL as server.py (apm_tags_add / apm_get_tag_definitions).
Logs every GraphQL HTTP JSON body to stderr (baseline getTags, tagsAdd, each getTags poll).

Observed behavior: Splunk treats tag indexing by (name, type) — e.g. `version` as **service** vs **global**
are different configurations. Re-running **tagsAdd** for an already **ACTIVE** service-scoped tag often
does **not** populate **analysisJob** (no TMS/MMS re-analysis). Submitting the **same** span tag name
with **global** type can queue a new analysis job because it is a distinct index configuration.

Env:
  SPLUNK_ACCESS_TOKEN (required), SPLUNK_REALM (default us0)
  APM_TAG_ANALYSIS_MAX_ROUNDS — default 30 (60s sleep between polls 2..N)
  APM_TAGS_ADD_TYPE — **service** (default), **global**, **common**, etc. (GraphQL `type` string)
  APM_TAGS_ADD_NAMES — comma-separated tag names (default: version). Service list only used when type=service.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from o11y_script_logging import setup_script_logging  # noqa: E402

logger = logging.getLogger(__name__)

# Services scoped to indexed tag `version` (see reports/indexed-span-tags.md).
VERSION_SERVICES: list[str] = [
    "adservice",
    "ButtercupPayments",
    "cartservice",
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "frontend",
    "mysql:LxvGChW075",
    "paymentservice",
    "productcatalogservice",
    "recommendationservice",
    "redis",
    "shippingservice",
]

# Copied from server.py — shared fragments for getTags + tagsAdd.
_GQL_TAG_DEF_FRAGMENT_BLOCK = """fragment TagsFragment on TagDefinitionTagInfo {
  tagName
  originalTagName
  status
  type
  serviceName
  addedEpochMillis
  lastAnalyzedAtMillis
  dimensions
  __typename
}

fragment AnalysisJobInfoAdaptedFragmentWithMMS on AnalysisJobInfoAdapted {
  status
  lastRanAtMillis
  resultsReadyForMillis
  cardinalityEstimate
  cardinalityEstimateMMS
  cardinalityEntitlement
  cardinalityEntitlementMMS
  analyzedTagSet {
    dimensionalizations {
      id
      tagList
      operations
      services
      environment
      disabled
      __typename
    }
    __typename
  }
  tags {
    ...TagsFragment
  }
  __typename
}

fragment InferredServiceFragment on InferredServiceInfo {
  status
  serviceName
  type
  addedEpochMillis
  lastAnalyzedAtMillis
  dimensions
  __typename
}

fragment TagDefinitionAdaptedResponseFragmentWithErrors on TagDefinitionAdaptedResponse {
  tags {
    ...TagsFragment
    __typename
  }
  analysisJob {
    ...AnalysisJobInfoAdaptedFragmentWithMMS
    __typename
  }
  inferredServices {
    ...InferredServiceFragment
    __typename
  }
  isAnalyzing
  dimensionalizations {
    id
    tagList
    operations
    services
    environment
    disabled
    __typename
  }
  databaseTags {
    commonSqlTags {
      ...TagsFragment
      __typename
    }
    redisTags {
      ...TagsFragment
      __typename
    }
    __typename
  }
  errorTags {
    ...TagsFragment
    __typename
  }
  __typename
}
"""

_GQL_GET_TAGS_DETAILED = _GQL_TAG_DEF_FRAGMENT_BLOCK + """
query getTags {
  getTags {
    ...TagDefinitionAdaptedResponseFragmentWithErrors
    __typename
  }
}
"""

_GQL_TAGS_ADD = _GQL_TAG_DEF_FRAGMENT_BLOCK + """
mutation tagsAdd($names: [String!]!, $type: String!, $service: [String], $dimensionalizations: [DimensionalizationInput!]) {
  tagsAdd(
    names: $names
    type: $type
    service: $service
    dimensionalizations: $dimensionalizations
  ) {
    ...TagDefinitionAdaptedResponseFragmentWithErrors
    __typename
  }
}
"""


def log_api_response(label: str, payload: dict) -> None:
    """Write full API JSON to stderr (no secrets — bodies are GraphQL responses only)."""
    print(f"\n=== {label} ===", file=sys.stderr)
    print(json.dumps(payload, indent=2, ensure_ascii=False), file=sys.stderr)
    print(file=sys.stderr)


def splunk_request(
    method: str,
    path: str,
    body: dict | None,
    app_url: str,
    token: str,
) -> dict:
    url = f"{app_url}{path}"
    headers = {
        "X-SF-Token": token,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw
        raise RuntimeError(f"Splunk API error {e.code}: {json.dumps(detail)}") from e


def _summarize_get_tags(gt: dict) -> tuple[str | None, bool | None, dict]:
    """Returns (analysisJob.status, isAnalyzing, analysisJob or {})."""
    job = gt.get("analysisJob") if isinstance(gt.get("analysisJob"), dict) else {}
    st = job.get("status") if job else None
    ia = gt.get("isAnalyzing")
    if not isinstance(ia, bool):
        ia = None
    return st, ia, job


def _analysis_in_progress(gt: dict) -> bool:
    """True if UI/job signals an active analysis (not necessarily terminal READY/FAILED)."""
    st, ia, _ = _summarize_get_tags(gt)
    if ia is True:
        return True
    if st in (None, "", "READY"):
        return False
    # Typical: ANALYZING (see .cursor/skills/o11y-apm-health-checks/SKILL.md)
    if st == "ANALYZING":
        return True
    # Defensive: any other non-terminal state
    if st not in ("FAILED", "ERROR", "CANCELLED"):
        return True
    return False


def main() -> None:
    """
    Orchestrate tagsAdd + getTags polling. Full GraphQL JSON bodies still go to stderr via
    ``log_api_response`` for deep debugging; structured lines use the logger.
    """
    setup_script_logging(__name__, verbose=False)
    logger.info("APM indexed tag analysis (tagsAdd + getTags poll)")

    token = os.environ.get("SPLUNK_ACCESS_TOKEN")
    if not token:
        logger.error("SPLUNK_ACCESS_TOKEN is required.")
        print("Error: SPLUNK_ACCESS_TOKEN is required.", file=sys.stderr)
        sys.exit(1)
    realm = os.environ.get("SPLUNK_REALM", "us0")
    app_url = f"https://app.{realm}.signalfx.com"
    logger.info("Realm: %s", realm)

    get_body = {
        "operationName": "getTags",
        "variables": {},
        "query": _GQL_GET_TAGS_DETAILED,
    }

    print("getTags baseline (before tagsAdd)...", file=sys.stderr)
    try:
        baseline = splunk_request(
            "POST",
            "/v2/apm/graphql?op=getTags",
            get_body,
            app_url,
            token,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    log_api_response("getTags [baseline — before tagsAdd]", baseline)
    if baseline.get("errors") and not (baseline.get("data") or {}).get("getTags"):
        print("Fatal: getTags baseline returned errors with no data.", file=sys.stderr)
        sys.exit(1)
    if baseline.get("errors"):
        print(
            "Warning: getTags baseline has GraphQL errors but partial data; continuing.",
            file=sys.stderr,
        )

    tag_type = os.environ.get("APM_TAGS_ADD_TYPE", "service").strip().lower()
    raw_names = os.environ.get("APM_TAGS_ADD_NAMES", "version")
    tag_names = [n.strip() for n in raw_names.split(",") if n.strip()]
    if not tag_names:
        tag_names = ["version"]

    add_vars: dict = {"names": tag_names, "type": tag_type}
    if tag_type == "service":
        add_vars["service"] = VERSION_SERVICES

    add_body = {
        "operationName": "tagsAdd",
        "variables": add_vars,
        "query": _GQL_TAGS_ADD,
    }
    print(
        f"Submitting tagsAdd names={tag_names!r} type={tag_type!r}"
        + (" with service list" if tag_type == "service" else "")
        + "...",
        file=sys.stderr,
    )
    try:
        add_resp = splunk_request(
            "POST",
            "/v2/apm/graphql?op=tagsAdd",
            add_body,
            app_url,
            token,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        add_resp = {"http_error": str(e)}
    log_api_response("tagsAdd [full response]", add_resp)
    if add_resp.get("errors"):
        print(
            "Note: tagsAdd returned GraphQL errors (may be benign for already-indexed tags); continuing to poll getTags.",
            file=sys.stderr,
        )

    max_rounds = int(os.environ.get("APM_TAG_ANALYSIS_MAX_ROUNDS", "30"))
    for i in range(max_rounds):
        if i > 0:
            print("Waiting 60s before next poll...", file=sys.stderr)
            time.sleep(60)
        resp = splunk_request(
            "POST",
            "/v2/apm/graphql?op=getTags",
            get_body,
            app_url,
            token,
        )
        log_api_response(f"getTags [poll {i + 1}/{max_rounds}]", resp)
        if resp.get("errors") and not (resp.get("data") or {}).get("getTags"):
            print("Fatal: getTags returned errors with no getTags data.", file=sys.stderr)
            sys.exit(1)
        if resp.get("errors"):
            print(
                "Warning: getTags returned GraphQL errors but partial data; continuing.",
                file=sys.stderr,
            )
        gt = (resp.get("data") or {}).get("getTags") or {}
        st, ia, job = _summarize_get_tags(gt)
        print(
            f"Poll {i + 1}/{max_rounds}: analysisJob.status={st!r} isAnalyzing={ia!r}",
            file=sys.stderr,
        )
        if i == 0:
            if _analysis_in_progress(gt):
                print(
                    "[check] Post-submit getTags shows analysis in progress "
                    f"(status={st!r}, isAnalyzing={ia!r}).",
                    file=sys.stderr,
                )
            elif st == "READY":
                print(
                    "[check] Post-submit getTags already has analysisJob.status=READY "
                    "(no in-flight job — estimates may be from the last completed run).",
                    file=sys.stderr,
                )
            elif not job and st is None:
                print(
                    "[check] Post-submit getTags has no analysisJob object — "
                    "API may not queue TMS/MMS analysis for this tag state "
                    "(e.g. re-adding an already ACTIVE tag with the same type). "
                    "Try APM_TAGS_ADD_TYPE=global if you need a fresh analysis for the same span key.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[check] Post-submit getTags: unexpected state status={st!r} isAnalyzing={ia!r}.",
                    file=sys.stderr,
                )
        status = st
        if status == "READY":
            tms = job.get("cardinalityEstimate")
            mms = job.get("cardinalityEstimateMMS")
            tags = gt.get("tags") or []
            name_set = set(tag_names)
            matched_services = sorted(
                {
                    t["serviceName"]
                    for t in tags
                    if t.get("tagName") in name_set and t.get("type") == tag_type and t.get("serviceName")
                }
            )
            if tag_type == "service":
                svc_cell = ", ".join(matched_services) if matched_services else ", ".join(VERSION_SERVICES)
            elif tag_type == "global":
                svc_cell = "— (global scope)"
            else:
                svc_cell = ", ".join(matched_services) if matched_services else f"— ({tag_type})"

            names_cell = ", ".join(f"`{n}`" for n in tag_names)
            print()
            print("| Indexed tag name(s) | Scope / services | TMS estimate | MMS estimate |")
            print("|---------------------|------------------|--------------|--------------|")
            print(f"| {names_cell} | {svc_cell} | {tms} | {mms} |")
            return

    logger.error("Timed out waiting for analysisJob.status == READY (max_rounds=%s)", max_rounds)
    print("Timed out waiting for analysisJob.status == READY.", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
