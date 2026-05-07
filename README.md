# Splunk Observability Health Check (scripts)

Repeatable health assessments against **Splunk Observability Cloud** using **Python 3** scripts. Criteria and report shape follow [`Splunk-Observability-Health-Check.md`](Splunk-Observability-Health-Check.md). (If you use Cursor + Splunk Observability MCP for assisted runs, keep a **local** copy of agent playbooks such as `AGENTS.md` and `.cursor/skills/` — they are **gitignored** and not shipped in this repository.)

**Recommended:** use the **Health Check Hub** (local web UI) to start runs, watch progress, open the interactive report, and download **JSON / PowerPoint / PDF** artifacts. The **command-line orchestrator** remains available for automation, CI, and advanced flags.

Many **per-domain scripts** use only the **stdlib**; the Hub, presentation export, and some integrations need **`pip`** — see [`requirements-health-hub.txt`](requirements-health-hub.txt) and [`requirements-presentation.txt`](requirements-presentation.txt) (`python-pptx`, **playwright** + Chromium). **Splunk-branded light/dark masters** live under [`splunk-ppt-template/`](splunk-ppt-template/README.md).

## Health Check Hub — main workflow

The Hub is a small **Flask** app ([`scripts/o11y_health_hub_server.py`](scripts/o11y_health_hub_server.py)) that drives the same consolidated runner as the CLI ([`scripts/o11y_health_check_run.py`](scripts/o11y_health_check_run.py)): **license utilization** → **platform engagement** → **IM** (metrics + integrations) → **detectors** → **dashboards** → **APM** → **RUM** → **Synthetics** → **tokens** → **OpenTelemetry Collectors**, then merges everything into one markdown report (and optional **PPTX** / **PDF**).

### One-time setup (from repo root)

Use **Python 3.9+** (`python3` on your PATH). A venv is recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-health-hub.txt
pip install -r requirements-presentation.txt
playwright install chromium
```

### Start the Hub

```bash
python3 scripts/o11y_health_hub_server.py
```

Optional: `--host 0.0.0.0` / `--port N` (defaults **127.0.0.1** and **8766**). The process prints the URL to open in a normal browser tab.

**Important:** do **not** open `web/o11y-health-hub/index.html` as a `file://` page. The UI needs the Flask server for **`POST /api/jobs`**, **`GET /api/jobs/<id>/markdown`**, and static files (`hub.js`, `hub.css`). Opening as a local file breaks “start run” and “open report.”

### Typical flow in the browser

1. **Reports** — see **Recent runs** (status, links).
2. **New health check** (or **New report**) — enter **company**, **report title**, **realm**, and **organization access token** (same token you would put in `customer-profile.yaml` for CLI runs).
3. Adjust **License utilization** / **Platform engagement** month windows if needed (optional; defaults use the runner’s lookback rules).
4. Under **Checklist scope & product modules**, leave defaults for a full run, or uncheck shared sections / **All product modules** to limit scope (faster smoke tests).
5. Under **Deliverables**, leave **Combined JSON**, **PowerPoint**, and **PDF** checked unless you want a shorter run (markdown is always produced).
6. **Review & confirm** → **Start health check** — watch **Progress** and the live log.
7. When the run finishes, use **Open web report** — the **detailed viewer** ([`web/o11y-health-report/`](web/o11y-health-report/)) loads markdown from the Hub API (`/health-report/?report=/api/jobs/<id>/markdown&hubJob=<id>`). Use **Download PDF** / **Download PowerPoint** in the viewer header when those files exist.
8. **Back to reports** returns to the Hub home. **Delete** removes a job folder (blocked while status is **running**).

### Where outputs go

Each Hub run writes under **`data/o11y-health-hub/jobs/<32-char-job-id>/`** (gitignored), including **`report.md`**, **`report.json`**, and (when enabled) **`report.pptx`** and **`report.pdf`**. The CLI, when run without the Hub, writes consolidated markdown under **`reports/`** by default (`health_check_output_dir` in profile).

