// Shared UI helpers — self-contained, no external libs, CSP-friendly. Loaded on every page.
(function () {
  "use strict";

  // ── Async-button busy state ───────────────────────────────────────────────
  // Generalises the save-label → disable + swap → run → restore-in-finally idiom that was
  // copy-pasted across the campaign form, credentials and login. `run` returns a promise.
  function busyButton(btn, busyLabel, run) {
    var orig = btn.textContent;
    btn.disabled = true;
    if (busyLabel != null) btn.textContent = busyLabel;
    return Promise.resolve().then(run).finally(function () {
      btn.disabled = false;
      if (busyLabel != null) btn.textContent = orig;
    });
  }

  // Escape a string for safe HTML-string concatenation (mirrors app.js).
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
  // Pass `typeToMatch` to require the user to type an exact string (e.g. a channel name)
  // before Confirm enables — used for the most destructive, cascading actions.
  function confirmDialog(message, typeToMatch) {
    var modal = document.getElementById("modal");
    var msgEl = document.getElementById("modal-msg");
    var okBtn = document.getElementById("modal-ok");
    var cancelBtn = document.getElementById("modal-cancel");
    var input = document.getElementById("modal-input");
    if (!modal || !okBtn) return Promise.resolve(window.confirm(message));   // graceful fallback
    var needType = !!typeToMatch;
    msgEl.textContent = message || "Are you sure?";
    if (input) {
      input.hidden = !needType;
      input.value = "";
      input.placeholder = needType ? 'Type “' + typeToMatch + '” to confirm' : "";
    }
    okBtn.disabled = needType;
    modal.hidden = false;
    (needType && input ? input : okBtn).focus();
    return new Promise(function (resolve) {
      function onInput() { okBtn.disabled = input.value.trim() !== typeToMatch; }
      function done(val) {
        modal.hidden = true;
        okBtn.removeEventListener("click", onOk);
        cancelBtn.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onBackdrop);
        document.removeEventListener("keydown", onKey);
        if (input) input.removeEventListener("input", onInput);
        resolve(val);
      }
      function onOk() { if (!okBtn.disabled) done(true); }
      function onCancel() { done(false); }
      function onBackdrop(e) { if (e.target === modal) done(false); }
      function onKey(e) {
        if (e.key === "Escape") done(false);
        else if (e.key === "Enter" && !okBtn.disabled && (!needType || document.activeElement === input)) done(true);
      }
      okBtn.addEventListener("click", onOk);
      cancelBtn.addEventListener("click", onCancel);
      modal.addEventListener("click", onBackdrop);
      document.addEventListener("keydown", onKey);
      if (needType && input) input.addEventListener("input", onInput);
    });
  }

  // Any <form data-confirm="…"> (optionally data-confirm-type="…") is gated by the styled dialog.
  function initConfirmForms() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (form.dataset.confirmed === "1") return;
        e.preventDefault();
        confirmDialog(form.dataset.confirm, form.dataset.confirmType).then(function (ok) {
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
      if (r) { el.title = el.textContent.trim(); el.textContent = r; }
    });
  }

  // ── Theme (dark default, optional light) ──────────────────────────────────
  function currentTheme() { return document.documentElement.dataset.theme === "light" ? "light" : "dark"; }
  function setThemeLabels() {
    var light = currentTheme() === "light";
    var side = document.getElementById("theme-toggle-side");
    if (side) side.textContent = (light ? "◑ Dark mode" : "◐ Light mode");
  }
  function toggleTheme() {
    var next = currentTheme() === "light" ? "dark" : "light";
    if (next === "light") document.documentElement.dataset.theme = "light";
    else document.documentElement.removeAttribute("data-theme");
    try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
    setThemeLabels();
  }
  function initTheme() {
    ["theme-toggle", "theme-toggle-side"].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.addEventListener("click", toggleTheme);
    });
    setThemeLabels();
  }

  // ── Live summary: cross-page attention badge + dashboard auto-refresh ──────
  var pollFails = 0, wasDown = false;
  function setBadge(key, n) {
    document.querySelectorAll('[data-badge="' + key + '"]').forEach(function (b) {
      if (n > 0) { b.textContent = n > 99 ? "99+" : n; b.hidden = false; } else { b.hidden = true; }
    });
  }
  function setLive(key, val) {
    document.querySelectorAll('[data-live="' + key + '"]').forEach(function (el) { el.textContent = val; });
  }
  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = v; }
  function updateHealth(h) {
    var strip = document.getElementById("health-strip");
    if (!strip) return;
    var degraded = !h.worker || !h.redis;
    strip.classList.toggle("degraded", degraded);
    var note = document.getElementById("degraded-note");
    if (note) {
      note.hidden = !degraded;
      if (degraded) {
        var c = [];
        if (!h.worker) c.push("worker");
        if (!h.redis) c.push("Redis");
        note.textContent = "⚠ The factory is degraded — " + c.join(" and ") +
          (c.length > 1 ? " are" : " is") + " down; rendering and publishing are paused.";
      }
    }
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
  function flashLive() {
    var d = document.getElementById("live-dot");
    if (!d) return;
    d.classList.remove("pulse");
    void d.offsetWidth;            // restart the animation
    d.classList.add("pulse");
  }
  function applySummary(d) {
    var c = d.counts || {};
    setBadge("failed", c.failed || 0);
    setBadge("awaiting_review", c.awaiting_review || 0);
    setBadge("attn", (c.failed || 0) + (c.awaiting_review || 0));
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
    flashLive();
  }
  function pollSummary() {
    fetch("/api/summary", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(r.status)); })
      .then(function (d) {
        pollFails = 0;
        if (wasDown) { wasDown = false; toast("Reconnected to the server.", "success"); }
        applySummary(d);
      })
      .catch(function () {
        pollFails++;
        if (pollFails === 2 && !wasDown) { wasDown = true; toast("Lost connection to the server — retrying…", "danger"); }
      });
  }

  // ── Mobile drawer navigation ──────────────────────────────────────────────
  function initNav() {
    var active = document.querySelector(".sidebar .nav a.active");
    if (active) active.setAttribute("aria-current", "page");
    var toggle = document.getElementById("nav-toggle");
    var backdrop = document.getElementById("nav-backdrop");
    var sidebar = document.getElementById("sidebar");
    if (!toggle || !sidebar) return;
    function setOpen(open) {
      document.body.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }
    toggle.addEventListener("click", function () { setOpen(!document.body.classList.contains("nav-open")); });
    if (backdrop) backdrop.addEventListener("click", function () { setOpen(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setOpen(false); });
    sidebar.querySelectorAll(".nav a").forEach(function (a) {
      a.addEventListener("click", function () { setOpen(false); });
    });
  }

  // Visibility-aware loop: no polling while the tab is backgrounded (saves server load + phone
  // battery); an immediate refresh + resume when it comes back to the foreground.
  var summaryTimer = null;
  function stopSummary() { clearTimeout(summaryTimer); summaryTimer = null; }
  function startSummary() {
    stopSummary();
    (function loop() { summaryTimer = setTimeout(function () { pollSummary(); loop(); }, 6000); })();
  }
  function init() {
    initTheme();
    initNav();
    initConfirmForms();
    initRelTimes();
    pollSummary();
    startSummary();
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) stopSummary();
      else { pollSummary(); startSummary(); }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.ui = { busyButton: busyButton, esc: esc, toast: toast, confirmDialog: confirmDialog };
})();
