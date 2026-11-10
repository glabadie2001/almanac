"use strict";
// Almanac cross-model mission control over one loopback door:
//   FLEET      every Claude & Gemini agent process (GET /api/fleet), spawn + squad launchers, kill + uds send
//   GALLERY    many live Claude & Gemini sessions tiled side-by-side
//   MESH       live peer roster, Peer Context Stream inspector, Contact Stream (uds-messaging), Role definitions
//   CHATS      every Claude & Gemini chat kept on disk or archive
//   TELEMETRY  the efficiency report (GET /api/report) drawn as ASCII: sparklines, block bars, small multiples
//   LOG        what was spawned, killed or said, by whom (GET /api/actions)
// Text from transcripts and peer messages is untrusted: everything is set with textContent, never innerHTML.

const SPARK = "▁▂▃▄▅▆▇█";
const SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
const PAIRS = [["search", "rg  > grep"], ["tests", "rt  > suites"], ["http", "door> curl"], ["edit", "swap> py edit"]];
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (id) => document.getElementById(id);
const DEFAULT_MODELS = {
  claude: [
    "fable", "mythos", "opus", "sonnet", "haiku", "opusplan",
    "claude-fable-5-2", "claude-fable-5-1", "fable-5-2", "fable-5-1",
    "claude-mythos-5-2", "claude-mythos-5-1", "mythos-5-2", "mythos-5-1",
    "claude-opus-5-5", "opus-5-5",
    "claude-sonnet-5-5", "sonnet-5-5",
    "claude-haiku-5-5", "haiku-5-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-4-6", "claude-haiku-4-5",
  ],
  gemini: [
    "gemini-4-carbon", "gemini-4-argon", "gemini-3.8-flash",
    "claude-opus-5-5", "claude-sonnet-5-5",
    "gpt-oss-120b", "gemini-2.5-pro",
  ],
};
DEFAULT_MODELS.all = [...new Set([...DEFAULT_MODELS.claude, ...DEFAULT_MODELS.gemini])];
const DEFAULT_EFFORTS = {
  claude: { "*": ["low", "medium", "high", "xhigh", "max"] },
  gemini: {
    "*": ["low", "medium", "high"],
    "gemini-4-carbon": ["low", "medium", "high"],
    "gemini-4-argon": ["low", "medium", "high"],
    "gemini-3.8-flash": ["low", "medium", "high"],
    "claude-opus-5-5": ["low", "medium", "high"],
    "claude-sonnet-5-5": ["low", "medium", "high"],
    "gpt-oss-120b": ["medium"],
    "gemini-2.5-pro": ["low", "medium", "high"],
  },
};

const state = {
  tab: "fleet", fleet: null, sel: null, selCreated: null, detail: null, cpu: new Map(), seen: new Set(),
  feedKeys: new Set(), confirmKill: false, days: get("days", "30"), bucket: get("bucket", "day"), report: null, frame: 0,
  pins: new Set(JSON.parse(get("pins", "[]"))), wide: new Set(JSON.parse(get("wide", "[]"))),
  gMode: get("g-mode", "all"), gCols: get("g-cols", "auto"), gHeight: get("g-height", "m"), tiles: new Map(),
  roles: [], squads: [],
  models: {
    claude: [...DEFAULT_MODELS.claude],
    gemini: [...DEFAULT_MODELS.gemini],
    all: [...DEFAULT_MODELS.all],
    efforts: structuredClone(DEFAULT_EFFORTS),
  },
};
const meshState = {
  peers: [], messages: [], selPeer: null, ctxKinds: "", msgScope: "all", channelFilter: "",
  seenMsgIds: new Set(), ctxSig: "", squadFilter: "",
};
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
const fmtEffortLabel = (e) => e === "medium" ? "med" : (e || "");

// "claude-opus-5-5" -> "opus-5.5", "gemini-4-carbon" -> "gemini-4-carbon"
function fmtModel(m) {
  if (!m) return "–";
  const stripped = m.replace(/-(low|med|medium|high|xhigh|max)$/i, "");
  return stripped.replace(/^claude-/, "").replace(/-\d{8}$/, "").replace(/-(\d+)-(\d+)$/, "-$1.$2");
}
function providerBadge(prov) {
  const p = (prov || "claude").toLowerCase();
  const short = p === "gemini" ? "G" : p === "almanac" ? "A" : "C";
  return el("span", { cls: `prov prov-${p}`, text: short, title: `provider: ${p}` });
}
function udsBadge(hasUds, pipe) {
  return el("span", {
    cls: hasUds ? "uds-on" : "uds-off",
    text: hasUds ? "● uds" : "○ tty",
    title: hasUds ? `uds-messaging active${pipe ? ` (${pipe})` : ""}` : "console keystroke fallback",
  });
}
function effortBadge(e) {
  const norm = e === "med" ? "medium" : e;
  const n = EFFORTS.indexOf(norm) + 1;
  if (!norm) return el("span", { cls: "dim", text: "–" });
  return el("span", { cls: `effort ${norm}`, title: `effort: ${norm}` },
    el("span", { cls: "lvl", text: "▮".repeat(Math.max(0, n)) }), el("span", { cls: "off", text: "▯".repeat(Math.max(0, 5 - n)) }), ` ${fmtEffortLabel(norm)}`);
}
const keyOf = (p) => `${p.pid}:${p.created}`;
const active = (p) => !!p.status && p.status !== "idle" && p.status !== "ended";
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
const del = (path) => api(path, { method: "DELETE" });

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
  const lines = ["ALMANAC MISSION CONTROL v3 // CROSS-MODEL MESH", "(c) " + new Date().getFullYear(), "",
    "> door " + location.port + " ............ ".slice(location.port.length), "> scanning claude+gemini ", "> uds-messaging bus .... ", "> telemetry ............ ", "", "READY."];
  const fleet = api("/api/fleet").catch(() => null);
  for (const [i, line] of lines.entries()) {
    for (const ch of line) { if (skip) break; screen.textContent += ch; await new Promise((r) => setTimeout(r, 8)); }
    if (skip) break;
    if (i === 3) screen.textContent += "OK";
    if (i === 4) { const f = await fleet; screen.textContent += f ? `${f.processes.length} found` : "FAIL"; }
    if (i === 5 || i === 6) screen.textContent += "OK";
    screen.textContent += "\n";
    await new Promise((r) => setTimeout(r, 65));
  }
  screen.classList.add("done");
  setTimeout(() => screen.remove(), 500);
}

// ------------------------------------------------------------------ roles, models, spawn & squad drawers
function modelsForProvider(prov) {
  const p = (prov || "").toLowerCase();
  if (p === "gemini") return state.models.gemini?.length ? state.models.gemini : DEFAULT_MODELS.gemini;
  if (p === "claude") return state.models.claude?.length ? state.models.claude : DEFAULT_MODELS.claude;
  return state.models.all?.length ? state.models.all : DEFAULT_MODELS.all;
}

function modelTag(m) {
  if (["fable", "mythos", "opus", "sonnet", "haiku", "opusplan"].includes(m)) return "alias";
  if (m.includes("fable")) return "fable";
  if (m.includes("mythos")) return "mythos";
  if (m.includes("gemini")) return "gemini";
  if (m.includes("opus")) return "opus";
  if (m.includes("sonnet")) return "sonnet";
  if (m.includes("haiku")) return "haiku";
  return "";
}

const modelPickerEl = $("model-picker");
const pickerState = { inp: null, opts: null, items: [], idx: -1, navigated: false };

function closeModelPicker() {
  if (!modelPickerEl) return;
  modelPickerEl.hidden = true;
  pickerState.inp = null;
  pickerState.opts = null;
  pickerState.items = [];
  pickerState.idx = -1;
  pickerState.navigated = false;
}

