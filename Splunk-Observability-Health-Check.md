# Splunk Observability Cloud Health Check

Structured health-check criteria for Splunk Observability Cloud (converted from the original Word document for easier reading in editors and agents).

## Table of contents

- [Goal](#goal)
- [Phases (development)](#phases-development)
- [Overview](#overview)
- [License utilization](#license-utilization)
- [Platform engagement](#platform-engagement)
- [APM health checks](#apm-health-checks)
- [Infrastructure monitoring health checks](#infrastructure-monitoring-health-checks)
- [Detectors health checks](#detectors-health-checks)
- [Dashboards health checks](#dashboards-health-checks)
- [Real User Monitoring (RUM) health checks](#real-user-monitoring-rum-health-checks)
- [Synthetics health check](#synthetics-health-check)
- [Token health check](#token-health-check)
- [OpenTelemetry Collectors](#opentelemetry-collectors)

## Goal

To develop an Agent Skill that empowers Sales Engineers (SEs) and Solutions Architects (SAs) to provide customers with comprehensive, actionable insights into the health of their Splunk Observability Cloud platform. This goes beyond simple license usage of metrics to include a thorough assessment of critical areas such as data sources, detector utilization, metric cardinality, and dashboard effectiveness. Additionally, the Agent Skill will offer analytical tools that enable comparison of usage patterns over time, helping to identify the root causes of increased data ingestion and optimize platform performance.

## Phases (development)

1. **Phase 1** — Initial assessment that users can leverage to manually create presentations for their customers.
2. **Phase 2** — The Agent Skill automatically populates a tailored PowerPoint template with detailed health-assessment results (ready to demo).
3. **Phase 3** — Host this as an internal application (e.g. Swipe-style) for ease of use across the Splunk team.

## Overview
Each health check provides a detailed assessment based on specific criteria and outputs. To help you quickly understand the status and urgency of each finding, results are categorized into three clear severity levels:
- Green – Healthy or Normal state
- Yellow – Warning or Low Critical Findings
- Red – Critical Items

## License utilization

**Utilization severity (license charts and entitlements):**

- **Green** — utilization from 40% to 85%
- **Yellow** — utilization less than 40%
- **Orange** — utilization greater than 85% and up to 100%
- **Red** — utilization greater than 100%

Analyze each license subscription and compare the usage over the assessment window. For each product, show utilization vs subscription in a way that is easy to scan.

Structure in the generated report:

- **`###` Product** — e.g. APM, Infrastructure monitoring, RUM, Synthetics (one subsection per product family).
- **`#### Entitlement: …`** — the entitlement display name (e.g. APM hosts, TAPM traces, Infra hosts).
- **Chart (per entitlement)** — time series of **completed UTC months**: **bars** = usage, **line** = subscription allowance, with utilization % on each bar; caption states latest month severity and utilization %. (HTML viewer renders the chart; raw markdown may include a compact JSON payload for the viewer.)
- **`#### Recommendation`** — suggested customer action for that entitlement.

### Product license utilization (example shape)

#### Entitlement: *(APM Host / TAPM / Infra Host / Infra MTS / …)*

Chart (web viewer): **bars** = usage by month with utilization %, **line** = subscription allowance, plus a one-line summary for the latest complete month (severity + utilization %).

#### Recommendation

*Guidance for that entitlement.*


## Platform Engagement

### Engagement Trends 
A trend comparison analysis of various metrics to determine how usage of the Observability Cloud has changed over the last 6 months.  

Total Number of Users - Metric Name: sf.org.num.orguser
Total Number of Teams - Metric Name: sf.org.num.team
Total Number of Dashboards - Metric Name: sf.org.num.dashboard
Total Number of Detectors  - Metric Name: sf.org.num.detector
Total Number of Applications (Services) Instrumented (if Applicable) - SignalFLow: data('service.request.count').sum(by=['service.name', 'sf_environment']).count().publish(label='A')
Total Number of Custom Metrics (If Applicable) - Metric Name: sf.org.numCustomMetrics
RUM Sessions Used (If Applicable) — **previous full UTC calendar month** vs the **full month six months earlier**: metric ``sf.org.rum.numSessions`` at daily resolution with org-wide ``sum()``; **current month value** = **sum of daily buckets** in that month. License-gated like other RUM entitlements.
Synthetics Total Test Runs (If Applicable) — same **two calendar months**: sum of daily ``synthetics.run.count`` buckets (org-wide ``sum()``) within each month. License-gated when synthetics run entitlements are present.

### Results
Create charts that represent the above metrics. **Most KPIs:** compare a **recent short window** (e.g. 7-day mean) to a **similar window about six months earlier** (rolling baseline). **RUM sessions used** and **Synthetics total test runs** instead compare **complete UTC calendar months** (previous full month vs six months prior full month). Expectation is a chart for each KPI, with baseline and current windows and % difference. 


## APM Health Checks

### Identify Usage by Service and Environment
Analyze APM data across all services to provide an overview of each service's overall usage. The usage is analyzed across a small window of time so this should be used as an estimate and does not reflect an usage across a billing period. 

- **Total Traces:** Σ `traces.count` for that service×environment (assessment window).
- **% of Total Usage:** that row’s trace total as a percentage of Σ `traces.count` across all listed service×environment pairs.

#### Results

| ServiceName | Environment | Total Traces | % of Total Usage |
| --- | --- | --- | --- |


### Health Endpoints Enabled
List of services that are sending traces for health-like endpoints (`sf_operation` values that match common health check patterns).

#### Results

| Color | Service Name | Environment Name | Endpoint Name | Requests (24H) |
| --- | --- | --- | --- | --- |

#### Recommendation
Use a Synthetics check instead and configure OTel Collector to drop health endpoint spans. <Find examples of this configuration>


### Traces with Minimal Spans
For this check, health endpoints will be ignored as they are covered by the health endpoint check. For each service we will analyze if there are many traces with a small number of spans; this may provide little information and may be noisy traces.

For each service and endpoint, if at least 20% of the traces analyzed meet one of the span-count bands below, the row is flagged in **Results**.

#### Results

| Color | Service Name | Sample size | % traces 1–2 spans | % traces 3–5 spans |
| --- | --- | --- | --- | --- |

#### Recommendation

Analyze yellow and red services and endpoints to see if there is value in keeping the traces with a small number of spans. If not configure the OTel Collector to drop those traces.


### Review Span Size (tag / attribute payload)
Spans with **large attribute payloads** drive trace-volume cost and noise. Assess using an **estimated span size** (e.g. UTF-8 byte length of span names plus all tag keys and values from retrieved traces), pooled across a **stratified sample** (top services by `spans.count`, bounded trace fetches). Flag spans **above mean + 2 standard deviations** in that sample (or equivalent percentile if sample is small).

#### Results

| Color | Service Name | Environment Name | Operation Name | Est. bytes |
| --- | --- | --- | --- | --- |

#### Recommendation

Truncate or drop oversized attributes at the **OTel collector**; move large blobs to **logs**. Review **Trace Volume / span bytes** entitlements. Combine with **Tags with High Cardinality** (subscription tag payload %) for a full picture.

**Assessment note:** Prefer **dynamic** service breadth (e.g. **min 5** and up to **10%** of distinct services with traffic) and **service×endpoint (`sf_operation`)** trace targets (e.g. **≥3 traces per pair** when Trace Analytics returns enough IDs). **Cap** total `get_trace_full` calls; report shortfall when the cap lands early. Statistics are **sample-based**, not a census of all spans.


### Tags with High Cardinality
Indexed span tags with a large number of unique values (high cardinality) may lead to an explosion on the usage of TMS and MMS. This health check does not provide analysis on your TMS and MMS, instead it focuses on analyzing the indexed tags and the number of unique values.  

#### Results

| Color | Tag Name | Unique Values |
| --- | --- | --- |

#### Recommendation
Confirm distinct value cardinality with Usage Analytics tooling where needed. Consider if the tag needs to be indexed and if the scope can be reduced.  

**Assessment note:** The GraphQL response includes **tagName**, **charCount** (indexed character volume for that tag), and **percentage**. There is **no distinct unique-value count per tag** in this query. Populate **Unique Values** with **charCount** from the same response and treat it as **indexed character volume** (subscription cost driver), not as a literal count of distinct tag values—unless a future API field supplies distinct counts.


### Sensitive Data in Spans
List of services and spans with sensitive data (PII, HIPAA, credit card numbers, etc.) not obfuscated.

#### Results

| Color | Service Name | Span Name |
| --- | --- | --- |


### Audit Debug/Verbose Spans in Production
Ensure that trace-level logging or debug-level spans haven't been accidentally left enabled.

List services and operations where debug level spans are enabled (one row per service × environment × operation).

#### Results

| Color | Service Name | Environment Name | Operation Name |
| --- | --- | --- | --- |


### Check for Orphan Services
Services that have no upstream or downstream dependencies. These may be services that still report traces but were supposed to be decommissioned, deprecated, or migrated, yet are still running and reporting data.

#### Results

| Color | Service Name | Environment Name |
| --- | --- | --- |


### Review Endpoint Grouping Rules
Poorly grouped endpoints (e.g., /user/123, /user/456 treated as separate endpoints) inflate cardinality. Use endpoint grouping rules to collapse them (e.g., /user/{id}).

#### Results

| Color | Service Name | Environment Name | Endpoint Name |
| --- | --- | --- | --- |



## Infrastructure Monitoring Health Checks

### Metric Cardinality & Volume
List of metrics with high cardinality and whether they are being utilized in detectors, dashboards, or API. Automation includes only metrics whose **% Over Total** is **≥ 1%**.

#### Results

| Metric Name | Billing Class | Cardinality (MTS) | Utilization | % Over Total |
| --- | --- | --- | --- | --- |
|  |  |  |  |  |

#### Recommendation
Use Metric Pipeline Management (MPP) to create rules to archive or drop metrics and dimensions not being used in detectors, dashboards, or API calls. Consider enabling auto-archiving rules.


### Analyze Integrations
Analyze cloud integrations (AWS, GCP, Azure) to see if all services have been enabled. Breakdown of each integration with the sources of data being pulled.

#### Results

| Integration Type | Integration Name | Active (T/F) |
| --- | --- | --- |

#### Recommendation

Review the list of data being pulled in Splunk Observability Cloud and filter out any sources that may not be providing value.


## Detectors Health Checks

### Noisy Detectors
List of detectors that fire constantly with the number of times the detector has fired out in the last week.

#### Results

| Color | Detector Name | Number of Triggers (7 Days) |
| --- | --- | --- |

#### Recommendation
Review detector rules and adjust trigger configuration to reduce the alert noise.


### Non-Firing Detectors
List of active detectors that have not triggered in the last 30 days.

#### Results
| Color | Detector Name | Number of Triggers (30 Days) |
| --- | --- | --- |

#### Recommendation
Review detector rules, thresholds, and whether the signal is still relevant. 


### Identify Detectors on Inactive Metric Time Series (MTS)
An inactive MTS is one that has not received any datapoints for at least 36 hours. A detector that is monitoring an inactive MTS is not doing anything. This could be because the MTS may have changed its name, and the detector was not updated.

#### Results
| Color | Detector Name | Inactive Signal |
| --- | --- | --- |

#### Recommendation
Review the list of detectors and determine if it should be deleted or if the signal needs to be updated.


### Redundant Detectors
Multiple teams may have created overlapping detectors for the same service, or metric.

#### Results
| Color | Detector Name | Redundant Detector IDs |
| --- | --- | --- |

#### Recommendation
Review list of redundant detectors. The Detector Name identifies the first detector and the redundant detector IDs how additional detectors monitoring the same signal as the detector name one. Consider if you need to have multiple detectors monitoring the same signals or consolidate. 


### Inactive Alert Destinations
Review integrations used for sending alerts to (Slack, Splunk, On-Call ..). Analyze what alert destinations are being used within detectors and create a list of detectors that are sending alerts to deactivated or deleted destinations or single e-mail addresses.

List of detectors that meet the above criteria (automation lists **Red** and **Yellow** only; **Green** is omitted):

#### Results
| Color | Detector Name | Detector state | Alert Sent to: |
| --- | --- | --- | --- |


### Muted Detectors
List of detectors that have been muted for longer than 3 days. Someone may have forgotten to re-enable the detector. Check muting rules to determine if there is a rule in place for the detector

#### Results
| Color | Detector Name | Muted Date | Muting Rule |
| --- | --- | --- | --- |

#### Recommendation
Review muted detectors and determine if they should be re-enabled. Assess if there is a muting rule that will re-enable the detector at a certain date


## Dashboards Health Checks

### Links to Deleted/Inactive Detectors
List of charts that have a link to a non-existing detector or an inactive detector.

#### Results
| Color | Dashboard Group | Dashboard Name | Chart Name | Detector Link |
| --- | --- | --- | --- | --- |

#### Recommendation
Delete old links.


### Inactive Charts
List of charts that are using inactive metrics.

#### Results
| Color | Dashboard Group | Dashboard Name | Chart Name |
| --- | --- | --- | --- |

#### Recommendation
Review dashboard groups that have many inactive charts/dashboards; they may no longer be used. Delete old charts/dashboards that are no longer needed.


### Duplicate Dashboards
Teams may often clone dashboards. This can lead to many identical dashboards across the organization.

#### Results
| Color | Dashboard Group | Dashboard Name | Duplicate Dashboard IDs |
| --- | --- | --- | --- |

#### Recommendation
Consolidate commonly used dashboards into a public dashboard group accessible to all teams.


## Real User Monitoring (RUM) Health Checks

### Volume by Application
List of applications by total number of sessions.

#### Results
| Color | Application Name | Number of Sessions | % Utilization of License |
| --- | --- | --- | --- |

#### Recommendation
Review to identify if any application is generating a disproportionate number of sessions. Review the environment for the application; if this is a dev/test application, should it be sending RUM data?


### Filter Synthetic/Bot traffic
Ensure crawler and bot sessions aren't being ingested as real user sessions.

#### Results
| Color | Application Name | IP Addresses likely to be bots or crawlers |
| --- | --- | --- |

#### Recommendation
Check RUM instrumentation and enable the disableBots flag to stop tracing data from known bots.


### Review Custom Events
Are teams sending excessive custom RUM events that provide little analytical value. Compare this against your RUM session volume entitlement.

#### Results
| Color | Application Name | Custom Events | Cardinality of Event |
| --- | --- | --- | --- |

#### Recommendation
Review each custom event and its cardinality to determine if it provides value. Remove the low value custom events.


### RUM Troubleshooting Metrics Sets (TMS) Usage Analysis
Provide a list of TMS by license usage %.

#### Results
| Color | Application Name | TMS Name | TMS Cardinality | % of License |
| --- | --- | --- | --- | --- |


### RUM Monitoring Metrics Sets (MMS) Usage Analysis
Provide a list of MMS by license usage %, number of services, endpoints enabled.

#### Results
| Color | Application Name | MMS Name | MMS Cardinality | % of License |
| --- | --- | --- | --- | --- |

#### Recommendation
Determine if the MMS can be converted into TMS to reduce license usage.


## Synthetics Health Check

### Test Usage Analysis
Detailed list of Synthetics tests. The total runs per month and utilization % are estimates based on the currently configured frequency. 

#### Results
| Test Name | Test Type | Frequency | # Locations | Round Robin (Y/N) | Total Runs per Month | % Utilization of License |
| --- | --- | --- | --- | --- | --- | --- |

#### Recommendation
Analyze the detailed test report to determine if each test is running at the correct frequency and using the right locations. This check is more for teams to have a good overview of the different tests running in their environment.


### Failing Tests
Active Tests that have a failure rate above 30% for the last 7 days. These may be tests that are running against old URLs, deprecated APIs, or retired services that for some reason are still enabled.

#### Results
| Color | Test Name | Test Type | Frequency | Failure Rate % (7D)|
| --- | --- | --- | --- | --- |

#### Recommendation
Update the test to point to the right URL/API, or delete/disable if not needed.


### Disabled Tests
List of tests that are disabled (not actively running).

#### Results
| Color | Test Name | Test Type | Last Run Date |
| --- | --- | --- | --- |

#### Recommendation
Review if the test is no longer needed; consider deleting.


### Similar Tests
List of tests that may be doing similar things, could be duplicates.  

#### Results
| Color | Test Name | Test Type | List of Similar Test Names |
| --- | --- | --- | --- |

#### Recommendation
Review tests to see if they are redundant, can be merged, or should stay separate with clearer ownership.


### Tests with No Detectors
List of tests that have no associated detector.

#### Results
| Color | Test Name | Test Type | Frequency |
| --- | --- | --- | --- |

#### Recommendation
Create a new detector to ensure alerts are being generated for the test.


## Token Health Check

### Expired Tokens
List of expired tokens.

#### Results
| Token Name | Token Type | Expired Date |
| --- | --- | --- |

#### Recommendation
Delete tokens if no longer needed.


### Near Expiration Tokens
List of tokens within 90 days of expiration.

#### Results
| Token Name | Token Type | Expiration Date |
| --- | --- | --- |

#### Recommendation
Assess whether the token should be extended or rotated.


## OpenTelemetry Collectors

### List of Collectors by version.  
List of deployed OpenTelemetry Collectors and their version. When present on the time series, **Host ID** and **Deployment context** summarize resource attributes (for example host identifier, cloud or platform, Kubernetes namespace/pod, deployment environment) to help locate the collector workload.

#### Results 
Yellow – Collector version 30 – 90 days from deprecation/support  
Red - Collector version less than 30 days deprecation/support 

| Color | Host Name | Host ID | Deployment context | OTel Collector Name | Version | Depreciation Date |
| --- | --- | --- | --- | --- | --- | --- |

#### Recommendations 
Review collectors that are near or have already reached their deprecated date. 