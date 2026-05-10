/**
 * Splunk Observability Health Check — static report viewer
 * Loads consolidated markdown (from o11y_health_check_run.py) and renders with section nav.
 */

/* global marked, DOMPurify, mermaid */

const STATE = {
  raw: "",
  title: "Health Check Report",
  meta: {},
  sections: [],
};

function parseMetaTable(md) {
  const meta = {};
  const lines = md.split("\n");
  let inTable = false;
  for (const line of lines) {
    if (line.includes("| Field | Value |")) {
      inTable = true;
      continue;
    }
    if (inTable) {
      if (line.startsWith("##")) break;
      if (!line.startsWith("|") || line.includes("---")) continue;
      const cells = line
        .split("|")
        .map((c) => c.trim())
        .filter(Boolean);
      if (cells.length >= 2 && cells[0] !== "Field") meta[cells[0]] = cells[1];
    }
  }
  return meta;
}

/**
 * Remove the GFM **Field | Value** metadata block from preamble text.
 * (The viewer renders this in summary cards; leaving it in caused a duplicate table.)
 */
function stripPreambleFieldValueTable(md) {
  const lines = md.split(/\r?\n/);
  const out = [];
  let skipping = false;
  for (const line of lines) {
    if (line.includes("| Field | Value |")) {
      skipping = true;
      continue;
    }
    if (skipping) {
      if (line.startsWith("|")) continue;
      skipping = false;
    }
    out.push(line);
  }
  return out.join("\n").trim();
}

/** Metadata assessment timestamp → long date in UTC (e.g. May 4, 2026). */
function formatAssessmentDateUtc(raw) {
  if (!raw || typeof raw !== "string") return "";
  const s = raw.trim();
  const d = new Date(s);
  if (!Number.isNaN(d.getTime())) {
    return d.toLocaleDateString("en-US", {
      month: "long",
      day: "numeric",
      year: "numeric",
      timeZone: "UTC",
    });
  }
  return s;
}

/**
 * Chips for the Scope summary card: derived from the report metadata "Scope" field
 * (e.g. ``License + Platform engagement (automated)``), not a hard-coded product list.
 */