function positionModelPicker(inp) {
  const rect = inp.getBoundingClientRect();
  const w = Math.max(rect.width, 230);
  const left = Math.min(rect.left, Math.max(8, innerWidth - w - 8));
  modelPickerEl.style.minWidth = `${Math.round(w)}px`;
  modelPickerEl.style.left = `${Math.round(left)}px`;
  const spaceBelow = innerHeight - rect.bottom;
  if (spaceBelow >= 200 || spaceBelow >= rect.top) {
    modelPickerEl.style.top = `${Math.round(rect.bottom + 2)}px`;
    modelPickerEl.style.bottom = "auto";
  } else {
    modelPickerEl.style.bottom = `${Math.round(innerHeight - rect.top + 2)}px`;
    modelPickerEl.style.top = "auto";
  }
}

function renderModelPicker(inp, opts, filterByQuery) {
  if (!modelPickerEl) return;
  const full = modelsForProvider(opts.getProvider?.());
  const q = inp.value.trim().toLowerCase();
  const list = filterByQuery && q ? full.filter((m) => m.toLowerCase().includes(q)) : full;
  if (!list.length) {
    modelPickerEl.hidden = true;
    pickerState.items = [];
    pickerState.idx = -1;
    return;
  }
  pickerState.inp = inp;
  pickerState.opts = opts;
  pickerState.items = list;
  pickerState.navigated = false;
  pickerState.idx = q ? list.findIndex((m) => m.toLowerCase() === q) : -1;

  modelPickerEl.replaceChildren(...list.map((m, i) => {
    const tag = modelTag(m);
    const li = el("li", { role: "option", "aria-selected": String(i === pickerState.idx) },
      el("span", { text: m }),
      tag ? el("span", { cls: "mp-tag", text: tag }) : null,
    );
    li.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      inp.value = m;
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      closeModelPicker();
      opts.onPick?.(m);
    });
    return li;
  }));
  positionModelPicker(inp);
  modelPickerEl.hidden = false;
  if (pickerState.idx >= 0) {
    modelPickerEl.children[pickerState.idx]?.scrollIntoView({ block: "nearest" });
  } else {
    modelPickerEl.scrollTop = 0;
  }
}

function moveModelPicker(delta) {
  const n = pickerState.items.length;
  if (!n || !modelPickerEl || modelPickerEl.hidden) return false;
  pickerState.navigated = true;
  pickerState.idx = pickerState.idx < 0 ? (delta > 0 ? 0 : n - 1) : (pickerState.idx + delta + n) % n;
  Array.from(modelPickerEl.children).forEach((li, i) => li.setAttribute("aria-selected", String(i === pickerState.idx)));
  modelPickerEl.children[pickerState.idx]?.scrollIntoView({ block: "nearest" });
  return true;
}

function attachModelPicker(inp, opts = {}) {
  inp.addEventListener("focus", () => renderModelPicker(inp, opts, false));
  inp.addEventListener("click", () => renderModelPicker(inp, opts, false));
  inp.addEventListener("input", () => renderModelPicker(inp, opts, true));
  inp.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      if (modelPickerEl.hidden) renderModelPicker(inp, opts, false);
      else moveModelPicker(1);
      e.preventDefault();
      e.stopPropagation();
    } else if (e.key === "ArrowUp" && !modelPickerEl.hidden) {
      moveModelPicker(-1);
      e.preventDefault();
      e.stopPropagation();
    } else if (e.key === "Enter" && !(e.isComposing || e.keyCode === 229)) {
      if (!modelPickerEl.hidden && pickerState.inp === inp && pickerState.navigated && pickerState.idx >= 0) {
        e.preventDefault();
        e.stopPropagation();
        const chosen = pickerState.items[pickerState.idx];
        inp.value = chosen;
        inp.dispatchEvent(new Event("input", { bubbles: true }));
        closeModelPicker();
        opts.onPick?.(chosen);
      } else {
        closeModelPicker();
      }
    } else if (e.key === "Escape" && !modelPickerEl.hidden && pickerState.inp === inp) {
      e.preventDefault();
      e.stopPropagation();
      closeModelPicker();
    }
  });
  inp.addEventListener("blur", () => {
    if (pickerState.inp === inp) closeModelPicker();
  });
}

addEventListener("resize", closeModelPicker);
addEventListener("scroll", (e) => { if (e.target !== modelPickerEl) closeModelPicker(); }, true);

function effortsForModel(prov, rawModel) {
  const m = String(rawModel || "").trim().toLowerCase().replace(/-(low|med|medium|high|xhigh|max)$/i, "");
  let p = (prov || "").toLowerCase();
  if (!p) p = m.startsWith("gemini") || m.startsWith("gpt-oss") ? "gemini" : "claude";
  const table = state.models.efforts?.[p] || DEFAULT_EFFORTS[p] || DEFAULT_EFFORTS.claude;
  const normalizedKey = m === "carbon" || m === "gemini-carbon" ? "gemini-4-carbon"
    : m === "argon" || m === "gemini-argon" ? "gemini-4-argon"
    : m === "flash" || m === "gemini-flash" ? "gemini-3.8-flash"
    : m;
  return table[normalizedKey] || table["*"] || EFFORTS;
}

// Order effort dropdowns high -> med -> low (+ xhigh, max when available) so common picks sit right at top
function orderedEffortOptions(levels) {
  const rank = { high: 0, medium: 1, low: 2, xhigh: 3, max: 4 };
  return [...levels].sort((a, b) => (rank[a] ?? 9) - (rank[b] ?? 9));
}

function syncEffortSelect(sel, prov, model, blankLabel = null) {
  if (!sel) return;
  const prev = sel.value === "med" ? "medium" : sel.value;
  const allowed = orderedEffortOptions(effortsForModel(prov, model));
  const opts = [];
  if (blankLabel !== null) opts.push(el("option", { value: "", text: blankLabel }));
  for (const lv of allowed) opts.push(el("option", { value: lv, text: fmtEffortLabel(lv) }));
  sel.replaceChildren(...opts);
  if (prev && allowed.includes(prev)) sel.value = prev;
  else if (blankLabel !== null && prev === "") sel.value = "";
  else if (allowed.includes("high")) sel.value = "high";
  else if (allowed.length) sel.value = allowed[0];
}

async function loadModels() {
  try {
    const doc = await api("/api/models");
    state.models = {
      claude: doc.claude?.length ? doc.claude : [...DEFAULT_MODELS.claude],
      gemini: doc.gemini?.length ? doc.gemini : [...DEFAULT_MODELS.gemini],
      all: doc.all?.length ? doc.all : [...DEFAULT_MODELS.all],
      efforts: doc.efforts || structuredClone(DEFAULT_EFFORTS),
    };
    syncSpawnModelField();
    syncEffortSelect($("role-effort"), $("role-provider").value || "claude", $("role-model").value, null);
  } catch { /* non-fatal */ }
}

function syncSpawnModelField() {
  const roleId = $("spawn-role").value;
  const roleObj = state.roles.find((r) => r.id === roleId);
  const prov = $("spawn-provider").value || roleObj?.provider || "";
  const inp = $("spawn-model");
  inp.placeholder = roleObj?.model ? `(from role: ${roleObj.model})` : "(from role, e.g. fable / gemini-4-carbon)";
  syncEffortSelect(
    $("spawn-effort"),
    prov,
    inp.value || roleObj?.model || "",
    roleObj?.effort ? `(role: ${fmtEffortLabel(roleObj.effort)})` : "(from role)",
  );
}

attachModelPicker($("spawn-model"), {
  getProvider: () => $("spawn-provider").value || state.roles.find((r) => r.id === $("spawn-role").value)?.provider || "",
  onPick: () => syncSpawnModelField(),
});
$("spawn-model").addEventListener("input", syncSpawnModelField);

attachModelPicker($("role-model"), {
  getProvider: () => $("role-provider").value || "claude",
  onPick: () => syncEffortSelect($("role-effort"), $("role-provider").value || "claude", $("role-model").value, null),
});
$("role-model").addEventListener("input", () => {
  syncEffortSelect($("role-effort"), $("role-provider").value || "claude", $("role-model").value, null);
});
$("role-provider").addEventListener("change", () => {
  syncEffortSelect($("role-effort"), $("role-provider").value || "claude", $("role-model").value, null);
});

