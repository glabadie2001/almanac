"use strict";
// Almanac mission control. Three views over one door:
//   FLEET      every claude.exe (GET /api/fleet), one open session (GET /api/session), kill + send (POST)
//   TELEMETRY  the efficiency report (GET /api/report) drawn as ASCII: sparklines, block bars, small multiples
//   LOG        what was killed or said, by whom (GET /api/actions)
// Text from transcripts is untrusted: everything is set with textContent, never innerHTML.

const SPARK = "▁▂▃▄▅▆▇█";
const SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
const PAIRS = [["search", "rg  > grep"], ["tests", "rt  > suites"], ["http", "door> curl"], ["edit", "swap> py edit"]];
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (id) => document.getElementById(id);
const state = {
  tab: "fleet", fleet: null, sel: null, selCreated: null, detail: null, cpu: new Map(), seen: new Set(),
  feedKeys: new Set(), confirmKill: false, days: get("days", "30"), bucket: get("bucket", "day"), report: null, frame: 0,
  pins: new Set(JSON.parse(get("pins", "[]"))), wide: new Set(JSON.parse(get("wide", "[]"))),
  gMode: get("g-mode", "all"), gCols: get("g-cols", "auto"), gHeight: get("g-height", "m"), tiles: new Map(),
};
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

// "claude-opus-5-5" -> "opus-5.5", "claude-haiku-4-5-20251001" -> "haiku-4.5"
function fmtModel(m) {
  if (!m) return "–";
  return m.replace(/^claude-/, "").replace(/-\d{8}$/, "").replace(/-(\d+)-(\d+)$/, "-$1.$2");
}
function effortBadge(e) {
  const n = EFFORTS.indexOf(e) + 1;
  if (!e) return el("span", { cls: "dim", text: "–" });
  return el("span", { cls: `effort ${e}`, title: `effort: ${e}` },
    el("span", { cls: "lvl", text: "▮".repeat(Math.max(0, n)) }), el("span", { cls: "off", text: "▯".repeat(Math.max(0, 5 - n)) }), ` ${e}`);
}
const keyOf = (p) => `${p.pid}:${p.created}`;
const active = (p) => !!p.status && p.status !== "idle";      // busy, shell, … anything but idle is working
function togglePin(p) {
  const k = keyOf(p);
  state.pins.has(k) ? state.pins.delete(k) : state.pins.add(k);
  put("pins", JSON.stringify([...state.pins]));
}

