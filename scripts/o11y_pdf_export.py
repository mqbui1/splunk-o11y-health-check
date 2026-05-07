"""
Render the static HTML report viewer as a polished PDF via headless Chromium.

Optional dependency: ``playwright`` (see ``requirements-presentation.txt``).
Requires a local HTTP server so the viewer can ``fetch()`` the markdown
(``file://`` URLs block fetch in browsers).

Usage is typically orchestrated by ``o11y_health_check_run.py`` (--pdf-out).
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

logger = logging.getLogger(__name__)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def export_pdf_via_viewer(
    *,
    markdown_path: Path,
    pdf_out: Path,
    viewer_dir: Path,
    timeout_ms: float = 120_000,
) -> str | None:
    """
    Copy ``markdown_path`` beside ``index.html``, serve ``viewer_dir`` on localhost,
    open ``?report=…&fresh=1``, wait for rendered sections, ``page.pdf()`` to ``pdf_out``.
    Returns error message string or ``None``.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError:
        return (
            "playwright is not installed. Run: pip install -r requirements-presentation.txt "
            "&& playwright install chromium"
        )

    markdown_path = markdown_path.expanduser().resolve()
    viewer_dir = viewer_dir.expanduser().resolve()
    pdf_out = pdf_out.expanduser().resolve()

    if not markdown_path.is_file():
        return f"Markdown report not found: {markdown_path}"
    index_html = viewer_dir / "index.html"
    if not index_html.is_file():
        return f"Report viewer folder missing index.html: {viewer_dir}"

    safe_name = f"_o11y_pdf_export_{markdown_path.stem[:80]}.md"
    staged = viewer_dir / safe_name
    try:
        shutil.copy2(markdown_path, staged)
    except OSError as e:
        return f"Could not stage markdown beside viewer: {e}"

    port = _pick_free_port()
    srv = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(viewer_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    err_msg: str | None = None
    try:
        time.sleep(0.15)
        if srv.poll() is not None:
            return "Local http.server exited immediately — could not bind or start."

        q = urllib.parse.urlencode({"report": safe_name, "fresh": "1"})
        url = f"http://127.0.0.1:{port}/index.html?{q}"

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_default_timeout(timeout_ms)
                # Wait for document + subresources; the viewer then fetch()es markdown asynchronously.
                page.goto(url, wait_until="load")
                wait_ui = min(120_000, int(timeout_ms))
                page.wait_for_function(
                    """() => {
                      const lp = document.getElementById('load-panel');
                      const shell = document.getElementById('app-content');
                      if (!lp || !shell) return false;
                      const panelHidden = lp.classList.contains('load-panel--hidden');
                      const shellOn = shell.style.display !== 'none';
                      return panelHidden && shellOn;
                    }""",
                    timeout=wait_ui,
                )
                # Sidebar populates after sections render; large reports need headroom.
                try:
                    page.wait_for_selector("#nav-sections li", timeout=min(60_000, wait_ui))
                except Exception:
                    try:
                        page.wait_for_selector("#sections-container .section-card", timeout=min(45_000, wait_ui))
                    except Exception:
                        page.wait_for_selector("#hero-title", timeout=min(30_000, wait_ui))
                page.wait_for_timeout(1200)
                # Paginated Results tables keep only one page of rows in the DOM; expand for PDF.
                page.evaluate(
                    "() => { if (typeof window.__o11yExpandTablesForPdf === 'function') "
                    "window.__o11yExpandTablesForPdf(); }"
                )
                page.wait_for_timeout(400)

                pdf_out.parent.mkdir(parents=True, exist_ok=True)
                page.pdf(
                    path=str(pdf_out),
                    format="A4",
                    landscape=True,
                    print_background=True,
                    margin={"top": "10mm", "bottom": "12mm", "left": "10mm", "right": "10mm"},
                )
            finally:
                browser.close()
        logger.info("Wrote viewer PDF: %s", pdf_out)
    except Exception as e:
        err_msg = f"PDF export failed: {e}"
        logger.warning("%s", err_msg)
    finally:
        try:
            srv.terminate()
            srv.wait(timeout=5)
        except Exception:
            try:
                srv.kill()
            except Exception:
                pass
        try:
            if staged.is_file():
                staged.unlink()
        except OSError:
            pass

    return err_msg