async function loadRoles() {
  try {
    const doc = await api("/api/roles");
    state.roles = doc.roles || [];
    syncRoleControls();
  } catch { /* non-fatal */ }
}

function syncRoleControls() {
  const spawnRole = $("spawn-role");
  const cur = spawnRole.value;
  spawnRole.replaceChildren(
    el("option", { value: "", text: "(custom / no role)" }),
    ...state.roles.map((r) => el("option", { value: r.id, text: `[${r.provider === "gemini" ? "G" : "C"}] ${r.title} (${r.id})` })),
  );
  if (cur && state.roles.some((r) => r.id === cur)) spawnRole.value = cur;
  else if (state.roles.length && !cur) spawnRole.value = state.roles[0].id;
  syncSpawnModelField();

  // Refresh any squad member role selects while keeping each row's model & effort options aligned
  document.querySelectorAll("#squad-members .member-row").forEach((row) => {
    const sel = row.querySelector("select[data-field='role_id']");
    const modelInp = row.querySelector("input[data-field='model']");
    const effSel = row.querySelector("select[data-field='effort']");
    if (!sel) return;
    const v = sel.value;
    sel.replaceChildren(...state.roles.map((r) => el("option", { value: r.id, text: `[${r.provider === "gemini" ? "G" : "C"}] ${r.title}` })));
    if (v && state.roles.some((r) => r.id === v)) sel.value = v;
    const picked = state.roles.find((r) => r.id === sel.value);
    if (modelInp && picked) {
      modelInp.placeholder = picked.model ? `(role: ${picked.model})` : "model (auto)";
    }
    if (effSel && picked) {
      syncEffortSelect(effSel, picked.provider, modelInp?.value || picked.model || "", picked.effort ? `(role: ${fmtEffortLabel(picked.effort)})` : "(role)");
    }
  });

  // Render roles table inside Mesh drawer
  const tbody = $("roles-table")?.querySelector("tbody");
  if (tbody) {
    tbody.replaceChildren(...state.roles.map((r) => {
      const tr = el("tr", {},
        el("td", { text: r.id }),
        el("td", {}, providerBadge(r.provider)),
        el("td", { text: r.title }),
        el("td", { cls: "model", text: fmtModel(r.model) }),
        el("td", {}, effortBadge(r.effort)),
        el("td", { cls: "dim", text: r.workspace_mode || "shared" }),
      );
      tr.addEventListener("click", () => populateRoleForm(r));
      return tr;
    }));
  }
}

$("spawn-role").addEventListener("change", syncSpawnModelField);
$("spawn-provider").addEventListener("change", syncSpawnModelField);

function defaultCwd() {
  const selProc = state.detail?.process;
  if (selProc?.cwd) return selProc.cwd;
  const anySession = (state.fleet?.processes || []).find((p) => p.role === "session" && p.cwd);
  return anySession?.cwd || "";
}

function setDrawer(drawerId, btnId, open) {
  const drawer = $(drawerId), btn = $(btnId);
  if (!drawer || !btn) return;
  drawer.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) {
    if (drawerId === "spawn-drawer") {
      setDrawer("squad-drawer", "toggle-squad", false);
      if (!$("spawn-cwd").value) $("spawn-cwd").value = defaultCwd();
      if (!state.roles.length) loadRoles();
      $("spawn-cwd").focus();
    } else if (drawerId === "squad-drawer") {
      setDrawer("spawn-drawer", "toggle-spawn", false);
      if (!$("squad-cwd").value) $("squad-cwd").value = defaultCwd();
      if (!$("squad-members").children.length) seedDefaultSquadMembers();
      $("squad-name").focus();
    }
  }
}

$("toggle-spawn").addEventListener("click", () => setDrawer("spawn-drawer", "toggle-spawn", $("spawn-drawer").hidden));
$("spawn-cancel").addEventListener("click", () => setDrawer("spawn-drawer", "toggle-spawn", false));
$("toggle-squad").addEventListener("click", () => setDrawer("squad-drawer", "toggle-squad", $("squad-drawer").hidden));
$("squad-cancel").addEventListener("click", () => setDrawer("squad-drawer", "toggle-squad", false));

function addSquadMemberRow(presetRole = "", presetName = "", presetModel = "", presetEffort = "", presetWs = "") {
  const roles = state.roles.length ? state.roles : [
    { id: "architect-claude", provider: "claude", title: "System Architect (Claude)", model: "opus", effort: "high" },
    { id: "implementer-gemini", provider: "gemini", title: "Implementer (Gemini)", model: "", effort: "high" },
    { id: "reviewer-gemini", provider: "gemini", title: "Peer Reviewer (Gemini)", model: "", effort: "high" },
  ];
  const roleSel = el("select", { "data-field": "role_id", "aria-label": "Member role" },
    ...roles.map((r) => el("option", { value: r.id, text: `[${r.provider === "gemini" ? "G" : "C"}] ${r.title}` })));
  if (presetRole && roles.some((r) => r.id === presetRole)) roleSel.value = presetRole;
  const getRole = () => (state.roles.length ? state.roles : roles).find((x) => x.id === roleSel.value) || roles[0];
  const activeRole = getRole();

  const nameInp = el("input", {
    "data-field": "agent_name", placeholder: "handle (auto)", value: presetName,
    autocorrect: "off", autocapitalize: "none", spellcheck: "false", "aria-label": "Member handle",
  });
  const modelInp = el("input", {
    "data-field": "model",
    placeholder: activeRole?.model ? `(role: ${activeRole.model})` : "model (auto)",
    value: presetModel,
    autocorrect: "off", autocapitalize: "none", spellcheck: "false", "aria-label": "Member model",
  });
  const effSel = el("select", { "data-field": "effort", "aria-label": "Reasoning effort" });
  const refreshRowEfforts = () => {
    const r = getRole();
    syncEffortSelect(
      effSel,
      r?.provider || "claude",
      modelInp.value.trim() || r?.model || "",
      r?.effort ? `(role: ${fmtEffortLabel(r.effort)})` : "(role)",
    );
  };
  refreshRowEfforts();
  if (presetEffort) {
    const normPreset = presetEffort === "med" ? "medium" : presetEffort;
    if ([...effSel.options].some((o) => o.value === normPreset)) effSel.value = normPreset;
  }

  if (presetModel) modelInp.dataset.touched = "1";
  modelInp.addEventListener("input", () => {
    modelInp.dataset.touched = modelInp.value.trim() ? "1" : "";
    refreshRowEfforts();
  });
  attachModelPicker(modelInp, {
    getProvider: () => getRole()?.provider || "claude",
    onPick: () => refreshRowEfforts(),
  });
  roleSel.addEventListener("change", () => {
    const r = getRole();
    modelInp.placeholder = r?.model ? `(role: ${r.model})` : "model (auto)";
    if (modelInp.dataset.touched !== "1") modelInp.value = "";
    refreshRowEfforts();
  });

  const wsSel = el("select", { "data-field": "workspace_mode", "aria-label": "Workspace isolation" },
    el("option", { value: "", text: "(role default)" }),
    el("option", { value: "shared", text: "shared" }),
    el("option", { value: "worktree", text: "worktree" }),
  );
  if (presetWs) wsSel.value = presetWs;
  const rmBtn = el("button", { type: "button", cls: "btn danger", text: "[✕]", title: "remove member" });
  const row = el("div", { cls: "member-row field" }, roleSel, nameInp, modelInp, effSel, wsSel, rmBtn);
  rmBtn.addEventListener("click", () => { if ($("squad-members").children.length > 1) row.remove(); });
  $("squad-members").append(row);
}

function seedDefaultSquadMembers() {
  $("squad-members").replaceChildren();
  addSquadMemberRow("architect-claude", "arch-claude", "opus-5-5", "high", "shared");
  addSquadMemberRow("implementer-gemini", "impl-gemini", "gemini-4-carbon", "high", "worktree");
  addSquadMemberRow("reviewer-gemini", "rev-gemini", "gemini-4-argon", "medium", "shared");
}