function get(k, d) { try { return localStorage.getItem("almanac." + k) || d; } catch { return d; } }
function put(k, v) { try { localStorage.setItem("almanac." + k, v); } catch { /* private window */ } }
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") n.textContent = v; else if (k === "cls") n.className = v; else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) if (kid !== null && kid !== undefined) n.append(kid);
  return n;
}
const num = (v, d = 1) => v === null || v === undefined ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const pct = (v) => v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`;
function age(sec) {
  if (!sec) return "–";
  const s = Math.max(0, Date.now() / 1000 - sec);
  return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : s < 86400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
}
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
function spark(values, max) {
  const top = max ?? Math.max(0, ...values.filter((v) => v !== null && v !== undefined));
  return values.map((v) => v === null || v === undefined ? " " : SPARK[top > 0 ? Math.min(7, Math.round((v / top) * 7)) : 0]).join("");
}
function meter(share, width = 16) {
  const filled = share === null || share === undefined ? 0 : Math.round(share * width);
  return el("span", { cls: "meter" }, "[", "█".repeat(filled), el("span", { cls: "rest", text: "░".repeat(width - filled) }), "]");
}
async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  const doc = await res.json().catch(() => ({}));
  $("link").textContent = res.ok || res.status === 409 ? "● LINK" : "○ LINK";
  $("link").className = "link " + (res.ok || res.status === 409 ? "up" : "down");
  if (!res.ok && res.status !== 409) throw new Error(doc.error || `${res.status}`);
  return doc;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ------------------------------------------------------------------ tooltip
const tip = $("tip");
function showTip(e, text) {
  tip.textContent = text; tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + w > innerWidth - 8) x = e.clientX - w - 14;
  if (y + h > innerHeight - 8) y = e.clientY - h - 14;
  tip.style.left = `${Math.max(8, x)}px`; tip.style.top = `${Math.max(8, y)}px`;
}
function hideTip() { tip.hidden = true; }

// ------------------------------------------------------------------ boot
async function boot() {
  const screen = $("boot");
  let seen = false;
  try { seen = sessionStorage.getItem("almanac.booted") === "1"; } catch { /* ignore */ }
  if (reduced || seen) { screen.remove(); return; }
  try { sessionStorage.setItem("almanac.booted", "1"); } catch { /* ignore */ }
  let skip = false;
  const stop = () => { skip = true; };
  addEventListener("keydown", stop, { once: true }); screen.addEventListener("click", stop, { once: true });
  const lines = ["ALMANAC MISSION CONTROL v2", "(c) " + new Date().getFullYear(), "",
    "> door " + location.port + " ............ ".slice(location.port.length), "> scanning claude.exe .. ", "> reading transcripts .. ", "> telemetry ............ ", "", "READY."];
  const fleet = api("/api/fleet").catch(() => null);
  for (const [i, line] of lines.entries()) {
    for (const ch of line) { if (skip) break; screen.textContent += ch; await new Promise((r) => setTimeout(r, 9)); }
    if (skip) break;
    if (i === 3) screen.textContent += "OK";
    if (i === 4) { const f = await fleet; screen.textContent += f ? `${f.processes.length} found` : "FAIL"; }
    if (i === 5 || i === 6) screen.textContent += "OK";
    screen.textContent += "\n";
    await new Promise((r) => setTimeout(r, 70));
  }
  screen.classList.add("done");
  setTimeout(() => screen.remove(), 500);
}

// ------------------------------------------------------------------ fleet
const ROLE = { session: "", headless: "claude -p", desktop: "Claude desktop", daemon: "claude daemon", "pty-host": "bg pty host", mcp: "mcp server", other: "claude.exe" };

function rowName(p) {
  // a name you gave the session wins; otherwise the chat's name (your /rename, Haiku's, or Claude's auto title)
  if (p.role === "session") return (p.name_source === "user" && p.name) || p.title || p.name || (p.cwd || "").split(/[\\/]/).pop() || "session";
  return ROLE[p.role] + (p.helpers ? ` (+${p.helpers})` : "");
}
function rowState(p) {
  if (p.role !== "session") return el("span", { cls: "dim", text: p.role === "desktop" ? "app" : p.role });
  if (active(p)) return el("span", { cls: "state-busy" }, el("span", { cls: "spin", "data-spin": "1", text: SPIN[state.frame % SPIN.length] }), ` ${p.status.toUpperCase()}`);
  return el("span", { cls: "state-idle", text: `· ${p.status || "starting"}` });
}

async function loadFleet() {
  let doc;
  try { doc = await api("/api/fleet"); } catch (err) { $("fleet-summary").textContent = `fleet unavailable: ${err.message}`; return; }
  state.fleet = doc;
  const s = doc.summary || {};
  $("fleet-summary").replaceChildren(
    el("span", { cls: "hi", text: `${s.sessions ?? 0} sessions` }), ` · `,
    el("span", { cls: s.busy ? "amber" : "dim", text: `${s.busy ?? 0} busy` }), ` · ${s.idle ?? 0} idle · `,
    el("span", { cls: s.daemon ? "hi" : "dim", text: `daemon ${s.daemon ? "▲" : "▽"}` }), " · ",
    el("span", { cls: s.desktop ? "hi" : "dim", text: `desktop ${s.desktop ? "▲" : "▽"}` }),
    ` · ${s.headless ?? 0} headless · cpu ${num(s.cpu)}% · mem ${num((s.mem_mb || 0) / 1024)}G · ${doc.processes.length} procs`);
  const shown = doc.processes.filter((p) => p.role !== "desktop-helper");
  const live = new Set(shown.map((p) => `${p.pid}:${p.created}`));
  for (const p of shown) {
    const key = `${p.pid}:${p.created}`;
    const hist = state.cpu.get(key) || [];
    hist.push(p.cpu ?? 0); if (hist.length > 12) hist.shift();
    state.cpu.set(key, hist);
  }
  for (const k of [...state.cpu.keys()]) if (!live.has(k)) state.cpu.delete(k);
  const stale = [...state.pins].filter((k) => !live.has(k));
  if (stale.length) { stale.forEach((k) => state.pins.delete(k)); put("pins", JSON.stringify([...state.pins])); }
  const body = $("fleet-table").querySelector("tbody");
  const first = state.seen.size === 0;
  body.replaceChildren(...shown.map((p) => {
    const key = `${p.pid}:${p.created}`;
    const pinned = state.pins.has(key);
    const pin = el("td", { cls: `pincell ${pinned ? "pinned" : "unpinned"}`, text: p.role === "session" ? (pinned ? "◆" : "◇") : "", title: p.role === "session" ? (pinned ? "unpin from the gallery" : "pin to the gallery") : null });
    if (p.role === "session") pin.addEventListener("click", (e) => { e.stopPropagation(); togglePin(p); loadFleet(); });
    const tr = el("tr", { cls: [p.role !== "session" ? "helper" : "", p.pid === state.sel ? "sel" : "", !first && !state.seen.has(key) && !reduced ? "fresh" : ""].join(" ").trim(), "data-pid": p.pid, tabindex: -1 },
      pin, el("td", { text: String(p.pid) }), el("td", { text: rowName(p).slice(0, 34), title: p.cwd || p.cmd }),
      el("td", { cls: "model", text: p.role === "session" ? fmtModel(p.model) : "", title: p.model || null }),
      el("td", {}, p.role === "session" ? effortBadge(p.effort) : ""),
      el("td", {}, rowState(p)), el("td", { cls: "r", text: p.cpu === null ? "–" : `${num(p.cpu)}%` }),
      el("td", { cls: "spark", text: spark(state.cpu.get(key), Math.max(5, ...state.cpu.get(key))) }),
      el("td", { cls: "r", text: `${p.mem_mb}M` }), el("td", { cls: "r", text: age(p.started) }));
    tr.addEventListener("click", () => select(p.pid));
    return tr;
  }));
  state.seen = live;
  $("fleet-empty").hidden = shown.length > 0;
  if (state.sel && !doc.processes.some((p) => p.pid === state.sel && p.created === state.selCreated)) {
    $("act-msg").textContent = `pid ${state.sel} is gone`;
    $("say").hidden = true;
  }
}

function select(pid) {
  if (state.sel !== pid) { state.feedKeys = new Set(); state.confirmKill = false; $("feed").replaceChildren(); $("act-msg").textContent = ""; }
  state.sel = pid;
  const p = state.fleet?.processes.find((x) => x.pid === pid);
  state.selCreated = p?.created ?? null;
  document.querySelectorAll("#fleet-table tbody tr").forEach((tr) => tr.classList.toggle("sel", Number(tr.dataset.pid) === pid));
  loadDetail();
}

async function loadDetail() {
  if (!state.sel) return;
  let doc;
  try { doc = await api(`/api/session?pid=${state.sel}`); } catch (err) { $("detail-head").replaceChildren(el("span", { cls: "red", text: err.message })); return; }
  state.detail = doc;
  const p = doc.process, u = doc.usage;
  $("detail").setAttribute("data-title", `SESSION ${p.pid}`);
  const kv = el("dl", { cls: "kv" });
  const add = (k, v, cls) => { if (v) kv.append(el("dt", { text: k }), el("dd", { cls, text: v, title: v })); };
  add("role", p.role + (p.kind && p.kind !== "interactive" ? ` (${p.kind})` : ""));
  const meta = doc.meta || {};
  if (meta.model || meta.effort) { kv.append(el("dt", { text: "model" }), el("dd", {}, el("span", { cls: "model", text: fmtModel(meta.model), title: meta.model }), "  ", effortBadge(meta.effort))); }
  add("state", p.status ? `${p.status} · ${age(p.updated)} ago` : null, active(p) ? "amber" : "");
  add("folder", p.cwd); add("session", p.session_id); add("version", p.version);
  add("parent", p.parent ? `${p.parent} (${p.ppid})` : null); add("command", p.role === "session" ? null : p.cmd);
  if (u && u.messages) add("tokens", `${num(u.output, 0)} out · ${num(u.input + u.cache_create + u.cache_read, 0)} context · ${num(u.calls, 0)} tool calls · ${num(u.read_back_kb, 0)} KB back`);
  $("detail-head").replaceChildren(el("div", { cls: "title", text: rowName(p) }), kv);

  const feed = $("feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  if (!doc.events.length) feed.replaceChildren(el("li", { cls: "result", text: p.session_id ? "no transcript yet" : "no session transcript for this process" }));
  else {
    const items = doc.events.map((e, i) => {
      const key = `${e.ts}|${e.kind}|${e.text.slice(0, 40)}|${i - doc.events.length}`;
      const li = el("li", { cls: `${e.kind}${state.feedKeys.size && !state.feedKeys.has(key) && !reduced ? " new" : ""}`, text: e.text, title: e.ts ? new Date(e.ts).toLocaleString() : null });
      li.dataset.key = key;
      return li;
    });
    state.feedKeys = new Set(items.map((li) => li.dataset.key));
    feed.replaceChildren(...items);
    if (atBottom || feed.dataset.pid !== String(p.pid)) feed.scrollTop = feed.scrollHeight;
  }
  feed.dataset.pid = String(p.pid);
  $("say").hidden = !p.can_send;
  $("say-text").placeholder = active(p) ? "busy: your line queues until it is free" : "type a line, enter sends it to this session";
  $("actions").hidden = false;
  const remote = $("remote");
  remote.hidden = !p.remote_url; if (p.remote_url) remote.href = p.remote_url;
  $("kill").hidden = !p.can_kill;
  if (!state.confirmKill) $("kill").textContent = "[KILL ✕]";
  $("pin").hidden = p.role !== "session";
  $("pin").textContent = state.pins.has(keyOf(p)) ? "[UNPIN ◆]" : "[PIN ◇]";
}
$("pin").addEventListener("click", () => { const p = state.detail?.process; if (p) { togglePin(p); loadDetail(); loadFleet(); } });

async function kill() {
  const p = state.detail?.process;
  if (!p || !p.can_kill) return;
  if (!state.confirmKill) {
    state.confirmKill = true;
    $("kill").textContent = `[TERMINATE ${p.pid}? y/N]`; $("kill").classList.add("confirm");
    $("act-msg").textContent = "press y or click again to end it and its children";
    return;
  }
  state.confirmKill = false; $("kill").classList.remove("confirm");
  const tr = document.querySelector(`#fleet-table tr[data-pid="${p.pid}"]`);
  if (tr) tr.classList.add("dying");
  let bar = 0;
  const anim = setInterval(() => { bar = (bar + 1) % 9; $("kill").textContent = `[${"▓".repeat(bar)}${"░".repeat(8 - bar)}]`; }, 60);
  try {
    const r = await post("/api/kill", { pid: p.pid, created: p.created });
    $("act-msg").textContent = r.ok ? `terminated: ${r.detail}` : `refused: ${r.error}`;
  } catch (err) { $("act-msg").textContent = `failed: ${err.message}`; }
  clearInterval(anim); $("kill").textContent = "[KILL ✕]";
  await loadFleet();
}
function cancelKill() { if (state.confirmKill) { state.confirmKill = false; $("kill").classList.remove("confirm"); $("kill").textContent = "[KILL ✕]"; $("act-msg").textContent = "kept"; } }