function parseScopeMetaToChipLabels(scopeRaw) {
  if (!scopeRaw || typeof scopeRaw !== "string") return [];
  let s = scopeRaw.trim();
  const ALT_SEP = " · ";
  if (s.includes(ALT_SEP)) {
    const tail = s.slice(s.lastIndexOf(ALT_SEP) + ALT_SEP.length).trim();
    if (/\(automated\)\s*$/i.test(tail) || /^placeholders only$/i.test(tail)) {
      s = tail;
    }
  }
  s = s.replace(/\s*\(automated\)\s*$/i, "").trim();
  if (/^placeholders only$/i.test(s)) return ["Placeholders only"];
  if (!s) return [];
  return s
    .split(/\s*\+\s*/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function buildScopeChipsHtml(scopeRaw) {
  const labels = parseScopeMetaToChipLabels(scopeRaw);
  if (!labels.length) {
    return `<div class="scope-chips" role="list"><span class="scope-chip scope-chip--empty" role="listitem">—</span></div>`;
  }
  return `<div class="scope-chips" role="list">${labels
    .map((name) => `<span class="scope-chip" role="listitem">${escapeHtml(name)}</span>`)
    .join("")}</div>`;
}

function summaryMetaCardLabel(metaKey) {
  switch (metaKey) {
    case "Customer / purpose":
      return "Customer";
    case "Assessment date (UTC)":
      return "Assessment date";
    case "Realm":
      return "Realm";
    case "Scope":
      return "Checklist scope";
    default:
      return metaKey;
  }
}

function extractTitle(md) {
  const m = md.match(/^# (.+)$/m);
  return m ? m[1].trim() : "Splunk Observability Health Check";
}

/**
 * Split markdown into preamble (before first ##) and H2 sections.
 * H2 = line where line starts with "## " (not "###").
 */
function splitSections(md) {
  const lines = md.split("\n");
  const outPreamble = [];
  const outSections = [];
  let buf = [];
  let sectionTitle = null;

  function isH2(line) {
    return line.startsWith("## ") && !line.startsWith("###");
  }

  for (const line of lines) {
    if (isH2(line)) {
      if (sectionTitle === null) {
        outPreamble.push(...buf);
      } else {
        outSections.push({ title: sectionTitle, body: buf.join("\n").trim() });
      }
      buf = [];
      sectionTitle = line.slice(3).trim();
    } else {
      buf.push(line);
    }
  }
  if (sectionTitle === null) {
    outPreamble.push(...buf);
  } else {
    outSections.push({ title: sectionTitle, body: buf.join("\n").trim() });
  }

  return {
    preamble: outPreamble.join("\n").trim(),
    sections: outSections,
  };
}

function slugify(s) {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

/** True for the license utilization section (current title or legacy ``License capacity`` reports). */
function isLicenseUtilizationSectionTitle(title) {
  const t = (title || "").trim().toLowerCase();
  return t === "license utilization" || t === "license capacity";
}

/** Sections omitted from the static viewer (still present in source markdown). */
function isSectionHiddenInViewer(title) {
  const t = (title || "").trim().toLowerCase();
  return t === "executive summary";
}

function renderMarkdown(md) {
  if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
    return `<pre>${escapeHtml(md)}</pre>`;
  }
  let html;
  if (typeof marked.parse === "function") {
    html = marked.parse(md, { gfm: true, breaks: false });
  } else if (typeof marked === "function") {
    html = marked(md, { gfm: true, breaks: false });
  } else {
    html = `<pre>${escapeHtml(md)}</pre>`;
  }
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ["target", "rel"],
  });
}

/** Escape text for SVG text/tspan (not HTML). */
function escapeSvgText(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatLicenseAxisNumber(n) {
  const x = Number(n);
  if (!Number.isFinite(x)) return "0";
  if (Math.abs(x - Math.round(x)) < 1e-6 * Math.max(1, Math.abs(x))) {
    return String(Math.round(x));
  }
  const a = Math.abs(x);
  if (a >= 1e9) return `${(x / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${(x / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${(x / 1e3).toFixed(1)}k`;
  if (a >= 100) return x.toFixed(0);
  if (a >= 10) return x.toFixed(1);
  return x.toFixed(2);
}

function niceLicenseStep(range, targetSteps) {
  const rough = range / Math.max(1, targetSteps - 1);
  const p10 = 10 ** Math.floor(Math.log10(Math.max(rough, 1e-12)));
  const frac = rough / p10;
  const n = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
  return n * p10;
}

function licenseYTickValues(yMax, maxTicks) {
  if (!(yMax > 0)) return [0];
  const step = niceLicenseStep(yMax, maxTicks);
  const out = [];
  for (let v = 0; v <= yMax + step * 0.001; v += step) {
    out.push(v);
  }
  if (out[out.length - 1] < yMax) {
    out.push(Math.ceil(yMax / step) * step);
  }
  return out;
}

const LICENSE_CHART_MIB = 1024 * 1024;
const LICENSE_CHART_MB_DEC = 1_000_000;

function licenseYKind(payload) {
  const u = String(payload?.yUnit || "").toLowerCase();
  if (u === "mib" || u === "mb") return u;
  return "count";
}

function licenseRawToAxisScalar(raw, yKind) {
  if (yKind === "mib") return raw / LICENSE_CHART_MIB;
  if (yKind === "mb") return raw / LICENSE_CHART_MB_DEC;
  return raw;
}

function formatLicenseYTick(raw, yKind) {
  return formatLicenseAxisNumber(licenseRawToAxisScalar(raw, yKind));
}

/** License utilization bands (Splunk-Observability-Health-Check.md). */
function licenseUtilBandFromPct(pct) {
  if (pct == null || !Number.isFinite(pct)) return "neutral";
  if (pct > 100) return "red";
  if (pct > 85) return "orange";
  if (pct >= 40) return "green";
  return "yellow";
}

function licenseUtilColorLabel(band) {
  if (band === "green") return "Green";
  if (band === "yellow") return "Yellow";
  if (band === "orange") return "Orange";
  if (band === "red") return "Red";
  return "—";
}

/** Plain-language criteria (matches report severity legend). */
function licenseUtilCriteriaForBand(band) {
  if (band === "green") return "Utilization from 40% to 85%";
  if (band === "yellow") return "Utilization less than 40%";
  if (band === "orange") return "Utilization greater than 85% and up to 100%";
  if (band === "red") return "Utilization greater than 100%";
  return "—";
}

function licenseBarGradientStops(band) {
  if (band === "green") {
    return `<stop offset="0%" stop-color="#86efac"/><stop offset="100%" stop-color="#15803d"/>`;
  }
  if (band === "yellow") {
    return `<stop offset="0%" stop-color="#fef08a"/><stop offset="100%" stop-color="#ca8a04"/>`;
  }
  if (band === "orange") {
    return `<stop offset="0%" stop-color="#fdba74"/><stop offset="100%" stop-color="#c2410c"/>`;
  }
  if (band === "red") {
    return `<stop offset="0%" stop-color="#fecaca"/><stop offset="100%" stop-color="#b91c1c"/>`;
  }
  return `<stop offset="0%" stop-color="#bae6fd"/><stop offset="100%" stop-color="#0369a1"/>`;
}

function formatLicenseTooltipAmount(raw, yKind) {
  const n = Number(raw);
  if (!Number.isFinite(n)) return "—";
  if (yKind === "mib") {
    const v = n / LICENSE_CHART_MIB;
    return `${formatLicenseAxisNumber(v)} MiB`;
  }
  if (yKind === "mb") {
    const v = n / LICENSE_CHART_MB_DEC;
    return `${formatLicenseAxisNumber(v)} MB`;
  }
  return formatLicenseAxisNumber(n);
}

/**
 * Bar hover: monthly usage, subscription for that UTC month, and utilization % (usage ÷ that month’s cap).
 */
function attachLicenseChartTooltips(fig) {
  const wrap = fig.querySelector(".o11y-license-chart__svg-wrap");
  const svg = fig.querySelector(".o11y-license-chart__svg");
  if (!wrap || !svg) {
    return;
  }
  const yKind = licenseYKind({ yUnit: svg.dataset.yKind || "count" });
  const tip = document.createElement("div");
  tip.className = "o11y-license-chart__tooltip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  wrap.appendChild(tip);

  function positionTip(ev) {
    const pad = 12;
    const tw = tip.offsetWidth || 200;
    const th = tip.offsetHeight || 120;
    let x = ev.clientX + pad;
    let y = ev.clientY + pad;
    if (x + tw > window.innerWidth - 8) {
      x = Math.max(8, ev.clientX - tw - pad);
    }
    if (y + th > window.innerHeight - 8) {
      y = Math.max(8, ev.clientY - th - pad);
    }
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }

  function fillTip(bar, ev) {
    const usage = Number(bar.dataset.usage || 0);
    const sub = Number(bar.dataset.subscription || 0);
    const month = (bar.dataset.month || "").trim() || "—";
    const pctNum =
      sub > 0 && Number.isFinite(usage) && Number.isFinite(sub) ? (100 * usage) / sub : null;
    const pctStr = pctNum != null && Number.isFinite(pctNum) ? `${pctNum.toFixed(2)}%` : "—";
    const band = licenseUtilBandFromPct(pctNum);

    tip.replaceChildren();
    const head = document.createElement("div");
    head.className = "o11y-license-chart__tooltip-title";
    head.appendChild(document.createTextNode(month));
    const utc = document.createElement("span");
    utc.className = "o11y-license-chart__tooltip-utc";
    utc.textContent = " · UTC month";
    head.appendChild(utc);
    tip.appendChild(head);

    const dl = document.createElement("dl");
    dl.className = "o11y-license-chart__tooltip-dl";

    function addRow(label, valueText) {
      const row = document.createElement("div");
      row.className = "o11y-license-chart__tooltip-row";
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = valueText;
      row.appendChild(dt);
      row.appendChild(dd);
      dl.appendChild(row);
    }

    addRow("Usage", formatLicenseTooltipAmount(usage, yKind));
    addRow("Subscription", formatLicenseTooltipAmount(sub, yKind));
    addRow("Utilization", pctStr);
    addRow("Color", licenseUtilColorLabel(band));
    addRow("Criteria", licenseUtilCriteriaForBand(band));
    tip.appendChild(dl);

    tip.hidden = false;
    positionTip(ev);
  }

  function hideTip() {
    tip.hidden = true;
    tip.replaceChildren();
  }

  wrap.addEventListener("pointerover", (ev) => {
    const bar = ev.target && ev.target.closest && ev.target.closest(".o11y-license-chart__bar");
    if (!bar || !wrap.contains(bar)) {
      return;
    }
    fillTip(bar, ev);
  });
  wrap.addEventListener("pointermove", (ev) => {
    if (tip.hidden) {
      return;
    }
    const bar = ev.target && ev.target.closest && ev.target.closest(".o11y-license-chart__bar");
    if (bar && wrap.contains(bar)) {
      positionTip(ev);
    }
  });
  wrap.addEventListener("pointerout", (ev) => {
    const related = ev.relatedTarget;
    if (related && wrap.contains(related)) {
      const enteringBar = related.closest && related.closest(".o11y-license-chart__bar");
      if (enteringBar) {
        return;
      }
    }
    hideTip();
  });

  const onScrollOrResize = () => {
    if (!tip.hidden) {
      hideTip();
    }
  };
  window.addEventListener("scroll", onScrollOrResize, true);
  window.addEventListener("resize", onScrollOrResize);
}

/**
 * Replace ```o11y-license-chart JSON blocks with a compact SVG combo chart (usage bars, subscription line, % labels).
 */
function renderLicenseCapacityCharts(rootEl) {
  if (!rootEl) {
    return;
  }
  const blocks = [...rootEl.querySelectorAll("pre code.language-o11y-license-chart")];
  for (const codeEl of blocks) {
    const pre = codeEl.parentElement;
    if (!pre || pre.tagName !== "PRE") {
      continue;
    }
    const raw = (codeEl.textContent || "").trim();
    if (!raw) {
      continue;
    }
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (e) {
      console.warn("o11y-license-chart: invalid JSON", e);
      continue;
    }
    if (!payload || payload.v !== 1 || !Array.isArray(payload.points) || !payload.points.length) {
      continue;
    }
    const fig = document.createElement("figure");
    fig.className = "o11y-license-chart";
    fig.setAttribute("role", "img");
    fig.setAttribute("aria-label", String(payload.title || "License utilization chart"));
    fig.innerHTML = buildLicenseComboChartHtml(payload);
    pre.replaceWith(fig);
    attachLicenseChartTooltips(fig);
  }
}

function buildLicenseComboChartHtml(payload) {
  const yKind = licenseYKind(payload);
  const points = payload.points.map((p) => ({
    month: String(p.month || "").trim(),
    subscription: Math.max(0, Number(p.subscription) || 0),
    usage: Math.max(0, Number(p.usage) || 0),
  }));
  const n = points.length;
  const maxSub = Math.max(1e-12, ...points.map((p) => p.subscription));
  const maxUse = Math.max(0, ...points.map((p) => p.usage));
  /** Single Y-axis: same scale for bars (usage) and line (subscription cap). Ticks are “nice” up to max(usage, cap). */
  const maxRaw = Math.max(1e-12, maxSub, maxUse);
  const yCeilCandidate = maxRaw * 1.1;
  const ticksLeft = licenseYTickValues(yCeilCandidate, 6);
  const yBarScale = ticksLeft[ticksLeft.length - 1] || yCeilCandidate;
  const ySubScale = yBarScale;

  const uShort = yKind === "mib" ? "MiB" : yKind === "mb" ? "MB" : "count";
  const yAxisTitle = `Single scale (${uShort}): bars = monthly usage; line = subscription. Utilization % is shown above every bar.`;

  const longestTickDigits = ticksLeft.reduce(
    (m, tv) => Math.max(m, String(formatLicenseYTick(tv, yKind)).length),
    0
  );
  /** Left gutter for y tick labels (title lives in HTML below the chart header). */
  const L = Math.round(52 + Math.max(4, longestTickDigits) * 7);
  /** Extra top padding so utilization % labels are not clipped. */
  const T = n > 16 ? 42 : 46;
  const plotH = n > 18 ? 124 : n > 16 ? 120 : 132;
  const pxPerSlot = Math.max(30, Math.min(68, Math.floor(1040 / Math.max(1, n))));
  const plotW = Math.max(320, n * pxPerSlot);
  const slotW = plotW / n;
  /** Room for angled month labels (+ optional staggering when many months). */
  const denseMonths = n >= 11;
  const B = Math.round(Math.max(58, slotW * 0.54 + (denseMonths ? 40 : 30) + (n >= 16 ? 10 : 0)));
  const R = Math.max(18, Math.round(16 + Math.min(22, n * 0.65)));
  const W = L + plotW + R;
  const H = T + plotH + B;
  const yBase = T + plotH;
  const barW = Math.max(11, slotW * 0.42);
  const gradPrefix = `o11y-licg-${Math.random().toString(36).slice(2, 11)}`;

  const barMeta = points.map((p, i) => {
    const pctNum =
      p.subscription > 0 && Number.isFinite(p.subscription) && Number.isFinite(p.usage)
        ? (100 * p.usage) / p.subscription
        : null;
    const band = licenseUtilBandFromPct(pctNum);
    return { pctNum, band, gid: `${gradPrefix}-b${i}` };
  });

  const barDefs = barMeta
    .map(
      (m) =>
        `<linearGradient id="${m.gid}" x1="0" y1="0" x2="0" y2="1">${licenseBarGradientStops(m.band)}</linearGradient>`
    )
    .join("");

  const yFromBarScale = (v) => yBase - (v / yBarScale) * plotH;
  const yFromSubScale = (v) => yBase - (v / ySubScale) * plotH;

  const gridLines = ticksLeft
    .map((tv) => {
      const y = yFromBarScale(tv);
      return `<line class="o11y-license-chart__grid" x1="${L}" y1="${y}" x2="${L + plotW}" y2="${y}" />`;
    })
    .join("");

  const yTickLabels = ticksLeft
    .map((tv) => {
      const y = yFromBarScale(tv);
      const lab = escapeSvgText(formatLicenseYTick(tv, yKind));
      return `<text class="o11y-license-chart__tick o11y-license-chart__tick--usage" x="${L - 14}" y="${y + 4}" text-anchor="end">${lab}</text>`;
    })
    .join("");

  const barRects = points
    .map((p, i) => {
      const meta = barMeta[i];
      const cx = L + slotW * (i + 0.5);
      const x0 = cx - barW / 2;
      const h = (p.usage / yBarScale) * plotH;
      const y0 = yBase - h;
      return `<rect class="o11y-license-chart__bar o11y-license-chart__bar--${meta.band}" x="${x0.toFixed(
        2
      )}" y="${y0.toFixed(2)}" width="${barW.toFixed(2)}" height="${Math.max(0, h).toFixed(2)}" rx="4" ry="4" fill="url(#${
        meta.gid
      })" data-month="${escapeSvgText(p.month)}" data-usage="${p.usage}" data-subscription="${p.subscription}" />`;
    })
    .join("");

  const pctLabels = points
    .map((p, i) => {
      const meta = barMeta[i];
      const cx = L + slotW * (i + 0.5);
      const h = (p.usage / yBarScale) * plotH;
      const y0 = yBase - h;
      const pctStr =
        meta.pctNum != null && Number.isFinite(meta.pctNum)
          ? meta.pctNum < 10
            ? `${meta.pctNum.toFixed(2)}%`
            : `${meta.pctNum.toFixed(1)}%`
          : "—";
      const pctY = Math.max(T + 12, y0 - 11);
      return `<text class="o11y-license-chart__pct" x="${cx.toFixed(2)}" y="${pctY.toFixed(2)}" text-anchor="middle">${escapeSvgText(
        pctStr
      )}</text>`;
    })
    .join("");

  const linePts = points.map((p, i) => {
    const cx = L + slotW * (i + 0.5);
    const y = yFromSubScale(p.subscription);
    return `${cx.toFixed(2)},${y.toFixed(2)}`;
  });
  const linePathD = linePts.length > 1 ? `M ${linePts.join(" L ")}` : "";

  const xDeg = denseMonths ? -36 : -28;
  const xLabels = points
    .map((p, i) => {
      const cx = L + slotW * (i + 0.5);
      const oddStagger = i % 2 === 1 ? (n >= 13 ? 16 : n >= 11 ? 12 : 0) : 0;
      const y = H - 18 - oddStagger;
      return `<text class="o11y-license-chart__xlabel" x="${cx.toFixed(2)}" y="${y}" text-anchor="middle" dominant-baseline="middle" transform="rotate(${xDeg} ${cx.toFixed(
        2
      )} ${y})">${escapeSvgText(p.month)}</text>`;
    })
    .join("");

  const axisStroke = "#cbd5e1";
  /** Subscription line / dots — neutral blue (usage bars carry severity color). */
  const accent = "#2563eb";

  const dotR = n > 16 ? 3.35 : n > 12 ? 3.65 : 4;
  const lineDots = points
    .map((p, i) => {
      const cx = L + slotW * (i + 0.5);
      const y = yFromSubScale(p.subscription);
      return `<circle class="o11y-license-chart__dot" cx="${cx.toFixed(2)}" cy="${y.toFixed(
        2
      )}" r="${dotR}" fill="${accent}" stroke="#fff" stroke-width="${n > 16 ? "1.05" : "1.2"}"/>`;
    })
    .join("");

  return `
<figcaption class="o11y-license-chart__head">
  <div class="o11y-license-chart__title-row">
    <span class="o11y-license-chart__title">${escapeHtml(String(payload.title || "Entitlement"))}</span>
    <span class="o11y-license-chart__legend" aria-hidden="true">
      <span class="o11y-license-chart__lg o11y-license-chart__lg--bar"></span><span>Usage</span>
      <span class="o11y-license-chart__lg o11y-license-chart__lg--line"></span><span>Subscription</span>
    </span>
  </div>
  <p class="o11y-license-chart__y-caption">${escapeHtml(yAxisTitle)}</p>
</figcaption>
<div class="o11y-license-chart__svg-wrap">
  <svg class="o11y-license-chart__svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" aria-hidden="true" data-y-kind="${yKind}">
    <defs>
      ${barDefs}
    </defs>
    ${gridLines}
    <line class="o11y-license-chart__axis" x1="${L}" y1="${T}" x2="${L}" y2="${yBase}" stroke="${axisStroke}" />
    <line class="o11y-license-chart__axis" x1="${L}" y1="${yBase}" x2="${L + plotW}" y2="${yBase}" stroke="${axisStroke}" />
    ${barRects}
    ${
      linePathD
        ? `<path class="o11y-license-chart__line" d="${linePathD}" fill="none" stroke="${accent}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />`
        : ""
    }
    ${lineDots}
    ${yTickLabels}
    ${xLabels}
    ${pctLabels}
  </svg>
</div>`.trim();
}

/**
 * Turn ```mermaid fenced blocks into rendered diagrams (xychart-beta, flowcharts, etc.).
 * Runs after markdown → HTML; replaces ``pre > code.language-mermaid`` with Mermaid output.
 */
async function runMermaidDiagrams(rootEl) {
  if (!rootEl || typeof mermaid === "undefined" || typeof mermaid.run !== "function") {
    return;
  }
  const blocks = [...rootEl.querySelectorAll("pre code.language-mermaid")];
  if (!blocks.length) {
    return;
  }
  const nodes = [];
  blocks.forEach((codeEl) => {
    const pre = codeEl.parentElement;
    if (!pre || pre.tagName !== "PRE") {
      return;
    }
    const src = (codeEl.textContent || "").trim();
    if (!src) {
      return;
    }
    const wrap = document.createElement("div");
    wrap.className = "o11y-mermaid-block";
    wrap.setAttribute("role", "img");
    const diagram = document.createElement("div");
    diagram.className = "mermaid";
    diagram.textContent = src;
    wrap.appendChild(diagram);
    pre.replaceWith(wrap);
    nodes.push(diagram);
  });
  if (!nodes.length) {
    return;
  }
  try {
    await mermaid.run({ nodes });
  } catch (e) {
    console.warn("Mermaid render failed", e);
  }
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/**
 * Replace Green / Yellow / Orange / Red text in the Color column with colored badges (by header name).
 * Severity-legend tables (Level | Meaning, or Band or Color paired with Criteria) get full-cell fills on the first column.
 */
function enhanceTables(rootEl) {
  rootEl.querySelectorAll("table").forEach((table) => {
    const headerRow =
      table.querySelector("thead tr") || (table.tHead && table.tHead.rows[0]) || null;
    const headers = headerRow ? [...headerRow.querySelectorAll("th, td")] : [];

    const levelIdx = headers.findIndex((th) => th.textContent.trim().toLowerCase() === "level");
    const meaningIdx = headers.findIndex((th) => th.textContent.trim().toLowerCase() === "meaning");
    const bandIdx = headers.findIndex((th) => th.textContent.trim().toLowerCase() === "band");
    const colorWithCriteriaIdx = headers.findIndex((th) => th.textContent.trim().toLowerCase() === "color");
    const criteriaIdx = headers.findIndex((th) => th.textContent.trim().toLowerCase() === "criteria");
    if (levelIdx >= 0 && meaningIdx >= 0) {
      let dataRows = [...table.querySelectorAll("tbody tr")];
      if (!dataRows.length && table.rows.length > 1) {
        dataRows = [...table.rows].slice(1);
      }
      dataRows.forEach((tr) => {
        const cells = tr.querySelectorAll("td");
        const td = cells[levelIdx];
        if (!td) return;
        const t = td.textContent.trim();
        if (t === "Green" || t === "Yellow" || t === "Orange" || t === "Red") {
          td.classList.add("severity-legend-level", `severity-legend-level--${t.toLowerCase()}`);
          td.textContent = t;
        }
      });
      return;
    }
    const licenseLegendColorCol =
      bandIdx >= 0 && criteriaIdx >= 0
        ? bandIdx
        : colorWithCriteriaIdx >= 0 && criteriaIdx >= 0 && colorWithCriteriaIdx !== criteriaIdx
          ? colorWithCriteriaIdx
          : -1;
    if (licenseLegendColorCol >= 0 && criteriaIdx >= 0) {
      let dataRows = [...table.querySelectorAll("tbody tr")];
      if (!dataRows.length && table.rows.length > 1) {
        dataRows = [...table.rows].slice(1);
      }
      dataRows.forEach((tr) => {
        const cells = tr.querySelectorAll("td");
        const td = cells[licenseLegendColorCol];
        if (!td) return;
        const t = td.textContent.trim();
        if (t === "Green" || t === "Yellow" || t === "Orange" || t === "Red") {
          td.classList.add("severity-legend-level", `severity-legend-level--${t.toLowerCase()}`);
          td.textContent = t;
        }
      });
      return;
    }

    let colorCol = -1;
    const idx = headers.findIndex((th) => th.textContent.trim().toLowerCase() === "color");
    if (idx >= 0) colorCol = idx;
    if (colorCol < 0) return;
    const dataRows = [...table.querySelectorAll("tbody tr")];
    dataRows.forEach((tr) => {
      const cells = tr.querySelectorAll("td");
      const td = cells[colorCol];
      if (!td) return;
      const t = td.textContent.trim();
      if (t === "Green" || t === "Yellow" || t === "Orange" || t === "Red") {
        td.innerHTML = `<span class="badge-cell badge-cell--${t.toLowerCase()}">${escapeHtml(t)}</span>`;
      }
    });
  });
}

/**
 * Column headers that indicate the cell is a percentage (markdown often omits the % sign).
 */
function isPercentColumnHeader(headerText) {
  const t = headerText.trim();
  if (!t) return false;
  if (/\butilization\s*%/i.test(t)) return true;
  if (/\butilization\s*\/\s*monitored\b/i.test(t)) return true;
  if (/^%\s*of\b/i.test(t)) return true;
  if (/%\s*of\s*total/i.test(t)) return true;
  return false;
}

/**
 * True if the cell looks like a raw number that should display with a trailing %.
 */
function isNumericPercentValue(text) {
  const t = text.trim();
  if (t === "" || t === "—" || t === "-") return false;
  if (/%\s*$/.test(t)) return false;
  if (/^(Green|Yellow|Orange|Red)$/i.test(t)) return false;
  return /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(t);
}

/**
 * Append % to percentage columns after markdown render (source tables use bare numbers).
 */
function enhancePercentFormatting(rootEl) {
  rootEl.querySelectorAll("table").forEach((table) => {
    const headerRow =
      table.querySelector("thead tr") || (table.tHead && table.tHead.rows[0]) || null;
    if (!headerRow) return;
    const headers = [...headerRow.querySelectorAll("th, td")];
    if (!headers.length) return;

    const percentColIndexes = [];
    headers.forEach((th, i) => {
      if (isPercentColumnHeader(th.textContent)) percentColIndexes.push(i);
    });
    if (!percentColIndexes.length) return;

    let dataRows = [...table.querySelectorAll("tbody tr")];
    if (!dataRows.length && table.rows.length > 1) {
      dataRows = [...table.rows].slice(1);
    }

    dataRows.forEach((tr) => {
      const cells = tr.querySelectorAll("td");
      percentColIndexes.forEach((idx) => {
        const td = cells[idx];
        if (!td) return;
        const raw = td.textContent;
        if (!isNumericPercentValue(raw)) return;
        td.textContent = `${raw.trim()}%`;
        td.classList.add("cell-percent");
      });
    });
  });
}

/**
 * Remove legacy **Findings** subsections from rendered HTML (reports no longer emit them).
 */
function removeFindingsSections(rootEl) {
  [...rootEl.querySelectorAll("h3")]
    .filter((h) => h.textContent.trim().toLowerCase() === "findings")
    .forEach((h3) => {
      const rm = [h3];
      let n = h3.nextSibling;
      while (n) {
        if (n.nodeType === 1 && /^H[23]$/i.test(n.tagName)) break;
        rm.push(n);
        n = n.nextSibling;
      }
      rm.forEach((el) => el.remove());
    });
}

/**
 * Remove **Recommendation** subheads (h3/h4) and their body until the next heading — License utilization viewer only.
 */
function removeRecommendationSectionsLicenseCapacity(rootEl) {
  [...rootEl.querySelectorAll("h3, h4")]
    .filter((h) => h.textContent.trim().toLowerCase() === "recommendation")
    .forEach((hx) => {
      const rm = [hx];
      let n = hx.nextSibling;
      while (n) {
        if (n.nodeType === 1 && /^H[1-6]$/i.test(n.tagName)) break;
        rm.push(n);
        n = n.nextSibling;
      }
      rm.forEach((el) => el.remove());
    });
}

/** Platform engagement — hidden HTML comment with urlsafe base64 JSON (see o11y_platform_engagement_trends.py). */
function extractPlatformEngagementKpiPayload(mdSectionBody) {
  const m = (mdSectionBody || "").match(/<!-- O11Y_PE_KPI:([A-Za-z0-9_-]+=*) -->/);
  if (!m) return null;
  try {
    const json = base64UrlToUtf8(m[1]);
    const data = JSON.parse(json);
    if (!data || !Array.isArray(data.kpis)) return null;
    return data;
  } catch (_) {
    return null;
  }
}

function stripPlatformEngagementKpiPayload(mdSectionBody) {
  return (mdSectionBody || "").replace(/\n?<!-- O11Y_PE_KPI:[A-Za-z0-9_-]+=* -->\n?/g, "\n");
}

function base64UrlToUtf8(s) {
  let b = s.replace(/-/g, "+").replace(/_/g, "/");
  const pad = b.length % 4;
  if (pad) b += "=".repeat(4 - pad);
  const bin = atob(b);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

function formatPeScalarDisplay(n) {
  if (n == null || n === "") return "—";
  const v = typeof n === "number" ? n : parseFloat(String(n).replace(/,/g, ""));
  if (!Number.isFinite(v)) return "—";
  const ax = Math.abs(v);
  if (ax >= 1e9) return `${(v / 1e9).toFixed(2).replace(/\.?0+$/, "")}B`;
  if (ax >= 1e6) return `${(v / 1e6).toFixed(2).replace(/\.?0+$/, "")}M`;
  if (ax >= 1e4) return `${(v / 1e3).toFixed(2).replace(/\.?0+$/, "")}k`;
  if (Math.abs(v - Math.round(v)) < 1e-6) return String(Math.round(v));
  return v.toFixed(2).replace(/\.?0+$/, "");
}

function formatPeShortDate(iso) {
  if (!iso || typeof iso !== "string") return "";
  const p = iso.split("-");
  if (p.length < 3) return iso;
  const mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][
    parseInt(p[1], 10) - 1
  ];
  return `${mo} ${parseInt(p[2], 10)}`;
}

/** e.g. 2026-04-01 + 2026-04-30 → "Apr 1 – Apr 30, 2026" (year disambiguates duplicate month/day pairs). */
function formatPeCalendarRangePretty(startIso, endIso) {
  if (!startIso || !endIso || typeof startIso !== "string" || typeof endIso !== "string") {
    return "";
  }
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const parse = (s) => {
    const p = s.trim().split("-");
    if (p.length < 3) return null;
    const y = parseInt(p[0], 10);
    const m = parseInt(p[1], 10) - 1;
    const d = parseInt(p[2], 10);
    if (!Number.isFinite(y) || m < 0 || m > 11 || !Number.isFinite(d)) return null;
    return { y, m, d };
  };
  const a = parse(startIso);
  const b = parse(endIso);
  if (!a || !b) return "";
  if (a.y === b.y) {
    return `${months[a.m]} ${a.d} – ${months[b.m]} ${b.d}, ${a.y}`;
  }
  return `${months[a.m]} ${a.d}, ${a.y} – ${months[b.m]} ${b.d}, ${b.y}`;
}

/**
 * KPI card value block: side-by-side Baseline | Comparison (or Current) aligned with the timeline banner.
 */
function peBuildKpiCardValuesHtml(kpi, payload, baseDisp, curDisp) {
  const tl = payload.timeline;
  const mode = tl && tl.comparisonMode;

  const baseStart = String(kpi.baselineWindowStart || payload.baselineWindowStart || "");
  const baseEnd = String(kpi.baselineWindowEnd || payload.baselineWindowEnd || "");
  const curStart = String(kpi.currentWindowStart || payload.currentWindowStart || "");
  const curEnd = String(kpi.currentWindowEnd || payload.currentWindowEnd || "");
  const b0 = formatPeShortDate(baseStart);
  const b1 = formatPeShortDate(baseEnd);
  const c0 = formatPeShortDate(curStart);
  const c1 = formatPeShortDate(curEnd);

  const split = (leftLabel, leftPeriod, leftVal, rightLabel, rightPeriod, rightVal, foot) => `
      <div class="pe-kpi-card__values pe-kpi-card__values--split">
        <div class="pe-kpi-card__split pe-kpi-card__split--baseline">
          <span class="pe-kpi-card__split-label">${escapeHtml(leftLabel)}</span>
          <span class="pe-kpi-card__split-period">${escapeHtml(leftPeriod)}</span>
          <span class="pe-kpi-card__split-value">${escapeHtml(leftVal)}</span>
        </div>
        <div class="pe-kpi-card__split-div" aria-hidden="true"></div>
        <div class="pe-kpi-card__split pe-kpi-card__split--comparison">
          <span class="pe-kpi-card__split-label">${escapeHtml(rightLabel)}</span>
          <span class="pe-kpi-card__split-period">${escapeHtml(rightPeriod)}</span>
          <span class="pe-kpi-card__split-value">${escapeHtml(rightVal)}</span>
        </div>
      </div>
      ${foot ? `<p class="pe-kpi-card__utc-foot">${escapeHtml(foot)}</p>` : ""}`;

  if (mode === "month_vs_month" && tl.baseline && tl.comparison) {
    const bm = String(tl.baseline.monthLabel || "").trim();
    const cm = String(tl.comparison.monthLabel || "").trim();
    if (bm && cm) {
      return split("Baseline", bm, baseDisp, "Comparison", cm, curDisp, "Full calendar month · UTC");
    }
  }

  if (mode === "custom_calendar_range" && tl.baseline && tl.comparison) {
    const lp = formatPeCalendarRangePretty(baseStart, baseEnd) || (b0 && b1 ? `${b0} – ${b1}` : "—");
    const rp = formatPeCalendarRangePretty(curStart, curEnd) || (c0 && c1 ? `${c0} – ${c1}` : "—");
    return split("Baseline", lp, baseDisp, "Comparison", rp, curDisp, "UTC comparison windows");
  }

  if (mode === "rolling_default" && tl.baseline && tl.comparison) {
    const lp = formatPeCalendarRangePretty(baseStart, baseEnd) || (b0 && b1 ? `${b0} – ${b1}` : "—");
    const rp = formatPeCalendarRangePretty(curStart, curEnd) || (c0 && c1 ? `${c0} – ${c1}` : "—");
    return split("Baseline", lp, baseDisp, "Current", rp, curDisp, "");
  }

  const baselineRange = b0 && b1 ? `${b0} – ${b1}` : "Baseline window";
  const currentRange = c0 && c1 ? `${c0} – ${c1}` : "Current window";
  return `
      <div class="pe-kpi-card__values pe-kpi-card__values--stacked">
        <div class="pe-kpi-card__value-row">
          <span class="pe-kpi-card__value-label">${escapeHtml(baselineRange)}</span>
          <span class="pe-kpi-card__value-num">${escapeHtml(baseDisp)}</span>
        </div>
        <div class="pe-kpi-card__value-row pe-kpi-card__value-row--current">
          <span class="pe-kpi-card__value-label">${escapeHtml(currentRange)}</span>
          <span class="pe-kpi-card__value-num">${escapeHtml(curDisp)}</span>
        </div>
      </div>`;
}

function formatPeDeltaDisplay(pct) {
  if (pct == null || pct === "") return { text: "—", dir: "flat" };
  const v = typeof pct === "number" ? pct : parseFloat(pct);
  if (!Number.isFinite(v)) return { text: "—", dir: "flat" };
  const dir = v > 0 ? "up" : v < 0 ? "down" : "flat";
  const sign = v > 0 ? "+" : "";
  return { text: `${sign}${v.toFixed(2)}%`, dir };
}

function peKpiSparklinePoints(kpi) {
  let pts = Array.isArray(kpi.points) ? kpi.points : [];
  if (pts.length < 2 && kpi.baseline != null && kpi.current != null) {
    const t0 = 0;
    const t1 = 1;
    pts = [
      [t0, Number(kpi.baseline)],
      [t1, Number(kpi.current)],
    ];
  }
  return pts
    .filter((row) => Array.isArray(row) && row.length >= 2)
    .map((row) => [Number(row[0]), Math.max(0, Number(row[1]))])
    .filter((row) => Number.isFinite(row[0]) && Number.isFinite(row[1]));
}

function peKpiSparklineSvg(pts, stroke, gradId) {
  const gid = gradId || "peSparkFill0";
  if (pts.length < 2) {
    return `<svg class="pe-kpi-spark" viewBox="0 0 120 36" preserveAspectRatio="none" aria-hidden="true"><line x1="0" y1="18" x2="120" y2="18" stroke="currentColor" stroke-opacity="0.12" stroke-width="1"/></svg>`;
  }
  const ys = pts.map((p) => p[1]);
  const ymin = Math.min(...ys);
  const ymax = Math.max(...ys, ymin + 1e-9);
  const pad = 4;
  const w = 120;
  const h = 36;
  const coords = pts.map((p, i) => {
    const x = pad + (i / (pts.length - 1)) * (w - 2 * pad);
    const yn = (p[1] - ymin) / (ymax - ymin);
    const y = h - pad - yn * (h - 2 * pad);
    return [x, y];
  });
  const d = coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  return `<svg class="pe-kpi-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="${escapeHtml(gid)}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${stroke}" stop-opacity="0.22"/><stop offset="100%" stop-color="${stroke}" stop-opacity="0"/></linearGradient></defs><path d="${d} L${(w - pad).toFixed(1)},${(h - pad).toFixed(1)} L${pad},${(h - pad).toFixed(1)} Z" fill="url(#${escapeHtml(gid)})" /><path d="${d}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function peKpiIconSvg(kpiId, accent) {
  const id = (kpiId || "").toLowerCase();
  let path = "";
  if (id.includes("user")) {
    path =
      '<path fill="none" stroke="currentColor" stroke-width="1.6" d="M12 12a3.5 3.5 0 1 0-7 0 3.5 3.5 0 0 0 7 0zM4.5 22c0-3.3 2.5-6 5.5-6s5.5 2.7 5.5 6"/>';
  } else if (id.includes("team")) {
    path =
      '<path fill="none" stroke="currentColor" stroke-width="1.6" d="M6 10a2 2 0 1 0-4 0 2 2 0 0 0 4 0zm10 0a2 2 0 1 0-4 0 2 2 0 0 0 4 0zM2 20c0-2.5 2-4.5 4.5-4.5H7m6 0h.5c2.5 0 4.5 2 4.5 4.5"/>';
  } else if (id.includes("dashboard")) {
    path =
      '<path fill="none" stroke="currentColor" stroke-width="1.6" d="M4 5h7v6H4V5zm9 0h7v6h-7V5zM4 13h7v6H4v-6zm9 0h7v6h-7v-6z"/>';
  } else if (id.includes("detector")) {
    path =
      '<path fill="none" stroke="currentColor" stroke-width="1.6" d="M12 4l1.8 3.6 4 .6-2.9 2.8.7 4L12 14.2 8.4 15l.7-4L6.2 8.2l4-.6L12 4z"/>';
  } else if (id.includes("custom") || id.includes("metric")) {
    path =
      '<path fill="none" stroke="currentColor" stroke-width="1.6" d="M5 18V6h4v12H5zm5 0V10h4v8h-4zm5 0v-4h4v4h-4z"/>';
  } else if (id.includes("instrument") || id.includes("app")) {
    path =
      '<path fill="none" stroke="currentColor" stroke-width="1.6" d="M12 4c-2.5 3-4 6.5-4 10a4 4 0 0 0 8 0c0-3.5-1.5-7-4-10zm0 14v4"/>';
  } else {
    path =
      '<path fill="none" stroke="currentColor" stroke-width="1.6" d="M4 14h16M4 10h16M8 6h8M8 18h8"/>';
  }
  return `<svg class="pe-kpi-icon-svg" viewBox="0 0 24 24" width="28" height="28" aria-hidden="true" style="color:${accent}">${path}</svg>`;
}

// ── Platform engagement KPI drill-down modal ─────────────────────────────────

/**
 * Format a Unix-ms timestamp as "MMM D, YYYY HH:MM UTC".
 */
function formatPeTimestamp(ms) {
  if (!Number.isFinite(ms)) return "—";
  const d = new Date(ms);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const pad = (n) => String(n).padStart(2, "0");
  return `${months[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

/**
 * Given a kpi and the payload timeline, classify each point as "baseline", "comparison", or "other".
 * Returns { baselinePts, comparisonPts, otherPts, baselineLabel, comparisonLabel }.
 */
function peClassifyPoints(kpi, payload) {
  const pts = (kpi.points || [])
    .filter((r) => Array.isArray(r) && r.length >= 2)
    .map((r) => [Number(r[0]), Number(r[1])])
    .filter((r) => Number.isFinite(r[0]) && Number.isFinite(r[1]));

  const tl = payload.timeline || {};
  const mode = tl.comparisonMode;

  // Resolve window bounds in ms from kpi-level overrides or payload-level fields.
  const parseIsoMs = (s) => {
    if (!s || typeof s !== "string") return null;
    const d = new Date(s + (s.length === 10 ? "T00:00:00Z" : ""));
    return Number.isFinite(d.getTime()) ? d.getTime() : null;
  };

  let baseStart = null, baseEnd = null, curStart = null, curEnd = null;
  let baselineLabel = "Baseline", comparisonLabel = "Comparison";

  if (mode === "month_vs_month" && tl.baseline && tl.comparison) {
    baseStart = parseIsoMs(tl.baseline.rangeStart);
    baseEnd = parseIsoMs(tl.baseline.rangeEnd);
    curStart = parseIsoMs(tl.comparison.rangeStart);
    curEnd = parseIsoMs(tl.comparison.rangeEnd);
    baselineLabel = `Baseline (${tl.baseline.monthLabel || ""})`;
    comparisonLabel = `Comparison (${tl.comparison.monthLabel || ""})`;
    // rangeEnd is end-of-month date string — extend to end of that day
    if (baseEnd != null) baseEnd += 86400000 - 1;
    if (curEnd != null) curEnd += 86400000 - 1;
  } else {
    // Use kpi-level or payload-level window fields (ISO date strings)
    const bs = kpi.baselineWindowStart || payload.baselineWindowStart;
    const be = kpi.baselineWindowEnd || payload.baselineWindowEnd;
    const cs = kpi.currentWindowStart || payload.currentWindowStart;
    const ce = kpi.currentWindowEnd || payload.currentWindowEnd;
    baseStart = parseIsoMs(bs);
    baseEnd = parseIsoMs(be);
    curStart = parseIsoMs(cs);
    curEnd = parseIsoMs(ce);
    if (baseEnd != null) baseEnd += 86400000 - 1;
    if (curEnd != null) curEnd += 86400000 - 1;
    if (mode === "rolling_default") comparisonLabel = "Current";
  }

  const inRange = (ts, lo, hi) => lo != null && hi != null && ts >= lo && ts <= hi;

  const baselinePts = pts.filter((r) => inRange(r[0], baseStart, baseEnd));
  const comparisonPts = pts.filter((r) => inRange(r[0], curStart, curEnd));
  const classifiedTs = new Set([...baselinePts, ...comparisonPts].map((r) => r[0]));
  const otherPts = pts.filter((r) => !classifiedTs.has(r[0]));

  return { baselinePts, comparisonPts, otherPts, baselineLabel, comparisonLabel, allPts: pts };
}

function peDrilldownTableHtml(pts, label, acc) {
  if (!pts.length) {
    return `<p class="pe-drill-empty">No data points in the ${escapeHtml(label)} window.</p>`;
  }
  const rows = pts
    .slice()
    .sort((a, b) => a[0] - b[0])
    .map(
      (r) =>
        `<tr><td class="pe-drill-ts">${escapeHtml(formatPeTimestamp(r[0]))}</td>` +
        `<td class="pe-drill-val">${escapeHtml(formatPeScalarDisplay(r[1]))}</td></tr>`
    )
    .join("");
  const vals = pts.map((r) => r[1]);
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  return `
    <table class="pe-drill-table">
      <thead><tr><th>Timestamp (UTC)</th><th>Value</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="pe-drill-summary">
      <span><strong>Points:</strong> ${pts.length}</span>
      <span><strong>Avg:</strong> ${escapeHtml(formatPeScalarDisplay(mean))}</span>
      <span><strong>Min:</strong> ${escapeHtml(formatPeScalarDisplay(min))}</span>
      <span><strong>Max:</strong> ${escapeHtml(formatPeScalarDisplay(max))}</span>
    </div>`;
}

/**
 * Fetch the custom-metrics breakdown from the hub API and render it into `container`.
 * Only available when the report was loaded via the hub (hubJob param in URL).
 */
function peShowCredentialForm(container, lookback, lookbackSel) {
  container.innerHTML = `
    <div class="pe-drill-cred-form">
      <p class="pe-drill-cred-note">The job session has expired. Enter your credentials to load the breakdown directly.</p>
      <div class="pe-drill-cred-row">
        <div class="pe-drill-cred-field">
          <label class="pe-drill-cred-label" for="pe-drill-realm-input">Realm</label>
          <input id="pe-drill-realm-input" class="pe-drill-cred-input" type="text" placeholder="us0" value="us0" />
        </div>
        <div class="pe-drill-cred-field" style="flex:2">
          <label class="pe-drill-cred-label" for="pe-drill-token-input">Org access token</label>
          <input id="pe-drill-token-input" class="pe-drill-cred-input" type="password" placeholder="••••••••" autocomplete="off" />
        </div>
        <button class="btn pe-drill-load-btn" id="pe-drill-cred-submit">Load</button>
      </div>
      <p class="pe-drill-cred-hint">Token is used only for this request and never stored.</p>
      <div id="pe-drill-cred-result"></div>
    </div>`;

  document.getElementById("pe-drill-cred-submit").addEventListener("click", async () => {
    const realm = (document.getElementById("pe-drill-realm-input")?.value || "us0").trim();
    const token = (document.getElementById("pe-drill-token-input")?.value || "").trim();
    if (!token) { document.getElementById("pe-drill-cred-result").innerHTML = `<p class="pe-drill-fetch-error">Token is required.</p>`; return; }
    const resultEl = document.getElementById("pe-drill-cred-result");
    resultEl.innerHTML = `<div class="pe-drill-loading"><span class="pe-drill-spinner"></span> Loading…</div>`;
    await peLoadCustomMetricsBreakdownDirect(resultEl, lookback, realm, token);
  });
}

async function peLoadCustomMetricsBreakdownDirect(container, lookback, realm, token) {
  try {
    const resp = await fetch(`/api/drilldown/custom-metrics?lookback=${encodeURIComponent(lookback)}&limit=2000&realm=${encodeURIComponent(realm)}`, {
      method: "GET",
      headers: { "X-CM-Token": token },
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      container.innerHTML = `<p class="pe-drill-fetch-error">API error: ${escapeHtml(data.error || `HTTP ${resp.status}`)}</p>`;
      return;
    }
    peRenderCustomMetricsData(container, data);
  } catch (e) {
    container.innerHTML = `<p class="pe-drill-fetch-error">Request failed: ${escapeHtml(String(e))}</p>`;
  }
}

async function peLoadCustomMetricsBreakdown(container, lookback) {
  const params = new URLSearchParams(window.location.search);
  const jobId = params.get("hubJob") || "";
  if (!jobId) {
    peShowCredentialForm(container, lookback, null);
    return;
  }

  container.innerHTML = `<div class="pe-drill-loading"><span class="pe-drill-spinner"></span> Loading metric breakdown from Usage Analytics API…</div>`;

  try {
    const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/drilldown/custom-metrics?lookback=${encodeURIComponent(lookback)}&limit=2000`);
    const data = await resp.json();
    if (!resp.ok || data.error) {
      // Job expired or deleted — fall back to credential form
      peShowCredentialForm(container, lookback, null);
      return;
    }

    const { metrics = [], utilizationSummary = {}, totalCustomMts, totalOrgMts, customMetricCount } = data;

    // Utilization breakdown bar
    const UTIL_ORDER = ["R0 - Unused", "R1 - Inactive Charts", "R2 - API queries", "R3 - Active charts", "R4 - Detectors"];
    peRenderCustomMetricsData(container, data);
  } catch (e) {
    container.innerHTML = `<p class="pe-drill-fetch-error">Request failed: ${escapeHtml(String(e))}</p>`;
  }
}

function peRenderCustomMetricsData(container, data) {
  const { metrics = [], utilizationSummary = {}, totalCustomMts, totalOrgMts, customMetricCount, lookback = "" } = data;

  const UTIL_ORDER = ["R0 - Unused", "R1 - Inactive Charts", "R2 - API queries", "R3 - Active charts", "R4 - Detectors"];
  const UTIL_COLORS = { "R0 - Unused": "#ef4444", "R1 - Inactive Charts": "#f97316", "R2 - API queries": "#eab308", "R3 - Active charts": "#22c55e", "R4 - Detectors": "#3b82f6" };
  const utilRows = UTIL_ORDER.filter((u) => utilizationSummary[u] > 0).map((u) => {
    const mts = utilizationSummary[u];
    const pct = totalCustomMts > 0 ? (100 * mts / totalCustomMts) : 0;
    return { label: u, mts, pct, color: UTIL_COLORS[u] || "#94a3b8" };
  });

  const barSegs = utilRows.map((u) =>
    `<div class="pe-drill-util-seg" style="width:${u.pct.toFixed(1)}%;background:${u.color}" title="${escapeHtml(u.label)}: ${u.pct.toFixed(1)}%"></div>`
  ).join("");

  const utilLegend = utilRows.map((u) =>
    `<div class="pe-drill-util-leg"><span class="pe-drill-util-dot" style="background:${u.color}"></span>` +
    `<span class="pe-drill-util-leg-label">${escapeHtml(u.label)}</span>` +
    `<span class="pe-drill-util-leg-val">${escapeHtml(formatPeScalarDisplay(u.mts))} MTS (${u.pct.toFixed(1)}%)</span></div>`
  ).join("");

  const tableRows = metrics.slice(0, 200).map((m) => {
    const utilColor = UTIL_COLORS[m.utilization] || "#94a3b8";
    return `<tr>
      <td class="pe-drill-mname">${escapeHtml(m.metricName)}</td>
      <td><span class="pe-drill-util-badge" style="background:${utilColor}20;color:${utilColor};border-color:${utilColor}40">${escapeHtml(m.utilization)}</span></td>
      <td class="pe-drill-val">${escapeHtml(formatPeScalarDisplay(m.averageHourlyMts))}</td>
      <td class="pe-drill-val">${m.pctOfCustomTotal.toFixed(2)}%</td>
      <td class="pe-drill-val">${m.pctOfOrgTotal.toFixed(2)}%</td>
    </tr>`;
  }).join("");

  const moreNote = metrics.length > 200 ? `<p class="pe-drill-more-note">Showing top 200 of ${metrics.length} custom metrics.</p>` : "";

  container.innerHTML = `
    <div class="pe-drill-cm-summary">
      <div class="pe-drill-cm-stat"><span class="pe-drill-cm-stat-num">${escapeHtml(formatPeScalarDisplay(customMetricCount))}</span><span class="pe-drill-cm-stat-label">Custom metrics</span></div>
      <div class="pe-drill-cm-stat"><span class="pe-drill-cm-stat-num">${escapeHtml(formatPeScalarDisplay(totalCustomMts))}</span><span class="pe-drill-cm-stat-label">Custom MTS (avg/hr)</span></div>
      <div class="pe-drill-cm-stat"><span class="pe-drill-cm-stat-num">${totalOrgMts > 0 ? (100 * totalCustomMts / totalOrgMts).toFixed(1) + "%" : "—"}</span><span class="pe-drill-cm-stat-label">% of org total MTS</span></div>
      ${lookback ? `<div class="pe-drill-cm-stat"><span class="pe-drill-cm-stat-num">${escapeHtml(lookback)}</span><span class="pe-drill-cm-stat-label">Lookback window</span></div>` : ""}
    </div>
    <h5 class="pe-drill-cm-section-head">Utilization breakdown</h5>
    <div class="pe-drill-util-bar">${barSegs || '<div style="width:100%;background:var(--splunk-border);height:100%"></div>'}</div>
    <div class="pe-drill-util-legend">${utilLegend || '<p class="pe-drill-empty">No utilization data.</p>'}</div>
    <div class="pe-drill-cm-insight">${buildCustomMetricsInsight(utilRows, totalCustomMts)}</div>
    <h5 class="pe-drill-cm-section-head">Top custom metrics by MTS volume</h5>
    <table class="pe-drill-table pe-drill-cm-table">
      <thead><tr><th>Metric name</th><th>Utilization</th><th>Avg MTS/hr</th><th>% of custom</th><th>% of org</th></tr></thead>
      <tbody>${tableRows}</tbody>
    </table>
    ${moreNote}`;
}

/**
 * Generate an actionable insight sentence based on utilization distribution.
 */
function buildCustomMetricsInsight(utilRows, totalCustomMts) {
  const unusedRow = utilRows.find((u) => u.label === "R0 - Unused");
  const inactiveRow = utilRows.find((u) => u.label === "R1 - Inactive Charts");
  const wastedPct = ((unusedRow?.pct || 0) + (inactiveRow?.pct || 0));
  const wastedMts = ((unusedRow?.mts || 0) + (inactiveRow?.mts || 0));
  if (wastedPct === 0) return "";
  const severity = wastedPct >= 50 ? "pe-drill-insight--high" : wastedPct >= 20 ? "pe-drill-insight--medium" : "pe-drill-insight--low";
  return `<div class="pe-drill-insight ${severity}">
    <strong>${wastedPct.toFixed(0)}% of custom MTS (${escapeHtml(formatPeScalarDisplay(wastedMts))} avg/hr) is Unused or only in Inactive Charts.</strong>
    Use Metric Pipeline Management to archive or drop these — they count toward your custom metrics limit but provide no active value.
  </div>`;
}

/**
 * Open the KPI drill-down modal for a given kpi + payload.
 */
function peOpenKpiDrilldown(kpi, payload, acc) {
  // Remove any existing modal
  document.getElementById("pe-drill-modal")?.remove();

  const { baselinePts, comparisonPts, otherPts, baselineLabel, comparisonLabel, allPts } =
    peClassifyPoints(kpi, payload);

  const hasWindows = baselinePts.length > 0 || comparisonPts.length > 0;
  const label = String(kpi.label || kpi.id || "KPI");
  const metric = String(kpi.metric || "");
  const delta = formatPeDeltaDisplay(kpi.pctChange);
  const arrow = delta.dir === "up" ? "▲" : delta.dir === "down" ? "▼" : "◆";
  const deltaClass =
    delta.dir === "up" ? "pe-kpi-card__delta--up" : delta.dir === "down" ? "pe-kpi-card__delta--down" : "pe-kpi-card__delta--flat";

  const baseDisp = kpi.baseline != null ? formatPeScalarDisplay(kpi.baseline) : "—";
  const curDisp = kpi.current != null ? formatPeScalarDisplay(kpi.current) : "—";

  const modal = document.createElement("div");
  modal.id = "pe-drill-modal";
  modal.className = "pe-drill-modal";
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("aria-label", `Drill-down: ${label}`);
  modal.style.setProperty("--pe-ring", acc.ring);
  modal.style.setProperty("--pe-stroke", acc.stroke);
  modal.style.setProperty("--pe-soft", acc.soft);

  const comparisonSection = hasWindows
    ? `<div class="pe-drill-windows">
        <div class="pe-drill-window pe-drill-window--baseline">
          <h4 class="pe-drill-window-title">${escapeHtml(baselineLabel)}</h4>
          <div class="pe-drill-avg-chip">${escapeHtml(baseDisp)}</div>
          ${peDrilldownTableHtml(baselinePts, baselineLabel, acc)}
        </div>
        <div class="pe-drill-window pe-drill-window--comparison">
          <h4 class="pe-drill-window-title">${escapeHtml(comparisonLabel)}</h4>
          <div class="pe-drill-avg-chip pe-drill-avg-chip--comparison">${escapeHtml(curDisp)}</div>
          ${peDrilldownTableHtml(comparisonPts, comparisonLabel, acc)}
        </div>
      </div>`
    : `<div class="pe-drill-windows">
        <div class="pe-drill-window" style="flex:1">
          <h4 class="pe-drill-window-title">All data points (${allPts.length})</h4>
          ${peDrilldownTableHtml(allPts, "all", acc)}
        </div>
      </div>`;

  const otherSection =
    otherPts.length > 0
      ? `<details class="pe-drill-other">
          <summary>Other / transition points (${otherPts.length})</summary>
          ${peDrilldownTableHtml(otherPts, "other", acc)}
        </details>`
      : "";

  const isCustomMetrics = String(kpi.id || "").toLowerCase() === "custom_metrics";
  const customMetricsSection = isCustomMetrics ? `
    <div class="pe-drill-cm-panel">
      <div class="pe-drill-cm-header">
        <h4 class="pe-drill-cm-title">Custom metrics source breakdown</h4>
        <p class="pe-drill-cm-subtitle">Which metrics are driving your custom MTS count, what they're used for, and where to reduce.</p>
        <div class="pe-drill-cm-controls">
          <label class="pe-drill-cm-lookback-label" for="pe-drill-lookback">Lookback</label>
          <select id="pe-drill-lookback" class="pe-drill-lookback-select">
            <option value="P1D">1 day</option>
            <option value="P7D" selected>7 days</option>
            <option value="P30D">30 days</option>
          </select>
          <button class="btn pe-drill-load-btn" id="pe-drill-load-cm">Load breakdown</button>
        </div>
      </div>
      <div id="pe-drill-cm-body"></div>
    </div>` : "";

  modal.innerHTML = `
    <div class="pe-drill-backdrop"></div>
    <div class="pe-drill-panel" role="document"${isCustomMetrics ? ' style="max-height:94vh"' : ""}>
      <div class="pe-drill-header" style="border-top: 3px solid ${escapeHtml(acc.ring)}">
        <div class="pe-drill-header-left">
          <div class="pe-drill-header-icon">${peKpiIconSvg(String(kpi.id || ""), acc.ring)}</div>
          <div>
            <h3 class="pe-drill-title">${escapeHtml(label)}</h3>
            ${metric ? `<p class="pe-drill-metric">${escapeHtml(metric)}</p>` : ""}
          </div>
        </div>
        <div class="pe-drill-header-right">
          <div class="pe-drill-header-vals">
            <div class="pe-drill-header-val pe-drill-header-val--base">
              <span class="pe-drill-header-val-label">Baseline</span>
              <span class="pe-drill-header-val-num">${escapeHtml(baseDisp)}</span>
            </div>
            <div class="pe-drill-header-val pe-drill-header-val--cmp">
              <span class="pe-drill-header-val-label">Comparison</span>
              <span class="pe-drill-header-val-num">${escapeHtml(curDisp)}</span>
            </div>
          </div>
          <div class="pe-kpi-card__delta ${deltaClass}" style="margin-top:0.35rem">
            <span aria-hidden="true">${arrow}</span> ${escapeHtml(delta.text)}
          </div>
        </div>
        <button class="pe-drill-close" aria-label="Close drill-down" id="pe-drill-close-btn">✕</button>
      </div>
      <div class="pe-drill-body">
        ${customMetricsSection}
        <details class="pe-drill-timeseries-details"${isCustomMetrics ? "" : " open"}>
          <summary class="pe-drill-timeseries-summary">Trend data — baseline vs comparison window</summary>
          ${comparisonSection}
          ${otherSection}
        </details>
      </div>
    </div>`;

  document.body.appendChild(modal);
  // Animate in
  requestAnimationFrame(() => modal.classList.add("pe-drill-modal--open"));

  const close = () => {
    modal.classList.remove("pe-drill-modal--open");
    modal.addEventListener("transitionend", () => modal.remove(), { once: true });
  };

  document.getElementById("pe-drill-close-btn").addEventListener("click", close);
  modal.querySelector(".pe-drill-backdrop").addEventListener("click", close);
  const onKey = (e) => {
    if (e.key === "Escape") { close(); document.removeEventListener("keydown", onKey); }
  };
  document.addEventListener("keydown", onKey);

  // Wire up custom metrics breakdown load button
  if (isCustomMetrics) {
    const loadBtn = document.getElementById("pe-drill-load-cm");
    const cmBody = document.getElementById("pe-drill-cm-body");
    const lookbackSel = document.getElementById("pe-drill-lookback");
    if (loadBtn && cmBody) {
      const doLoad = () => {
        loadBtn.disabled = true;
        peLoadCustomMetricsBreakdown(cmBody, lookbackSel?.value || "P7D").finally(() => {
          loadBtn.disabled = false;
        });
      };
      loadBtn.addEventListener("click", doLoad);
      // Auto-load immediately when hubJob is present
      if (new URLSearchParams(window.location.search).get("hubJob")) {
        doLoad();
      }
    }
  }
}

const PE_KPI_ACCENTS = [
  { ring: "#7c3aed", stroke: "#8b5cf6", soft: "rgba(124,58,237,0.12)" },
  { ring: "#ea580c", stroke: "#f97316", soft: "rgba(234,88,12,0.12)" },
  { ring: "#ca8a04", stroke: "#eab308", soft: "rgba(202,138,4,0.14)" },
  { ring: "#0d9488", stroke: "#14b8a6", soft: "rgba(13,148,136,0.12)" },
  { ring: "#2563eb", stroke: "#3b82f6", soft: "rgba(37,99,235,0.12)" },
  { ring: "#db2777", stroke: "#ec4899", soft: "rgba(219,39,119,0.12)" },
];

/**
 * Executive-style comparison banner (baseline vs comparison windows). Data from `_viewer_pe_kpi_payload.timeline`.
 */
function buildPlatformEngagementTimelineStrip(tl) {
  if (!tl || typeof tl !== "object" || !tl.comparisonMode) return null;

  const host = document.createElement("div");
  host.className = "pe-compare-banner";
  host.setAttribute("role", "region");

  const mode = tl.comparisonMode;
  const subtitle = tl.rumSyntheticsNote ? String(tl.rumSyntheticsNote) : "";

  if (mode === "month_vs_month" && tl.baseline && tl.comparison) {
    const b = tl.baseline;
    const c = tl.comparison;
    host.innerHTML = `
      <div class="pe-compare-banner__eyebrow">Platform engagement snapshot</div>
      <h3 class="pe-compare-banner__title">Comparison windows <span class="pe-compare-banner__tz">(UTC)</span></h3>
      <div class="pe-compare-banner__strip">
        <div class="pe-compare-slot pe-compare-slot--baseline">
          <div class="pe-compare-slot__label">Baseline</div>
          <div class="pe-compare-slot__month">${escapeHtml(String(b.monthLabel || ""))}</div>
          <div class="pe-compare-slot__range">${escapeHtml(String(b.rangeStart || ""))} → ${escapeHtml(String(b.rangeEnd || ""))}</div>
          <div class="pe-compare-slot__eom"><span class="pe-compare-slot__eom-k">Meter reading (end)</span>
            <time datetime="${escapeHtml(String(b.endUtc || ""))}">${escapeHtml(String(b.endUtc || ""))}</time></div>
        </div>
        <div class="pe-compare-slot__versus" aria-hidden="true"><span></span></div>
        <div class="pe-compare-slot pe-compare-slot--comparison">
          <div class="pe-compare-slot__label">Comparison</div>
          <div class="pe-compare-slot__month">${escapeHtml(String(c.monthLabel || ""))}</div>
          <div class="pe-compare-slot__range">${escapeHtml(String(c.rangeStart || ""))} → ${escapeHtml(String(c.rangeEnd || ""))}</div>
          <div class="pe-compare-slot__eom"><span class="pe-compare-slot__eom-k">Meter reading (end)</span>
            <time datetime="${escapeHtml(String(c.endUtc || ""))}">${escapeHtml(String(c.endUtc || ""))}</time></div>
        </div>
      </div>
      ${subtitle ? `<p class="pe-compare-banner__note">${escapeHtml(subtitle)}</p>` : ""}`;
    return host;
  }

  if (mode === "custom_calendar_range" && tl.baseline && tl.comparison) {
    const b = tl.baseline;
    const c = tl.comparison;
    host.innerHTML = `
      <div class="pe-compare-banner__eyebrow">Platform engagement snapshot</div>
      <h3 class="pe-compare-banner__title">Comparison windows <span class="pe-compare-banner__tz">(UTC)</span></h3>
      <div class="pe-compare-banner__strip">
        <div class="pe-compare-slot pe-compare-slot--baseline">
          <div class="pe-compare-slot__label">Baseline</div>
          <div class="pe-compare-slot__range-strong">${escapeHtml(String(b.rangeStart || ""))} → ${escapeHtml(String(b.rangeEnd || ""))}</div>
          <div class="pe-compare-slot__eom"><span class="pe-compare-slot__eom-k">Window end snapshot</span>
            <time datetime="${escapeHtml(String(b.endUtc || ""))}">${escapeHtml(String(b.endUtc || ""))}</time></div>
        </div>
        <div class="pe-compare-slot__versus" aria-hidden="true"><span></span></div>
        <div class="pe-compare-slot pe-compare-slot--comparison">
          <div class="pe-compare-slot__label">Comparison</div>
          <div class="pe-compare-slot__range-strong">${escapeHtml(String(c.rangeStart || ""))} → ${escapeHtml(String(c.rangeEnd || ""))}</div>
          <div class="pe-compare-slot__eom"><span class="pe-compare-slot__eom-k">Window end snapshot</span>
            <time datetime="${escapeHtml(String(c.endUtc || ""))}">${escapeHtml(String(c.endUtc || ""))}</time></div>
        </div>
      </div>
      ${subtitle ? `<p class="pe-compare-banner__note">${escapeHtml(subtitle)}</p>` : ""}`;
    return host;
  }

  if (mode === "rolling_default" && tl.baseline && tl.comparison) {
    const br = tl.baseline;
    const cr = tl.comparison;
    host.innerHTML = `
      <div class="pe-compare-banner__eyebrow">Platform engagement snapshot</div>
      <h3 class="pe-compare-banner__title">Rolling windows <span class="pe-compare-banner__tz">(UTC)</span></h3>
      <div class="pe-compare-banner__strip">
        <div class="pe-compare-slot pe-compare-slot--baseline">
          <div class="pe-compare-slot__label">Baseline</div>
          <div class="pe-compare-slot__range-strong">${escapeHtml(String(br.rangeStart || ""))} → ${escapeHtml(String(br.rangeEnd || ""))}</div>
        </div>
        <div class="pe-compare-slot__versus" aria-hidden="true"><span></span></div>
        <div class="pe-compare-slot pe-compare-slot--comparison">
          <div class="pe-compare-slot__label">Current</div>
          <div class="pe-compare-slot__range-strong">${escapeHtml(String(cr.rangeStart || ""))} → ${escapeHtml(String(cr.rangeEnd || ""))}</div>
        </div>
      </div>
      ${subtitle ? `<p class="pe-compare-banner__note">${escapeHtml(subtitle)}</p>` : ""}`;
    return host;
  }

  return null;
}

function buildPlatformEngagementKpiDeck(payload) {
  const wrap = document.createElement("div");
  wrap.className = "pe-kpi-deck";
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "Engagement trend KPIs");

  const banner = buildPlatformEngagementTimelineStrip(payload.timeline);
  if (banner) {
    wrap.appendChild(banner);
  }

  const grid = document.createElement("div");
  grid.className = "pe-kpi-grid";

  (payload.kpis || []).forEach((kpi, idx) => {
    const acc = PE_KPI_ACCENTS[idx % PE_KPI_ACCENTS.length];
    const card = document.createElement("article");
    card.className = "pe-kpi-card";
    card.style.setProperty("--pe-ring", acc.ring);
    card.style.setProperty("--pe-stroke", acc.stroke);
    card.style.setProperty("--pe-soft", acc.soft);

    const err = kpi.error ? String(kpi.error) : "";
    const curDisp = err ? "—" : formatPeScalarDisplay(kpi.current);
    const baseDisp = err ? "—" : formatPeScalarDisplay(kpi.baseline);
    const delta = formatPeDeltaDisplay(kpi.pctChange);
    const deltaClass =
      delta.dir === "up" ? "pe-kpi-card__delta--up" : delta.dir === "down" ? "pe-kpi-card__delta--down" : "pe-kpi-card__delta--flat";

    const sparkPts = peKpiSparklinePoints(kpi);
    const gradId = `peSparkFill-${idx}`;
    const spark = peKpiSparklineSvg(sparkPts, acc.stroke, gradId);

    const valuesHtml = peBuildKpiCardValuesHtml(kpi, payload, baseDisp, curDisp);

    const arrow =
      delta.dir === "up"
        ? "▲"
        : delta.dir === "down"
          ? "▼"
          : delta.dir === "flat"
            ? "◆"
            : "—";

    card.setAttribute("tabindex", "0");
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", `Drill down: ${String(kpi.label || kpi.id || "KPI")}`);
    card.classList.add("pe-kpi-card--drillable");
    card.innerHTML = `
      <h4 class="pe-kpi-card__title">${escapeHtml(String(kpi.label || kpi.id || "KPI"))}</h4>
      <div class="pe-kpi-card__icon-ring" aria-hidden="true">
        ${peKpiIconSvg(String(kpi.id || ""), acc.ring)}
      </div>
      <div class="pe-kpi-card__spark-wrap">${spark}</div>
      ${valuesHtml}
      <div class="pe-kpi-card__delta ${deltaClass}" aria-label="Change vs baseline">
        <span class="pe-kpi-card__delta-arrow" aria-hidden="true">${arrow}</span>
        <span class="pe-kpi-card__delta-text">${escapeHtml(err ? "Error" : delta.text)}</span>
      </div>
      ${err ? `<p class="pe-kpi-card__err">${escapeHtml(err.slice(0, 200))}</p>` : ""}
      <span class="pe-kpi-card__drill-hint" aria-hidden="true">View breakdown →</span>
    `;
    card.addEventListener("click", () => peOpenKpiDrilldown(kpi, payload, acc));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); peOpenKpiDrilldown(kpi, payload, acc); }
    });
    grid.appendChild(card);
  });

  wrap.appendChild(grid);
  return wrap;
}

/**
 * Web-only: replace Engagement Trends **Results** KPI table + Mermaid trend blocks with KPI cards.
 * ``### Findings`` may already be stripped by ``removeFindingsSections`` — anchor on Recommendation or User Analysis.
 */
function replaceEngagementTrendsTablesWithKpiDeck(rootEl, payload) {
  if (!payload || !payload.kpis || !payload.kpis.length) return;
  const h3s = [...rootEl.querySelectorAll("h3")];
  const findingsH = h3s.find((h) => h.textContent.trim().toLowerCase() === "findings");
  const recH = h3s.find((h) => h.textContent.trim().toLowerCase() === "recommendation");
  const uaH = h3s.find((h) => h.textContent.trim().toLowerCase() === "user analysis");
  const insertBefore = findingsH || recH || uaH || null;
  const barrierIdx = insertBefore ? h3s.indexOf(insertBefore) : Infinity;
  const resultsH = h3s.find(
    (h, i) => i < barrierIdx && h.textContent.trim().toLowerCase() === "results"
  );
  if (!resultsH) return;

  let n = resultsH.nextElementSibling;
  while (n && n !== insertBefore) {
    const nx = n.nextElementSibling;
    n.remove();
    n = nx;
  }
  resultsH.remove();

  const deck = buildPlatformEngagementKpiDeck(payload);
  if (insertBefore) {
    insertBefore.before(deck);
  } else {
    rootEl.appendChild(deck);
  }

  /** Drop markdown **Engagement Trends** prose/table — the KPI deck carries the executive comparison UI. */
  const engTrendsH = [...rootEl.querySelectorAll("h3")].find(
    (h) => h.textContent.trim().toLowerCase() === "engagement trends"
  );
  if (engTrendsH) {
    let n = engTrendsH.nextSibling;
    while (n) {
      if (n.nodeType === 1 && n.classList && n.classList.contains("pe-kpi-deck")) break;
      const nx = n.nextSibling;
      n.remove();
      n = nx;
    }
    engTrendsH.remove();
  }
}

/**
 * Remove an ``h3`` and all following siblings until the next ``h3`` (same section).
 */
function removeH3BlockUntilNextH3(h3) {
  if (!h3) return;
  const rm = [h3];
  let n = h3.nextSibling;
  while (n) {
    if (n.nodeType === 1 && n.tagName.toUpperCase() === "H3") break;
    rm.push(n);
    n = n.nextSibling;
  }
  rm.forEach((el) => el.remove());
}

/**
 * Platform engagement (viewer): keep intro + KPI deck only — no Results / Recommendation / User Analysis.
 */
function trimPlatformEngagementSectionForViewer(rootEl) {
  const ua = [...rootEl.querySelectorAll("h3")].find(
    (h) => h.textContent.trim().toLowerCase() === "user analysis"
  );
  if (ua) {
    let n = ua;
    while (n) {
      const nx = n.nextSibling;
      n.remove();
      n = nx;
    }
  }
  let again = true;
  while (again) {
    again = false;
    for (const h3 of [...rootEl.querySelectorAll("h3")]) {
      const t = h3.textContent.trim().toLowerCase();
      if (t === "recommendation" || t === "results") {
        removeH3BlockUntilNextH3(h3);
        again = true;
        break;
      }
    }
  }
}

/**
 * Style each health-check block title (`###` in markdown) like the APM checks: left accent bar
 * and subtle background. Skips structural subheads **Results** and **Recommendation** only.
 * Applied to License, IM, Detectors, Dashboards, RUM, Synthetics, Tokens, APM, etc.
 */
function enhanceHealthCheckBlockHeadings(rootEl) {
  const skip = new Set(["results", "recommendation"]);
  rootEl.querySelectorAll("h3").forEach((h3) => {
    const t = h3.textContent.trim().toLowerCase();
    if (skip.has(t)) return;
    h3.classList.add("health-check-block-heading");
  });
}

/**
 * Sort table body rows by the YYYY-MM column (license utilization monthly tables).
 * Latest calendar month first; unparsable labels last.
 */
/**
 * RUM **Volume by Application** Results: keep rows sorted by session count (desc), not by Color.
 * (Otherwise ``sortTablesByColorColumn`` groups Red/Yellow/Green and hides highest-traffic apps.)
 */
function isRumVolumeByApplicationResultsTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const headers = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (headers.length < 4) return false;
  if (headers[0] !== "color") return false;
  if (!headers[1].includes("application")) return false;
  if (!headers[2].includes("session")) return false;
  if (!headers[3].includes("utilization") && !headers[3].includes("license")) return false;
  return true;
}

/**
 * Re-sort RUM Volume by Application table by **Number of Sessions** (highest first).
 */
function sortRumVolumeByApplicationTableBySessionsDesc(rootEl) {
  const sessionsIdx = 2;
  rootEl.querySelectorAll("table").forEach((table) => {
    if (!isRumVolumeByApplicationResultsTable(table)) return;

    const tbody = table.querySelector("tbody");
    let dataRows;
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    } else {
      dataRows = [...table.querySelectorAll("tr")]
        .slice(1)
        .filter((tr) => tr.querySelector("td"));
    }
    if (dataRows.length < 2) return;

    function sessionVal(tr) {
      const cells = tr.querySelectorAll("td");
      const raw = cells[sessionsIdx] ? cells[sessionsIdx].textContent.replace(/,/g, "").trim() : "";
      const n = parseFloat(raw);
      return Number.isFinite(n) ? n : -1;
    }

    dataRows.sort((a, b) => sessionVal(b) - sessionVal(a));

    if (tbody) {
      dataRows.forEach((tr) => tbody.appendChild(tr));
    } else {
      dataRows.forEach((tr) => table.appendChild(tr));
    }
  });
}

/**
 * Sort table body rows by Color column: Red → Yellow → Green (then stable by row text).
 * Applies to any markdown table that has a **Color** header (health-check Results tables).
 */
function sortTablesByColorColumn(rootEl) {
  const rank = { Red: 0, Orange: 1, Yellow: 2, Green: 3 };
  rootEl.querySelectorAll("table").forEach((table) => {
    if (isRumVolumeByApplicationResultsTable(table)) return;
    const headerRow =
      table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
    if (!headerRow) return;
    const headers = [...headerRow.querySelectorAll("th, td")];
    const idx = headers.findIndex((th) => th.textContent.trim().toLowerCase() === "color");
    if (idx < 0) return;

    const tbody = table.querySelector("tbody");
    let dataRows;
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")];
    } else {
      dataRows = [...table.querySelectorAll("tr")].slice(1);
    }
    if (dataRows.length < 1) return;

    dataRows.sort((a, b) => {
      const cellsA = a.querySelectorAll("td, th");
      const cellsB = b.querySelectorAll("td, th");
      const ta = (cellsA[idx] && cellsA[idx].textContent.trim()) || "";
      const tb = (cellsB[idx] && cellsB[idx].textContent.trim()) || "";
      const ra = rank[ta] ?? 99;
      const rb = rank[tb] ?? 99;
      if (ra !== rb) return ra - rb;
      return (a.textContent || "").localeCompare(b.textContent || "");
    });

    if (tbody) {
      dataRows.forEach((tr) => tbody.appendChild(tr));
    } else {
      dataRows.forEach((tr) => table.appendChild(tr));
    }
  });
}

/**
 * **Noisy Detectors** Results: keep rows sorted by trigger count (desc), overriding Color-column sort.
 * (Same header shape as Non-Firing — scope by preceding `### Noisy Detectors` / `### Results`.)
 */
function isNoisyDetectorsResultsTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const headers = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (headers.length < 3) return false;
  if (headers[0] !== "color") return false;
  if (!headers[1].includes("detector name")) return false;
  if (!headers[2].includes("trigger")) return false;
  let el = table.previousElementSibling;
  let hops = 0;
  while (el && hops < 40) {
    hops += 1;
    const tag = el.tagName && el.tagName.toUpperCase();
    if (tag === "H3") {
      const t = el.textContent.trim();
      if (t === "Results") {
        el = el.previousElementSibling;
        continue;
      }
      return t === "Noisy Detectors";
    }
    el = el.previousElementSibling;
  }
  return false;
}

/**
 * Re-sort only the Noisy Detectors Results table by numeric triggers (7d), highest first.
 */
function sortNoisyDetectorsTableByTriggersDesc(rootEl) {
  rootEl.querySelectorAll("table").forEach((table) => {
    if (!isNoisyDetectorsResultsTable(table)) return;

    const tbody = table.querySelector("tbody");
    let dataRows;
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    } else {
      const rows = [...table.querySelectorAll("tr")].slice(1);
      dataRows = rows.filter((tr) => tr.querySelector("td"));
    }
    if (dataRows.length < 2) return;

    function triggerVal(tr) {
      const cells = tr.querySelectorAll("td");
      const raw = cells[2] ? cells[2].textContent.replace(/,/g, "").trim() : "";
      const n = parseFloat(raw);
      return Number.isFinite(n) ? n : 0;
    }

    dataRows.sort((a, b) => triggerVal(b) - triggerVal(a));

    if (tbody) {
      dataRows.forEach((tr) => tbody.appendChild(tr));
    } else {
      dataRows.forEach((tr) => table.appendChild(tr));
    }
  });
}

/**
 * **Test Usage Analysis** (Synthetics): no Color column; Test Name | Test Type | … | Total Runs per Month | …
 */
function isSyntheticsTestUsageResultsTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const headers = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (headers.length < 7) return false;
  if (headers[0] === "color") return false;
  if (!headers[0].includes("test name")) return false;
  if (!headers[1].includes("test type")) return false;
  if (!headers[2].includes("frequency")) return false;
  if (!headers[5].includes("total") || !headers[5].includes("run")) return false;
  if (!headers[6].includes("utilization") || !headers[6].includes("license")) return false;
  return true;
}

/**
 * Sort **Test Usage Analysis** by Test Type (A→Z), then Total Runs per Month (high→low).
 */
function sortSyntheticsTestUsageTableByTypeAndRunsDesc(rootEl) {
  const typeIdx = 1;
  const runsIdx = 5;
  rootEl.querySelectorAll("table").forEach((table) => {
    if (!isSyntheticsTestUsageResultsTable(table)) return;

    const tbody = table.querySelector("tbody");
    let dataRows;
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    } else {
      dataRows = [...table.querySelectorAll("tr")]
        .slice(1)
        .filter((tr) => tr.querySelector("td"));
    }
    if (dataRows.length < 2) return;

    function typeVal(tr) {
      const cells = tr.querySelectorAll("td");
      return (cells[typeIdx] && cells[typeIdx].textContent.trim().toLowerCase()) || "";
    }

    function runsVal(tr) {
      const cells = tr.querySelectorAll("td");
      const raw = cells[runsIdx] ? cells[runsIdx].textContent.replace(/,/g, "").trim() : "";
      const n = parseFloat(raw);
      return Number.isFinite(n) ? n : -1;
    }

    dataRows.sort((a, b) => {
      const c = typeVal(a).localeCompare(typeVal(b));
      if (c !== 0) return c;
      return runsVal(b) - runsVal(a);
    });

    if (tbody) {
      dataRows.forEach((tr) => tbody.appendChild(tr));
    } else {
      dataRows.forEach((tr) => table.appendChild(tr));
    }
  });
}

/**
 * **Failing Tests** (Synthetics) Results: Color | Test Name | … | Failure Rate % …
 */
function isSyntheticsFailingTestsResultsTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const headers = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (headers.length < 5) return false;
  if (headers[0] !== "color") return false;
  if (!headers[1].includes("test name")) return false;
  if (!headers[headers.length - 1].includes("failure rate")) return false;
  let el = table.previousElementSibling;
  let hops = 0;
  while (el && hops < 40) {
    hops += 1;
    const tag = el.tagName && el.tagName.toUpperCase();
    if (tag === "H3") {
      const t = el.textContent.trim();
      if (t === "Results") {
        el = el.previousElementSibling;
        continue;
      }
      return t === "Failing Tests";
    }
    el = el.previousElementSibling;
  }
  return false;
}

/**
 * Re-sort **Failing Tests** Results by numeric failure rate (desc), then test name.
 * Overrides Color-column sort (all rows are typically Red).
 */
function sortSyntheticsFailingTestsTableByFailureRateDesc(rootEl) {
  rootEl.querySelectorAll("table").forEach((table) => {
    if (!isSyntheticsFailingTestsResultsTable(table)) return;

    const headerRow =
      table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
    if (!headerRow) return;
    const headers = [...headerRow.querySelectorAll("th, td")];
    const rateIdx = headers.findIndex((th) => /failure\s*rate/i.test(th.textContent.trim()));
    if (rateIdx < 0) return;

    const tbody = table.querySelector("tbody");
    let dataRows;
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    } else {
      const rows = [...table.querySelectorAll("tr")].slice(1);
      dataRows = rows.filter((tr) => tr.querySelector("td"));
    }
    if (dataRows.length < 2) return;

    function rateVal(tr) {
      const cells = tr.querySelectorAll("td");
      const td = cells[rateIdx];
      if (!td) return 0;
      const raw = td.textContent.replace(/,/g, "").replace(/%/g, "").trim();
      const n = parseFloat(raw);
      return Number.isFinite(n) ? n : 0;
    }

    dataRows.sort((a, b) => {
      const dr = rateVal(b) - rateVal(a);
      if (dr !== 0) return dr;
      return (a.textContent || "").localeCompare(b.textContent || "");
    });

    if (tbody) {
      dataRows.forEach((tr) => tbody.appendChild(tr));
    } else {
      dataRows.forEach((tr) => table.appendChild(tr));
    }
  });
}

