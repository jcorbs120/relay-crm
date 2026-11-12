// Relay CRM front-end helpers (no external dependencies)
(function () {
  // ---------- security: CSRF header on every state-changing request; bounce to sign-in when the session ends
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const CSRF = csrfMeta ? csrfMeta.content : "";
  const _fetch = window.fetch.bind(window);
  window.fetch = async function (url, opts) {
    opts = Object.assign({}, opts || {});
    const method = (opts.method || "GET").toUpperCase();
    const h = new Headers(opts.headers || {});
    if (!h.has("X-Requested-With")) h.set("X-Requested-With", "fetch");
    if (method !== "GET" && method !== "HEAD") h.set("X-CSRF-Token", CSRF);
    opts.headers = h;
    opts.credentials = "same-origin";
    const r = await _fetch(url, opts);
    if (r.status === 401) { location = "/login?next=" + encodeURIComponent(location.pathname + location.search); }
    if (r.status === 403) { toast("You don't have permission to do that.", "error"); }
    return r;
  };

  // ---------- user menu
  const um = document.getElementById("userMenuBtn");
  if (um) {
    const menu = document.getElementById("userMenu");
    um.addEventListener("click", e => { e.stopPropagation(); menu.classList.toggle("on"); });
    document.addEventListener("click", e => { if (!menu.contains(e.target)) menu.classList.remove("on"); });
  }

  // ---------- global search
  const box = document.getElementById("gsearch");
  const res = document.getElementById("gresults");
  let t;
  if (box) {
    box.addEventListener("input", () => {
      clearTimeout(t);
      const q = box.value.trim();
      if (!q) { res.style.display = "none"; return; }
      t = setTimeout(async () => {
        const r = await fetch("/search?q=" + encodeURIComponent(q));
        const items = await r.json();
        res.innerHTML = items.length ? items.map(i =>
          `<a href="${i.url}"><span class="t">${i.type}</span><b>${esc(i.label)}</b><br><span class="small muted">${esc(i.sub)}</span></a>`
        ).join("") : `<div class="empty">No matches</div>`;
        res.style.display = "block";
      }, 180);
    });
    box.addEventListener("keydown", e => {
      if (e.key === "Enter") { location = "/contacts?q=" + encodeURIComponent(box.value); }
      if (e.key === "Escape") { res.style.display = "none"; }
    });
    document.addEventListener("click", e => { if (!res.contains(e.target) && e.target !== box) res.style.display = "none"; });
  }

  // ---------- sync button
  const sb = document.getElementById("syncBtn");
  if (sb) {
    sb.addEventListener("click", async () => {
      sb.disabled = true;
      const lbl = sb.innerHTML;
      sb.innerHTML = '<span class="spin"></span> Syncing\u2026';
      try {
        const r = await fetch("/sync/run", { method: "POST", headers: { "X-Requested-With": "fetch" } });
        const j = await r.json();
        toast(j.message || "Done", j.status === "ok" ? "ok" : "error");
        if (j.status === "ok" && (j.logged || j.new_contacts || j.tasks_created || j.tasks_closed)) {
          setTimeout(() => location.reload(), 1300);
        }
      } catch (e) { toast("Sync failed: " + e, "error"); }
      sb.disabled = false; sb.innerHTML = lbl;
    });
  }

  // ---------- task checkboxes
  document.querySelectorAll("[data-toggle-task]").forEach(cb => {
    cb.addEventListener("change", async () => {
      await fetch(`/tasks/${cb.dataset.toggleTask}/toggle`, { method: "POST", headers: { "X-Requested-With": "fetch" } });
      const row = cb.closest("[data-task-row]");
      if (row) { row.style.opacity = cb.checked ? .45 : 1; row.style.textDecoration = cb.checked ? "line-through" : "none"; }
    });
  });

  // ---------- expandable email bodies
  document.querySelectorAll("[data-more]").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    const txt = a.previousElementSibling; txt.classList.toggle("open");
    a.textContent = txt.classList.contains("open") ? "Show less" : "Show more";
  }));
  document.querySelectorAll(".tl .txt").forEach(el => {
    const more = el.nextElementSibling;
    if (more && more.hasAttribute("data-more") && el.scrollHeight <= el.clientHeight + 2) more.style.display = "none";
  });

  // ---------- modals
  document.querySelectorAll("[data-modal]").forEach(b => b.addEventListener("click", e => {
    e.preventDefault();
    const m = document.getElementById(b.dataset.modal);
    if (b.dataset.kind) {
      const sel = m.querySelector("[name=kind]"); if (sel) { sel.value = b.dataset.kind; sel.dispatchEvent(new Event("change")); }
    }
    m.classList.add("on");
    const f = m.querySelector("input[type=text],textarea"); if (f) f.focus();
  }));
  document.querySelectorAll(".modal-bg").forEach(m => {
    m.addEventListener("click", e => { if (e.target === m || e.target.hasAttribute("data-close")) m.classList.remove("on"); });
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") document.querySelectorAll(".modal-bg.on").forEach(m => m.classList.remove("on")); });

  // ---------- select all
  const all = document.getElementById("selAll");
  if (all) all.addEventListener("change", () => document.querySelectorAll("input[name=ids]").forEach(c => c.checked = all.checked));

  // ---------- confirm
  document.querySelectorAll("form[data-confirm]").forEach(f => f.addEventListener("submit", e => {
    if (!confirm(f.dataset.confirm)) e.preventDefault();
  }));

  // ---------- pipeline drag & drop
  let dragged = null;
  document.querySelectorAll(".opp[draggable]").forEach(card => {
    card.addEventListener("dragstart", () => { dragged = card; card.classList.add("dragging"); });
    card.addEventListener("dragend", () => { card.classList.remove("dragging"); dragged = null; });
  });
  document.querySelectorAll(".col[data-stage]").forEach(col => {
    col.addEventListener("dragover", e => { e.preventDefault(); col.classList.add("drop"); });
    col.addEventListener("dragleave", () => col.classList.remove("drop"));
    col.addEventListener("drop", async e => {
      e.preventDefault(); col.classList.remove("drop");
      if (!dragged) return;
      const stage = col.dataset.stage, id = dragged.dataset.id;
      col.querySelector(".col-b").prepend(dragged);
      const r = await fetch(`/opps/${id}/stage`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stage }) });
      if (r.ok) { toast(`Moved to ${stage}`, "ok"); recalcBoard(); } else toast("Could not move", "error");
    });
  });
  function recalcBoard() {
    document.querySelectorAll(".col[data-stage]").forEach(col => {
      const cards = col.querySelectorAll(".opp");
      let sum = 0; cards.forEach(c => sum += parseFloat(c.dataset.value || 0));
      col.querySelector(".cnt").textContent = cards.length;
      col.querySelector(".sum").textContent = "$" + Math.round(sum).toLocaleString();
    });
  }

  // ---------- toast
  window.toast = function (msg, kind) {
    const d = document.createElement("div");
    d.className = "flash " + (kind || "ok");
    d.style.cssText = "position:fixed;right:20px;bottom:20px;z-index:99;max-width:440px;box-shadow:0 8px 24px rgba(0,0,0,.15)";
    d.textContent = msg; document.body.appendChild(d);
    setTimeout(() => d.remove(), 4200);
  };
  function esc(s) { return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
})();
