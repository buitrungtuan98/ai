// AJAX driver for the Real-Time Task Logs panel (no external libs, CSP-friendly).
// Server-side paginated + searched + scoped: page 1 carries the live jobs; older pages walk the
// full history. Search and scope run in SQL, so they cover every task, not just the current page.
(function () {
  var tbody = document.getElementById("task-rows");
  if (!tbody) return;
  var filterEl = document.getElementById("task-filter");
  var pagerEl = document.getElementById("task-pager");
  var scopeCampaign = tbody.dataset.scopeCampaign || "";
  var scopeChannel = tbody.dataset.scopeChannel || "";

  var page = 1;
  var query = "";
  var meta = { page: 1, pages: 1, total: 0 };
  var lastTasks = [];
  var seq = 0;              // request token — ignore responses that arrive out of order
  var searchTimer = null;

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

  function renderRows(tasks) {
    if (!tasks.length) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty">' +
        (query
          ? '<span class="empty-ico">🔎</span><h3>No matching tasks</h3><p>No task matches “' + esc(query) + '”.</p>'
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
          '<td data-label="Task"><a href="/episodes/' + t.id + '">#' + t.id + "</a></td>" +
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

  function renderPager() {
    if (!pagerEl) return;
    if (meta.pages <= 1) { pagerEl.innerHTML = ""; return; }
    var newer = meta.page > 1
      ? '<button class="btn ghost sm" data-page="' + (meta.page - 1) + '">← Newer</button>'
      : '<span class="btn ghost sm pager-off">← Newer</span>';
    var older = meta.page < meta.pages
      ? '<button class="btn ghost sm" data-page="' + (meta.page + 1) + '">Older →</button>'
      : '<span class="btn ghost sm pager-off">Older →</span>';
    pagerEl.innerHTML = newer +
      '<span class="meta">Page ' + meta.page + " of " + meta.pages +
      " · " + meta.total + (meta.total === 1 ? " task" : " tasks") + "</span>" + older;
  }

  tbody.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-retry]");
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = "Retrying…";
    fetch("/api/tasks/" + btn.dataset.retry + "/retry", { method: "POST" })
      .then(function (r) { if (!r.ok) throw new Error(); return r.json(); })
      .then(function () { poll(); })
      .catch(function () { btn.disabled = false; btn.textContent = "↻ Retry"; });
  });

  if (pagerEl) {
    pagerEl.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-page]");
      if (!btn) return;
      page = Number(btn.dataset.page) || 1;
      poll();  // server clamps + returns the true page; jump immediately
    });
  }

  var TERMINAL = { COMPLETED: 1, FAILED: 1, AWAITING_REVIEW: 1, SCHEDULED: 1 };
  var pollTimer = null;
  function nextDelay() {
    // Fast only while an episode is actually in flight on THIS page (page 1 in practice); relaxed
    // when everything visible is settled — so browsing history doesn't hammer the box.
    var active = lastTasks.some(function (t) { return !TERMINAL[t.status]; });
    return active ? 3000 : 15000;
  }
  function scheduleNext() {
    clearTimeout(pollTimer);
    if (!document.hidden) pollTimer = setTimeout(poll, nextDelay());  // pause when backgrounded
  }
  function url() {
    var p = ["page=" + page];
    if (query) p.push("q=" + encodeURIComponent(query));
    if (scopeCampaign) p.push("campaign=" + encodeURIComponent(scopeCampaign));
    if (scopeChannel) p.push("channel=" + encodeURIComponent(scopeChannel));
    return "/api/tasks?" + p.join("&");
  }
  function poll() {
    clearTimeout(pollTimer);
    var mine = ++seq;
    fetch(url(), { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/login"; throw new Error("unauthenticated"); }
        return r.json();
      })
      .then(function (d) {
        if (mine !== seq) return;                       // a newer request superseded this one
        lastTasks = d.tasks || [];
        meta = { page: d.page || 1, pages: d.pages || 1, total: d.total || 0 };
        page = meta.page;                               // adopt the server's clamped page
        renderRows(lastTasks);
        renderPager();
      })
      .catch(function () { /* transient — try again next tick */ })
      .finally(function () { if (mine === seq) scheduleNext(); });
  }

  if (filterEl) {
    filterEl.addEventListener("input", function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () {
        query = filterEl.value.trim();
        page = 1;            // a new search always starts at the newest match
        poll();
      }, 300);               // debounce — search now hits the server
    });
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) clearTimeout(pollTimer);
    else poll();   // immediate refresh + resume on return to foreground
  });

  poll();
})();