/**
 * **Disabled Tests** (Synthetics) Results: Color | Test Name | Test Type | Last Run Date |
 */
function isSyntheticsDisabledTestsResultsTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const headers = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (headers.length < 4) return false;
  if (headers[0] !== "color") return false;
  if (!headers[1].includes("test name")) return false;
  if (!headers[3].includes("last run")) return false;
  let el = table.previousElementSibling;
  let hops = 0;
  while (el && hops < 40) {
    hops += 1;
    const tag = el.tagName && el.tagName.toUpperCase();
    if (tag === "H3") {
      const t = el.textContent.trim();
      if (t === "Results") {
        el = el.previousElementSibling;
        continue;
      }
      return t === "Disabled Tests";
    }
    el = el.previousElementSibling;
  }
  return false;
}

function parseSyntheticsLastRunDateMs(cellText) {
  const t = (cellText || "").trim();
  if (!t || t === "—" || t === "-") return Number.MAX_SAFE_INTEGER;
  const m = t.match(/^(\d{4}-\d{2}-\d{2})/);
  if (m) {
    const ms = Date.parse(`${m[1]}T00:00:00.000Z`);
    if (Number.isFinite(ms)) return ms;
  }
  const ms2 = Date.parse(t);
  return Number.isFinite(ms2) ? ms2 : Number.MAX_SAFE_INTEGER;
}

