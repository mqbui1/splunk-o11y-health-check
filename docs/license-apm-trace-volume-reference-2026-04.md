# APM trace volume — reference window (April 2026, UTC)

Align **Splunk Observability Chart Builder** Plot D (**Mean(monthly)**) with license automation (`apm_span_bytes`, hourly mean-of-points path).

## Calendar month (completed UTC month)

| Field | Value |
| --- | --- |
| Month | **2026-04** |
| `--calendar-month` | `2026-04` (sets start/end to full April) |
| Usage query | Default path: hourly `mean()` stream; **no** stop extension. Compare script’s **platform cycle** row still extends stop (+3d) to fetch the month-boundary point. |

## Chart Builder / SignalFlow (Plot D)

```text
data('sf.org.apm.numSpanBytesReceived', rollup='rate').scale(60)
  .mean(cycle='month', cycle_start='1d', partial_values=False).publish(label='B')
```

**Automation (default):** `data('sf.org.apm.numSpanBytesReceived', rollup='rate').scale(60).mean().publish(label='usage')` at hourly resolution; Python = **mean of hourly values** per UTC month (× `license_apm_span_bytes_rate_integral_scale` if set).

**Plot D / platform cycle:** month labels for `mean(cycle='month',…)` use the **first hour UTC of the 1st** to mean the **previous** calendar month (e.g. **1 May 00:xx** → **2026-04**). The hourly path uses ordinary UTC month bucketing instead.

## Standalone license query (April 2026)

```bash
python3 scripts/o11y_license_utilization.py \
  --calendar-month 2026-04 \
  --keys apm_span_bytes \
  --profile /path/to/customer-profile.yaml
```

Default **usage** execute resolution is **1h** (profile: `license_apm_span_bytes_usage_resolution_hours`, fallback `license_apm_span_bytes_cycle_resolution_hours`). Global `--resolution-hours` does not change this usage query.

## Compare methods (debug)

```bash
python3 scripts/compare_apm_span_bytes_methods.py \
  --calendar-month 2026-04 \
  --profile /path/to/customer-profile.yaml

# Several months (platform cycle vs hourly mean only):
python3 scripts/compare_apm_span_bytes_methods.py \
  --from-month 2026-01 --to-month 2026-04 \
  --profile /path/to/customer-profile.yaml
```

## Extra multiplier (rare)

- Profile: `license_apm_span_bytes_rate_integral_scale`
- CLI: `--apm-span-bytes-rate-scale`

See [`License_utilizations.md`](../License_utilizations.md) and `scripts/o11y_license_utilization.py --help`.