### Security

The Hub accepts an **org API token** in the browser and writes a **short-lived** profile YAML under the job directory for the subprocess. Use only on a **trusted machine** or **private network**; there is no built-in authentication or TLS.

## Project layout & code overview

| Location | Role |
|----------|------|
| [`scripts/o11y_health_hub_server.py`](scripts/o11y_health_hub_server.py) | **Hub** — Flask app, job queue, serves [`web/o11y-health-hub/`](web/o11y-health-hub/) and [`web/o11y-health-report/`](web/o11y-health-report/) under `/health-report/`, REST API under `/api/jobs/…`. |
| [`scripts/o11y_health_check_run.py`](scripts/o11y_health_check_run.py) | **Orchestrator** — invokes domain scripts, merges markdown, optional PPTX ([`o11y_pptx_export.py`](scripts/o11y_pptx_export.py)) and PDF ([`o11y_pdf_export.py`](scripts/o11y_pdf_export.py)). |
| [`scripts/o11y_*`](scripts/) | **Domain health scripts** — license, platform engagement, IM, detectors, dashboards, APM, RUM, Synthetics, tokens, OTel collectors (each can be run standalone; see table below). |
| [`scripts/o11y_report_format.py`](scripts/o11y_report_format.py), [`scripts/o11y_script_logging.py`](scripts/o11y_script_logging.py), [`scripts/o11y_presentation_util.py`](scripts/o11y_presentation_util.py) | Shared formatting, logging, and presentation helpers. |
| [`web/o11y-health-hub/`](web/o11y-health-hub/) | Hub front end: `index.html`, `hub.js`, `hub.css`. |
| [`web/o11y-health-report/`](web/o11y-health-report/) | **Static report viewer** — `index.html`, `app.js`, `styles.css`; optional demo `report.md` / `sample-report.md`. Used by the Hub and by PDF export (headless Chromium loads markdown via `?report=…`). |
| [`reports/`](reports/) | Default output directory for **CLI** consolidated markdown (`health_check_output_dir` in profile). The folder is **gitignored**; it is created when you run the CLI (or point `health_check_output_dir` elsewhere). |
| [`data/o11y-health-hub/`](data/o11y-health-hub/) | **Hub job artifacts** (created at runtime; gitignored). |
| [`splunk-ppt-template/`](splunk-ppt-template/) | Branded PPTX masters for executive deck export. |
| [`Splunk-Observability-Health-Check.md`](Splunk-Observability-Health-Check.md) | Canonical checklist: section order, severities, table columns. |
| [`customer-profile.example.yaml`](customer-profile.example.yaml) | Template for **CLI** token/realm/output settings; copy to `customer-profile.yaml` / `customer-profile.local.yaml` (gitignored). The Hub does not require this file if you paste token + realm in the form. |
| *(local only — gitignored)* | `.cursor/`, `mcps/`, `AGENTS.md`, `internal-process-notes.md` — optional Cursor/MCP setup and agent notes; **not** required to run the Hub or CLI. |

**Flow:** Hub **POST** creates a job → background thread runs `o11y_health_check_run.py` with an ephemeral profile → artifacts land in `data/o11y-health-hub/jobs/<id>/` → browser opens the viewer, which **GET**s markdown from `/api/jobs/<id>/markdown`.

## Command line (automation & advanced options)

For **CI**, **scheduling**, or flags not exposed in the Hub UI, use the orchestrator directly.

1. **Python 3.9+** and (optional) the same venv as above.
2. **Profile** — Copy [`customer-profile.example.yaml`](customer-profile.example.yaml) to **`customer-profile.yaml`** or **`customer-profile.local.yaml`** in the **repository root** (gitignored). Set at least:
   - `access_token` — org API token (or `SPLUNK_ACCESS_TOKEN` in the environment; env **overrides** the file)
   - `realm` — e.g. `us0`, `us1`, `eu0`
   - `report_title`, `company_or_purpose`, and optional `health_check_*` keys (see the example file)