$("say").addEventListener("submit", async (e) => {
  e.preventDefault();
  const p = state.detail?.process, input = $("say-text"), text = input.value.trim();
  if (!p || !text) return;
  input.disabled = true; $("act-msg").textContent = "typing…";
  try {
    const r = await post("/api/send", { pid: p.pid, created: p.created, text });
    $("act-msg").textContent = r.ok ? "sent ✓" : `refused: ${r.error}`;
    if (r.ok) { input.value = ""; $("feed").append(el("li", { cls: "you new", text: text + "  (typed)" })); $("feed").scrollTop = $("feed").scrollHeight; }
  } catch (err) { $("act-msg").textContent = `failed: ${err.message}`; }
  input.disabled = false; input.focus();
  setTimeout(loadDetail, 1500);
});
$("kill").addEventListener("click", kill);

// ------------------------------------------------------------------ gallery
// Tiles are built once per process and updated in place, so a half-typed line, a tile's scroll position
// and keyboard focus survive the 2 s refresh.
const LIMITS = { s: 15, m: 30, l: 60 };

function gallerySessions() {
  const all = (state.fleet?.processes || []).filter((p) => p.role === "session");
  return state.gMode === "pinned" ? all.filter((p) => state.pins.has(keyOf(p))) : all;
}

function makeTile(p) {
  const key = keyOf(p);
  const box = el("div", { cls: `box${reduced ? "" : " tile-enter"}`, "data-key": key });
  const head = el("div", { cls: "thead pad" });
  const feed = el("ol", { cls: "feed", "aria-live": "polite" });
  const input = el("input", { maxlength: "4000", placeholder: "say something to this session", "aria-label": `message to pid ${p.pid}` });
  const form = el("form", { cls: "say", autocomplete: "off" }, el("span", { cls: "hi", text: "say>" }), input, el("button", { type: "submit", text: "[SEND]" }));
  const msg = el("span", { cls: "dim" });
  const remote = el("a", { cls: "btn", target: "_blank", rel: "noopener", text: "[REMOTE ↗]", hidden: true });
  const pin = el("button", { cls: "btn" });
  const wide = el("button", { cls: "btn" });
  const open = el("button", { cls: "btn", text: "[FLEET ▸]", title: "open in the fleet view" });
  const kill = el("button", { cls: "btn danger", text: "[KILL ✕]" });
  box.append(head, feed, form, el("div", { cls: "actions pad" }, remote, pin, wide, open, kill, msg));
  const t = { box, head, feed, input, msg, remote, pin, wide, kill, p, feedSig: "", armed: 0 };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.disabled = true; msg.textContent = "typing…";
    try {
      const r = await post("/api/send", { pid: t.p.pid, created: t.p.created, text });
      msg.textContent = r.ok ? "sent ✓" : `refused: ${r.error}`;
      if (r.ok) { input.value = ""; feed.append(el("li", { cls: "you new", text: `${text}  (typed)` })); feed.scrollTop = feed.scrollHeight; }
    } catch (err) { msg.textContent = `failed: ${err.message}`; }
    input.disabled = false; input.focus();
  });
  pin.addEventListener("click", () => { togglePin(t.p); paintTile(t, null); if (state.gMode === "pinned") loadGallery(); });
  wide.addEventListener("click", () => {
    state.wide.has(key) ? state.wide.delete(key) : state.wide.add(key);
    put("wide", JSON.stringify([...state.wide])); paintTile(t, null);
  });
  open.addEventListener("click", () => { show("fleet"); select(t.p.pid); });
  kill.addEventListener("click", async () => {
    if (Date.now() - t.armed > 4000) {          // first click arms for four seconds
      t.armed = Date.now(); kill.textContent = "[SURE? ✕]"; kill.classList.add("confirm");
      setTimeout(() => { if (Date.now() - t.armed >= 4000) { kill.textContent = "[KILL ✕]"; kill.classList.remove("confirm"); } }, 4100);
      return;
    }
    t.armed = 0; kill.classList.remove("confirm"); kill.textContent = "[▓▓▓▒░]";
    try {
      const r = await post("/api/kill", { pid: t.p.pid, created: t.p.created });
      msg.textContent = r.ok ? "terminated" : `refused: ${r.error}`;
    } catch (err) { msg.textContent = `failed: ${err.message}`; }
    kill.textContent = "[KILL ✕]";
    await loadFleet(); loadGallery();
  });
  return t;
}