/**
 * Re-sort **Disabled Tests** by last run date ascending (oldest first), then row text.
 */
function sortSyntheticsDisabledTestsTableByLastRunAsc(rootEl) {
  rootEl.querySelectorAll("table").forEach((table) => {
    if (!isSyntheticsDisabledTestsResultsTable(table)) return;

    const headerRow =
      table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
    if (!headerRow) return;
    const headers = [...headerRow.querySelectorAll("th, td")];
    const dateIdx = headers.findIndex((th) => /last\s*run/i.test(th.textContent.trim()));
    if (dateIdx < 0) return;

    const tbody = table.querySelector("tbody");
    let dataRows;
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    } else {
      const rows = [...table.querySelectorAll("tr")].slice(1);
      dataRows = rows.filter((tr) => tr.querySelector("td"));
    }
    if (dataRows.length < 2) return;

    function lastRunMs(tr) {
      const cells = tr.querySelectorAll("td");
      const td = cells[dateIdx];
      return parseSyntheticsLastRunDateMs(td ? td.textContent : "");
    }

    dataRows.sort((a, b) => {
      const d = lastRunMs(a) - lastRunMs(b);
      if (d !== 0) return d;
      return (a.textContent || "").localeCompare(b.textContent || "");
    });

    if (tbody) {
      dataRows.forEach((tr) => tbody.appendChild(tr));
    } else {
      dataRows.forEach((tr) => table.appendChild(tr));
    }
  });
}

