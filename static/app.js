// Live episode rows — the last piece of the old /tasks page (ADR-065).
//
// It used to be a whole second table: its own AJAX driver, its own search grammar, its own pager and
// its own status words, rendering the same episodes as /episodes in a different shape. Now there is
// ONE server-rendered episode table and this script only keeps the moving parts moving: a row in a
// working stage gets its stage pill and progress updated in place, so "the live render log" is a
// filter of the list rather than a separate destination.
//
// Rows are matched by task id and updated with textContent / class changes only — nothing is built
// from server strings, so there is no HTML-injection surface here at all.
(function () {
  "use strict";

  var tbody = document.getElementById("episode-rows");
  if (!tbody) return;

  // Same vocabulary as the server-rendered pills (ADR-064) — one word per stage, everywhere.
  var STAGE_LABELS = {
    PENDING_QUEUE: "Queued",
    AI_GENERATION: "Writing",
    AUDIO_SYNCED: "Rendering",
    RENDERING: "Rendering",
    AWAITING_REVIEW: "Review",
    SCHEDULED: "Scheduled",
    PUBLISHING: "Publishing",
    COMPLETED: "Published",
    FAILED: "Failed",
    CANCELLED: "Cancelled",
  };
  var HAS_PROGRESS = { AI_GENERATION: 1, AUDIO_SYNCED: 1, RENDERING: 1 };
  // Stages that can still change on their own. Once a row reaches anything else, polling it is waste.
  var MOVING = { PENDING_QUEUE: 1, AI_GENERATION: 1, AUDIO_SYNCED: 1, RENDERING: 1, PUBLISHING: 1 };

  var timer = null;
  var seq = 0;

  function liveRows() {
    return Array.prototype.slice.call(tbody.querySelectorAll("[data-live-task]"));
  }

  function applyTask(row, t) {
    var pillHost = row.querySelector('[data-live="pill"]');
    var pill = pillHost && pillHost.querySelector(".pill");
    if (pill) {
      pill.className = "pill " + t.status;                       // status value IS the class (CSS/tests)
      pill.textContent = STAGE_LABELS[t.status] || t.status;
    }
    var progHost = row.querySelector('[data-live="progress"]');
    if (progHost) {
      if (HAS_PROGRESS[t.status]) {
        var pct = progHost.querySelector(".ep-pct");
        if (!pct) {
          pct = document.createElement("span");
          pct.className = "meta ep-pct";
          progHost.appendChild(pct);
        }
        pct.textContent = Math.round(t.progress || 0) + "%";
      } else {
        progHost.textContent = "";                               // settled: no bar, no stale number
      }
    }
    // A row that has stopped moving stops being polled; it keeps whatever it last showed until the
    // operator reloads (a page reload is the only thing that can re-sort or re-filter the list).
    if (!MOVING[t.status]) row.removeAttribute("data-live-task");
  }

  function nextDelay() {
    return liveRows().length ? 4000 : 20000;
  }

  function schedule() {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(poll, nextDelay());
  }

  function poll() {
    clearTimeout(timer);
    var rows = liveRows();
    if (!rows.length) { schedule(); return; }                    // nothing in flight — idle cheaply
    var mine = ++seq;
    // `live=1` returns only episodes in a working stage: a handful, whatever the history size, so
    // this never pages through hundreds of settled rows to find the one that is rendering.
    fetch("/api/tasks?live=1", { headers: { Accept: "application/json" } })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/login"; throw new Error("unauthenticated"); }
        return r.json();
      })
      .then(function (d) {
        if (mine !== seq) return;                                // superseded by a newer request
        var byId = {};
        (d.tasks || []).forEach(function (t) { byId[t.id] = t; });
        rows.forEach(function (row) {
          var t = byId[row.dataset.liveTask];
          if (t) { applyTask(row, t); return; }
          // Absent from the live set = it left the working stages. Ask the server what it became
          // rather than guessing, but only once per row (the attribute comes off first, so this
          // never repeats). Without the ask, the one transition the live log exists to show —
          // render → Failed/Published — was the one it froze on (R22).
          var id = row.dataset.liveTask;
          row.removeAttribute("data-live-task");
          fetch("/api/tasks?q=" + encodeURIComponent(id), { headers: { Accept: "application/json" } })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              var hit = (d.tasks || []).filter(function (x) { return String(x.id) === String(id); })[0];
              if (hit) applyTask(row, hit);
            })
            .catch(function () { /* a reload remains the fallback */ });
        });
      })
      .catch(function () { /* transient — the next tick tries again */ })
      .finally(function () { if (mine === seq) schedule(); });
  }

  poll();
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) clearTimeout(timer);
    else poll();
  });
})();
