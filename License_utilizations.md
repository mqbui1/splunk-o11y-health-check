Splunk Observability has multiple products within the platform and have a couple of different pricing models  


# Application Performance Management (APM) 

Two models:  

- Host based 
- Traces Analyzed Per Minute (TAPM) 

Both Models contain entitlements under their main plan. All entitlements need to be assessed individually to determine utilization.  

- Containers or Serverless Functions 
- Profiled Containers 
- Monitoring MetricSets (MMS) 
- Troubleshooting MetricSets (TMS) 
- Trace Volume 
- Profiling Volume 

## How to Measure Utilization 
Utilization in Splunk Observability APM is calculated at the end of a billing month as the monthly average with one minute snapshots. 

Host Model: 

- sf.org.apm.subscription.hosts metric tracks license/subscription 
- sf.org.apm.numHosts tracks customer utilization  


TAPM Model: 

- sf.org.apm.subscription.traces 
- sf.org.apm.numTracesReceived

Entitlements:

Containers:
- sf.org.apm.subscription.containers
- sf.org.apm.numContainers

MMS:
- sf.org.apm.subscription.monitoringMetricSets
- sf.org.apm.numMonitoringMetricSets

TMS:
- sf.org.apm.subscription.troubleshootingMetricSets
- sf.org.apm.numTroubleshootingMetricSets

Trace Volume:
- sf.org.apm.subscription.spanBytes
- sf.org.apm.numSpanBytesReceived

Profiling Volume:
- sf.org.profiling.numMessageBytesReceived (cumulative counter — **sum** series for org total, **delta** per interval, then **sum** deltas into each UTC month; not **mean** of the raw counter.)

### Volume metrics and rollup types (Health Check automation)

Splunk assigns each metric a type (gauge, counter, cumulative counter). For **license math** you must align the **SignalFlow rollup/aggregation** with that type:

| Kind | Example `sf.org` metrics | How to roll up over time for totals |
| --- | --- | --- |
| **Rate (bytes/sec)** | `sf.org.apm.numSpanBytesReceived` | Values are **bytes per second** per report interval. **Do not** average raw rates and treat the result as total bytes. Integrate: **sum over each month of (mean rate in bucket × bucket width in seconds)** to get **bytes received in that month** (repo default: multiplier **1**; optional profile/CLI scale only when verified). Compare to subscription as bytes (or MB). |
| **Cumulative counter** | `sf.org.profiling.numMessageBytesReceived` | **Sum** across series, then **delta** per reporting interval; **sum** those deltas within each UTC month for total bytes (health-check automation: ``data('…').sum().delta()`` + ``monthly_sum``). |
| **Gauges** | `sf.org.apm.subscription.hosts`, `sf.org.apm.numHosts`, most `sf.org.apm.subscription.*` | **Mean** (or last) over the window is usually appropriate for subscription/capacity **counts**. |

Trace volume (**numSpanBytesReceived**) is treated as a **rate** in Observability; profiling ingest is a **cumulative counter** — these are different shapes and are not interchangeable.

---

### Profiling allowance (no dedicated subscription metric)

There is no standalone `sf.org` metric for **profiling byte subscription**. Automation and spreadsheets derive allowance **in MB** from the APM **host or TAPM** subscription metrics using the formulas below.

**Important — two different meanings of “Enterprise”:**

- **Splunk product / deal naming** (e.g. “APM Enterprise”, “host-based model”) does **not** by itself set the MB multiplier below.
- **Enterprise vs Standard in *this* document** refers only to the **ratio tests** on **subscription** metrics. Those ratios pick whether the **profiling MB multiplier** is the larger or smaller column in the host and TAPM branches.

**Host model — profiling MB per host**

With the **host** branch, allowance is:

- **Enterprise path (this doc):** `subscription.containers / subscription.hosts` ≈ **20** (within tolerance) → **10.24 MB × hosts** (e.g. 200 × 10.24 = **2048 MB**).
- **Standard path:** that ratio is **not** ~20 → **5.12 MB × hosts** (e.g. 200 × 5.12 = **1024 MB**).

So for the **same host count**, **Enterprise path** (ratio ≈ 20) yields **double** the profiling bytes allowance of **Standard path**. Always check `sf.org.apm.subscription.hosts` and `sf.org.apm.subscription.containers` for the month you are analyzing — product SKU name alone does not set this toggle.

TAPM branch uses **traces** × an MB factor; **Enterprise** there is when `monitoringMetricSets / traces` ≈ **10** (else Standard).

