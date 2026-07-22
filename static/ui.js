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
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setOpen(false);
    });
    // Tapping a destination closes the drawer.
    sidebar.querySelectorAll(".nav a").forEach(function (a) {
      a.addEventListener("click", function () { setOpen(false); });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initNav);
  } else {
    initNav();
  }

  window.ui = { busyButton: busyButton, esc: esc };
})();