$("squad-add-member").addEventListener("click", () => addSquadMemberRow("implementer-gemini", "", "gemini-4-carbon", "high", ""));

$("spawn-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("spawn-submit"), msg = $("spawn-msg");
  const cwd = $("spawn-cwd").value.trim();
  if (!cwd) { $("spawn-cwd").focus(); return; }
  const channels = $("spawn-channels").value.split(",").map((s) => s.trim()).filter(Boolean);
  btn.disabled = true; msg.textContent = "launching agent…";
  try {
    const r = await post("/api/spawn", {
      cwd,
      role_id: $("spawn-role").value || null,
      agent_name: $("spawn-name").value.trim() || null,
      provider: $("spawn-provider").value || null,
      model: $("spawn-model").value.trim() || null,
      effort: $("spawn-effort").value || null,
      workspace_mode: $("spawn-workspace").value || null,
      channels: channels.length ? channels : null,
      initial_prompt: $("spawn-prompt").value.trim() || null,
      headless: $("spawn-headless").checked,
      remote: $("spawn-remote").checked,
    });
    if (r.ok) {
      msg.textContent = `launched ${r.agent_name} (${r.provider}, pid ${r.pid})${r.worktree_path ? " in worktree" : ""}`;
      $("spawn-name").value = "";
      $("spawn-prompt").value = "";
      setTimeout(() => { loadFleet(); if (state.tab === "mesh") loadMesh(); }, 900);
    } else {
      msg.textContent = `refused: ${r.error || "spawn failed"}`;
    }
  } catch (err) {
    msg.textContent = `failed: ${err.message}`;
  }
  btn.disabled = false;
});

$("squad-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("squad-submit"), msg = $("squad-msg");
  const name = $("squad-name").value.trim(), cwd = $("squad-cwd").value.trim(), brief = $("squad-brief").value.trim();
  if (!name || !cwd || !brief) return;
  const members = [...document.querySelectorAll("#squad-members .member-row")].map((row) => ({
    role_id: row.querySelector("[data-field='role_id']").value,
    agent_name: row.querySelector("[data-field='agent_name']").value.trim() || undefined,
    model: row.querySelector("[data-field='model']").value.trim() || undefined,
    effort: row.querySelector("[data-field='effort']").value || undefined,
    workspace_mode: row.querySelector("[data-field='workspace_mode']").value || undefined,
  }));
  btn.disabled = true; msg.textContent = `launching ${members.length} squad agents…`;
  try {
    const r = await post("/api/squads", { name, cwd, brief, members });
    if (r.ok) {
      msg.textContent = `squad ${r.squad.name} live: ${r.launched}/${members.length} agents on #squad`;
      setTimeout(() => { loadFleet(); show("mesh"); }, 1000);
    } else {
      msg.textContent = `partial/failed: launched ${r.launched || 0}/${members.length}`;
    }
  } catch (err) {
    msg.textContent = `failed: ${err.message}`;
  }
  btn.disabled = false;
});

// ------------------------------------------------------------------ fleet
const ROLE = { session: "", headless: "headless -p", desktop: "Claude desktop", daemon: "claude daemon", "pty-host": "bg pty host", mcp: "mcp server", other: "agent proc" };

function rowName(p) {
  if (p.role === "session") return (p.name_source === "user" && p.name) || p.title || p.name || (p.cwd || "").split(/[\\/]/).pop() || "session";
  return (ROLE[p.role] || p.role) + (p.helpers ? ` (+${p.helpers})` : "");
}
function rowState(p) {
  if (p.role !== "session") return el("span", { cls: "dim", text: p.role === "desktop" ? "app" : p.role });
  if (active(p)) return el("span", { cls: "state-busy" }, el("span", { cls: "spin", "data-spin": "1", text: SPIN[state.frame % SPIN.length] }), ` ${p.status.toUpperCase()}`);
  return el("span", { cls: "state-idle", text: `· ${p.status || "starting"}` });
}

function openInlineModelEditor(td, p) {
  if (td.querySelector("input")) return;
  const current = p.model || "";
  const inp = el("input", {
    cls: "inline-model-input",
    value: current,
    placeholder: p.provider === "gemini" ? "gemini-4-carbon" : "fable / opus-5-5",
    autocorrect: "off", autocapitalize: "none", spellcheck: "false",
    "aria-label": `Set model for ${rowName(p)}`,
  });
  let done = false;
  const restore = (labelModel) => {
    if (done) return;
    done = true;
    closeModelPicker();
    td.textContent = fmtModel(labelModel);
    td.title = `click to set model${labelModel ? ` (${labelModel})` : ""}`;
  };
  const commit = async () => {
    if (done) return;
    closeModelPicker();
    const next = inp.value.trim();
    if (!next || next === current) { done = true; td.textContent = fmtModel(current); return; }
    done = true;
    inp.disabled = true;
    $("act-msg").textContent = `setting model to ${next}…`;
    try {
      const r = await post("/api/model", { pid: p.pid, created: p.created, model: next });
      if (r.ok) {
        p.model = r.model || next;
        if (r.effort) p.effort = r.effort;
        td.textContent = fmtModel(p.model);
        td.title = `click to set model (${p.model})`;
        $("act-msg").textContent = `${r.detail} ✓`;
        await loadFleet();
        if (state.sel === p.pid) loadDetail();
      } else {
        td.textContent = fmtModel(current);
        $("act-msg").textContent = `refused: ${r.error || "failed"}`;
      }
    } catch (err) {
      td.textContent = fmtModel(current);
      $("act-msg").textContent = `failed: ${err.message}`;
    }
  };
  attachModelPicker(inp, {
    getProvider: () => p.provider || "claude",
    onPick: () => commit(),
  });
  inp.addEventListener("click", (e) => e.stopPropagation());
  inp.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      if (e.isComposing || e.keyCode === 229) return;
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      restore(current);
    }
  });
  inp.addEventListener("blur", () => { restore(current); });
  td.replaceChildren(inp);
  inp.focus();
  inp.select();
}

function openInlineEffortEditor(td, p) {
  if (td.querySelector("select")) return;
  const current = p.effort === "med" ? "medium" : (p.effort || "");
  const allowed = orderedEffortOptions(effortsForModel(p.provider, p.model));
  const opts = [];
  if (!current) opts.push(el("option", { value: "", text: "effort…" }));
  for (const lv of allowed) opts.push(el("option", { value: lv, text: fmtEffortLabel(lv) }));
  if (current && !allowed.includes(current)) opts.push(el("option", { value: current, text: fmtEffortLabel(current) }));
  const sel = el("select", {
    cls: "inline-effort-select",
    "aria-label": `Set effort for ${rowName(p)}`,
  }, ...opts);
  sel.value = current || "";
  let done = false;
  const restore = (eff) => {
    if (done) return;
    done = true;
    td.replaceChildren(effortBadge(eff));
    td.title = `click to set effort${eff ? ` (${eff})` : ""}`;
  };
  const commit = async () => {
    if (done) return;
    const next = sel.value;
    if (!next || next === current) { restore(current); return; }
    done = true;
    sel.disabled = true;
    $("act-msg").textContent = `setting effort to ${fmtEffortLabel(next)}…`;
    try {
      const r = await post("/api/model", { pid: p.pid, created: p.created, effort: next });
      if (r.ok) {
        p.effort = r.effort || next;
        td.replaceChildren(effortBadge(p.effort));
        td.title = `click to set effort (${p.effort})`;
        $("act-msg").textContent = `${r.detail} ✓`;
        await loadFleet();
        if (state.sel === p.pid) loadDetail();
      } else {
        td.replaceChildren(effortBadge(current));
        $("act-msg").textContent = `refused: ${r.error || "failed"}`;
      }
    } catch (err) {
      td.replaceChildren(effortBadge(current));
      $("act-msg").textContent = `failed: ${err.message}`;
    }
  };
  sel.addEventListener("click", (e) => e.stopPropagation());
  sel.addEventListener("change", () => commit());
  sel.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      restore(current);
    }
  });
  sel.addEventListener("blur", () => restore(current));
  td.replaceChildren(sel);
  sel.focus();
}

