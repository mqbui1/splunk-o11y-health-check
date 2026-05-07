"""
Shared stderr logging for Splunk Observability standalone CLI scripts.

Design:
  - Logs go to **stderr** so **stdout** stays free for structured JSON or TSV machine output.
  - Call :func:`setup_script_logging` once per process from ``main()`` after parsing ``--verbose``.
  - Use ``logging.getLogger(__name__)`` in each module (or a named logger passed from main).

Secrets: never log access tokens, refresh tokens, or ``Authorization`` headers.
"""

from __future__ import annotations

import logging
import sys


def setup_script_logging(name: str, *, verbose: bool = False) -> logging.Logger:
    """
    Configure a logger writing to stderr. Idempotent for the same logger name: avoids
    duplicate StreamHandlers; updates level when called again (e.g. if re-run in tests).
    """
    level = logging.DEBUG if verbose else logging.INFO
    log = logging.getLogger(name)
    log.setLevel(level)

    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(levelname)s [%(name)s] %(message)s"),
        )
        log.addHandler(handler)
        log.propagate = False

    for handler in log.handlers:
        handler.setLevel(level)
    return log
