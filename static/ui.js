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
  // before Confirm enables — used for the most destructive, cascading actions. `verb` labels the
  // action button with what it does ("Publish", "Delete") instead of a generic "Confirm": the button
  // is what gets read and clicked, so it should be the thing that says what is about to happen.
  function confirmDialog(message, typeToMatch, verb) {
    var modal = document.getElementById("modal");
    var msgEl = document.getElementById("modal-msg");
    var okBtn = document.getElementById("modal-ok");
    var cancelBtn = document.getElementById("modal-cancel");
    var input = document.getElementById("modal-input");
    if (!modal || !okBtn) return Promise.resolve(window.confirm(message));   // graceful fallback
    var needType = !!typeToMatch;
    msgEl.textContent = message || "Are you sure?";
    okBtn.textContent = verb || "Confirm";
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

  // Any <form data-confirm="…"> (optionally data-confirm-type="…", data-confirm-verb="…") is gated
  // by the styled dialog.
  function initConfirmForms() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (form.dataset.confirmed === "1") return;
        e.preventDefault();
        confirmDialog(form.dataset.confirm, form.dataset.confirmType, form.dataset.confirmVerb)
          .then(function (ok) {
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
  // A dead OR wedged worker is the one fault that stops the whole factory, so the Operations rail
  // item carries it even when the health strip isn't on screen.
  function setWorkerBadge(h) {
    var bad = !!h && (!h.worker || h.worker_stalled);
    document.querySelectorAll('[data-badge="worker_down"]').forEach(function (b) {
      b.textContent = "!";
      b.hidden = !bad;
    });
  }
  function updateHealth(h) {
    var strip = document.getElementById("health-strip");
    if (!strip) return;
    var degraded = !h.worker || !h.redis || h.worker_stalled;
    strip.classList.toggle("degraded", degraded);
    var note = document.getElementById("degraded-note");
    if (note) {
      note.hidden = !degraded;
      if (degraded) {
        var c = [];
        if (!h.worker) c.push("worker");
        if (!h.redis) c.push("Redis");
        // A registered-but-wedged worker is its own fault: nothing is "down", yet nothing moves.
        note.textContent = c.length
          ? "⚠ The factory is degraded — " + c.join(" and ") +
            (c.length > 1 ? " are" : " is") + " down; rendering and publishing are paused."
          : "⚠ The render worker is wedged — it has stopped making progress. It restarts itself; " +
            "see Operations for details.";
      }
    }
    var wd = document.getElementById("hd-worker"), wl = document.getElementById("hl-worker");
    if (wd) wd.className = "dot2 " + (h.worker && !h.worker_stalled ? "ok" : "bad");
    if (wl) wl.textContent = !h.worker ? "stopped" : (h.worker_stalled ? "wedged" : "running");
    var rd = document.getElementById("hd-redis"), rl = document.getElementById("hl-redis");
    if (rd) rd.className = "dot2 " + (h.redis ? "ok" : "bad");
    if (rl) rl.textContent = h.redis ? "connected" : "down";
    setText("hv-queue", h.queue_depth == null ? "—" : h.queue_depth);
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
    setBadge("autopilot_proposed", d.autopilot_proposed || 0);
    // ONE attention number, computed server-side, rendered by every badge that answers "what needs
    // me" — hamburger, Dashboard rail item, bell and the triage card (ADR-064).
    setBadge("attn", d.attention || 0);
    setText("triage-count", d.attention || 0);
    setLive("channels", d.channels);
    setLive("active_campaigns", d.active_campaigns);
    setLive("published", c.published);
    setLive("working", c.working);
    setLive("awaiting_review", c.awaiting_review);
    setLive("failed", c.failed);
    if (d.health) { updateHealth(d.health); setWorkerBadge(d.health); }
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

  // ── Alert bell: one cross-channel inbox of what is wrong right now ────────
  // The badge is a live count (red + amber), not an unread counter: fix the problem and it clears
  // itself. Rows are built with createElement/textContent — channel, campaign and error text are
  // user/AI data and must never be interpolated as HTML.
  var alertTimer = null;
  function renderAlerts(data) {
    var list = document.getElementById("bell-list");
    var count = document.getElementById("bell-count");
    var summary = document.getElementById("bell-summary");
    if (!list || !count) return;
    var rows = (data && data.alerts) || [];
    // `attention` is the shared count (ADR-064). The panel may GROUP rows ("2 episodes waiting"), so
    // its row count is not the number to badge — that disagreement is exactly what confused people.
    var n = (data && data.attention) || 0;
    count.textContent = n > 99 ? "99+" : n;
    count.hidden = n === 0;
    count.className = "bell-count" + (data && data.worst === "amber" ? " amber" : "");
    if (summary) {
      summary.textContent = n ? n + " need" + (n === 1 ? "s" : "") + " attention"
                              : "Nothing needs attention";
    }
    list.textContent = "";
    if (!rows.length) {
      var empty = document.createElement("li");
      empty.className = "bell-empty";
      empty.textContent = "All clear — no failures, nothing waiting.";
      list.appendChild(empty);
      return;
    }
    rows.forEach(function (a) {
      var li = document.createElement("li");
      li.className = "bell-item";
      var dot = document.createElement("span");
      dot.className = "bell-dot " + (a.level || "");
      var body = document.createElement("div");
      body.className = "bell-body";
      var chain = [a.channel, a.campaign].filter(Boolean);
      if (chain.length) {
        var ch = document.createElement("span");
        ch.className = "bell-chain";
        ch.textContent = chain.join(" › ");
        body.appendChild(ch);
      }
      var text = document.createElement("span");
      text.className = "bell-text";
      text.textContent = a.text || "";
      body.appendChild(text);
      if (a.at) {
        var when = document.createElement("span");
        when.className = "bell-when";
        when.textContent = relTime(a.at);
        body.appendChild(when);
      }
      li.appendChild(dot);
      li.appendChild(body);
      if (a.href && a.action) {
        var link = document.createElement("a");
        link.className = "btn ghost sm";
        link.href = a.href;
        link.textContent = a.action;
        li.appendChild(link);
      }
      list.appendChild(li);
    });
  }
  function pollAlerts() {
    fetch("/api/alerts", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error(r.status)); })
      .then(renderAlerts)
      .catch(function () { /* transient — the summary poller owns the offline toast */ });
  }
  function stopAlerts() { clearTimeout(alertTimer); alertTimer = null; }
  function startAlerts() {
    stopAlerts();
    (function loop() { alertTimer = setTimeout(function () { pollAlerts(); loop(); }, 30000); })();
  }
  function initBell() {
    var btn = document.getElementById("bell");
    var panel = document.getElementById("bell-panel");
    if (!btn || !panel) return;
    function setOpen(open) {
      panel.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) pollAlerts();                       // always current the moment it is opened
    }
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      setOpen(panel.hidden);
    });
    document.addEventListener("click", function (e) {
      if (!panel.hidden && !panel.contains(e.target) && e.target !== btn) setOpen(false);
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setOpen(false); });
    pollAlerts();
    startAlerts();
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
    // The bottom bar's "More" opens the SAME drawer — one nav, reached two ways (no second menu).
    var more = document.getElementById("tabbar-more");
    if (more) more.addEventListener("click", function () { setOpen(!document.body.classList.contains("nav-open")); });
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
  // Channel scope switcher. Changing it merges ?channel into the CURRENT query (keeping status/q,
  // resetting page). The choice is remembered in localStorage so it survives visits to unscoped
  // pages (Dashboard/Channels/Credentials) — on load the remembered channel is reflected in the
  // dropdown and carried onto the scope-aware nav links. An explicit ?channel in the URL always wins.
  var SCOPED_PATHS = ["/episodes", "/campaigns", "/assets", "/calendar"];
  var SCOPE_KEY = "scopeChannel";

  function initScopeSwitcher() {
    var sel = document.getElementById("scope-switcher");
    var urlCh = new URLSearchParams(window.location.search).get("channel");
    var sticky = null;
    try {
      if (urlCh) { localStorage.setItem(SCOPE_KEY, urlCh); sticky = urlCh; }
      else { sticky = localStorage.getItem(SCOPE_KEY); }
    } catch (e) { /* private mode — degrade to URL-only scope */ }

    if (sel) {
      var has = Array.prototype.some.call(sel.options, function (o) { return o.value === sticky; });
      if (!urlCh && sticky && has) sel.value = sticky;           // reflect the remembered channel
      else if (sticky && !has) { try { localStorage.removeItem(SCOPE_KEY); } catch (e) {} sticky = null; }
      sel.addEventListener("change", function () {
        var params = new URLSearchParams(window.location.search);
        if (sel.value) params.set("channel", sel.value); else params.delete("channel");
        params.delete("page");                                   // scope change → back to page 1
        try {
          if (sel.value) localStorage.setItem(SCOPE_KEY, sel.value);
          else localStorage.removeItem(SCOPE_KEY);
        } catch (e) {}
        var qs = params.toString();
        window.location.href = window.location.pathname + (qs ? "?" + qs : "");
      });
    }

    // Carry the remembered channel onto scope-aware nav links when the URL didn't already scope.
    if (!urlCh && sticky) {
      document.querySelectorAll(".sidebar a, .tabbar a").forEach(function (a) {
        try {
          var u = new URL(a.getAttribute("href"), window.location.origin);
          if (SCOPED_PATHS.indexOf(u.pathname) >= 0 && !u.searchParams.get("channel")) {
            u.searchParams.set("channel", sticky);
            a.setAttribute("href", u.pathname + "?" + u.searchParams.toString());
          }
        } catch (e) { /* skip malformed href */ }
      });
    }
  }

  // ── One-shot flashes ──────────────────────────────────────────────────────
  // A flash describes something that just happened ("Publish queued"), so it must not survive a
  // reload, a Back, or a shared link — those re-displayed a stale success for an action nobody took.
  // The banner is already rendered server-side; this only rewrites the address bar (no reload).
  function initFlash() {
    try {
      var u = new URL(window.location.href);
      if (!u.searchParams.has("flash")) return;
      u.searchParams.delete("flash");
      u.searchParams.delete("flash_reason");
      var qs = u.searchParams.toString();
      window.history.replaceState(null, "", u.pathname + (qs ? "?" + qs : "") + u.hash);
    } catch (e) { /* no History API — the banner simply repeats on reload */ }
  }

  // Every destination the palette can jump to, with the words an operator might actually type
  // (including the Vietnamese ones). Local, so pages answer instantly and keep working even when the
  // search request fails — and it means ⌘K is a way to *navigate*, not only to find content.
  var CMD_PAGES = [
    { label: "Dashboard", href: "/", sub: "overview & triage", keys: "home tong quan bang dieu khien" },
    { label: "Campaigns", href: "/campaigns", sub: "build & schedule", keys: "chien dich series" },
    { label: "New campaign", href: "/campaigns/new", sub: "create", keys: "them tao moi chien dich add create" },
    { label: "Episodes", href: "/episodes", sub: "every video", keys: "tap video render log tasks" },
    { label: "Review queue", href: "/episodes?status=awaiting_review", sub: "approve videos", keys: "duyet xet approve assets pool" },
    { label: "Publishing", href: "/calendar", sub: "when each episode goes out", keys: "calendar lich dang schedule" },
    { label: "Channels", href: "/channels", sub: "YouTube & Facebook", keys: "kenh youtube facebook connect" },
    { label: "Autopilot", href: "/autopilot", sub: "AI channel manager", keys: "tu dong proposals" },
    { label: "Operations", href: "/operations", sub: "worker, queue & recovery", keys: "worker queue restart van hanh" },
    { label: "Credentials", href: "/credentials", sub: "API keys", keys: "keys api gemini pexels khoa" },
    { label: "Settings", href: "/settings", sub: "defaults & AI budget", keys: "cai dat preferences" }
  ];

  // Same folding as the server (main.py `_fold`): lowercase, drop Vietnamese diacritics, đ → d — so
  // "lich dang" finds "Lịch đăng" and "chien dich" finds "Chiến dịch".
  function fold(s) {
    return (s || "").toLowerCase().replace(/đ/g, "d")
      .normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  }

  function matchPages(q) {
    var needle = fold(q);
    if (!needle) return [];
    return CMD_PAGES.filter(function (p) {
      return fold(p.label).indexOf(needle) >= 0 || p.keys.indexOf(needle) >= 0;
    }).slice(0, 5).map(function (p) {
      return { type: "Go to", label: p.label, sub: p.sub, href: p.href };
    });
  }

  // Global search palette (⌘K / Ctrl-K, or "/"): one box across channels/campaigns/episodes.
  function initCmdK() {
    var backdrop = document.getElementById("cmdk");
    var input = document.getElementById("cmdk-input");
    var list = document.getElementById("cmdk-results");
    if (!backdrop || !input || !list) return;
    var items = [], sel = -1, timer = null, seq = 0;

    function open() {
      backdrop.hidden = false;
      input.value = "";
      sel = -1;
      // Opening with an empty box lists where you can go, so the palette teaches the app instead of
      // showing "Type to search…" and leaving the operator to guess what it indexes.
      render(CMD_PAGES.map(function (p) {
        return { type: "Go to", label: p.label, sub: p.sub, href: p.href };
      }));
      sel = -1;
      highlight();
      input.focus();
    }
    function close() { backdrop.hidden = true; }
    function go() { if (sel >= 0 && items[sel]) window.location.href = items[sel].href; }

    function highlight() {
      Array.prototype.forEach.call(list.children, function (li, i) {
        li.classList.toggle("active", i === sel);
      });
      if (sel >= 0 && list.children[sel]) list.children[sel].scrollIntoView({ block: "nearest" });
    }
    function render(results) {
      items = results || [];
      sel = items.length ? 0 : -1;
      list.innerHTML = "";
      if (!items.length) {
        var empty = document.createElement("li");
        empty.className = "cmdk-empty";
        empty.textContent = input.value.trim().length < 2 ? "Type to search…" : "No matches.";
        list.appendChild(empty);
        return;
      }
      items.forEach(function (r, i) {
        // Built with textContent (never innerHTML) — the labels are user/AI data.
        var li = document.createElement("li");
        li.className = "cmdk-item" + (i === 0 ? " active" : "");
        var tag = document.createElement("span");
        tag.className = "cmdk-type";
        tag.textContent = r.type;
        var label = document.createElement("span");
        label.className = "cmdk-label";
        label.textContent = r.label;
        var sub = document.createElement("span");
        sub.className = "cmdk-sub";
        sub.textContent = r.sub || "";
        li.appendChild(tag);
        li.appendChild(label);
        li.appendChild(sub);
        li.addEventListener("click", function () { sel = i; go(); });
        list.appendChild(li);
      });
    }
    // Destinations are matched locally and shown first (instant, and still there if the request
    // fails); content matches from the server are appended.
    function search() {
      var q = input.value.trim();
      var pages = matchPages(q);
      if (q.length < 2) { render(pages); return; }
      var mine = ++seq;
      fetch("/api/search?q=" + encodeURIComponent(q), { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (mine === seq) render(pages.concat(d.results || [])); })
        .catch(function () { if (mine === seq) render(pages); });
    }

    document.getElementById("cmdk-open") &&
      document.getElementById("cmdk-open").addEventListener("click", open);
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(search, 180); });
    backdrop.addEventListener("click", function (e) { if (e.target === backdrop) close(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { sel = Math.min(sel + 1, items.length - 1); highlight(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); highlight(); e.preventDefault(); }
      else if (e.key === "Enter") { go(); e.preventDefault(); }
      else if (e.key === "Escape") { close(); }
    });
    document.addEventListener("keydown", function (e) {
      var typing = /^(input|textarea|select)$/i.test((e.target.tagName || ""));
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { open(); e.preventDefault(); }
      else if (e.key === "/" && !typing && backdrop.hidden) { open(); e.preventDefault(); }
    });
  }

  function init() {
    initTheme();
    initFlash();
    initNav();
    initScopeSwitcher();
    initCmdK();
    initConfirmForms();
    initRelTimes();
    initBell();
    pollSummary();
    startSummary();
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { stopSummary(); stopAlerts(); }
      else { pollSummary(); startSummary(); pollAlerts(); startAlerts(); }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.ui = { busyButton: busyButton, esc: esc, toast: toast, confirmDialog: confirmDialog };
})();
