// Shared UI helpers — self-contained, no external libs, CSP-friendly. Loaded on every page.
(function () {
  "use strict";

  // ── Async-button busy state ───────────────────────────────────────────────
  // Generalises the save-label → disable + swap → run → restore-in-finally idiom that was
  // copy-pasted across the campaign form, credentials and login. `run` receives no args and
  // returns a promise; its result/rejection is propagated so callers can render inline output.
  function busyButton(btn, busyLabel, run) {
    var orig = btn.textContent;
    btn.disabled = true;
    if (busyLabel != null) btn.textContent = busyLabel;
    return Promise.resolve()
      .then(run)
      .finally(function () {
        btn.disabled = false;
        if (busyLabel != null) btn.textContent = orig;
      });
  }

  // Escape a string for safe insertion when building HTML by concatenation (mirrors app.js).
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  // ── Toasts (transient, aria-live) ─────────────────────────────────────────
  function toast(msg, kind) {
    var host = document.getElementById("toasts");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;                                   // textContent — never innerHTML
    host.appendChild(el);
    requestAnimationFrame(function () { el.classList.add("show"); });
    setTimeout(function () {
      el.classList.remove("show");
      setTimeout(function () { el.remove(); }, 250);
    }, 3500);
  }

  // ── Accessible confirm dialog (replaces native confirm()) ─────────────────
  function confirmDialog(message) {
    var modal = document.getElementById("modal");
    var msgEl = document.getElementById("modal-msg");
    var okBtn = document.getElementById("modal-ok");
    var cancelBtn = document.getElementById("modal-cancel");
    if (!modal || !okBtn) return Promise.resolve(window.confirm(message));   // graceful fallback
    msgEl.textContent = message || "Are you sure?";
    modal.hidden = false;
    okBtn.focus();
    return new Promise(function (resolve) {
      function done(val) {
        modal.hidden = true;
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onBackdrop);
        document.removeEventListener("keydown", onKey);
        resolve(val);
      }
      function onOk() { done(true); }
      function onCancel() { done(false); }
      function onBackdrop(e) { if (e.target === modal) done(false); }
      function onKey(e) { if (e.key === "Escape") done(false); }
      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      modal.addEventListener("click", onBackdrop);
      document.addEventListener("keydown", onKey);
    });
  }

  // Any <form data-confirm="…"> is gated by the styled dialog instead of native confirm().
  function initConfirmForms() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (form.dataset.confirmed === "1") return;         // already approved → let it through
        e.preventDefault();
        confirmDialog(form.dataset.confirm).then(function (ok) {
          if (ok) { form.dataset.confirmed = "1"; form.submit(); }
        });
      });
    });
  }

  // ── Relative timestamps ───────────────────────────────────────────────────
  function relTime(iso) {
    var then = Date.parse(iso);
    if (isNaN(then)) return "";
    var s = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }
  function initRelTimes() {
    document.querySelectorAll("[data-reltime]").forEach(function (el) {
      var r = relTime(el.dataset.reltime);
      if (r) { el.title = el.textContent.trim(); el.textContent = r; }  // full timestamp → tooltip
    });
  }

  // ── Live summary: cross-page attention badge + dashboard auto-refresh ──────
  function setBadge(key, n) {
    document.querySelectorAll('[data-badge="' + key + '"]').forEach(function (b) {
      if (n > 0) { b.textContent = n > 99 ? "99+" : n; b.hidden = false; }
      else { b.hidden = true; }
    });
  }
  function setLive(key, val) {
    document.querySelectorAll('[data-live="' + key + '"]').forEach(function (el) {
      el.textContent = val;
    });
  }
  function updateHealth(h) {
    var strip = document.getElementById("health-strip");
    if (!strip) return;
    var degraded = !h.worker || !h.redis;
    strip.classList.toggle("degraded", degraded);
    var note = document.getElementById("degraded-note");
    if (note) note.hidden = !degraded;
    var wd = document.getElementById("hd-worker"), wl = document.getElementById("hl-worker");
    if (wd) wd.className = "dot2 " + (h.worker ? "ok" : "bad");
    if (wl) wl.textContent = h.worker ? "running" : "stopped";
    var rd = document.getElementById("hd-redis"), rl = document.getElementById("hl-redis");
    if (rd) rd.className = "dot2 " + (h.redis ? "ok" : "bad");
    if (rl) rl.textContent = h.redis ? "connected" : "down";
    setText("hv-queue", h.queue_depth == null ? "—" : h.queue_depth);
    setText("hv-buffer", h.buffer_ready);
    setText("hv-disk", h.disk_pct == null ? "—" : h.disk_pct + "%");
  }
  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = v; }

  function applySummary(d) {
    var c = d.counts || {};
    setBadge("failed", c.failed || 0);
    setBadge("awaiting_review", c.awaiting_review || 0);
    setBadge("attn", (c.failed || 0) + (c.awaiting_review || 0));
    // Dashboard live values (no-op on other pages — selectors simply match nothing).
    setLive("channels", d.channels);
    setLive("active_campaigns", d.active_campaigns);
    setLive("published", c.published);
    setLive("working", c.working);
    setLive("awaiting_review", c.awaiting_review);
    setLive("failed", c.failed);
    var bf = document.getElementById("banner-failed");
    if (bf) bf.hidden = !(c.failed > 0);
    var br = document.getElementById("banner-review");
    if (br) br.hidden = !(c.awaiting_review > 0);
    if (d.health) updateHealth(d.health);
  }
  function pollSummary() {
    fetch("/api/summary", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) applySummary(d); })
      .catch(function () { /* transient — try again next tick */ });
  }

  // ── Mobile drawer navigation ──────────────────────────────────────────────
  function initNav() {
    var toggle = document.getElementById("nav-toggle");
    var backdrop = document.getElementById("nav-backdrop");
    var sidebar = document.getElementById("sidebar");
    if (!toggle || !sidebar) return;
    function setOpen(open) {
      document.body.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    toggle.addEventListener("click", function () {
      setOpen(!document.body.classList.contains("nav-open"));
    });
    if (backdrop) backdrop.addEventListener("click", function () { setOpen(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setOpen(false); });
    sidebar.querySelectorAll(".nav a").forEach(function (a) {
      a.addEventListener("click", function () { setOpen(false); });
    });
  }

  function init() {
    initNav();
    initConfirmForms();
    initRelTimes();
    pollSummary();
    setInterval(pollSummary, 6000);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.ui = { busyButton: busyButton, esc: esc, toast: toast, confirmDialog: confirmDialog };
})();