/**
 * Usage analytics **Metric Cardinality & Volume** table (IM health checks) — unique header fingerprint.
 */
function isMetricCardinalityVolumeTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const cells = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (cells.length < 5) return false;
  return (
    cells[0].includes("metric name") &&
    cells[1].includes("billing class") &&
    cells[2].includes("cardinality") &&
    cells[2].includes("mts") &&
    cells[3].includes("utilization") &&
    (cells[4].includes("% over total") || cells[4].includes("over total"))
  );
}

/**
 * IM **Analyze Integrations** Results: Integration Type | Integration Name | Active (T/F)
 */
function isAnalyzeIntegrationsTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const cells = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (cells.length !== 3) return false;
  return (
    cells[0].includes("integration type") &&
    cells[1].includes("integration name") &&
    cells[2].includes("active")
  );
}

/**
 * Detectors health check **Results** tables: Color | Detector Name | … (excludes IM cardinality table).
 */
function isDetectorHealthResultsTable(table) {
  if (isMetricCardinalityVolumeTable(table)) return false;
  if (isAnalyzeIntegrationsTable(table)) return false;
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const cells = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (cells.length < 3) return false;
  return cells[0] === "color" && cells[1].includes("detector name");
}

/**
 * Dashboards health check **Results** tables: Color | Dashboard Group | …
 */
