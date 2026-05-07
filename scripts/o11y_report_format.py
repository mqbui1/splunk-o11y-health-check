"""
Shared numeric formatting for health-check markdown tables:
thousands separators, no scientific notation, fractional part capped at two digits.

Operational logging for CLI scripts lives in :mod:`o11y_script_logging` (stderr; stdout reserved for
tables/JSON where applicable).
"""

from __future__ import annotations

from typing import Any

BYTES_PER_MB_DEFAULT: float = 1_000_000.0

# Maximum fractional digits in any table / MB display (callers cannot exceed via ``decimals``).
MAX_FRACTION_DIGITS: int = 2


def fmt_table_number(
    x: Any,
    *,
    decimals: int | None = None,
    percent: bool = False,
) -> str:
    """
    Format a numeric table cell. Fractional part is never longer than ``MAX_FRACTION_DIGITS``
    (explicit ``decimals`` is clamped to that cap). If ``decimals`` is omitted, integers render
    with commas; other floats use at most that many fractional digits, with trailing zeros stripped.
    With ``percent=True``, appends % (value is on a 0–100 scale).
    """
    if x is None:
        return "—"
    if isinstance(x, str) and x.strip() in ("", "—", "-", "n/a", "N/A"):
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if v != v:  # NaN
        return "—"
    suffix = "%" if percent else ""
    if decimals is not None:
        d = max(0, min(int(decimals), MAX_FRACTION_DIGITS))
        return format(v, f",.{d}f") + suffix
    v2 = round(v, MAX_FRACTION_DIGITS)
    if abs(v2 - round(v2)) < 1e-9 * max(1.0, abs(v2)):
        return f"{int(round(v2)):,}" + suffix
    s = format(v2, f",.{MAX_FRACTION_DIGITS}f").rstrip("0").rstrip(".")
    return s + suffix


def fmt_bytes_as_mb(
    x: Any,
    *,
    bytes_per_mb: float = BYTES_PER_MB_DEFAULT,
    max_decimals: int = 2,
) -> str:
    """Byte amounts shown as MB with comma grouping (no scientific notation)."""
    if x is None:
        return "—"
    try:
        v = float(x) / float(bytes_per_mb)
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"
    if v != v:
        return "—"
    md = max(0, min(int(max_decimals), MAX_FRACTION_DIGITS))
    s = format(v, f",.{md}f").rstrip("0").rstrip(".")
    return s


BYTES_PER_MIB_DEFAULT: float = 1024.0 * 1024.0


def fmt_bytes_as_mib(
    x: Any,
    *,
    bytes_per_mib: float = BYTES_PER_MIB_DEFAULT,
    max_decimals: int = 2,
) -> str:
    """Byte amounts shown as MiB (1024²) with comma grouping (no scientific notation)."""
    if x is None:
        return "—"
    try:
        v = float(x) / float(bytes_per_mib)
    except (TypeError, ValueError, ZeroDivisionError):
        return "—"
    if v != v:
        return "—"
    md = max(0, min(int(max_decimals), MAX_FRACTION_DIGITS))
    s = format(v, f",.{md}f").rstrip("0").rstrip(".")
    return s


def fmt_amount_commas(x: Any) -> str:
    """Alias for table amounts (counts, bytes, etc.) — same rules as ``fmt_table_number``."""
    return fmt_table_number(x)
