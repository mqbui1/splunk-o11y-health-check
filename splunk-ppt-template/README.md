# Splunk PowerPoint templates (health check export)

This folder holds **official Splunk-branded** `.pptx` masters with **light** and **dark** themes, stock layouts, and embedded **color / branding guidelines**.

| File | Use when |
|------|----------|
| `Splunk Template_Dark_04-26.pptx` | **Default** for automated health-check export (screen-first) |
| `Splunk Template_Light_FY27_04-26.pptx` | Customer-facing / print-friendly — use ``--pptx-light`` or ``health_check_pptx_theme: light`` |

## How the health check uses them

The orchestrator (`scripts/o11y_health_check_run.py`) **does not edit** these files on disk. It opens the template you choose, **drops every slide** that ships in the file (reference, branding examples, stock agendas, etc.), and **keeps slide masters and layouts** so fonts, colors, and placeholders still match Splunk branding. Generated health-check slides are then added—**only** those appear in the downloaded deck.

**CLI (from repo root):**

```bash
# Default: dark Splunk master (no --pptx-template needed)
python3 scripts/o11y_health_check_run.py --pptx-out reports/Customer-Health.pptx

# Light master instead
python3 scripts/o11y_health_check_run.py --pptx-out reports/Customer-Health.pptx --pptx-light
```

**Profile:** `health_check_pptx_theme: "light"` or `"dark"` (default **dark**). Override the file entirely with `health_check_pptx_template` (path relative to repo root or absolute).

## File size / unused masters

Template `.pptx` files can be large because of **multiple slide masters**. Export uses layouts from the template; you may shrink the file in PowerPoint by removing unused masters if needed—**slides** in the template are never retained in the export.

## Requirements

Executive `.pptx` export needs **`python-pptx`** — see [`requirements-presentation.txt`](../requirements-presentation.txt) in the repo root.