async function loadFleet() {
  let doc;
  try { doc = await api("/api/fleet"); } catch (err) { $("fleet-summary").textContent = `fleet unavailable: ${err.message}`; return; }
  state.fleet = doc;
  const s = doc.summary || {};
  const byProv = s.providers || {};
  $("mesh-pill").textContent = `${byProv.claude || 0} claude · ${byProv.gemini || 0} gemini // uds`;
  $("fleet-summary").replaceChildren(
    el("span", { cls: "hi", text: `${s.sessions ?? 0} sessions` }),
    el("span", { cls: "dim", text: ` (${byProv.claude || 0}C/${byProv.gemini || 0}G)` }), ` · `,
    el("span", { cls: s.busy ? "amber" : "dim", text: `${s.busy ?? 0} busy` }), ` · ${s.idle ?? 0} idle · `,
    el("span", { cls: s.daemon ? "hi" : "dim", text: `daemon ${s.daemon ? "▲" : "▽"}` }), " · ",
    el("span", { cls: s.desktop ? "hi" : "dim", text: `desktop ${s.desktop ? "▲" : "▽"}` }),
    ` · ${s.headless ?? 0} headless · cpu ${num(s.cpu)}% · mem ${num((s.mem_mb || 0) / 1024)}G · ${doc.processes.length} procs`);
  // Keep any active inline model or effort editor intact while the user is editing
  if (document.activeElement?.classList.contains("inline-model-input") || document.activeElement?.classList.contains("inline-effort-select")) return;
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
    const modelCell = el("td", {
      cls: p.role === "session" ? "model editable" : "model",
      text: p.role === "session" ? fmtModel(p.model) : "",
      title: p.role === "session" ? `click to set model${p.model ? ` (${p.model})` : ""}` : null,
    });
    if (p.role === "session") {
      modelCell.addEventListener("click", (e) => {
        e.stopPropagation();
        select(p.pid);
        openInlineModelEditor(modelCell, p);
      });
    }
    const effortCell = el("td", {
      cls: p.role === "session" ? "effort-cell editable" : "effort-cell",
      title: p.role === "session" ? `click to set effort${p.effort ? ` (${p.effort})` : ""}` : null,
    }, p.role === "session" ? effortBadge(p.effort) : "");
    if (p.role === "session") {
      effortCell.addEventListener("click", (e) => {
        e.stopPropagation();
        select(p.pid);
        openInlineEffortEditor(effortCell, p);
      });
    }
    const tr = el("tr", { cls: [p.role !== "session" ? "helper" : "", p.pid === state.sel ? "sel" : "", !first && !state.seen.has(key) && !reduced ? "fresh" : ""].join(" ").trim(), "data-pid": p.pid, tabindex: -1 },
      pin,
      el("td", { text: String(p.pid) }),
      el("td", {}, providerBadge(p.provider)),
      el("td", { text: rowName(p).slice(0, 28), title: p.cwd || p.cmd }),
      el("td", { cls: "role-tag", text: p.role === "session" ? (p.agent_role || "–").slice(0, 18) : "" }),
      modelCell,
      effortCell,
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
  kv.append(el("dt", { text: "provider" }), el("dd", {}, providerBadge(p.provider), "  ", udsBadge(p.has_uds, p.pipe), p.agent_role ? el("span", { cls: "role-tag", text: `  · ${p.agent_role}` }) : null));
  add("role", p.role + (p.kind && p.kind !== "interactive" ? ` (${p.kind})` : ""));
  if (p.squad_id) add("squad", p.squad_id, "cyan");
  const meta = doc.meta || {};
  if (meta.model || meta.effort || p.model) {
    const m = meta.model || p.model;
    kv.append(el("dt", { text: "model" }), el("dd", {}, el("span", { cls: "model", text: fmtModel(m), title: m }), "  ", effortBadge(meta.effort || p.effort)));
  }
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
  const via = p.has_uds ? "uds-messaging" : "console";
  $("say-text").placeholder = active(p) ? `busy: queues via ${via} until free` : `send message via ${via}`;
  $("actions").hidden = false;
  const remote = $("remote");
  remote.hidden = !p.remote_url; if (p.remote_url) remote.href = p.remote_url;
  $("open-mesh-peer").hidden = p.role !== "session";
  $("set-model-btn").hidden = p.role !== "session";
  $("kill").hidden = !p.can_kill;
  if (!state.confirmKill) $("kill").textContent = "[KILL ✕]";
  $("pin").hidden = p.role !== "session";
  $("pin").textContent = state.pins.has(keyOf(p)) ? "[UNPIN ◆]" : "[PIN ◇]";
}
function editSelectedSessionModel() {
  const p = state.detail?.process;
  if (!p || p.role !== "session") return;
  const td = document.querySelector(`#fleet-table tr[data-pid="${p.pid}"] td.model.editable`);
  if (td) { td.scrollIntoView({ block: "nearest" }); openInlineModelEditor(td, p); }
}
$("set-model-btn").addEventListener("click", editSelectedSessionModel);
$("pin").addEventListener("click", () => { const p = state.detail?.process; if (p) { togglePin(p); loadDetail(); loadFleet(); } });
$("open-mesh-peer").addEventListener("click", () => {
  const p = state.detail?.process;
  if (!p) return;
  const handle = p.name || p.session_id || String(p.pid);
  meshState.selPeer = handle;
  $("mesh-to").value = p.name || String(p.pid);
  show("mesh");
});

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
  input.disabled = true; $("act-msg").textContent = p.has_uds ? "sending via uds…" : "typing…";
  try {
    const r = await post("/api/send", { pid: p.pid, created: p.created, text });
    const tag = p.has_uds ? "uds" : "typed";
    $("act-msg").textContent = r.ok ? `sent (${tag}) ✓` : `refused: ${r.error}`;
    if (r.ok) { input.value = ""; $("feed").append(el("li", { cls: `${p.has_uds ? "peer_out" : "you"} new`, text: `${text}  (${tag})` })); $("feed").scrollTop = $("feed").scrollHeight; }
  } catch (err) { $("act-msg").textContent = `failed: ${err.message}`; }
  input.disabled = false; input.focus();
  setTimeout(loadDetail, 1200);
});
$("kill").addEventListener("click", kill);

