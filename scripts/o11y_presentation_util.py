"""Shared helpers for presentation / PDF export (stdlib only — no third-party imports)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def strip_inline_markdown(s: str) -> str:
    """Remove common GFM markers for slide body text."""
    t = (s or "").replace("`", "")
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"\*(.+?)\*", r"\1", t)
    return t.strip()


def extract_executive_bullets_from_markdown(md: str) -> list[str]:
    """Parse bullets under ``## Executive summary`` until the next ``##`` heading."""
    if "## Executive summary" not in md:
        return []
    start = md.index("## Executive summary")
    rest = md[start:]
    m = re.search(r"\n## [^#]", rest[1:])
    chunk = rest if not m else rest[: m.start() + 1]
    out: list[str] = []
    for line in chunk.splitlines():
        s = line.strip()
        if s.startswith("- "):
            out.append(strip_inline_markdown(s[2:]))
    return out


def color_rank(c: str | None) -> int:
    s = (c or "").strip().lower()
    if s == "red":
        return 0
    if s == "orange":
        return 1
    if s == "yellow":
        return 2
    if s == "green":
        return 3
    return 4


def sort_rows_by_severity(rows: list[dict[str, Any]], *, max_n: int) -> list[dict[str, Any]]:
    """Prefer Red, then Yellow; stable by stringified row."""
    if not rows or max_n <= 0:
        return []
    keyed = sorted(
        rows,
        key=lambda r: (color_rank(r.get("color")), str(r).lower()),
    )
    return keyed[:max_n]


def default_splunk_pptx_template_path(repo_root: Path, *, use_light: bool) -> Path:
    """
    Built-in Splunk-branded masters under ``splunk-ppt-template/`` (repo root).
    Default export uses **dark**; pass ``use_light=True`` for the FY27 light master.
    """
    name = (
        "Splunk Template_Light_FY27_04-26.pptx"
        if use_light
        else "Splunk Template_Dark_04-26.pptx"
    )
    return (repo_root / "splunk-ppt-template" / name).resolve()


def resolve_repo_relative_path(repo_root: Path, raw: str) -> Path:
    """Resolve a path from the profile or CLI; relative paths are under ``repo_root``."""
    p = Path(raw).expanduser()
    return (repo_root / p).resolve() if not p.is_absolute() else p.resolve()


def parse_profile_aux_path(
    md_path: Path,
    *,
    profile_val: str | None,
    arg_val: str | None,
    suffix: str,
) -> Path | None:
    """
    Resolve optional output path from CLI or profile.
    ``true`` / ``1`` / ``yes`` → same basename as markdown with ``suffix`` (.pptx / .pdf).
    """
    if arg_val:
        return Path(arg_val).expanduser().resolve()
    val = (profile_val or "").strip()
    if not val or val.lower() in ("false", "0", "no"):
        return None
    if val.lower() in ("true", "1", "yes"):
        return md_path.with_suffix(suffix).resolve()
    return Path(val).expanduser().resolve()