# If Host model
if sf.org.apm.subscription.hosts > 0:

    # Enterprise allowance path (this doc — ratio on subscription metrics)
    if (sf.org.apm.subscription.containers / sf.org.apm.subscription.hosts) == 20:
        sf.org.apm.subscription.hosts * 10.24
    else: # Standard allowance path (this doc)
        sf.org.apm.subscription.hosts * 5.12
else:
    # TAPM Model
    # Enterprise allowance path (this doc)
    if (sf.org.apm.subscription.monitoringMetricSets / sf.org.apm.subscription.traces ) == 10:
        sf.org.apm.subscription.traces * 0.00256
    else: # Standard allowance path (this doc)
        sf.org.apm.subscription.traces * 0.00128


Other Metrics - Not exactly part of utilization but good to report on health of the system. 
- Spans dropped due to ingest limits: sf.org.apm.numSpansDroppedThrottle
- Spans dropped due to token limits: sf.org.apm.numSpansDroppedThrottleByToken
- Profiling Messages Throttling: sf.org.profiling.numMessagesDroppedThrottle
- Invalid Spans Dropped: sf.org.apm.numSpansDroppedInvalid
- Invalid Spans by Token: sf.org.apm.numSpansDroppedInvalidByToken
- Blocked Spans Dropped: sf.org.apm.numSpansDroppedBlocked
- Blocked Spans by Token: sf.org.apm.numSpansDroppedBlockedByToken


 # Infrastracture Monitoring (IM)

 IM is licensed in two different models:
 - Usage Based Monitoring Metric Sets (MTS) 
 - Hosts 

 The Host model has entitlements built into it:
 - Containers or Serverless Functions
 - Custom Metrics included

## How to Measure Utilization 
MTS based subscription is measured as the monthly average of hourly usage. 

- Host Subscription: sf.org.subscription.hosts
- Host monitored: sf.org.numResourcesMonitored  Filter: resourceType:host

- Custom Metrics Subscription/License: sf.org.subscription.customMetrics 
- Custom metrics usage: sf.org.numCustomMetrics

- Container Subscription: sf.org.subscription.containers   
- Container monitored: sf.org.numResourcesMonitored  Filter: resourceType:container

Other Metrics - Not exactly part of utilization but good to report on health of the system. 

- Inactive MTS: sf.org.numInactiveTimeSeries
- API Calls: sf.org.numRestCalls
- Throttled Metrics: sf.org.numThrottledMetricTimeSeriesCreateCalls
- 

DataPoints Dropped:
- sf.org.numDatapointsDroppedExceededQuota
- sf.org.numDatapointsDroppedThrottle
- sf.org.numDatapointsDroppedBatchSize
- sf.org.numDatapointsDroppedInTimeout
- sf.org.numDatapointsDroppedInvalid


# Real User Monitoring (RUM)

RUM is licensed by the number of user sessions which include a couple of entitlements 

Entitlements:
- Monitoring MetricSets (MMS)
- Troubleshooting MetricSets (TMS) (Not Measure at this time)
- Session Volume (Not Measure at this time)


## How to Measure Utilization 
RUM is measured by the cumulative number of session at the end of the billing period (Month).

RUM Sessions:
- Session Subscription: sf.org.rum.subscription.sessionsPerMonth
- Number of Sessions: sf.org.rum.numSessions 

Entitlements:
- RUM MMS Limit: sf.org.rum.limit.monitoringMetricSets
- RUM MMs Usage: sf.org.numRumMonitoringMetricSetMetrics
- RUM MMS by Token: sf.org.numRumMonitoringMetricSetMetricsByToken

Other Metrics - Not exactly part of utilization but good to report on health of the system.
- Invalid Spans Dropped: sf.org.rum.numSpansDroppedInvalid
- Blocked spans dropped: sf.org.rum.numSpansDroppedBlocked
- Timeout Spans dropped: sf.org.rum.numDatapointsDroppedInTimeout


# Synthetic Monitoring 
There are 3 different licenses for Synthetics monitoring:
- Browser Test
- API Test
- Uptime Test

## How to Measure Utilization 
Synthetic tests are measured by the cumulative number of tests ran at the end of the billing period (Month).

Main metric: 
- synthetics.run.count

Filters: 
- test_type:browser    -- Browser Tests
- test_type:api        -- API tests 
- test_type:http,port  -- Uptime Tests 


Subscription/License:
- sf.org.synthetics.subscription.browser_tests
- sf.org.synthetics.subscription.api_tests
- sf.org.synthetics.subscription.uptime_tests