// ------------------------------------------------------------------ gallery
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
  const pin = el("button", { type: "button", cls: "btn" });
  const wide = el("button", { type: "button", cls: "btn" });
  const meshBtn = el("button", { type: "button", cls: "btn cyan", text: "[MESH ▸]", title: "inspect in Mesh view" });
  const open = el("button", { type: "button", cls: "btn", text: "[FLEET ▸]", title: "open in the fleet view" });
  const killBtn = el("button", { type: "button", cls: "btn danger", text: "[KILL ✕]" });
  box.append(head, feed, form, el("div", { cls: "actions pad" }, remote, pin, wide, meshBtn, open, killBtn, msg));
  const t = { box, head, feed, input, msg, remote, pin, wide, kill: killBtn, p, feedSig: "", armed: 0 };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.disabled = true; msg.textContent = t.p.has_uds ? "uds…" : "typing…";
    try {
      const r = await post("/api/send", { pid: t.p.pid, created: t.p.created, text });
      const tag = t.p.has_uds ? "uds" : "typed";
      msg.textContent = r.ok ? `sent (${tag}) ✓` : `refused: ${r.error}`;
      if (r.ok) { input.value = ""; feed.append(el("li", { cls: `${t.p.has_uds ? "peer_out" : "you"} new`, text: `${text}  (${tag})` })); feed.scrollTop = feed.scrollHeight; }
    } catch (err) { msg.textContent = `failed: ${err.message}`; }
    input.disabled = false; input.focus();
  });
  pin.addEventListener("click", () => { togglePin(t.p); paintTile(t, null); if (state.gMode === "pinned") loadGallery(); });
  wide.addEventListener("click", () => {
    state.wide.has(key) ? state.wide.delete(key) : state.wide.add(key);
    put("wide", JSON.stringify([...state.wide])); paintTile(t, null);
  });
  meshBtn.addEventListener("click", () => {
    meshState.selPeer = t.p.name || t.p.session_id || String(t.p.pid);
    $("mesh-to").value = t.p.name || String(t.p.pid);
    show("mesh");
  });
  open.addEventListener("click", () => { show("fleet"); select(t.p.pid); });
  killBtn.addEventListener("click", async () => {
    if (Date.now() - t.armed > 4000) {
      t.armed = Date.now(); killBtn.textContent = "[SURE? ✕]"; killBtn.classList.add("confirm");
      setTimeout(() => { if (Date.now() - t.armed >= 4000) { killBtn.textContent = "[KILL ✕]"; killBtn.classList.remove("confirm"); } }, 4100);
      return;
    }
    t.armed = 0; killBtn.classList.remove("confirm"); killBtn.textContent = "[▓▓▓▒░]";
    try {
      const r = await post("/api/kill", { pid: t.p.pid, created: t.p.created });
      msg.textContent = r.ok ? "terminated" : `refused: ${r.error}`;
    } catch (err) { msg.textContent = `failed: ${err.message}`; }
    killBtn.textContent = "[KILL ✕]";
    await loadFleet(); loadGallery();
  });
  return t;
}

function paintTile(t, doc) {
  const p = doc?.process || t.p;
  t.p = p;
  const key = keyOf(p), meta = doc?.meta || t.meta || {};
  t.meta = meta;
  const provTag = (p.provider || "claude") === "gemini" ? "[G]" : "[C]";
  t.box.setAttribute("data-title", `${provTag} ${p.pid} ${rowName(p)}`.slice(0, 62));
  t.box.classList.toggle("busy", active(p));
  t.box.classList.toggle("wide", state.wide.has(key));
  const u = doc?.usage || t.usage;
  t.usage = u;
  const m = meta.model || p.model;
  t.head.replaceChildren(...[
    providerBadge(p.provider), rowState(p),
    p.agent_role ? el("span", { cls: "role-tag", text: p.agent_role }) : null,
    el("span", { cls: "model", text: fmtModel(m), title: m || "no assistant reply yet" }), effortBadge(meta.effort || p.effort),
    udsBadge(p.has_uds, p.pipe),
    el("span", { cls: "dim", text: `cpu ${p.cpu === null ? "–" : num(p.cpu) + "%"} · ${p.mem_mb}M · up ${age(p.started)}` }),
    u && u.messages ? el("span", { cls: "dim", text: `· ${num(u.output, 0)} out tok` }) : null,
    el("span", { cls: "dim where", text: p.cwd || "", title: p.cwd || "" })].filter(Boolean));
  t.pin.textContent = state.pins.has(key) ? "[UNPIN ◆]" : "[PIN ◇]";
  t.wide.textContent = state.wide.has(key) ? "[NARROW ⇥]" : "[WIDE ⇔]";
  t.remote.hidden = !p.remote_url;
  if (p.remote_url) t.remote.href = p.remote_url;
  t.input.placeholder = active(p) ? "busy: queues until free" : `send via ${p.has_uds ? "uds-messaging" : "console"}`;
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
  $("g-empty").textContent = state.gMode === "pinned" ? "nothing pinned: click ◇ on a fleet row, press p, or use [PIN ◇] on a tile" : "no Claude or Gemini sessions running";
  const keys = new Set(list.map(keyOf));
  for (const [k, t] of state.tiles) if (!keys.has(k)) { t.box.remove(); state.tiles.delete(k); }
  for (const p of list) if (!state.tiles.has(keyOf(p))) state.tiles.set(keyOf(p), makeTile(p));
  const order = list.map((p) => state.tiles.get(keyOf(p)).box);
  if (order.some((box, i) => grid.children[i] !== box)) grid.append(...order);
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

// ------------------------------------------------------------------ mesh: peer roster + context stream + contact stream
function populateRoleForm(r) {
  const prov = r ? (r.provider || "claude") : "claude";
  const mod = r ? (r.model || "") : "";
  const eff = r ? ((r.effort === "med" ? "medium" : r.effort) || "high") : "high";
  $("role-id").value = r ? r.id : "";
  $("role-title").value = r ? r.title : "";
  $("role-provider").value = prov;
  $("role-model").value = mod;
  syncEffortSelect($("role-effort"), prov, mod, null);
  if ([...$("role-effort").options].some((o) => o.value === eff)) $("role-effort").value = eff;
  $("role-workspace").value = r ? (r.workspace_mode || "shared") : "shared";
  $("role-prompt").value = r ? (r.system_prompt || "") : "";
  $("role-msg").textContent = r ? `editing ${r.id}` : "";
}

$("toggle-roles").addEventListener("click", () => {
  const open = $("roles-drawer").hidden;
  $("roles-drawer").hidden = !open;
  $("toggle-roles").setAttribute("aria-expanded", String(open));
  if (open) syncRoleControls();
});
$("role-close").addEventListener("click", () => { $("roles-drawer").hidden = true; $("toggle-roles").setAttribute("aria-expanded", "false"); });
$("role-new").addEventListener("click", () => populateRoleForm(null));
$("role-save").closest("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("role-save"), msg = $("role-msg");
  btn.disabled = true;
  try {
    const r = await post("/api/roles", {
      id: $("role-id").value.trim(),
      title: $("role-title").value.trim(),
      provider: $("role-provider").value,
      model: $("role-model").value.trim() || null,
      effort: $("role-effort").value || null,
      workspace_mode: $("role-workspace").value,
      system_prompt: $("role-prompt").value.trim(),
    });
    msg.textContent = r.ok ? `saved role ${r.role.id} ✓` : "save failed";
    await loadRoles();
  } catch (err) {
    msg.textContent = `failed: ${err.message}`;
  }
  btn.disabled = false;
});
$("role-delete").addEventListener("click", async () => {
  const id = $("role-id").value.trim();
  if (!id) return;
  try {
    const r = await del(`/api/roles?id=${encodeURIComponent(id)}`);
    $("role-msg").textContent = r.ok ? `deleted ${id}` : `${id} not found`;
    populateRoleForm(null);
    await loadRoles();
  } catch (err) {
    $("role-msg").textContent = `failed: ${err.message}`;
  }
});