function paintTile(t, doc) {
  const p = doc?.process || t.p;
  t.p = p;
  const key = keyOf(p), meta = doc?.meta || t.meta || {};
  t.meta = meta;
  t.box.setAttribute("data-title", `${p.pid} ${rowName(p)}`.slice(0, 60));
  t.box.classList.toggle("busy", active(p));
  t.box.classList.toggle("wide", state.wide.has(key));
  const u = doc?.usage || t.usage;
  t.usage = u;
  t.head.replaceChildren(...[      // append(null) would print "null": drop the empty slots
    rowState(p), el("span", { cls: "model", text: fmtModel(meta.model), title: meta.model || "no assistant reply yet" }), effortBadge(meta.effort),
    el("span", { cls: "dim", text: `cpu ${p.cpu === null ? "–" : num(p.cpu) + "%"} · ${p.mem_mb}M · up ${age(p.started)}` }),
    u && u.messages ? el("span", { cls: "dim", text: `· ${num(u.output, 0)} out tok` }) : null,
    el("span", { cls: "dim where", text: p.cwd || "", title: p.cwd || "" })].filter(Boolean));
  t.pin.textContent = state.pins.has(key) ? "[UNPIN ◆]" : "[PIN ◇]";
  t.wide.textContent = state.wide.has(key) ? "[NARROW ⇥]" : "[WIDE ⇔]";
  t.remote.hidden = !p.remote_url;
  if (p.remote_url) t.remote.href = p.remote_url;
  t.input.placeholder = active(p) ? "busy: your line queues until it is free" : "say something to this session";
  if (!doc) return;
  const events = doc.events || [];
  const sig = `${events.length}|${events.length ? events[events.length - 1].ts + events[events.length - 1].text.slice(0, 30) : ""}`;
  if (sig === t.feedSig) return;
  const fresh = t.feedSig !== "";
  t.feedSig = sig;
  const atBottom = t.feed.scrollHeight - t.feed.scrollTop - t.feed.clientHeight < 40;
  const prev = new Set([...t.feed.children].map((li) => li.dataset.k));
  t.feed.replaceChildren(...(events.length ? events.map((e) => {
    const k = `${e.ts}|${e.kind}|${e.text.slice(0, 40)}`;
    const li = el("li", { cls: `${e.kind}${fresh && !prev.has(k) && !reduced ? " new" : ""}`, text: e.text, title: e.ts ? new Date(e.ts).toLocaleString() : null });
    li.dataset.k = k;
    return li;
  }) : [el("li", { cls: "result", text: "no transcript yet" })]));
  if (atBottom || !fresh) t.feed.scrollTop = t.feed.scrollHeight;
}

