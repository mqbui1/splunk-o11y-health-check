/**
 * Health Check Hub — client UI
 */
(function () {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  if (window.location.protocol === "file:") {
    const b = document.createElement("div");
    b.className = "banner-warn";
    b.setAttribute("role", "alert");
    b.innerHTML =
      "<strong>This page was opened as a local file (file://).</strong> The Health Check Hub cannot start runs from here because <code>/api/jobs</code> is not available. " +
      "From the repo root run <code>python3 scripts/o11y_health_hub_server.py</code> and open " +
      "<strong>http://127.0.0.1:8766/</strong> (or the port shown in the terminal).";
    if (document.body) {
      document.body.insertBefore(b, document.body.firstChild);
    }
  }

  function reportViewerUrl(jobId) {
    const path =
      "/health-report/?report=" +
      encodeURIComponent("/api/jobs/" + jobId + "/markdown") +
      "&hubJob=" +
      encodeURIComponent(jobId);
    const o = window.location.origin;
    if (o && o !== "null") {
      return o + path;
    }
    return path;
  }

  const views = {
    home: ["view-home", "view-home-list"],
    form: ["view-form"],
    confirm: ["view-confirm"],
    progress: ["view-progress"],
  };

  function showView(name) {
    Object.entries(views).forEach(([k, ids]) => {
      const show = k === name;
      ids.forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle("hidden", !show);
      });
    });
  }

  let pendingPayload = null;
  let pollTimer = null;

  function getRealm() {
    const sel = $("#realm");
    if (sel.value === "custom") {
      const c = $("#realmCustom").value.trim();
      return c || "us0";
    }
    return sel.value;
  }

  const realmEl0 = $("#realm");
  const realmCustomEl0 = $("#realmCustom");
  if (realmEl0 && realmCustomEl0) {
    realmEl0.addEventListener("change", () => {
      const custom = realmEl0.value === "custom";
      realmCustomEl0.classList.toggle("hidden", !custom);
    });
  }

  const allProducts = $("#allProducts");
  const productGridEl = $("#productGrid");
  const prodInputs = $$('#productGrid input[name="prod"]');
  const licStartEl = $("#licenseUtilizationStartMonth");
  const licEndEl = $("#licenseUtilizationEndMonth");
  const includeLicenseEl = $("#includeLicense");
  const hubLicenseWindowRoot = $(".hub-license-window");
  const peComparisonMonthEl = $("#platformEngagementComparisonMonth");
  const peBaselineMonthEl = $("#platformEngagementBaselineMonth");
  const includePlatformEngagementEl = $("#includePlatformEngagement");
  const platformEngagementWindowRoot = $("#platformEngagementWindow");
  const hcFormEl = $("#hc-form");

  function syncLicenseWindowFields() {
    if (!licStartEl || !licEndEl || !includeLicenseEl) return;
    const on = includeLicenseEl.checked;
    licStartEl.disabled = !on;
    licEndEl.disabled = !on;
    if (hubLicenseWindowRoot) {
      hubLicenseWindowRoot.classList.toggle("hub-license-window--inactive", !on);
    }
    if (!on) {
      licStartEl.setCustomValidity("");
      licEndEl.setCustomValidity("");
    }
  }

  function syncLicenseMonthValidity() {
    if (!licStartEl || !licEndEl || licStartEl.disabled) return;
    const s = String(licStartEl.value || "").trim();
    const e = String(licEndEl.value || "").trim();
    licStartEl.setCustomValidity("");
    licEndEl.setCustomValidity("");
    if (s && !e) {
      licEndEl.setCustomValidity("Set end date (month/year) or clear start.");
      return;
    }
    if (e && !s) {
      licStartEl.setCustomValidity("Set start date (month/year) or clear end.");
      return;
    }
    if (s && e && s > e) {
      const msg = "Start must not be after end.";
      licStartEl.setCustomValidity(msg);
      licEndEl.setCustomValidity(msg);
    }
  }
  if (licStartEl && licEndEl) {
    ["input", "change"].forEach((ev) => {
      licStartEl.addEventListener(ev, syncLicenseMonthValidity);
      licEndEl.addEventListener(ev, syncLicenseMonthValidity);
    });
  }
  if (includeLicenseEl) {
    includeLicenseEl.addEventListener("change", () => {
      syncLicenseWindowFields();
      syncLicenseMonthValidity();
    });
  }
  syncLicenseWindowFields();

  function syncPlatformEngagementWindowFields() {
    if (!peComparisonMonthEl || !includePlatformEngagementEl) return;
    const on = includePlatformEngagementEl.checked;
    [peComparisonMonthEl, peBaselineMonthEl].forEach((el) => {
      if (el) el.disabled = !on;
    });
    if (platformEngagementWindowRoot) {
      platformEngagementWindowRoot.classList.toggle("hub-license-window--inactive", !on);
    }
    if (!on) {
      [peComparisonMonthEl, peBaselineMonthEl].forEach((el) => {
        if (el) el.setCustomValidity("");
      });
    }
  }

  function syncPlatformEngagementMonthValidity() {
    if (!peComparisonMonthEl || !includePlatformEngagementEl || peComparisonMonthEl.disabled) return;
    const c = String(peComparisonMonthEl.value || "").trim();
    const b = String(peBaselineMonthEl.value || "").trim();
    peComparisonMonthEl.setCustomValidity("");
    peBaselineMonthEl.setCustomValidity("");
    if (c && !b) {
      peBaselineMonthEl.setCustomValidity("Set baseline month or clear comparison.");
      return;
    }
    if (b && !c) {
      peComparisonMonthEl.setCustomValidity("Set comparison month or clear baseline.");
    }
  }

  if (peComparisonMonthEl && peBaselineMonthEl) {
    ["input", "change"].forEach((ev) => {
      peComparisonMonthEl.addEventListener(ev, syncPlatformEngagementMonthValidity);
      peBaselineMonthEl.addEventListener(ev, syncPlatformEngagementMonthValidity);
    });
  }
  if (includePlatformEngagementEl) {
    includePlatformEngagementEl.addEventListener("change", () => {
      syncPlatformEngagementWindowFields();
      syncPlatformEngagementMonthValidity();
    });
  }
  syncPlatformEngagementWindowFields();

  if (allProducts && productGridEl) {
    allProducts.addEventListener("change", () => {
      const all = allProducts.checked;
      $$("input[type='checkbox']", productGridEl).forEach((inp) => {
        inp.disabled = all;
        inp.checked = false;
      });
    });
  }

  function optMonth(id) {
    const v = ($(id) && $(id).value) || "";
    const t = String(v).trim();
    return t || null;
  }

  function collectHubOptions() {
    const allProd = allProducts.checked;
    const skipLic = !$("#includeLicense").checked;
    const skipPe = !$("#includePlatformEngagement").checked;
    const licenseUtilizationStartMonth = skipLic ? null : optMonth("#licenseUtilizationStartMonth");
    const licenseUtilizationEndMonth = skipLic ? null : optMonth("#licenseUtilizationEndMonth");
    return {
      outputJson: $("#outputJson").checked,
      outputPptx: $("#outputPptx").checked,
      outputPdf: $("#outputPdf").checked,
      skipLicense: skipLic,
      skipPlatformEngagement: skipPe,
      skipDetectors: allProd ? false : !$("#includeDetectors").checked,
      skipDashboards: allProd ? false : !$("#includeDashboards").checked,
      skipTokens: allProd ? false : !$("#includeTokens").checked,
      skipOtelCollectors: allProd ? false : !$("#includeOtelCollectors").checked,
      licenseUtilizationStartMonth,
      licenseUtilizationEndMonth,
      platformEngagementComparisonMonth: skipPe ? null : optMonth("#platformEngagementComparisonMonth"),
      platformEngagementBaselineMonth: skipPe ? null : optMonth("#platformEngagementBaselineMonth"),
    };
  }

  if (hcFormEl) hcFormEl.addEventListener("submit", (e) => {
    e.preventDefault();
    const company = $("#company").value.trim();
    const reportTitle = $("#reportTitle").value.trim();
    const token = $("#token").value.trim();
    const realm = getRealm();

    if (!company || !reportTitle || !token) return;

    syncLicenseWindowFields();
    syncLicenseMonthValidity();
    syncPlatformEngagementWindowFields();
    syncPlatformEngagementMonthValidity();
    if (hcFormEl && !hcFormEl.checkValidity()) {
      hcFormEl.reportValidity();
      return;
    }

    const opts = collectHubOptions();

    let products;
    if (allProducts.checked) {
      products = "all";
    } else {
      products = prodInputs.filter((i) => i.checked).map((i) => i.value);
    }

    pendingPayload = { company, reportTitle, token, realm, products, ...opts };

    const dl = $("#confirm-dl");
    dl.innerHTML = "";
    const prodShort = { apm: "APM", im: "IM", rum: "RUM", synthetics: "Synthetics" };
    let productLabel;
    if (products === "all") {
      productLabel = "All (APM, IM, RUM, Synthetics, Detectors, OpenTelemetry collectors)";
    } else {
      const parts = products.map((p) => prodShort[p] || String(p).toUpperCase());
      if ($("#includeDetectors").checked) parts.push("Detectors");
      if ($("#includeOtelCollectors").checked) parts.push("OpenTelemetry collectors");
      productLabel =
        parts.length === 0 ? "None (all product modules skipped)" : parts.join(", ");
    }
    const deliv = [
      "Markdown (always)",
      opts.outputJson ? "JSON" : null,
      opts.outputPptx ? "PowerPoint" : null,
      opts.outputPdf ? "PDF" : null,
    ]
      .filter(Boolean)
      .join(", ");
    const skips = [];
    if (opts.skipLicense) skips.push("license");
    if (opts.skipPlatformEngagement) skips.push("engagement");
    if (opts.skipDetectors) skips.push("detectors");
    if (opts.skipDashboards) skips.push("dashboards");
    if (opts.skipTokens) skips.push("tokens");
    if (opts.skipOtelCollectors) skips.push("OTel collectors");
    const rows = [
      ["Company", company],
      ["Report title", reportTitle],
      ["Realm", realm],
      ["Token", token.length > 4 ? "••••••••" + token.slice(-4) : "•••• (short token)"],
      ["Products", productLabel],
      ["Deliverables", deliv],
      ["Excluded shared checklist", skips.length ? skips.join(", ") : "None"],
    ];
    if (opts.licenseUtilizationStartMonth && opts.licenseUtilizationEndMonth) {
      rows.push([
        "License utilization (UTC)",
        opts.licenseUtilizationStartMonth + " → " + opts.licenseUtilizationEndMonth + " (full months, inclusive)",
      ]);
    }
    const peC = opts.platformEngagementComparisonMonth;
    const peB = opts.platformEngagementBaselineMonth;
    if (peC && peB) {
      rows.push([
        "Platform engagement (UTC)",
        "Comparison month " + peC + " vs baseline month " + peB + " (mean within each full month)",
      ]);
    } else if (!opts.skipPlatformEngagement) {
      rows.push(["Platform engagement (UTC)", "Default rolling windows (no custom months)"]);
    }
    rows.forEach(([dt, dd]) => {
      const dte = document.createElement("dt");
      dte.textContent = dt;
      const dde = document.createElement("dd");
      dde.textContent = dd;
      dl.appendChild(dte);
      dl.appendChild(dde);
    });

    showView("confirm");
  });

  const btnFormCancel = $("#btn-form-cancel");
  if (btnFormCancel) {
    btnFormCancel.addEventListener("click", () => {
      showView("home");
      loadJobs();
    });
  }

  const btnConfirmBack = $("#btn-confirm-back");
  if (btnConfirmBack) {
    btnConfirmBack.addEventListener("click", () => showView("form"));
  }

  const btnConfirmSubmit = $("#btn-confirm-submit");
  if (btnConfirmSubmit) {
    btnConfirmSubmit.addEventListener("click", async () => {
      if (!pendingPayload) return;
      const btn = btnConfirmSubmit;
      btn.disabled = true;
      try {
        const res = await fetch("/api/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(pendingPayload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          alert(data.error || "Failed to start job");
          btn.disabled = false;
          return;
        }
        pendingPayload = null;
        const tok = $("#token");
        if (tok) tok.value = "";
        const jobId = data.job && data.job.id;
        if (!jobId) {
          alert("Server returned an unexpected response (missing job id). Check the Hub server log.");
          btn.disabled = false;
          return;
        }
        showProgress(jobId);
      } catch (err) {
        alert(String(err));
        btn.disabled = false;
      }
    });
  }

  function statusClass(s) {
    const m = {
      queued: "status-pill--queued",
      running: "status-pill--running",
      completed: "status-pill--completed",
      failed: "status-pill--failed",
    };
    return m[s] || "status-pill--queued";
  }

  function formatStepElapsed(sec) {
    if (sec == null || sec < 1) return "";
    const s = Math.floor(sec);
    if (s < 60) return s + "s on this step so far";
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m + "m " + r + "s on this step so far";
  }

  function stepIconChar(status) {
    const m = {
      pending: "○",
      running: "◉",
      done: "✓",
      error: "!",
      skipped: "–",
      cancelled: "·",
    };
    return m[status] || "○";
  }

  function renderProgressChecklist(progress) {
    const bar = $("#progress-bar-fill");
    const wrap = $("#progress-bar-wrap");
    const pctLbl = $("#progress-percent-label");
    const foot = $("#progress-footnote");
    const line = $("#progress-status-line");
    const stepsOl = $("#progress-steps");

    if (!progress || !progress.steps || !stepsOl) return;

    const pct = Math.max(0, Math.min(100, Number(progress.overallPercent) || 0));
    if (bar) bar.style.width = pct + "%";
    if (wrap) wrap.setAttribute("aria-valuenow", String(Math.round(pct)));
    if (pctLbl) pctLbl.textContent = Math.round(pct) + "%";

    if (line) line.textContent = progress.statusLine || "";

    if (foot) {
      if (progress.footnote) {
        foot.textContent = progress.footnote;
        foot.classList.remove("hidden");
      } else {
        foot.textContent = "";
        foot.classList.add("hidden");
      }
    }

    const cur = progress.currentStepId;
    stepsOl.innerHTML = progress.steps
      .map(function (step) {
        const st = step.status || "pending";
        const isCur = cur === step.id;
        const cls =
          "step--" +
          st +
          (isCur && st === "running" ? " step--current" : "") +
          (isCur && st === "pending" ? " step--current" : "");
        let elapsedHtml = "";
        if (isCur && st === "running" && progress.runningElapsedSec != null) {
          const fe = formatStepElapsed(progress.runningElapsedSec);
          if (fe) elapsedHtml = '<div class="progress-step__elapsed">' + escapeHtml(fe) + "</div>";
        }
        const det = step.detail ? '<div class="progress-step__detail">' + escapeHtml(step.detail) + "</div>" : "";
        const hint = step.hint
          ? '<p class="progress-step__hint">' + escapeHtml(step.hint) + "</p>"
          : "";
        return (
          '<li class="' +
          cls +
          '">' +
          '<span class="progress-step__icon" aria-hidden="true">' +
          stepIconChar(st) +
          "</span>" +
          '<div class="progress-step__body">' +
          "<p class=\"progress-step__title\">" +
          escapeHtml(step.label || step.id) +
          "</p>" +
          hint +
          det +
          elapsedHtml +
          "</div></li>"
        );
      })
      .join("");
  }

  function showProgress(jobId) {
    showView("progress");
    $("#progress-page-title").textContent = "Run in progress";
    $("#progress-summary").textContent = "Job " + String(jobId).slice(0, 8) + "…";
    const logEl = $("#progress-log");
    if (logEl) logEl.textContent = "";
    const pill = $("#progress-status");
    const openReportA = $("#btn-open-report");
    if (openReportA) openReportA.classList.add("hidden");
    $("#progress-status-line").textContent = "";
    const bar = $("#progress-bar-fill");
    if (bar) bar.style.width = "0%";
    const pctLbl = $("#progress-percent-label");
    if (pctLbl) pctLbl.textContent = "0%";
    const stepsOl = $("#progress-steps");
    if (stepsOl) stepsOl.innerHTML = "";

    if (pollTimer) clearInterval(pollTimer);

    async function tick() {
      try {
        const res = await fetch("/api/jobs/" + jobId);
        const j = await res.json();
        if (!res.ok) return;
        if (pill) {
          pill.textContent = j.status;
          pill.className = "status-pill " + statusClass(j.status);
        }
        if (j.progress) renderProgressChecklist(j.progress);
        if (logEl) {
          logEl.textContent = j.logTail || "";
          logEl.scrollTop = logEl.scrollHeight;
        }

        if (j.status === "completed") {
          clearInterval(pollTimer);
          pollTimer = null;
          $("#progress-page-title").textContent = "Run completed";
          $("#progress-summary").textContent =
            "Finished in " + (j.elapsed_sec != null ? j.elapsed_sec + "s" : "—") + ". Open the web report for PDF and PowerPoint.";
          const openBtn = $("#btn-open-report");
          if (openBtn) {
            openBtn.href = reportViewerUrl(jobId);
            openBtn.classList.remove("hidden");
          }
        }
        if (j.status === "failed") {
          clearInterval(pollTimer);
          pollTimer = null;
          $("#progress-page-title").textContent = "Run failed";
          $("#progress-summary").textContent = j.error || "Run failed";
          if (j.hasMarkdown) {
            const openBtn = $("#btn-open-report");
            if (openBtn) {
              openBtn.href = reportViewerUrl(jobId);
              openBtn.classList.remove("hidden");
            }
          }
        }
      } catch (_) {}
    }

    tick();
    pollTimer = setInterval(tick, 1500);
  }

  const btnProgressHome = $("#btn-progress-home");
  if (btnProgressHome) {
    btnProgressHome.addEventListener("click", () => {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
      showView("home");
      loadJobs();
    });
  }

  const navHome = $("#nav-home");
  if (navHome) {
    navHome.addEventListener("click", (e) => {
      e.preventDefault();
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
      showView("home");
      loadJobs();
    });
  }

  const navNew = $("#nav-new");
  if (navNew) {
    navNew.addEventListener("click", (e) => {
      e.preventDefault();
      showView("form");
    });
  }

  const btnGotoForm = $("#btn-goto-form");
  if (btnGotoForm) {
    btnGotoForm.addEventListener("click", () => showView("form"));
  }

  async function loadJobs() {
    const mount = $("#job-list-mount");
    mount.innerHTML = '<p class="empty-state">Loading…</p>';
    try {
      const res = await fetch("/api/jobs");
      const data = await res.json();
      const jobs = data.jobs || [];
      if (jobs.length === 0) {
        mount.innerHTML = '<p class="empty-state">No runs yet. Start a new health check.</p>';
        return;
      }
      const table = document.createElement("table");
      table.className = "job-table";
      table.innerHTML =
        "<thead><tr><th>Title</th><th>Company</th><th>Realm</th><th>Status</th><th>Started</th><th>Report</th><th>Actions</th></tr></thead>";
      const tb = document.createElement("tbody");
      jobs.forEach((j) => {
        const tr = document.createElement("tr");
        const pill = document.createElement("span");
        pill.className = "status-pill " + statusClass(j.status);
        pill.textContent = j.status;
        const link =
          j.status === "completed" && j.hasMarkdown
            ? '<a href="' +
              reportViewerUrl(j.id).replace(/"/g, "&quot;") +
              '">Open report</a>'
            : j.status === "running" || j.status === "queued"
              ? '<a href="#" data-job="' +
                j.id +
                '" class="js-resume">Progress</a>'
              : j.hasMarkdown
                ? '<a href="' +
                  reportViewerUrl(j.id).replace(/"/g, "&quot;") +
                  '">Partial report</a>'
                : "—";
        tr.innerHTML =
          "<td>" +
          escapeHtml(j.reportTitle || "—") +
          "</td><td>" +
          escapeHtml(j.company || "—") +
          "</td><td>" +
          escapeHtml(j.realm || "—") +
          "</td><td></td><td>" +
          escapeHtml(j.created || "—") +
          "</td><td>" +
          link +
          "</td><td></td>";
        tr.querySelector("td:nth-child(4)").appendChild(pill);
        const actTd = tr.querySelector("td:nth-child(7)");
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "btn btn--danger btn--compact";
        delBtn.textContent = "Delete";
        delBtn.setAttribute(
          "aria-label",
          "Delete run " + (j.reportTitle || j.id || "").slice(0, 80)
        );
        delBtn.addEventListener("click", () => {
          deleteJob(j.id, j.reportTitle || j.id || "this run");
        });
        actTd.appendChild(delBtn);
        tb.appendChild(tr);
      });
      table.appendChild(tb);
      mount.innerHTML = "";
      mount.appendChild(table);

      $$(".js-resume", mount).forEach((a) => {
        a.addEventListener("click", (ev) => {
          ev.preventDefault();
          showProgress(a.getAttribute("data-job"));
        });
      });
    } catch {
      mount.innerHTML = '<p class="empty-state">Could not load jobs.</p>';
    }
  }

  async function deleteJob(jobId, title) {
    const label = String(title || "this run").slice(0, 200);
    if (!window.confirm('Delete run "' + label + '" and all artifacts? This cannot be undone.')) return;
    try {
      const res = await fetch("/api/jobs/" + encodeURIComponent(jobId), { method: "DELETE" });
      const data = await res.json().catch(() => ({}));
      if (res.status === 409) {
        alert(data.error || "Cannot delete a run that is still in progress.");
        return;
      }
      if (!res.ok) {
        alert(data.error || "Delete failed");
        return;
      }
      loadJobs();
    } catch (err) {
      alert(String(err));
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  const params = new URLSearchParams(window.location.search);
  if (params.get("job")) {
    showProgress(params.get("job"));
  } else {
    showView("home");
    loadJobs();
  }
})();