$("mesh-spawn-jump").addEventListener("click", () => {
  show("fleet");
  setDrawer("spawn-drawer", "toggle-spawn", true);
});
$("mesh-refresh").addEventListener("click", () => { loadMesh(); if (meshState.selPeer) loadPeerContext(); });
$("mesh-squad-filter").addEventListener("change", (e) => {
  meshState.squadFilter = e.target.value;
  loadMesh();
});
$("mesh-ctx-kinds").addEventListener("click", (e) => {
  if (e.target.dataset.v === undefined) return;
  meshState.ctxKinds = e.target.dataset.v;
  meshState.ctxSig = "";
  document.querySelectorAll("#mesh-ctx-kinds button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === meshState.ctxKinds)));
  if (meshState.selPeer) loadPeerContext();
});
$("mesh-msg-scope").addEventListener("click", (e) => {
  if (!e.target.dataset.v) return;
  meshState.msgScope = e.target.dataset.v;
  document.querySelectorAll("#mesh-msg-scope button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === meshState.msgScope)));
  paintMeshMessages();
});
$("mesh-filter-ch").addEventListener("input", (e) => {
  meshState.channelFilter = e.target.value.trim();
  paintMeshMessages();
});

async function loadMesh() {
  let doc;
  const sq = meshState.squadFilter;
  try {
    doc = await api(`/api/mesh?limit=150${sq ? `&squad=${encodeURIComponent(sq)}` : ""}`);
  } catch (err) {
    $("mesh-summary").textContent = `mesh unavailable: ${err.message}`;
    return;
  }
  meshState.peers = doc.peers || [];
  meshState.messages = doc.messages || [];
  if (doc.roles?.length) { state.roles = doc.roles; syncRoleControls(); }
  state.squads = doc.squads || [];

  const cCount = meshState.peers.filter((p) => p.provider === "claude").length;
  const gCount = meshState.peers.filter((p) => p.provider === "gemini").length;
  $("mesh-summary").replaceChildren(
    el("span", { cls: "hi", text: `${meshState.peers.length} peers` }),
    el("span", { cls: "dim", text: ` (${cCount} claude · ${gCount} gemini)` }),
    " · ",
    el("span", { cls: doc.hub_pipe ? "hi" : "dim", text: doc.hub_pipe ? "uds hub ▲" : "uds hub ▽" }),
    ` · ${state.squads.length} squad${state.squads.length === 1 ? "" : "s"}`,
  );

  // Sync squad selector
  const sqSel = $("mesh-squad-filter");
  const prevSq = sqSel.value;
  sqSel.replaceChildren(
    el("option", { value: "", text: "all peers" }),
    ...state.squads.map((s) => el("option", { value: s.id, text: `${s.name} (${s.id})` })),
  );
  if (prevSq && state.squads.some((s) => s.id === prevSq)) sqSel.value = prevSq;

  // Sync datalist targets
  const dl = $("mesh-targets");
  dl.replaceChildren(
    el("option", { value: "@all" }),
    el("option", { value: "@squad" }),
    el("option", { value: "#squad" }),
    el("option", { value: "#review" }),
    ...meshState.peers.map((p) => el("option", { value: p.name })),
  );

  // Render peer roster
  const tbody = $("mesh-peers-table").querySelector("tbody");
  $("mesh-peers-empty").hidden = meshState.peers.length > 0;
  tbody.replaceChildren(...meshState.peers.map((p) => {
    const isSel = meshState.selPeer && (p.name === meshState.selPeer || p.session_id === meshState.selPeer || String(p.pid) === meshState.selPeer);
    const lastTxt = p.last_event ? `[${p.last_event.kind}] ${p.last_event.text}`.replace(/\s+/g, " ").slice(0, 44) : "–";
    const tr = el("tr", { cls: isSel ? "sel" : "" },
      el("td", {}, providerBadge(p.provider)),
      el("td", { text: p.name.slice(0, 24), title: `pid ${p.pid} · ${p.cwd || ""}` }),
      el("td", { cls: "role-tag", text: (p.agent_role || "–").slice(0, 18) }),
      el("td", { cls: "cyan", text: (p.squad_id || "–").slice(0, 16) }),
      el("td", {}, udsBadge(p.has_uds, p.pipe)),
      el("td", {}, rowState({ ...p, role: "session" })),
      el("td", { cls: "dim", text: lastTxt, title: p.last_event?.text || "" }),
    );
    tr.addEventListener("click", () => {
      meshState.selPeer = p.name || p.session_id || String(p.pid);
      meshState.ctxSig = "";
      if (document.activeElement !== $("mesh-to")) $("mesh-to").value = p.name;
      loadMesh();
      loadPeerContext();
    });
    return tr;
  }));

  if (!meshState.selPeer && meshState.peers.length) {
    meshState.selPeer = meshState.peers[0].name;
    loadPeerContext();
  }
  paintMeshMessages();
}

function paintMeshMessages() {
  const feed = $("mesh-feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  let list = meshState.messages;
  if (meshState.msgScope === "peer" && meshState.selPeer) {
    const target = meshState.selPeer;
    list = list.filter((m) => m.from_name === target || m.to_name === target || m.to_name.startsWith("@") || m.to_name.startsWith("#"));
  }
  if (meshState.channelFilter) {
    const ch = meshState.channelFilter.toLowerCase();
    list = list.filter((m) => (m.channel || "").toLowerCase().includes(ch) || (m.to_name || "").toLowerCase().includes(ch));
  }
  $("mesh-msg-count").textContent = `${list.length} msg${list.length === 1 ? "" : "s"}`;
  const first = meshState.seenMsgIds.size === 0;
  if (!list.length) {
    feed.replaceChildren(el("li", { cls: "dim", text: "no contact stream messages yet — send via uds below or let peers coordinate with SendMessage / mesh_send" }));
  } else {
    feed.replaceChildren(...list.map((m) => {
      const fresh = !first && !meshState.seenMsgIds.has(m.id) && !reduced;
      const head = el("div", { cls: "msg-head" },
        el("span", { cls: "dim", text: clock(m.ts) }),
        m.from_provider ? providerBadge(m.from_provider) : null,
        el("span", { cls: "hi", text: m.from_name }),
        el("span", { cls: "dim", text: "──▸" }),
        m.to_provider ? providerBadge(m.to_provider) : null,
        el("span", { cls: m.to_name.startsWith("@") || m.to_name.startsWith("#") ? "amber" : "cyan", text: m.to_name }),
        m.channel ? el("span", { cls: "violet", text: m.channel }) : null,
        el("span", { cls: "dim", text: `[${m.transport || "uds"}]` }),
      );
      const body = el("div", { cls: "msg-body", text: m.body });
      return el("li", { cls: fresh ? "new" : "" }, head, body);
    }));
  }
  meshState.messages.forEach((m) => meshState.seenMsgIds.add(m.id));
  if (atBottom || first) feed.scrollTop = feed.scrollHeight;
}

async function loadPeerContext() {
  if (!meshState.selPeer) return;
  let doc;
  try {
    const kindsQ = meshState.ctxKinds ? `&kinds=${encodeURIComponent(meshState.ctxKinds)}` : "";
    doc = await api(`/api/mesh/context?peer=${encodeURIComponent(meshState.selPeer)}&limit=60${kindsQ}`);
  } catch (err) {
    $("mesh-ctx-head").replaceChildren(el("span", { cls: "red", text: err.message }));
    return;
  }
  const p = doc.peer, u = doc.usage;
  $("mesh-ctx-box").setAttribute("data-title", `CONTEXT STREAM // ${p.name}`.slice(0, 56));
  $("mesh-ctx-head").replaceChildren(
    providerBadge(p.provider), " ",
    el("span", { cls: "title", text: p.name }),
    p.agent_role ? el("span", { cls: "role-tag", text: ` · ${p.agent_role}` }) : null,
    p.model ? el("span", { cls: "model", text: ` · ${fmtModel(p.model)}` }) : null,
    el("span", { cls: "dim", text: ` · ${doc.live ? `live pid ${p.pid}` : "ended"}` }),
    u && u.messages ? el("span", { cls: "dim", text: ` · ${num(u.output, 0)} out tok · ${num(u.calls, 0)} tools` }) : null,
  );
  const events = doc.events || [];
  const sig = `${p.name}|${events.length}|${events.length ? events[events.length - 1].ts + events[events.length - 1].text.slice(0, 30) : ""}`;
  if (sig === meshState.ctxSig) return;
  meshState.ctxSig = sig;
  const feed = $("mesh-ctx-feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  feed.replaceChildren(...(events.length
    ? events.map((e) => el("li", { cls: e.kind, text: e.text, title: e.ts ? new Date(e.ts).toLocaleString() : null }))
    : [el("li", { cls: "result", text: "no matching context events yet" })]));
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

// IME-safe Enter-to-submit for Contact Stream textarea (Shift+Enter inserts newline)
$("mesh-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (e.isComposing || e.keyCode === 229) return;
    $("mesh-send-form").requestSubmit();
  }
});

$("mesh-send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const to = $("mesh-to").value.trim(), text = $("mesh-text").value.trim(), channel = $("mesh-channel").value.trim();
  const btn = $("mesh-send-btn"), msg = $("mesh-send-msg");
  if (!to || !text) return;
  btn.disabled = true; msg.textContent = "routing over uds-messaging…";
  try {
    const r = await post("/api/mesh/send", {
      to,
      text,
      channel: channel || null,
      squad_id: meshState.squadFilter || null,
    });
    if (r.ok) {
      $("mesh-text").value = "";
      const got = r.delivered || [], lost = (r.failed || []).map((f) => f.name);
      msg.textContent = got.length
        ? `delivered to ${got.join(", ")} ✓` + (lost.length ? ` · not delivered to ${lost.join(", ")}` : "")
        : lost.length ? `not delivered: ${lost.join(", ")}` : `nobody received it: no live peer matches ${to}`;
      await loadMesh();
      if (meshState.selPeer) loadPeerContext();
    } else {
      msg.textContent = `refused: ${r.error || "delivery failed"}`;
    }
  } catch (err) {
    msg.textContent = `failed: ${err.message}`;
  }
  btn.disabled = false;
  $("mesh-text").focus();
});

