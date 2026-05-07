# Demo Org Splunk Observability Health Check

| Field | Value |
| --- | --- |
| Customer / purpose | Demo Org |
| Assessment date (UTC) | 2026-04-08T12:00:00Z |
| Realm | us0 |
| Scope | License + APM (automated) |

## Severity legend

| Level | Meaning |
| --- | --- |
| Green | Healthy or normal |
| Yellow | Warning or lower criticality |
| Red | Critical or immediate attention |

## Executive summary

- License utilization: **0** Red, **0** Orange, **1** Yellow entitlement rows (latest complete month severity per entitlement).
- APM: snapshot over **24** hour(s); review domain subsections for Yellow/Red rows.
- Other domains (platform engagement, IM, detectors, dashboards, RUM, synthetics, tokens): **not assessed** in this automated run unless covered by a future script.

- **Scope:** License utilization, APM, Other domains not executed

## License utilization

### Severity legend

| Color | Criteria |
| --- | --- |
| Green | Utilization from 40% to 85% |
| Yellow | Utilization less than 40% |
| Orange | Utilization greater than 85% and up to 100% |
| Red | Utilization greater than 100% |

### Results

| Color | Product | Entitlement | Utilization % |
| --- | --- | --- | --- |
| Green | APM | APM hosts (host model) | 85.0 |
| Yellow | Infrastructure | Hosts | 55.0 |

## Platform engagement

### User Analysis

### Results

| Metric | Value |
| --- | --- |

### Findings

*None — check not executed.*

### Recommendation

*None.*

## APM health checks

### Health Endpoints Enabled

### Results

| Color | Service Name | Environment Name | # Traces (24hrs) |
| --- | --- | --- | --- |
| Yellow | checkout | prod | 1200 |

### Findings

- Health check traffic appears on one or more services — confirm whether that instrumentation should remain.

### Recommendation

- Use Synthetics for health checks where possible.

## Recommendations summary

Consolidate actions from **Red** and **Yellow** rows above.