function isDashboardHealthResultsTable(table) {
  if (isMetricCardinalityVolumeTable(table)) return false;
  if (isAnalyzeIntegrationsTable(table)) return false;
  if (isDetectorHealthResultsTable(table)) return false;
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const cells = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (cells.length < 4) return false;
  return cells[0] === "color" && cells[1].includes("dashboard group");
}

/**
 * Token health **Expired Tokens** / **Near Expiration Tokens**:
 * Token Name | Token Type | Expired Date | or | Expiration Date | (no Color column).
 */
function isTokenHealthExpiredOrNearTable(table) {
  if (isMetricCardinalityVolumeTable(table)) return false;
  if (isAnalyzeIntegrationsTable(table)) return false;
  if (isDetectorHealthResultsTable(table)) return false;
  if (isDashboardHealthResultsTable(table)) return false;
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const cells = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (cells.length !== 3) return false;
  if (!cells[0].includes("token name")) return false;
  if (!cells[1].includes("token type")) return false;
  const dateCol = cells[2];
  return dateCol.includes("expired date") || dateCol.includes("expiration date");
}

/**
 * Keep refs on paginated table wraps so print / headless PDF can render all rows at once.
 * @param {HTMLElement} wrap
 * @param {HTMLTableSectionElement} tbody
 * @param {HTMLTableRowElement[]} allRows
 * @param {() => void} renderPage
 */
function bindPaginatedTableForPrint(wrap, tbody, allRows, renderPage) {
  wrap.__o11yPaginationTbody = tbody;
  wrap.__o11yPaginationAllRows = allRows;
  wrap.__o11yPaginationRenderPage = renderPage;
}

function expandPaginatedTablesForPrintExport() {
  document.querySelectorAll(".table-pagination-wrap").forEach((wrap) => {
    const tbody = wrap.__o11yPaginationTbody;
    const allRows = wrap.__o11yPaginationAllRows;
    if (!tbody || !allRows || !allRows.length) return;
    tbody.innerHTML = "";
    allRows.forEach((tr) => tbody.appendChild(tr));
  });
}

function restorePaginatedTablesAfterPrint() {
  document.querySelectorAll(".table-pagination-wrap").forEach((wrap) => {
    const fn = wrap.__o11yPaginationRenderPage;
    if (typeof fn === "function") fn();
  });
}

/**
 * Paginate the IM metrics usage table only (large row counts). Runs after other table enhancements.
 */
function paginateMetricCardinalityTables(rootEl) {
  const DEFAULT_PAGE_SIZE = 10;
  const PAGE_SIZE_CHOICES = [10, 25, 50, 100, 200, 500];

  rootEl.querySelectorAll("table").forEach((table) => {
    if (!isMetricCardinalityVolumeTable(table)) return;
    if (table.closest(".table-pagination-wrap")) return;

    const tbody = table.querySelector("tbody");
    if (!tbody) return;

    const dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    if (dataRows.length <= DEFAULT_PAGE_SIZE) return;

    const wrap = document.createElement("div");
    wrap.className = "table-pagination-wrap";
    const toolbar = document.createElement("div");
    toolbar.className = "table-pagination-toolbar";

    const btnFirst = document.createElement("button");
    btnFirst.type = "button";
    btnFirst.className = "table-pagination-btn";
    btnFirst.textContent = "« First";
    const btnPrev = document.createElement("button");
    btnPrev.type = "button";
    btnPrev.className = "table-pagination-btn";
    btnPrev.textContent = "‹ Prev";
    const btnNext = document.createElement("button");
    btnNext.type = "button";
    btnNext.className = "table-pagination-btn";
    btnNext.textContent = "Next ›";
    const btnLast = document.createElement("button");
    btnLast.type = "button";
    btnLast.className = "table-pagination-btn";
    btnLast.textContent = "Last »";

    const status = document.createElement("span");
    status.className = "table-pagination-status";

    const labelPer = document.createElement("label");
    labelPer.className = "table-pagination-per";
    labelPer.textContent = "Rows per page";
    const select = document.createElement("select");
    select.className = "table-pagination-select";
    PAGE_SIZE_CHOICES.forEach((n) => {
      const opt = document.createElement("option");
      opt.value = String(n);
      opt.textContent = String(n);
      if (n === DEFAULT_PAGE_SIZE) opt.selected = true;
      select.appendChild(opt);
    });
    labelPer.appendChild(select);

    toolbar.appendChild(btnFirst);
    toolbar.appendChild(btnPrev);
    toolbar.appendChild(status);
    toolbar.appendChild(btnNext);
    toolbar.appendChild(btnLast);
    toolbar.appendChild(labelPer);

    const parent = table.parentNode;
    parent.insertBefore(wrap, table);
    wrap.appendChild(toolbar);
    wrap.appendChild(table);

    let pageSize = DEFAULT_PAGE_SIZE;
    let page = 0;
    const allRows = dataRows;

    function totalPages() {
      return Math.max(1, Math.ceil(allRows.length / pageSize));
    }

    function renderPage() {
      const pages = totalPages();
      if (page >= pages) page = pages - 1;
      if (page < 0) page = 0;

      tbody.innerHTML = "";
      const start = page * pageSize;
      const end = Math.min(start + pageSize, allRows.length);
      for (let i = start; i < end; i += 1) {
        tbody.appendChild(allRows[i]);
      }

      const from = allRows.length === 0 ? 0 : start + 1;
      const to = end;
      status.textContent = `Showing ${from}–${to} of ${allRows.length} metrics · Page ${page + 1} of ${pages}`;

      const atFirst = page <= 0;
      const atLast = page >= pages - 1;
      btnFirst.disabled = atFirst;
      btnPrev.disabled = atFirst;
      btnNext.disabled = atLast;
      btnLast.disabled = atLast;
    }

    btnFirst.addEventListener("click", () => {
      page = 0;
      renderPage();
    });
    btnPrev.addEventListener("click", () => {
      page -= 1;
      renderPage();
    });
    btnNext.addEventListener("click", () => {
      page += 1;
      renderPage();
    });
    btnLast.addEventListener("click", () => {
      page = totalPages() - 1;
      renderPage();
    });

    select.addEventListener("change", () => {
      pageSize = parseInt(select.value, 10) || DEFAULT_PAGE_SIZE;
      page = 0;
      renderPage();
    });

    bindPaginatedTableForPrint(wrap, tbody, allRows, renderPage);
    allRows.forEach((tr) => tr.remove());
    renderPage();
  });
}

/**
 * Paginate **Analyze Integrations** Results tables (Integration Type | Integration Name | Active).
 */