// ------------------------------------------------------------------ chats: every chat ever kept, resumable
const chatState = { list: [], sel: null, armed: 0, timer: 0 };
const NAMED_BY = { renamed: "you (/rename)", haiku: "Haiku", auto: "auto title", prompt: "its first prompt" };
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
      el("td", {}, providerBadge(c.provider)),
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
  add("provider", null, el("dd", {}, providerBadge(c.provider)));
  add("model", null, el("dd", {}, el("span", { cls: "model", text: fmtModel(c.model), title: c.model }), "  ", effortBadge(c.effort)));
  add("folder", c.cwd);
  add("when", `${c.first ? new Date(c.first * 1000).toLocaleString() : "?"} → ${c.last ? new Date(c.last * 1000).toLocaleString() : "?"}`);
  add("size", `${num(c.replies, 0)} replies · ${num(c.output, 0)} output tokens`);
  if (c.provider !== "gemini") add("resume", `re-sends ${num(c.context, 0)} context tokens; ≥ ${usd(c.resume_usd)} at the input rate, more as a cache write`);
  add("session", c.id);
  add("kept", null, el("dd", {}, kept(c), el("span", { cls: "dim", text: doc.read_from ? `  (reading the ${doc.read_from})` : "" })));
  $("chat-head").replaceChildren(el("div", { cls: "title", text: c.title }), kv);
  const feed = $("chat-feed");
  feed.replaceChildren(...(doc.events.length ? doc.events.map((e) => el("li", { cls: e.kind, text: e.text, title: e.ts ? new Date(e.ts).toLocaleString() : null }))
    : [el("li", { cls: "result", text: "nothing readable in this transcript" })]));
  feed.scrollTop = feed.scrollHeight;
  $("chat-actions").hidden = false;
  $("resume").disabled = !!c.live_pid || c.provider === "gemini";
  if (c.live_pid) $("chat-msg").textContent = `running now as pid ${c.live_pid}: open it in FLEET, GALLERY, or MESH`;
  else if (c.provider === "gemini") $("chat-msg").textContent = "spawn a new Gemini session in FLEET to continue work in this folder";
  chatState.chat = c;
}

$("resume").addEventListener("click", async () => {
  const c = chatState.chat;
  if (!c || c.live_pid || c.provider === "gemini") return;
  if (Date.now() - chatState.armed > 5000) {
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
    el("span", { cls: a.action === "kill" ? "red" : a.action === "spawn" || a.action === "squad" ? "amber" : "cyan", text: a.action.toUpperCase().padEnd(6) }),
    el("span", { cls: a.ok ? "hi" : "red", text: a.ok ? " ok   " : " FAIL " }),
    `${a.pid ? `pid ${a.pid} ` : ""}${a.name ? `(${a.name}) ` : ""}by ${a.who || "?"}: ${a.detail}`))
    : [el("li", { cls: "dim", text: "nothing spawned, killed or said yet" })]));
}

// ------------------------------------------------------------------ tabs, keys, loops
function show(tab) {
  state.tab = tab;
  document.querySelectorAll(".tabs [role=tab]").forEach((b) => {
    const sel = b.dataset.tab === tab;
    b.setAttribute("aria-selected", String(sel));
    if (sel) b.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  for (const id of ["fleet", "gallery", "mesh", "chats", "telemetry", "log"]) $(id).hidden = id !== tab;
  if (tab === "chats") { loadChats(); nameStatus(); }
  if (tab === "telemetry") loadReport();
  if (tab === "log") loadLog();
  if (tab === "fleet") loadFleet();
  if (tab === "gallery") loadFleet().then(loadGallery);
  if (tab === "mesh") { loadMesh(); if (meshState.selPeer) loadPeerContext(); }
}
document.querySelectorAll(".tabs [role=tab]").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
$("help-btn").addEventListener("click", () => { $("help").hidden = !$("help").hidden; });
$("help").addEventListener("click", () => { $("help").hidden = true; });

addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) { if (e.key === "Escape") e.target.blur(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const rows = [...document.querySelectorAll("#fleet-table tbody tr")];
  const idx = rows.findIndex((r) => Number(r.dataset.pid) === state.sel);
  if (state.confirmKill && e.key.toLowerCase() === "y") { e.preventDefault(); kill(); return; }
  if (state.confirmKill && e.key !== "x") cancelKill();
  switch (e.key) {
    case "1": show("fleet"); break;
    case "2": show("gallery"); break;
    case "3": show("mesh"); break;
    case "4": show("chats"); break;
    case "5": show("telemetry"); break;
    case "6": show("log"); break;
    case "m": if (state.tab === "fleet" && state.detail?.process?.role === "session") { e.preventDefault(); editSelectedSessionModel(); } break;
    case "s": if (state.tab === "fleet") { e.preventDefault(); setDrawer("spawn-drawer", "toggle-spawn", $("spawn-drawer").hidden); } break;
    case "p": if (state.tab === "fleet" && state.detail?.process?.role === "session") { togglePin(state.detail.process); loadDetail(); loadFleet(); } break;
    case "?": $("help").hidden = !$("help").hidden; break;
    case "Escape":
      $("help").hidden = true;
      setDrawer("spawn-drawer", "toggle-spawn", false);
      setDrawer("squad-drawer", "toggle-squad", false);
      $("roles-drawer").hidden = true;
      break;
    case "r":
      state.tab === "chats" ? loadChats()
        : state.tab === "telemetry" ? loadReport()
        : state.tab === "log" ? loadLog()
        : state.tab === "gallery" ? loadFleet().then(loadGallery)
        : state.tab === "mesh" ? (loadMesh(), meshState.selPeer && loadPeerContext())
        : loadFleet();
      break;
    case "j": case "ArrowDown": if (state.tab === "fleet" && rows.length) { e.preventDefault(); select(Number(rows[Math.min(rows.length - 1, idx + 1)].dataset.pid)); } break;
    case "k": case "ArrowUp": if (state.tab === "fleet" && rows.length) { e.preventDefault(); select(Number(rows[Math.max(0, idx - 1)].dataset.pid)); } break;
    case "Enter": if (state.tab === "fleet" && state.sel) loadDetail(); break;
    case "/":
      if (state.tab === "chats") { e.preventDefault(); $("chat-q").focus(); }
      else if (state.tab === "mesh") { e.preventDefault(); $("mesh-text").focus(); }
      else if (!$("say").hidden) { e.preventDefault(); $("say-text").focus(); }
      break;
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
  if (state.tab === "mesh") { loadMesh(); if (meshState.selPeer) loadPeerContext(); }
}, 2000);
setInterval(() => { if (!document.hidden && state.tab === "log") loadLog(); }, 5000);
setInterval(() => { if (!document.hidden && state.tab === "telemetry") loadReport(); }, 5 * 60 * 1000);

boot();
loadModels();
loadRoles();
loadFleet().then(() => {
  const busy = state.fleet?.processes.find((p) => p.role === "session");
  if (busy && !state.sel) select(busy.pid);
});
