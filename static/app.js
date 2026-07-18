// Minimal AJAX polling for the Real-Time Task Logs panel (no external libs, CSP-friendly).
(function () {
  const tbody = document.getElementById("task-rows");
  if (!tbody) return;

  const STATUS_LABELS = {
    PENDING_QUEUE: "Pending Queue",
    AI_GENERATION: "AI Generation",
    AUDIO_SYNCED: "Audio Synced",
    RENDERING: "Rendering",
    PUBLISHING: "Publishing",
    COMPLETED: "Completed",
    FAILED: "Failed",
  };

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function render(tasks) {
    if (!tasks.length) {
      tbody.innerHTML = '<tr><td colspan="6"><div class="empty">No tasks yet. Start a campaign to begin rendering.</div></td></tr>';
      return;
    }
    tbody.innerHTML = tasks
      .map(function (t) {
        const label = STATUS_LABELS[t.status] || t.status;
        const err = t.error ? '<div class="err">' + esc(t.error.slice(0, 400)) + "</div>" : "";
        return (
          "<tr>" +
          "<td>#" + t.id + "</td>" +
          "<td>C" + t.campaign_id + " · Ep " + t.episode + "</td>" +
          '<td><span class="pill ' + esc(t.status) + '">' + esc(label) + "</span></td>" +
          '<td><div class="progress"><span style="width:' + (t.progress || 0) + '%"></span></div></td>' +
          "<td>" + (t.progress || 0) + "%</td>" +
          "<td>" + err + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function poll() {
    fetch("/api/tasks", { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/login"; throw new Error("unauthenticated"); }
        return r.json();
      })
      .then(function (d) { render(d.tasks || []); })
      .catch(function () { /* transient — try again next tick */ });
  }

  poll();
  setInterval(poll, 3000);
})();