function paginateAnalyzeIntegrationsTables(rootEl) {
  const DEFAULT_PAGE_SIZE = 10;
  const PAGE_SIZE_CHOICES = [10, 25, 50, 100, 200, 500];

  rootEl.querySelectorAll("table").forEach((table) => {
    if (!isAnalyzeIntegrationsTable(table)) return;
    if (table.closest(".table-pagination-wrap")) return;

    let tbody = table.querySelector("tbody");
    let dataRows = [];
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    } else {
      const rows = [...table.rows];
      if (rows.length < 2) return;
      dataRows = rows.slice(1).filter((tr) => tr.querySelector("td"));
      if (!dataRows.length) return;
      tbody = document.createElement("tbody");
      dataRows.forEach((tr) => tbody.appendChild(tr));
      table.appendChild(tbody);
    }

    if (dataRows.length <= DEFAULT_PAGE_SIZE) return;

    const wrap = document.createElement("div");
    wrap.className = "table-pagination-wrap";
    const toolbar = document.createElement("div");
    toolbar.className = "table-pagination-toolbar";

    const btnFirst = document.createElement("button");
    btnFirst.type = "button";
    btnFirst.className = "table-pagination-btn";
    btnFirst.textContent = "« First";
    const btnPrev = document.createElement("button");
    btnPrev.type = "button";
    btnPrev.className = "table-pagination-btn";
    btnPrev.textContent = "‹ Prev";
    const btnNext = document.createElement("button");
    btnNext.type = "button";
    btnNext.className = "table-pagination-btn";
    btnNext.textContent = "Next ›";
    const btnLast = document.createElement("button");
    btnLast.type = "button";
    btnLast.className = "table-pagination-btn";
    btnLast.textContent = "Last »";

    const status = document.createElement("span");
    status.className = "table-pagination-status";

    const labelPer = document.createElement("label");
    labelPer.className = "table-pagination-per";
    labelPer.textContent = "Rows per page";
    const select = document.createElement("select");
    select.className = "table-pagination-select";
    PAGE_SIZE_CHOICES.forEach((n) => {
      const opt = document.createElement("option");
      opt.value = String(n);
      opt.textContent = String(n);
      if (n === DEFAULT_PAGE_SIZE) opt.selected = true;
      select.appendChild(opt);
    });
    labelPer.appendChild(select);

    toolbar.appendChild(btnFirst);
    toolbar.appendChild(btnPrev);
    toolbar.appendChild(status);
    toolbar.appendChild(btnNext);
    toolbar.appendChild(btnLast);
    toolbar.appendChild(labelPer);

    const parent = table.parentNode;
    parent.insertBefore(wrap, table);
    wrap.appendChild(toolbar);
    wrap.appendChild(table);

    let pageSize = DEFAULT_PAGE_SIZE;
    let page = 0;
    const allRows = dataRows;

    function totalPages() {
      return Math.max(1, Math.ceil(allRows.length / pageSize));
    }

    function renderPage() {
      const pages = totalPages();
      if (page >= pages) page = pages - 1;
      if (page < 0) page = 0;

      tbody.innerHTML = "";
      const start = page * pageSize;
      const end = Math.min(start + pageSize, allRows.length);
      for (let i = start; i < end; i += 1) {
        tbody.appendChild(allRows[i]);
      }

      const from = allRows.length === 0 ? 0 : start + 1;
      const to = end;
      status.textContent = `Showing ${from}–${to} of ${allRows.length} integrations · Page ${page + 1} of ${pages}`;

      const atFirst = page <= 0;
      const atLast = page >= pages - 1;
      btnFirst.disabled = atFirst;
      btnPrev.disabled = atFirst;
      btnNext.disabled = atLast;
      btnLast.disabled = atLast;
    }

    btnFirst.addEventListener("click", () => {
      page = 0;
      renderPage();
    });
    btnPrev.addEventListener("click", () => {
      page -= 1;
      renderPage();
    });
    btnNext.addEventListener("click", () => {
      page += 1;
      renderPage();
    });
    btnLast.addEventListener("click", () => {
      page = totalPages() - 1;
      renderPage();
    });

    select.addEventListener("change", () => {
      pageSize = parseInt(select.value, 10) || DEFAULT_PAGE_SIZE;
      page = 0;
      renderPage();
    });

    bindPaginatedTableForPrint(wrap, tbody, allRows, renderPage);
    allRows.forEach((tr) => tr.remove());
    renderPage();
  });
}

/**
 * Paginate health-check tables with **Color** as the first column (detectors, dashboards, …).
 * @param {string} rowKindLabel — e.g. "detectors" or "rows" (status line)
 */
function paginateColorColumnTables(rootEl, isTargetTable, rowKindLabel) {
  const DEFAULT_PAGE_SIZE = 10;
  const PAGE_SIZE_CHOICES = [10, 25, 50, 100, 200, 500];

  rootEl.querySelectorAll("table").forEach((table) => {
    if (!isTargetTable(table)) return;
    if (table.closest(".table-pagination-wrap")) return;

    let tbody = table.querySelector("tbody");
    let dataRows = [];
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    } else {
      const rows = [...table.rows];
      if (rows.length < 2) return;
      dataRows = rows.slice(1).filter((tr) => tr.querySelector("td"));
      if (!dataRows.length) return;
      tbody = document.createElement("tbody");
      dataRows.forEach((tr) => tbody.appendChild(tr));
      table.appendChild(tbody);
    }

    if (dataRows.length <= DEFAULT_PAGE_SIZE) return;

    const allRows = dataRows;

    const wrap = document.createElement("div");
    wrap.className = "table-pagination-wrap";
    const toolbar = document.createElement("div");
    toolbar.className = "table-pagination-toolbar";

    const btnFirst = document.createElement("button");
    btnFirst.type = "button";
    btnFirst.className = "table-pagination-btn";
    btnFirst.textContent = "« First";
    const btnPrev = document.createElement("button");
    btnPrev.type = "button";
    btnPrev.className = "table-pagination-btn";
    btnPrev.textContent = "‹ Prev";
    const btnNext = document.createElement("button");
    btnNext.type = "button";
    btnNext.className = "table-pagination-btn";
    btnNext.textContent = "Next ›";
    const btnLast = document.createElement("button");
    btnLast.type = "button";
    btnLast.className = "table-pagination-btn";
    btnLast.textContent = "Last »";

    const status = document.createElement("span");
    status.className = "table-pagination-status";

    const labelPer = document.createElement("label");
    labelPer.className = "table-pagination-per";
    labelPer.textContent = "Rows per page";
    const select = document.createElement("select");
    select.className = "table-pagination-select";
    PAGE_SIZE_CHOICES.forEach((n) => {
      const opt = document.createElement("option");
      opt.value = String(n);
      opt.textContent = String(n);
      if (n === DEFAULT_PAGE_SIZE) opt.selected = true;
      select.appendChild(opt);
    });
    labelPer.appendChild(select);

    toolbar.appendChild(btnFirst);
    toolbar.appendChild(btnPrev);
    toolbar.appendChild(status);
    toolbar.appendChild(btnNext);
    toolbar.appendChild(btnLast);
    toolbar.appendChild(labelPer);

    const parent = table.parentNode;
    parent.insertBefore(wrap, table);
    wrap.appendChild(toolbar);
    wrap.appendChild(table);

    let pageSize = DEFAULT_PAGE_SIZE;
    let page = 0;

    function totalPages() {
      return Math.max(1, Math.ceil(allRows.length / pageSize));
    }

    function renderPage() {
      const pages = totalPages();
      if (page >= pages) page = pages - 1;
      if (page < 0) page = 0;

      tbody.innerHTML = "";
      const start = page * pageSize;
      const end = Math.min(start + pageSize, allRows.length);
      for (let i = start; i < end; i += 1) {
        tbody.appendChild(allRows[i]);
      }

      const from = allRows.length === 0 ? 0 : start + 1;
      const to = end;
      status.textContent = `Showing ${from}–${to} of ${allRows.length} ${rowKindLabel} · Page ${page + 1} of ${pages}`;

      const atFirst = page <= 0;
      const atLast = page >= pages - 1;
      btnFirst.disabled = atFirst;
      btnPrev.disabled = atFirst;
      btnNext.disabled = atLast;
      btnLast.disabled = atLast;
    }

    btnFirst.addEventListener("click", () => {
      page = 0;
      renderPage();
    });
    btnPrev.addEventListener("click", () => {
      page -= 1;
      renderPage();
    });
    btnNext.addEventListener("click", () => {
      page += 1;
      renderPage();
    });
    btnLast.addEventListener("click", () => {
      page = totalPages() - 1;
      renderPage();
    });

    select.addEventListener("change", () => {
      pageSize = parseInt(select.value, 10) || DEFAULT_PAGE_SIZE;
      page = 0;
      renderPage();
    });

    bindPaginatedTableForPrint(wrap, tbody, allRows, renderPage);
    allRows.forEach((tr) => tr.remove());
    renderPage();
  });
}

function paginateDetectorHealthTables(rootEl) {
  paginateColorColumnTables(rootEl, isDetectorHealthResultsTable, "detectors");
}

function paginateDashboardHealthTables(rootEl) {
  paginateColorColumnTables(rootEl, isDashboardHealthResultsTable, "rows");
}

function paginateTokenHealthExpiredNearTables(rootEl) {
  paginateColorColumnTables(rootEl, isTokenHealthExpiredOrNearTable, "tokens");
}

/**
 * Document / intro **Field | Value** metadata table (not a health-check Results grid).
 */
function isPreambleFieldValueTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const cells = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  return cells.length >= 2 && cells[0] === "field" && cells[1].includes("value");
}

/**
 * **License utilization** monthly entitlement tables (exclude from generic pagination per product request).
 */
function isLicenseCapacityMonthlyResultsTable(table) {
  const headerRow =
    table.querySelector("thead tr") || (table.rows && table.rows[0]) || null;
  if (!headerRow) return false;
  const cells = [...headerRow.querySelectorAll("th, td")].map((c) =>
    c.textContent.trim().toLowerCase()
  );
  if (cells.length < 5) return false;
  if (cells[0] !== "color") return false;
  if (cells[1] !== "yyyy-mm") return false;
  if (!cells[2].includes("subscription")) return false;
  const u0 = cells[3].includes("utilization");
  const u1 = cells[4].includes("utilization");
  return u0 && (u1 || cells[4].includes("%"));
}

/**
 * Paginate any remaining markdown **Results** tables (default **10** per page) after
 * specialized paginators. Skips license-capacity-shaped tables and preamble meta tables.
 */
function paginateRemainingHealthResultsTables(rootEl) {
  const DEFAULT_PAGE_SIZE = 10;
  const PAGE_SIZE_CHOICES = [10, 25, 50, 100, 200, 500];

  rootEl.querySelectorAll("table").forEach((table) => {
    if (table.closest(".table-pagination-wrap")) return;
    if (isPreambleFieldValueTable(table)) return;
    if (isLicenseCapacityMonthlyResultsTable(table)) return;

    let tbody = table.querySelector("tbody");
    let dataRows = [];
    if (tbody) {
      dataRows = [...tbody.querySelectorAll("tr")].filter((tr) => tr.querySelector("td"));
    } else {
      const rows = [...table.rows];
      if (rows.length < 2) return;
      dataRows = rows.slice(1).filter((tr) => tr.querySelector("td"));
      if (!dataRows.length) return;
      tbody = document.createElement("tbody");
      dataRows.forEach((tr) => tbody.appendChild(tr));
      table.appendChild(tbody);
    }

    if (dataRows.length <= DEFAULT_PAGE_SIZE) return;

    const wrap = document.createElement("div");
    wrap.className = "table-pagination-wrap";
    const toolbar = document.createElement("div");
    toolbar.className = "table-pagination-toolbar";

    const btnFirst = document.createElement("button");
    btnFirst.type = "button";
    btnFirst.className = "table-pagination-btn";
    btnFirst.textContent = "« First";
    const btnPrev = document.createElement("button");
    btnPrev.type = "button";
    btnPrev.className = "table-pagination-btn";
    btnPrev.textContent = "‹ Prev";
    const btnNext = document.createElement("button");
    btnNext.type = "button";
    btnNext.className = "table-pagination-btn";
    btnNext.textContent = "Next ›";
    const btnLast = document.createElement("button");
    btnLast.type = "button";
    btnLast.className = "table-pagination-btn";
    btnLast.textContent = "Last »";

    const status = document.createElement("span");
    status.className = "table-pagination-status";

    const labelPer = document.createElement("label");
    labelPer.className = "table-pagination-per";
    labelPer.textContent = "Rows per page";
    const select = document.createElement("select");
    select.className = "table-pagination-select";
    PAGE_SIZE_CHOICES.forEach((n) => {
      const opt = document.createElement("option");
      opt.value = String(n);
      opt.textContent = String(n);
      if (n === DEFAULT_PAGE_SIZE) opt.selected = true;
      select.appendChild(opt);
    });
    labelPer.appendChild(select);

    toolbar.appendChild(btnFirst);
    toolbar.appendChild(btnPrev);
    toolbar.appendChild(status);
    toolbar.appendChild(btnNext);
    toolbar.appendChild(btnLast);
    toolbar.appendChild(labelPer);

    const parent = table.parentNode;
    parent.insertBefore(wrap, table);
    wrap.appendChild(toolbar);
    wrap.appendChild(table);

    let pageSize = DEFAULT_PAGE_SIZE;
    let page = 0;
    const allRows = dataRows;

    function totalPages() {
      return Math.max(1, Math.ceil(allRows.length / pageSize));
    }

    function renderPage() {
      const pages = totalPages();
      if (page >= pages) page = pages - 1;
      if (page < 0) page = 0;

      tbody.innerHTML = "";
      const start = page * pageSize;
      const end = Math.min(start + pageSize, allRows.length);
      for (let i = start; i < end; i += 1) {
        tbody.appendChild(allRows[i]);
      }

      const from = allRows.length === 0 ? 0 : start + 1;
      const to = end;
      status.textContent = `Showing ${from}–${to} of ${allRows.length} rows · Page ${page + 1} of ${pages}`;

      const atFirst = page <= 0;
      const atLast = page >= pages - 1;
      btnFirst.disabled = atFirst;
      btnPrev.disabled = atFirst;
      btnNext.disabled = atLast;
      btnLast.disabled = atLast;
    }

    btnFirst.addEventListener("click", () => {
      page = 0;
      renderPage();
    });
    btnPrev.addEventListener("click", () => {
      page -= 1;
      renderPage();
    });
    btnNext.addEventListener("click", () => {
      page += 1;
      renderPage();
    });
    btnLast.addEventListener("click", () => {
      page = totalPages() - 1;
      renderPage();
    });

    select.addEventListener("change", () => {
      pageSize = parseInt(select.value, 10) || DEFAULT_PAGE_SIZE;
      page = 0;
      renderPage();
    });

    bindPaginatedTableForPrint(wrap, tbody, allRows, renderPage);
    allRows.forEach((tr) => tr.remove());
    renderPage();
  });
}

function sortMonthlyTablesChronologically(rootEl) {
  function monthTuple(text) {
    const s = (text || "").trim();
    const m = s.match(/^(\d{4})-(\d{1,2})/);
    if (!m) return [-1, -1];
    return [parseInt(m[1], 10), parseInt(m[2], 10)];
  }

  rootEl.querySelectorAll("table").forEach((table) => {
    const headerRow =
      table.querySelector("thead tr") || (table.tHead && table.tHead.rows[0]) || null;
    if (!headerRow) return;
    const headers = [...headerRow.querySelectorAll("th, td")];
    const idx = headers.findIndex((th) => th.textContent.trim().toUpperCase() === "YYYY-MM");
    if (idx < 0) return;
    const tbody = table.querySelector("tbody");
    if (!tbody) return;
    const rows = [...tbody.querySelectorAll("tr")];
    rows.sort((a, b) => {
      const ca = a.querySelectorAll("td")[idx];
      const cb = b.querySelectorAll("td")[idx];
      const ta = monthTuple(ca ? ca.textContent : "");
      const tb = monthTuple(cb ? cb.textContent : "");
      if (ta[0] === -1 && tb[0] === -1) return 0;
      if (ta[0] === -1) return 1;
      if (tb[0] === -1) return -1;
      if (tb[0] !== ta[0]) return tb[0] - ta[0];
      return tb[1] - ta[1];
    });
    rows.forEach((tr) => tbody.appendChild(tr));
  });
}