async function loadGallery() {
  const grid = $("tiles-grid");
  grid.dataset.cols = state.gCols; grid.dataset.height = state.gHeight;
  for (const [id, v] of [["g-mode", state.gMode], ["g-cols", state.gCols], ["g-height", state.gHeight]])
    document.querySelectorAll(`#${id} button`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
  const list = gallerySessions();
  const all = (state.fleet?.processes || []).filter((p) => p.role === "session").length;
  $("g-count").textContent = `${list.length} tile${list.length === 1 ? "" : "s"} · ${state.pins.size} pinned · ${all} sessions running`;
  $("g-empty").hidden = list.length > 0;
  $("g-empty").textContent = state.gMode === "pinned" ? "nothing pinned: click ◇ on a fleet row, press p, or use [PIN ◇] on a tile" : "no Claude sessions running";
  const keys = new Set(list.map(keyOf));
  for (const [k, t] of state.tiles) if (!keys.has(k)) { t.box.remove(); state.tiles.delete(k); }
  for (const p of list) if (!state.tiles.has(keyOf(p))) state.tiles.set(keyOf(p), makeTile(p));
  const order = list.map((p) => state.tiles.get(keyOf(p)).box);
  if (order.some((box, i) => grid.children[i] !== box)) grid.append(...order);   // moves only when the order changed
  for (const p of list) paintTile(state.tiles.get(keyOf(p)), { process: p });
  if (!list.length) return;
  let doc;
  try { doc = await api(`/api/sessions?pids=${list.map((p) => p.pid).join(",")}&limit=${LIMITS[state.gHeight] || 30}`); } catch { return; }
  for (const d of doc.sessions) {
    if (d.gone) continue;
    const t = state.tiles.get(keyOf(d.process));
    if (t) paintTile(t, d);
  }
}
for (const [id, field] of [["g-mode", "gMode"], ["g-cols", "gCols"], ["g-height", "gHeight"]]) {
  $(id).addEventListener("click", (e) => {
    if (!e.target.dataset.v) return;
    state[field] = e.target.dataset.v; put(id, state[field]);
    if (field === "gHeight") for (const t of state.tiles.values()) t.feedSig = "x";
    loadGallery();
  });
}

// ------------------------------------------------------------------ chats: every chat ever kept, resumable
const chatState = { list: [], sel: null, armed: 0, timer: 0 };
const NAMED_BY = { renamed: "you (/rename)", haiku: "Haiku", auto: "Claude's auto title", prompt: "its first prompt" };
const usd = (v) => v === null || v === undefined ? "–" : v < 0.01 ? "<$0.01" : `$${v.toFixed(2)}`;
const ktok = (n) => !n ? "–" : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}k`;
function when(sec) {
  if (!sec) return "–";
  const d = new Date(sec * 1000), today = new Date();
  return d.toDateString() === today.toDateString() ? d.toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" });
}
function kept(c) {
  if (c.live_pid) return el("span", { cls: "kept-live", text: `live ${c.live_pid}` });
  if (c.on_disk && c.archived) return el("span", { cls: "kept-both", text: "disk+archive" });
  if (c.archived) return el("span", { cls: "kept-archive", title: "Claude Code deleted it; Almanac's copy restores it on resume", text: "archive only" });
  if (c.on_disk) return el("span", { cls: "dim", text: "disk" });
  return el("span", { cls: "red", text: "gone" });
}

async function loadChats() {
  let doc;
  const q = $("chat-q").value.trim();
  try { doc = await api(`/api/chats?limit=500${q ? `&q=${encodeURIComponent(q)}` : ""}`); }
  catch (err) { $("chat-count").textContent = `chats unavailable: ${err.message}`; return; }
  chatState.list = doc.chats;
  const a = doc.archive || {};
  $("chat-count").textContent = `${doc.chats.length} chat${doc.chats.length === 1 ? "" : "s"}${q ? " match" : " kept"}` +
    (a.at ? ` · archive ${num(a.files, 0)} files, synced ${clock(a.at)}` : " · archive syncing…");
  const body = $("chat-table").querySelector("tbody");
  body.replaceChildren(...doc.chats.map((c) => {
    const tr = el("tr", { cls: c.id === chatState.sel ? "sel" : "", "data-id": c.id },
      el("td", { text: when(c.last), title: c.last ? new Date(c.last * 1000).toLocaleString() : null }),
      el("td", { cls: "title", title: `named by ${NAMED_BY[c.title_source] || "nothing yet"}\n${c.first_prompt || ""}` },
        el("span", { cls: c.title_source === "haiku" ? "hi" : c.title_source === "renamed" ? "cyan" : "dim", text: c.title_source === "haiku" ? "✦ " : c.title_source === "renamed" ? "◆ " : "· " }), c.title),
      el("td", { cls: "folder", text: (c.cwd || "").split(/[\\/]/).slice(-2).join("/"), title: c.cwd }),
      el("td", { cls: "model", text: fmtModel(c.model) }), el("td", { cls: "r", text: num(c.replies, 0) }),
      el("td", { cls: "r", text: ktok(c.context) }), el("td", { cls: "r", text: usd(c.resume_usd) }), el("td", {}, kept(c)));
    tr.addEventListener("click", () => openChat(c.id));
    return tr;
  }));
}

async function openChat(id) {
  chatState.sel = id; chatState.armed = 0;
  document.querySelectorAll("#chat-table tbody tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.id === id));
  $("chat-msg").textContent = ""; $("resume").textContent = "[RESUME ▸]"; $("resume").classList.remove("confirm");
  let doc;
  try { doc = await api(`/api/chat?id=${encodeURIComponent(id)}&limit=300`); }
  catch (err) { $("chat-head").replaceChildren(el("span", { cls: "red", text: err.message })); return; }
  const c = doc.chat;
  $("chat-detail").setAttribute("data-title", `CHAT ${c.id.slice(0, 8)}`);
  const kv = el("dl", { cls: "kv" });
  const add = (k, v, node) => { if (v || node) kv.append(el("dt", { text: k }), node || el("dd", { text: v, title: v })); };
  add("model", null, el("dd", {}, el("span", { cls: "model", text: fmtModel(c.model), title: c.model }), "  ", effortBadge(c.effort)));
  add("folder", c.cwd);
  add("when", `${c.first ? new Date(c.first * 1000).toLocaleString() : "?"} → ${c.last ? new Date(c.last * 1000).toLocaleString() : "?"}`);
  add("size", `${num(c.replies, 0)} replies · ${num(c.output, 0)} output tokens`);
  add("resume", `re-sends ${num(c.context, 0)} context tokens; ≥ ${usd(c.resume_usd)} at the input rate, more as a cache write`);
  add("session", c.id);
  add("kept", null, el("dd", {}, kept(c), el("span", { cls: "dim", text: doc.read_from ? `  (reading the ${doc.read_from})` : "" })));
  $("chat-head").replaceChildren(el("div", { cls: "title", text: c.title }), kv);
  const feed = $("chat-feed");
  feed.replaceChildren(...(doc.events.length ? doc.events.map((e) => el("li", { cls: e.kind, text: e.text, title: e.ts ? new Date(e.ts).toLocaleString() : null }))
    : [el("li", { cls: "result", text: "nothing readable in this transcript" })]));
  feed.scrollTop = feed.scrollHeight;
  $("chat-actions").hidden = false;
  $("resume").disabled = !!c.live_pid;
  if (c.live_pid) $("chat-msg").textContent = `running now as pid ${c.live_pid}: open it in FLEET or GALLERY`;
  chatState.chat = c;
}

$("resume").addEventListener("click", async () => {
  const c = chatState.chat;
  if (!c || c.live_pid) return;
  if (Date.now() - chatState.armed > 5000) {        // first click arms for five seconds and says what it costs
    chatState.armed = Date.now();
    $("resume").textContent = `[RESUME ≥ ${usd(c.resume_usd)}? click again]`; $("resume").classList.add("confirm");
    setTimeout(() => { if (Date.now() - chatState.armed >= 5000) { $("resume").textContent = "[RESUME ▸]"; $("resume").classList.remove("confirm"); } }, 5100);
    return;
  }
  chatState.armed = 0; $("resume").classList.remove("confirm"); $("resume").textContent = "[STARTING…]";
  try {
    const r = await post("/api/resume", { id: c.id, remote: $("resume-remote").checked });
    $("chat-msg").textContent = r.ok ? `${r.detail}. It shows up in FLEET and GALLERY in a few seconds.` : `refused: ${r.error}`;
  } catch (err) { $("chat-msg").textContent = `failed: ${err.message}`; }
  $("resume").textContent = "[RESUME ▸]";
  setTimeout(loadChats, 4000);
});
$("chat-q").addEventListener("input", () => { clearTimeout(chatState.timer); chatState.timer = setTimeout(loadChats, 250); });
async function nameStatus() {
  try {
    const n = await api("/api/names");
    $("name-status").textContent = n.running ? `naming… ${n.done} named, ${n.failed} failed, $${(n.usd || 0).toFixed(3)} so far`
      : n.done || n.failed ? `last run: ${n.done} named, ${n.failed} failed, $${(n.usd || 0).toFixed(3)}` : "";
    if (n.running) setTimeout(() => { nameStatus(); loadChats(); }, 3000);
  } catch { /* the link light already shows trouble */ }
}
$("name-all").addEventListener("click", async () => {
  try { const r = await post("/api/name", { all: true }); $("name-status").textContent = `${r.queued} chats to name…`; }
  catch (err) { $("name-status").textContent = `failed: ${err.message}`; return; }
  setTimeout(nameStatus, 1500);
});

// ------------------------------------------------------------------ telemetry
function cells(values, fmt, labels, max) {
  const top = max ?? Math.max(0, ...values.filter((v) => v !== null && v !== undefined));
  const box = el("span", { cls: "cells" });
  values.forEach((v, i) => {
    const none = v === null || v === undefined;
    const c = el("span", { cls: none ? "none" : "", text: none ? "·" : SPARK[top > 0 ? Math.min(7, Math.round((v / top) * 7)) : 0] });
    c.addEventListener("pointermove", (e) => showTip(e, `${labels[i]}  ${none ? "no data" : fmt(v)}`));
    c.addEventListener("pointerleave", hideTip);
    box.append(c);
  });
  return box;
}
function tableView(head, rows) {
  const t = el("table", {}, el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { text: h })))));
  t.append(el("tbody", {}, ...rows.map((r) => el("tr", {}, ...r.map((c) => el("td", { text: c }))))));
  return el("details", {}, el("summary", { text: "table view" }), el("div", { cls: "scroll" }, t));
}
function countUp(node, target, fmt) {
  if (reduced || target === null || target === undefined) { node.textContent = fmt(target); return; }
  const t0 = performance.now();
  const step = (t) => { const k = Math.min(1, (t - t0) / 700); node.textContent = fmt(target * (1 - Math.pow(1 - k, 3))); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}

function drawReport(r) {
  const labels = r.series.map((s) => s.bucket);
  $("tiles").replaceChildren(...r.tiles.map((t) => {
    const fmt = (v) => v === null || v === undefined ? "–" : t.unit === "%" ? pct(v) : t.unit === "×" ? `${num(v, 0)}×` : `${num(v, 2)} ${t.unit}`;
    const big = el("div", { cls: "big" });
    countUp(big, t.value, fmt);
    let delta = el("div", { cls: "dim", text: "no earlier period yet" });
    if (t.value !== null && t.previous) {
      const ch = (t.value - t.previous) / Math.abs(t.previous), better = t.better === "down" ? ch < 0 : ch > 0, flat = Math.abs(ch) < 0.02;
      delta = el("div", {}, el("span", { cls: flat ? "dim" : better ? "good" : "bad", text: flat ? "= flat" : `${ch > 0 ? "▲" : "▼"} ${Math.abs(Math.round(ch * 100))}% ${better ? "better" : "worse"}` }),
        el("span", { cls: "dim", text: ` vs ${fmt(t.previous)}` }));
    }
    return el("div", { cls: "box tile", "data-title": t.label.toUpperCase() }, el("div", { cls: "pad" }, big, delta));
  }));

  const trend = [["KB back / call", r.series.map((s) => s.kb_per_call), (v) => `${num(v, 2)} KB`],
    ["context / output", r.series.map((s) => s.ctx_per_out), (v) => `${num(v, 0)}×`],
    ["shell wait (min)", r.series.map((s) => s.shell_min), (v) => `${num(v, 1)} min`],
    ["tool calls", r.series.map((s) => s.calls), (v) => num(v, 0)],
    ["output tokens", r.series.map((s) => s.output_tokens), (v) => num(v, 0)]];
  const rows = el("div", { cls: "rows" });
  for (const [name, vals, fmt] of trend) {
    const last = [...vals].reverse().find((v) => v !== null && v !== undefined);
    rows.append(el("span", { cls: "lbl", text: name }), el("span", { cls: "val", text: fmt(last ?? null) }), cells(vals, fmt, labels), el("span", { cls: "dim", text: "latest" }));
  }
  $("trends").replaceChildren(rows, tableView(["bucket", ...trend.map((t) => t[0])], labels.map((b, i) => [b, ...trend.map(([, v, f]) => v[i] === null ? "–" : f(v[i]))])));

  const fams = r.families.slice(0, 12), maxCalls = Math.max(1, ...fams.map((f) => f.calls));
  const frows = el("div", { cls: "rows" });
  for (const f of fams) {
    const per = r.series.map((s) => s.families[f.family] || 0);
    frows.append(el("span", { cls: "lbl", text: f.family }), el("span", { cls: "val", text: num(f.calls, 0) }),
      el("span", { cls: "meter", text: "█".repeat(Math.max(1, Math.round((f.calls / maxCalls) * 24))) }), cells(per, (v) => `${num(v, 0)} calls`, labels));
  }
  $("families").replaceChildren(el("div", { cls: "dim", text: "purpose        calls  share of busiest            per bucket" }), frows,
    tableView(["purpose", "calls", "previous", "median wait", "total wait", "KB back", "KB/call"],
      r.families.map((f) => [f.family, num(f.calls, 0), num(f.previous, 0), f.median_s === null ? "–" : `${num(f.median_s)} s`, `${num(f.total_min)} min`, num(f.out_kb, 0), f.kb_per_call === null ? "–" : num(f.kb_per_call, 2)])));

  const arows = el("div", { cls: "rows" });
  for (const [key, name] of PAIRS) {
    const vals = r.series.map((s) => s.adoption[key]);
    const last = [...vals].reverse().find((v) => v !== null && v !== undefined);
    arows.append(el("span", { cls: "lbl", text: name }), el("span", { cls: "val", text: pct(last ?? null) }), meter(last ?? null), cells(vals, pct, labels, 1));
  }
  $("adoption").replaceChildren(arows, el("p", { cls: "dim", text: "share of each habit done the new way, per bucket; · = habit not used that bucket" }));

  const tmax = Math.max(1, ...r.tools.map((t) => t.calls));
  $("tools").replaceChildren(el("div", { cls: "rows" }, ...r.tools.flatMap((t) => [
    el("span", { cls: "lbl", text: t.tool.replace(/^mcp__/, "").slice(0, 28) }), el("span", { cls: "val", text: num(t.calls, 0) }),
    el("span", { cls: "meter", text: "█".repeat(Math.max(1, Math.round((t.calls / tmax) * 24))) }), el("span", { cls: "dim", text: `${num(t.out_kb, 0)} KB back` })])));

  const c = r.coverage, last = r.last_ingest;
  $("coverage").textContent = `${num(c.calls, 0)} calls · ${num(c.sessions, 0)} sessions kept since ${c.first ? new Date(c.first * 1000).toLocaleDateString() : "–"} · ` +
    `${num(c.legacy_calls, 0)} from a seeded inventory of pruned transcripts (no sizes, waits or tokens)`;
  $("ingest-status").textContent = last ? `read ${clock(last.at)} · ${last.files} files · ${last.read} changed` : "waiting for first read";
}

async function loadReport() {
  document.querySelectorAll("#days button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state.days)));
  document.querySelectorAll("#bucket button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state.bucket)));
  try { state.report = await api(`/api/report?days=${state.days}&bucket=${state.bucket}`); drawReport(state.report); }
  catch (err) { $("ingest-status").textContent = `report unavailable: ${err.message}`; }
}
$("days").addEventListener("click", (e) => {
  if (!e.target.dataset.v) return;
  state.days = e.target.dataset.v; put("days", state.days);
  if (state.days === "90" || state.days === "0") { state.bucket = "week"; put("bucket", "week"); }
  loadReport();
});
$("bucket").addEventListener("click", (e) => { if (e.target.dataset.v) { state.bucket = e.target.dataset.v; put("bucket", state.bucket); loadReport(); } });
$("rescan").addEventListener("click", async () => {
  $("rescan").textContent = "[READING…]";
  try { await post("/api/ingest", {}); } catch { /* shown by the report */ }
  $("rescan").textContent = "[RESCAN]"; loadReport();
});

// ------------------------------------------------------------------ log
async function loadLog() {
  let doc;
  try { doc = await api("/api/actions?limit=100"); } catch (err) { $("log-list").replaceChildren(el("li", { cls: "red", text: err.message })); return; }
  $("log-list").replaceChildren(...(doc.actions.length ? doc.actions.map((a) => el("li", {},
    el("span", { cls: "dim", text: `${new Date(a.ts * 1000).toLocaleString([], { hour12: false })} ` }),
    el("span", { cls: a.action === "kill" ? "red" : "cyan", text: a.action.toUpperCase().padEnd(5) }),
    el("span", { cls: a.ok ? "hi" : "red", text: a.ok ? " ok   " : " FAIL " }),
    `pid ${a.pid} ${a.name ? `(${a.name}) ` : ""}by ${a.who || "?"}: ${a.detail}`))
    : [el("li", { cls: "dim", text: "nothing killed or said yet" })]));
}

// ------------------------------------------------------------------ tabs, keys, loops
function show(tab) {
  state.tab = tab;
  document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  for (const id of ["fleet", "gallery", "chats", "telemetry", "log"]) $(id).hidden = id !== tab;
  if (tab === "chats") { loadChats(); nameStatus(); }
  if (tab === "telemetry") loadReport();
  if (tab === "log") loadLog();
  if (tab === "fleet") loadFleet();
  if (tab === "gallery") loadFleet().then(loadGallery);
}
document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
$("help-btn").addEventListener("click", () => { $("help").hidden = !$("help").hidden; });
$("help").addEventListener("click", () => { $("help").hidden = true; });

addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") { if (e.key === "Escape") e.target.blur(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const rows = [...document.querySelectorAll("#fleet-table tbody tr")];
  const idx = rows.findIndex((r) => Number(r.dataset.pid) === state.sel);
  if (state.confirmKill && e.key.toLowerCase() === "y") { e.preventDefault(); kill(); return; }
  if (state.confirmKill && e.key !== "x") cancelKill();
  switch (e.key) {
    case "1": show("fleet"); break;
    case "2": show("gallery"); break;
    case "3": show("chats"); break;
    case "4": show("telemetry"); break;
    case "5": show("log"); break;
    case "p": if (state.tab === "fleet" && state.detail?.process?.role === "session") { togglePin(state.detail.process); loadDetail(); loadFleet(); } break;
    case "?": $("help").hidden = !$("help").hidden; break;
    case "Escape": $("help").hidden = true; break;
    case "r": state.tab === "chats" ? loadChats() : state.tab === "telemetry" ? loadReport() : state.tab === "log" ? loadLog() : state.tab === "gallery" ? loadFleet().then(loadGallery) : loadFleet(); break;
    case "j": case "ArrowDown": if (state.tab === "fleet" && rows.length) { e.preventDefault(); select(Number(rows[Math.min(rows.length - 1, idx + 1)].dataset.pid)); } break;
    case "k": case "ArrowUp": if (state.tab === "fleet" && rows.length) { e.preventDefault(); select(Number(rows[Math.max(0, idx - 1)].dataset.pid)); } break;
    case "Enter": if (state.tab === "fleet" && state.sel) loadDetail(); break;
    case "/": if (state.tab === "chats") { e.preventDefault(); $("chat-q").focus(); } else if (!$("say").hidden) { e.preventDefault(); $("say-text").focus(); } break;
    case "x": if (state.tab === "fleet") kill(); break;
    default: break;
  }
});

setInterval(() => {
  $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false });
}, 1000);
if (!reduced) setInterval(() => {
  state.frame += 1;
  document.querySelectorAll("[data-spin]").forEach((n) => { n.textContent = SPIN[state.frame % SPIN.length]; });
}, 90);
setInterval(() => {
  if (document.hidden) return;
  if (state.tab === "fleet") { loadFleet(); loadDetail(); }
  if (state.tab === "gallery") loadFleet().then(loadGallery);
}, 2000);
setInterval(() => { if (!document.hidden && state.tab === "log") loadLog(); }, 5000);
setInterval(() => { if (!document.hidden && state.tab === "telemetry") loadReport(); }, 5 * 60 * 1000);

boot();
loadFleet().then(() => {
  const busy = state.fleet?.processes.find((p) => p.role === "session");
  if (busy && !state.sel) select(busy.pid);
});
