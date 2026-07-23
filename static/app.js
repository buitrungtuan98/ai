// Minimal AJAX polling for the Real-Time Task Logs panel (no external libs, CSP-friendly).
(function () {
  var tbody = document.getElementById("task-rows");
  if (!tbody) return;
  var filterEl = document.getElementById("task-filter");
  var scopeCampaign = tbody.dataset.scopeCampaign ? Number(tbody.dataset.scopeCampaign) : null;
  var lastTasks = [];

  var STATUS_LABELS = {
    PENDING_QUEUE: "Pending Queue",
    AI_GENERATION: "AI Generation",
    AUDIO_SYNCED: "Audio Synced",
    RENDERING: "Rendering",
    AWAITING_REVIEW: "Awaiting Review",
    SCHEDULED: "Scheduled",
    PUBLISHING: "Publishing",
    COMPLETED: "Completed",
    FAILED: "Failed",
  };

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function fmtDuration(s) {
    if (s == null) return "—";
    var m = Math.floor(s / 60), sec = s % 60;
    return m > 0 ? m + "m " + sec + "s" : sec + "s";
  }

  function resultCell(t) {
    if (t.published_url) {
      return '<a href="' + esc(t.published_url) + '" target="_blank" rel="noopener">View ↗</a>';
    }
    if (t.status === "AWAITING_REVIEW") {
      return '<a href="/assets">Preview in Asset Pool →</a>';
    }
    if (t.status === "SCHEDULED") {
      return '<span class="meta">Rendered — publishing at the next posting slot</span>';
    }
    if (t.error) {
      return '<div class="err">' + esc(t.error.slice(0, 300)) + "</div>";
    }
    return "";
  }

  function actionCell(t) {
    if (!t.can_retry) return "";
    return '<button class="btn ghost sm" data-retry="' + t.id + '">↻ Retry</button>';
  }

  function matches(t, q) {
    return (t.id + " " + (t.topic || "") + " " + (t.channel || "") + " " + (t.status || ""))
      .toLowerCase().indexOf(q) >= 0;
  }

  function render(allTasks) {
    var scoped = scopeCampaign ? allTasks.filter(function (t) { return t.campaign_id === scopeCampaign; }) : allTasks;
    var q = (filterEl && filterEl.value.trim().toLowerCase()) || "";
    var tasks = q ? scoped.filter(function (t) { return matches(t, q); }) : scoped;
    if (!tasks.length) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty">' +
        (q
          ? '<span class="empty-ico">🔎</span><h3>No matching tasks</h3><p>No task matches “' + esc(q) + '”.</p>'
          : '<span class="empty-ico">≣</span><h3>No tasks yet</h3>' +
            '<p>Start a campaign to begin rendering — episodes will stream in here live.</p>') +
        "</div></td></tr>";
      return;
    }
    tbody.innerHTML = tasks
      .map(function (t) {
        var label = STATUS_LABELS[t.status] || t.status;
        var retries = t.retry_count > 0 ? ' <span class="meta">(retry ' + t.retry_count + ")</span>" : "";
        var ptone = t.status === "COMPLETED" ? " done" : (t.status === "FAILED" ? "" : " work");
        return (
          "<tr>" +
          '<td data-label="Task">#' + t.id + "</td>" +
          '<td data-label="Episode">' + esc(t.topic) + " · Ep " + t.episode +
            '<div class="meta">' + esc(t.channel) + "</div></td>" +
          '<td data-label="Status"><span class="pill ' + esc(t.status) + '">' + esc(label) + "</span>" + retries + "</td>" +
          '<td data-label="Progress"><div class="progress' + ptone + '"><span style="width:' + (t.progress || 0) + '%"></span></div>' +
            '<span class="meta">' + (t.progress || 0) + "%</span></td>" +
          '<td data-label="Time">' + fmtDuration(t.duration_s) + "</td>" +
          '<td data-label="Result">' + resultCell(t) + "</td>" +
          '<td data-label="">' + actionCell(t) + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  tbody.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-retry]");
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = "Retrying…";
    fetch("/api/tasks/" + btn.dataset.retry + "/retry", { method: "POST" })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(poll)
      .catch(function () { btn.disabled = false; btn.textContent = "↻ Retry"; });
  });

  var TERMINAL = { COMPLETED: 1, FAILED: 1, AWAITING_REVIEW: 1, SCHEDULED: 1 };
  var pollTimer = null;
  function nextDelay() {
    // Fast while an episode is actually in flight; relaxed when everything is settled.
    var active = lastTasks.some(function (t) { return !TERMINAL[t.status]; });
    return active ? 3000 : 15000;
  }
  function scheduleNext() {
    clearTimeout(pollTimer);
    if (!document.hidden) pollTimer = setTimeout(poll, nextDelay());  // pause when backgrounded
  }
  function poll() {
    fetch("/api/tasks", { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/login"; throw new Error("unauthenticated"); }
        return r.json();
      })
      .then(function (d) { lastTasks = d.tasks || []; render(lastTasks); })
      .catch(function () { /* transient — try again next tick */ })
      .finally(scheduleNext);
  }

  if (filterEl) filterEl.addEventListener("input", function () { render(lastTasks); });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) clearTimeout(pollTimer);
    else poll();   // immediate refresh + resume on return to foreground
  });

  poll();
})();