3. From the **repo root**:

   ```bash
   python3 scripts/o11y_health_check_run.py
   ```

   Progress and a short **run summary** go to **stderr** (INFO). Use `-v` / `--verbose` for debug detail. If one step fails, the run **continues** when possible and still writes the consolidated report when it can.

   Default markdown path: **`reports/<sanitized report_title>.md`** (folder from `health_check_output_dir`).

### `o11y_health_check_run.py` — all flags

Run `python3 scripts/o11y_health_check_run.py --help` for the same list with built-in descriptions.

| Flag | Purpose |
|------|---------|
| `--out PATH` | Output path for consolidated markdown (default: `reports/<report_title>.md`) |
| `--profile PATH` | Profile YAML (default: `customer-profile.local.yaml` or `customer-profile.yaml` in repo root) |
| `--customer TEXT` | Override **Customer / purpose** in metadata (default: `company_or_purpose` in profile) |
| `--title TEXT` | Override report **H1** title (default: `report_title` in profile) |
| `--realm REALM` | Override realm (default: profile `realm`, else `SPLUNK_REALM`, else `us0`) |
| `--skip-license` | Skip license utilization (`o11y_license_utilization.py`) |
| `--skip-platform-engagement` | Skip platform engagement trends (`o11y_platform_engagement_trends.py`) |
| `--skip-im` | Skip IM metrics usage **and** IM integrations (`o11y_im_metrics_usage_breakdown.py` + `o11y_im_integrations.py`) |
| `--skip-detectors` | Skip detectors health (`o11y_detectors_health_check.py`) |
| `--skip-dashboards` | Skip dashboards health (`o11y_dashboards_health_check.py`) |
| `--skip-synthetics` | Skip Synthetics health (`o11y_synthetics_health_check.py`) |
| `--skip-apm` | Skip APM health (`o11y_apm_health_check.py`) |
| `--skip-rum` | Skip RUM health (`o11y_rum_health_check.py`) |
| `--skip-tokens` | Skip token health (`o11y_token_health_check.py`) |
| `--skip-otel-collectors` | Skip OpenTelemetry Collectors (`o11y_otel_collectors_health_check.py`) |
| `--license-days N` | License lookback days (default: profile `health_check_license_days`, else **90**) |
| `--pe-lookback-days N` | Platform engagement SignalFlow window (default: profile `health_check_pe_lookback_days`, else **180**; clamped **90–366**) |
| `--pe-resolution-hours N` | Platform engagement rollup for `sf.org` gauges (default: profile `health_check_pe_resolution_hours`, else **24**; clamped **6–168**) |
| `--pe-apps-resolution-hours N` | Rollup for instrumented-apps matrix (default: profile `health_check_pe_apps_resolution_hours`, else **168**) |
| `--apm-hours N` | APM lookback hours (default: profile `health_check_apm_hours`, else **24**) |
| `--apm-signalflow-max-data-points N` | APM `spans.count` / `traces.count` stream cap (default: profile `health_check_apm_signalflow_max_data_points`, else **300000**; raise if Health Endpoints rows disappear in large orgs) |
| `--rum-lookback-hours N` | RUM SignalFlow window hours (default: profile `health_check_rum_lookback_hours`, else **168**; max **720**) |
| `--rum-resolution-minutes N` | RUM rollup resolution (default: profile `health_check_rum_resolution_minutes`, else **360**) |
| `--rum-max-rows N` | RUM max rows per volumetric table (default: profile `health_check_rum_max_rows`, else **200**) |
| `--im-lookback {P1D,P7D,P30D}` | IM usage analytics lookback (default: profile `health_check_im_lookback`, else **P1D**) |
| `--im-limit N` | IM API max metrics (default: profile `health_check_im_limit`, else **10000**) |
| `--im-top N` | IM report: only top **N** metrics by MTS (default: profile `health_check_im_top`; omit = all rows) |
| `--max-detectors N` | Max detectors to analyze (default: profile `health_check_detectors_max`, else **400**) |
| `--max-dashboards N` | Max dashboards with full detail (default: profile `health_check_max_dashboards`, else **150**) |
| `--max-synthetics-tests N` | Max Synthetics tests to list (default: profile `health_check_max_synthetics_tests`, else **2000**) |
| `--otel-lookback-hours N` | OTel collector SignalFlow window in hours (default: profile `health_check_otel_lookback_hours`, else **4**; clamped 1–168) |
| `--otel-support-days N` | OTel: depreciation = GitHub release **published_at** + **N** days for Splunk-distribution collectors (default: profile `health_check_otel_support_days`, else **730**) |
| `--otel-skip-github-catalog` | OTel: do not call GitHub for [splunk-otel-collector](https://github.com/signalfx/splunk-otel-collector) releases; depreciation dates show as **—** |
| `--skip-inactive-mts` | Detectors: pass through to skip `/v2/metrictimeseries` sampling for inactive-MTS (faster) |
| `--inactive-mts-hours H` | Detectors: stale threshold hours (default: profile `health_check_inactive_mts_hours`, else **36**) |
| `--inactive-mts-max-metrics N` | Detectors: max `data('…')` metrics per detector (default: profile, else **3**) |
| `--inactive-mts-search-limit N` | Detectors: MTS rows per metric search (default: profile, else **3**) |
| `--inactive-mts-max-evaluations N` | Detectors: max MTS checks per detector (default: profile, else **6**) |
| `--no-apm-trace-checks` | APM: omit `--trace-checks` (faster; overrides profile `health_check_apm_trace_checks`) |
| `--keep-intermediate-json` | Keep intermediate JSON next to the report (also: profile `health_check_keep_intermediate_json`) |
| `--json-out PATH` | Write combined JSON snapshot to this path (defaults from profile `health_check_json_out` when enabled) |
| `--pptx-out PATH` | Write **executive** PowerPoint deck (highlights only; install `python-pptx`). Use profile `health_check_pptx_out: "true"` for `<report>.pptx` next to the markdown |
| `--pptx-light` | Use Splunk **light** FY27 template; default export uses **dark** (`splunk-ppt-template/…Dark…`) |
| `--pptx-template PATH` | Optional override `.pptx` (relative to repo root or absolute); **masters/layouts** apply branding; **template slides are removed** so the file is export-only |
| `--pdf-out PATH` | Write **full** report as PDF from the styled HTML viewer (install `playwright` and run `playwright install chromium`). Profile: `health_check_pdf_out: "true"` |
| `--pdf-viewer-dir PATH` | Viewer root with `index.html` (default `web/o11y-health-report` or profile `health_check_pdf_viewer_dir`) |
| `-v`, `--verbose` | Verbose logging (debug), including full subprocess command lines |

**Presentation export setup** (one-time):

```bash
pip install -r requirements-presentation.txt
playwright install chromium
```

The deck is a **summary** (top Red/Yellow rows, executive bullets); the **PDF** matches the HTML viewer and holds full tables.

**Synthetics failure-rate tuning** (no CLI flags — set in profile only): `health_check_skip_synthetics_failure_metrics`, `health_check_synthetics_failure_lookback_hours`, `health_check_synthetics_failure_resolution_minutes`, `health_check_synthetics_failing_rate_threshold_pct`. See comments in [`customer-profile.example.yaml`](customer-profile.example.yaml).

**Other profile-only options** include `signalfx_app_base` (UI origin for token detail links), `health_check_json_out`, `health_check_keep_intermediate_json`, `health_check_apm_signalflow_max_data_points`, presentation keys `health_check_pptx_out`, `health_check_pdf_out`, `health_check_pptx_theme` (`dark` default, `light` for FY27 light master), `health_check_pptx_template` (optional path override), `health_check_pdf_viewer_dir`, `health_check_pptx_max_table_rows`, `health_check_pptx_max_exec_bullets`, and all `health_check_skip_*` mirrors of the `--skip-*` flags. RUM tuning: `health_check_rum_lookback_hours`, `health_check_rum_resolution_minutes`, `health_check_rum_max_rows`. Platform engagement: `health_check_skip_platform_engagement`, `health_check_pe_lookback_days`, `health_check_pe_resolution_hours`, `health_check_pe_apps_resolution_hours` (see [`customer-profile.example.yaml`](customer-profile.example.yaml)).

## Individual scripts

Run from the repo root. Each script accepts `--profile` (and usually `--realm`) with the same token rules as the orchestrator. For a full list of options: **`python3 scripts/<script>.py --help`**.

| Script | Role |
|--------|------|
| [`scripts/o11y_health_check_run.py`](scripts/o11y_health_check_run.py) | **Orchestrator** — merged checklist report |
| [`scripts/o11y_license_utilization.py`](scripts/o11y_license_utilization.py) | License / entitlement utilization (SignalFlow) |
| [`scripts/o11y_platform_engagement_trends.py`](scripts/o11y_platform_engagement_trends.py) | Platform engagement org trends (`sf.org` KPIs; optional APM app count + custom metrics when licensed) |
| [`scripts/o11y_im_metrics_usage_breakdown.py`](scripts/o11y_im_metrics_usage_breakdown.py) | IM metrics usage / cardinality |
| [`scripts/o11y_im_integrations.py`](scripts/o11y_im_integrations.py) | IM cloud integrations inventory |
| [`scripts/o11y_detectors_health_check.py`](scripts/o11y_detectors_health_check.py) | Detectors inventory, events, muting, inactive MTS, … |
| [`scripts/o11y_dashboards_health_check.py`](scripts/o11y_dashboards_health_check.py) | Dashboard groups, charts, detector links |
| [`scripts/o11y_apm_health_check.py`](scripts/o11y_apm_health_check.py) | APM checks (`--trace-checks` for trace-heavy tests) |
| [`scripts/o11y_rum_health_check.py`](scripts/o11y_rum_health_check.py) | RUM session volume / MMS usage from `sf.org` metrics (SignalFlow) |
| [`scripts/o11y_synthetics_health_check.py`](scripts/o11y_synthetics_health_check.py) | Synthetic tests, locations, failure heuristics |
| [`scripts/o11y_token_health_check.py`](scripts/o11y_token_health_check.py) | Org tokens (expired / near expiration) |
| [`scripts/o11y_otel_collectors_health_check.py`](scripts/o11y_otel_collectors_health_check.py) | OTel collector versions from metrics |

### Notable standalone flags (examples)

| Script | Common optional flags |
|--------|------------------------|
| `o11y_license_utilization.py` | `--days`, `--resolution-hours`, `--keys`, `--json-out`, `--md-out`, `--report-title`, `--print-json`, `--quiet`, `-v` |
| `o11y_platform_engagement_trends.py` | `--lookback-days`, `--resolution-hours`, `--apps-resolution-hours`, `--license-json`, `--structured-json-out`, `--md-out`, `-v` |
| `o11y_im_metrics_usage_breakdown.py` | `--lookback`, `--limit`, `--billable` / `--no-billable`, `--order-by`, `--top`, `--json-out`, `--structured-json-out`, `--md-out`, `--print-schema`, `-v` |
| `o11y_im_integrations.py` | `--limit`, `--json-out`, `--structured-json-out`, `--print-schema`, `-v` |
| `o11y_detectors_health_check.py` | `--max-detectors`, `--noisy-red`, `--noisy-yellow`, `--skip-inactive-mts`, inactive-MTS knobs, `--structured-json-out`, `--md-out`, `-v` |
| `o11y_dashboards_health_check.py` | `--max-dashboards`, `--sleep`, `--structured-json-out`, `--md-out`, `-v` |
| `o11y_apm_health_check.py` | `--hours`, `--checks`, `--trace-checks`, `--json-out`, `--md-out`, `-v` |
| `o11y_rum_health_check.py` | `--lookback-hours`, `--resolution-minutes`, `--max-rows`, `--structured-json-out`, `--md-out`, `-v` |
| `o11y_synthetics_health_check.py` | `--max-tests`, `--per-page`, `--sleep`, `--skip-failure-metrics`, failure-metric tuning, `--structured-json-out`, `--md-out`, `-v` |
| `o11y_token_health_check.py` | `--per-page`, `--sleep`, `--structured-json-out`, `--md-out`, `-v` |
| `o11y_otel_collectors_health_check.py` | `--lookback-hours`, `--resolution-minutes`, `--min-splunk-version`, `--min-oss-version`, `--support-days`, `--skip-github-catalog`, `--structured-json-out`, `--md-out`, `-v` (optional env **`GITHUB_TOKEN`** for GitHub API) |

Each script may define more switches than listed above; **`python3 scripts/<name>.py --help`** is authoritative.

## HTML report viewer (optional)

After a **Hub** run, use **Open web report** (same origin as the Hub) so markdown loads from `/api/jobs/…/markdown`. The section below is for **standalone** viewing (e.g. local `http.server` or a copied `report.md`).

To browse markdown in the same Splunk-styled UI without the Hub:

```bash
python3 -m http.server 8765 --directory web/o11y-health-report
```

**How it loads a report (`reports/` is not on this server’s filesystem root):**

1. **`report.md` next to `index.html`** — On load, the page tries `fetch("report.md")`. The repo includes a **demo** `web/o11y-health-report/report.md`; your real run goes to `reports/<report_title>.md` unless you override `--out`.
2. **Write straight into the viewer folder** — e.g.  
   `python3 scripts/o11y_health_check_run.py --out web/o11y-health-report/report.md`  
   so the next refresh picks up the latest consolidated report automatically.
3. **Query string from repo root** — If you want `?report=` to point at `reports/<file>.md`, run the server from the **repository root** (not `--directory web/o11y-health-report` only), then open  
   `http://localhost:8765/web/o11y-health-report/?report=/reports/Acme%20Corp%20O11y%20Health%20Check.md`  
   (leading `/reports/…` is served from the repo; `../reports/…` with the viewer-only server usually **404**s).
4. **Health Check Hub** — `python3 scripts/o11y_health_hub_server.py` serves the viewer and `/api/jobs/<id>/markdown`; **Open report** uses that path automatically.
5. **Manual** — Drag/drop or **Choose file** for any `.md` file; the last loaded content is also stored in **localStorage** for the next visit on the same origin.

Open `http://localhost:8765/` (viewer-only) or the Hub URL from the docs above.

If the viewer still shows **stale license charts** or an old report layout, you are loading a **cached or copied** markdown file. Re-run the health check and **overwrite** `web/o11y-health-report/report.md` (or clear **localStorage** for the site / use **Load report** and pick the fresh `reports/<title>.md`). **License utilization** uses embedded **o11y-license-chart** JSON (and may include **Mermaid** elsewhere); charts render after the page finishes loading scripts.

## Further reading

- [`Splunk-Observability-Health-Check.md`](Splunk-Observability-Health-Check.md) — Checklist sections, severities, table columns  
- [`trace-sampling-plan.md`](trace-sampling-plan.md) — Trace sampling notes for APM  
- **Cursor / MCP users:** maintain `AGENTS.md`, `.cursor/skills/`, and related notes **locally** (see `.gitignore`); they are excluded from the public tree.  

**Security:** Never commit real tokens. Use env vars or local profile files only.