async function buildUI() {
  const loadPanel = document.getElementById("load-panel");
  const appContent = document.getElementById("app-content");
  const headerTitle = document.getElementById("header-doc-title");
  const heroTitle = document.getElementById("hero-title");
  const heroSubtitle = document.getElementById("hero-subtitle");
  const summaryGrid = document.getElementById("summary-grid");
  const navList = document.getElementById("nav-sections");
  const sectionsContainer = document.getElementById("sections-container");

  if (!STATE.raw) {
    if (loadPanel) loadPanel.classList.remove("load-panel--hidden");
    if (appContent) appContent.style.display = "none";
    return;
  }

  if (loadPanel) loadPanel.classList.add("load-panel--hidden");
  if (appContent) appContent.style.display = "";

  const title = extractTitle(STATE.raw);
  const meta = parseMetaTable(STATE.raw);
  const { preamble, sections } = splitSections(STATE.raw);

  STATE.title = title;
  STATE.meta = meta;
  const visibleSections = sections.filter((sec) => !isSectionHiddenInViewer(sec.title));
  STATE.sections = visibleSections;

  document.title = `${title} · Observability Health`;

  headerTitle.textContent = title;
  heroTitle.textContent = title;
  heroSubtitle.textContent =
    meta["Assessment date (UTC)"]
      ? `Assessment snapshot · ${formatAssessmentDateUtc(meta["Assessment date (UTC)"])}`
      : "Consolidated health assessment";

  summaryGrid.innerHTML = "";
  const keys = ["Customer / purpose", "Assessment date (UTC)", "Realm", "Scope"];
  for (const k of keys) {
    if (!meta[k]) continue;
    const card = document.createElement("div");
    card.className = "summary-card";
    if (k === "Scope") card.classList.add("summary-card--scope");

    const labelEl = document.createElement("div");
    labelEl.className = "summary-card__label";
    labelEl.textContent = summaryMetaCardLabel(k);

    const valueEl = document.createElement("div");
    valueEl.className =
      k === "Scope" ? "summary-card__value summary-card__value--scope" : "summary-card__value";
    if (k === "Scope") {
      valueEl.innerHTML = buildScopeChipsHtml(meta[k]);
    } else if (k === "Assessment date (UTC)") {
      valueEl.textContent = formatAssessmentDateUtc(meta[k]);
    } else {
      valueEl.textContent = meta[k];
    }

    card.appendChild(labelEl);
    card.appendChild(valueEl);
    summaryGrid.appendChild(card);
  }

  navList.innerHTML = "";
  sectionsContainer.innerHTML = "";

  visibleSections.forEach((sec, idx) => {
    const id = `sec-${slugify(sec.title)}-${idx}`;
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `#${id}`;
    a.textContent = sec.title;
    a.dataset.target = id;
    li.appendChild(a);
    navList.appendChild(li);

    const article = document.createElement("article");
    article.className = "section-card";
    article.id = id;
    article.innerHTML = `
      <div class="section-card__header">
        <h2 class="section-card__title">${escapeHtml(sec.title)}</h2>
      </div>
      <div class="section-card__body">
        <div class="markdown-body"></div>
      </div>`;
    const bodyEl = article.querySelector(".markdown-body");
    let sectionMd = sec.body;
    let peKpiPayload = null;
    if (sec.title.trim().toLowerCase() === "platform engagement") {
      peKpiPayload = extractPlatformEngagementKpiPayload(sectionMd);
      sectionMd = stripPlatformEngagementKpiPayload(sectionMd);
    }
    bodyEl.innerHTML = renderMarkdown(sectionMd);
    enhanceHealthCheckBlockHeadings(bodyEl);
    sortTablesByColorColumn(bodyEl);
    sortRumVolumeByApplicationTableBySessionsDesc(bodyEl);
    sortNoisyDetectorsTableByTriggersDesc(bodyEl);
    enhanceTables(bodyEl);
    enhancePercentFormatting(bodyEl);
    sortSyntheticsTestUsageTableByTypeAndRunsDesc(bodyEl);
    sortSyntheticsFailingTestsTableByFailureRateDesc(bodyEl);
    sortSyntheticsDisabledTestsTableByLastRunAsc(bodyEl);
    sortMonthlyTablesChronologically(bodyEl);
    paginateMetricCardinalityTables(bodyEl);
    paginateAnalyzeIntegrationsTables(bodyEl);
    paginateDetectorHealthTables(bodyEl);
    paginateDashboardHealthTables(bodyEl);
    paginateTokenHealthExpiredNearTables(bodyEl);
    if (!isLicenseUtilizationSectionTitle(sec.title)) {
      paginateRemainingHealthResultsTables(bodyEl);
    }
    removeFindingsSections(bodyEl);
    if (isLicenseUtilizationSectionTitle(sec.title)) {
      removeRecommendationSectionsLicenseCapacity(bodyEl);
    }
    if (
      sec.title.trim().toLowerCase() === "platform engagement" &&
      peKpiPayload &&
      Array.isArray(peKpiPayload.kpis) &&
      peKpiPayload.kpis.length
    ) {
      replaceEngagementTrendsTablesWithKpiDeck(bodyEl, peKpiPayload);
    }
    if (sec.title.trim().toLowerCase() === "platform engagement") {
      trimPlatformEngagementSectionForViewer(bodyEl);
    }
    sectionsContainer.appendChild(article);
  });

  if (preamble) {
    let introMd = preamble.replace(/^#\s+.+\n+/, "");
    introMd = stripPreambleFieldValueTable(introMd);
    const intro = document.getElementById("intro-block");
    if (intro) {
      if (introMd.length > 20) {
        intro.innerHTML = `<div class="markdown-body">${renderMarkdown(introMd)}</div>`;
        const introBody = intro.querySelector(".markdown-body");
        enhanceHealthCheckBlockHeadings(introBody);
        sortTablesByColorColumn(introBody);
        sortRumVolumeByApplicationTableBySessionsDesc(introBody);
        sortNoisyDetectorsTableByTriggersDesc(introBody);
        enhanceTables(introBody);
        enhancePercentFormatting(introBody);
        sortSyntheticsTestUsageTableByTypeAndRunsDesc(introBody);
        sortSyntheticsFailingTestsTableByFailureRateDesc(introBody);
        sortSyntheticsDisabledTestsTableByLastRunAsc(introBody);
        sortMonthlyTablesChronologically(introBody);
        paginateMetricCardinalityTables(introBody);
        paginateAnalyzeIntegrationsTables(introBody);
        paginateDetectorHealthTables(introBody);
        paginateDashboardHealthTables(introBody);
        paginateTokenHealthExpiredNearTables(introBody);
        paginateRemainingHealthResultsTables(introBody);
        removeFindingsSections(introBody);
        intro.style.display = "";
      } else {
        intro.style.display = "none";
      }
    }
  }

  renderLicenseCapacityCharts(appContent);
  await runMermaidDiagrams(appContent);

  setupScrollSpy();
}

function setupScrollSpy() {
  const links = [...document.querySelectorAll("#nav-sections a")];
  const cards = [...document.querySelectorAll("#sections-container .section-card")];
  if (!links.length || !cards.length) return;

  function onScroll() {
    const y = window.scrollY + 140;
    let currentId = cards[0] ? cards[0].id : null;
    for (const c of cards) {
      if (c.offsetTop <= y) currentId = c.id;
    }
    links.forEach((a) => {
      a.classList.toggle("is-active", a.getAttribute("href") === `#${currentId}`);
    });
  }

  window.addEventListener("scroll", () => window.requestAnimationFrame(onScroll), { passive: true });
  onScroll();

  links.forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const id = a.getAttribute("href").slice(1);
      const el = document.getElementById(id);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      links.forEach((x) => x.classList.remove("is-active"));
      a.classList.add("is-active");
    });
  });
}

function isHubJobId(s) {
  return typeof s === "string" && /^[0-9a-f]{32}$/i.test(s.trim());
}

/** Resolve ?report= value for fetch (absolute paths use location.origin so file:// + /api/… is not mangled). */
function resolveReportFetchUrl(reportParam) {
  const s = (reportParam || "").trim();
  if (!s) return null;
  try {
    if (s.startsWith("/")) {
      const origin = window.location.origin;
      if (!origin || origin === "null") {
        return null;
      }
      return new URL(s, origin).href;
    }
    return new URL(s, window.location.href).href;
  } catch (_) {
    return null;
  }
}

function setLoadPanelFetchError(message) {
  const el = document.getElementById("load-panel-fetch-error");
  if (!el) return;
  el.textContent = message;
  el.removeAttribute("hidden");
}

function clearLoadPanelFetchError() {
  const el = document.getElementById("load-panel-fetch-error");
  if (!el) return;
  el.textContent = "";
  el.setAttribute("hidden", "");
}

function hubApiUrl(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  try {
    return new URL(p, window.location.origin).href;
  } catch (_) {
    return p;
  }
}

function setupHubJobToolbar(jobId) {
  const wrap = document.getElementById("header-hub-actions");
  const pdf = document.getElementById("hub-dl-pdf");
  const pptx = document.getElementById("hub-dl-pptx");
  if (!wrap || !pdf || !pptx) return;
  const id = String(jobId).trim().toLowerCase();
  if (!isHubJobId(id)) return;
  wrap.classList.remove("hidden");
  pdf.setAttribute("href", hubApiUrl(`/api/jobs/${encodeURIComponent(id)}/download/pdf`));
  pptx.setAttribute("href", hubApiUrl(`/api/jobs/${encodeURIComponent(id)}/download/pptx`));
  const back = document.getElementById("hub-back");
  if (back) back.setAttribute("href", "/");
  pdf.style.opacity = "0.45";
  pdf.style.pointerEvents = "none";
  pptx.style.opacity = "0.45";
  pptx.style.pointerEvents = "none";
  fetch(hubApiUrl(`/api/jobs/${encodeURIComponent(id)}`), { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : {}))
    .then((j) => {
      if (j && j.hasPdf) {
        pdf.style.opacity = "";
        pdf.style.pointerEvents = "";
      }
      if (j && j.hasPptx) {
        pptx.style.opacity = "";
        pptx.style.pointerEvents = "";
      }
    })
    .catch(() => {});
  const br = document.getElementById("btn-reload");
  if (br) br.style.display = "none";
}

async function finishLoadFromMarkdown() {
  try {
    await buildUI();
  } catch (err) {
    console.error("buildUI failed", err);
    STATE.raw = "";
    setLoadPanelFetchError(
      `Report loaded but the viewer failed to render (${err && err.message ? err.message : String(err)}). Try a different browser, allow CDN scripts (marked / DOMPurify / Mermaid), or use Choose file after downloading the markdown.`
    );
    void buildUI();
    return;
  }
  const params = new URLSearchParams(window.location.search);
  const hj = params.get("hubJob");
  if (hj && isHubJobId(hj)) {
    setupHubJobToolbar(hj.trim().toLowerCase());
  }
}

function handleFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    STATE.raw = reader.result;
    clearLoadPanelFetchError();
    try {
      localStorage.setItem("o11y_health_report_md", STATE.raw);
    } catch (_) {}
    void finishLoadFromMarkdown();
  };
  reader.readAsText(file, "UTF-8");
}

function init() {
  const drop = document.getElementById("drop-zone");
  const input = document.getElementById("file-input");
  const btnReload = document.getElementById("btn-reload");

  if (drop && input) {
    drop.addEventListener("click", () => input.click());
    drop.addEventListener("dragover", (e) => {
      e.preventDefault();
      drop.style.borderColor = "var(--splunk-accent)";
    });
    drop.addEventListener("dragleave", () => {
      drop.style.borderColor = "";
    });
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.style.borderColor = "";
      const f = e.dataTransfer.files[0];
      if (f && (f.name.endsWith(".md") || f.type === "text/markdown" || f.type === "text/plain")) {
        handleFile(f);
      }
    });
    input.addEventListener("change", () => {
      const f = input.files && input.files[0];
      if (f) handleFile(f);
    });
  }

  if (btnReload) {
    btnReload.addEventListener("click", () => {
      STATE.raw = "";
      clearLoadPanelFetchError();
      try {
        localStorage.removeItem("o11y_health_report_md");
      } catch (_) {}
      const hubW = document.getElementById("header-hub-actions");
      if (hubW) hubW.classList.add("hidden");
      const pdf = document.getElementById("hub-dl-pdf");
      const pptx = document.getElementById("hub-dl-pptx");
      if (pdf) {
        pdf.style.opacity = "0.45";
        pdf.style.pointerEvents = "none";
      }
      if (pptx) {
        pptx.style.opacity = "0.45";
        pptx.style.pointerEvents = "none";
      }
      btnReload.style.display = "";
      const loadPanel = document.getElementById("load-panel");
      const appContent = document.getElementById("app-content");
      if (loadPanel) loadPanel.classList.remove("load-panel--hidden");
      if (appContent) appContent.style.display = "none";
      const fi = document.getElementById("file-input");
      if (fi) fi.value = "";
    });
  }

  const params = new URLSearchParams(window.location.search);
  const reportUrl = params.get("report");
  const hubJob = params.get("hubJob");
  clearLoadPanelFetchError();
  if (hubJob && isHubJobId(hubJob)) {
    try {
      localStorage.removeItem("o11y_health_report_md");
    } catch (_) {}
  }
  /* Drop cached report and reload from disk: ?fresh=1 or ?reset=1 (e.g. after editing report.md). */
  if (params.get("fresh") === "1" || params.get("reset") === "1") {
    try {
      localStorage.removeItem("o11y_health_report_md");
    } catch (_) {}
  }

  function tryLocalStorage() {
    try {
      const saved = localStorage.getItem("o11y_health_report_md");
      if (saved && saved.length > 100) {
        STATE.raw = saved;
        void finishLoadFromMarkdown();
        return true;
      }
    } catch (_) {}
    return false;
  }

  if (reportUrl) {
    const resolved = resolveReportFetchUrl(reportUrl);
    if (!resolved) {
      setLoadPanelFetchError(
        "This page cannot resolve the report link (invalid URL, or open via file:// with an absolute /api/… path). Use Choose file, run the Health Check Hub, or serve this folder over http://."
      );
      void buildUI();
    } else {
      fetch(resolved, { cache: "no-store", credentials: "same-origin" })
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.text();
        })
        .then((text) => {
          if (typeof text !== "string" || text.trim().length < 2) {
            throw new Error("Report response was empty or not text");
          }
          const head = text.trim().slice(0, 800);
          if (
            String(reportUrl).includes("/api/jobs/") &&
            head.startsWith("{") &&
            /"error"\s*:/.test(head)
          ) {
            throw new Error("Report API returned an error JSON body instead of markdown");
          }
          STATE.raw = text;
          clearLoadPanelFetchError();
          void finishLoadFromMarkdown();
        })
        .catch((err) => {
          const detail = err && err.message ? err.message : String(err);
          if (hubJob && isHubJobId(hubJob)) {
            STATE.raw =
              "# Report not available yet\n\nThe consolidated markdown is missing or the job id is invalid. Use **All reports** to return to the hub.";
            clearLoadPanelFetchError();
            void finishLoadFromMarkdown();
          } else {
            setLoadPanelFetchError(
              `Could not load the linked report (${detail}). Tried: ${resolved}. If you used ?report=../reports/… with python -m http.server --directory web/o11y-health-report, that path is not served — use repo-root server + ?report=/reports/… (see hint) or Choose file.`
            );
            void buildUI();
          }
        });
    }
  } else if (!tryLocalStorage()) {
    fetch("report.md", { cache: "no-store" })
      .then((r) => (r.ok ? r.text() : Promise.reject()))
      .then((text) => {
        STATE.raw = text;
        void finishLoadFromMarkdown();
      })
      .catch(() => void buildUI());
  }
}

if (typeof window !== "undefined") {
  window.__o11yExpandTablesForPdf = expandPaginatedTablesForPrintExport;
  window.addEventListener("beforeprint", expandPaginatedTablesForPrintExport);
  window.addEventListener("afterprint", restorePaginatedTablesAfterPrint);
}

if (typeof mermaid !== "undefined") {
  mermaid.initialize({ startOnLoad: false });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
