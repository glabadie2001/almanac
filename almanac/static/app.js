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
  roles: [], squads: [], fKinds: get("f-kinds", ""), gKinds: get("g-kinds", ""),
  models: {
    claude: [...DEFAULT_MODELS.claude],
    gemini: [...DEFAULT_MODELS.gemini],
    all: [...DEFAULT_MODELS.all],
    efforts: structuredClone(DEFAULT_EFFORTS),
  },
};
const meshState = {
  peers: [], messages: [], selPeer: null, ctxKinds: "", msgScope: "all", channelFilter: "",
  seenMsgIds: new Set(), ctxSig: "", squadFilter: "", open: [], pair: null, squadChosen: false,
};
// MESH plan: the selected squad's tasks (GET /api/mesh/objects), refetched only when objects_rev or the squad changes
const planState = { key: null, rev: undefined, rows: [], branches: "", note: "", stale: true, sig: "", seq: 0, group: get("plan-group", "tree") };
// feed filters shared by FLEET, GALLERY and the MESH context stream: [kinds= value, label]
const KIND_SETS = [["", "all"], ["you,claude,gemini,peer_in,peer_out", "dialog"], ["peer_in,peer_out", "contacts"], ["tool,result,error", "tools"]];
function fillKindSeg(id, current) {
  $(id).replaceChildren(...KIND_SETS.map(([v, label]) => el("button", { type: "button", "data-v": v, "aria-pressed": String(v === current), text: label })));
}
function pressKindSeg(id, current) {
  document.querySelectorAll(`#${id} button`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === current)));
}
const OPERATOR = new Set(["operator", "almanac-hub"]);
// a hub notice (transport "system", e.g. an escalation "[stalled] impl-x ...") is sent as almanac-hub but is not the
// operator's own: it comes from the system, about the agent its body names. Rows without the field read as before.
const isSystem = (m) => m.transport === "system";
const sysAgent = (m) => (isSystem(m) && /^\[stalled\]\s+(\S+)/.exec(m.body || "")?.[1]) || null;
const isMine = (m) => OPERATOR.has(m.from_name) && !isSystem(m);
const msgFrom = (m) => (isSystem(m) ? sysAgent(m) || "system" : m.from_name);   // who a message is about, for a label
const ANY_ROLE_NOTE = "any role the operator has defined, including ones that may page you or add members themselves "
  + "(an architect); every member added still counts against this one limit";
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit" });
// a message that asked for an answer: open (BLOCKING 4m / REPLY), or answered / resolved at hh:mm
function needsBadge(m) {
  if (!m.needs) return null;
  if (m.answered_ts) return el("span", { cls: "flag done", text: `${m.answered_by === 0 ? "resolved" : "answered"} ✓ ${hhmm(m.answered_ts)}` });
  return m.needs === "blocking"
    ? el("span", { cls: "flag blocking", text: `BLOCKING ${age(m.ts)}`, title: `${m.from_name} cannot continue until ${m.to_name} answers` })
    : el("span", { cls: "flag reply", text: "REPLY", title: `${m.from_name} wants an answer from ${m.to_name}` });
}
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
// elastic roles: `models` / `efforts` list what mesh_spawn may choose besides the defaults (absent or [] = fixed)
const elasticAlt = (list, own) => (Array.isArray(list) ? list.filter((x) => x !== own) : []);
// a role may use its own model / effort and every other entry of its list, most capable first as saved
const roleOptions = (list, own) => [own, ...elasticAlt(list, own)].filter(Boolean);
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

// ------------------------------------------------------------------ markdown (what agents write), as DOM nodes
// md:begin. A pure parser, text to a small tree: no DOM and no globals, so tests/test_md.py runs this part under node.
// ponytail: a hand-written subset (fenced code, inline code, # to ####, bold, italic, lists nested by indent, pipe
// tables, quotes, rules, http(s) links, paragraphs, a newline as a line break). Not handled, shown as literal text:
// raw HTML, setext headings, reference links, bare URLs, code inside a list item, table alignment, lazy quote lines.
// Upgrade path: vendor a real parser (markdown-it) plus a sanitizer (DOMPurify) when agents' text outgrows this.
// Every scan is linear: delimiters are found with indexOf, and a search that found nothing is not repeated.
const MD_MAX_DEPTH = 8;                               // quotes, lists and emphasis nest this deep; deeper stays literal
const MD_FENCE = /^ {0,3}(`{3,}|~{3,})(.*)$/;
const MD_HEAD = /^ {0,3}(#{1,4})[ \t]+(.*)$/;
const MD_ITEM = /^([ \t]*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
const MD_QUOTE = /^ {0,3}>/;
const MD_PUNCT = "\\`*_{}[]()#+-.!|>~<";

function mdIndent(s) {
  let n = 0;
  for (const ch of s) { if (ch === " ") n++; else if (ch === "\t") n += 4; else break; }
  return n;
}
function mdIsRule(line) {                             // three or more of one of - * _, spaces allowed between
  const s = line.replace(/[ \t]/g, "");
  return s.length >= 3 && "-*_".includes(s[0]) && s === s[0].repeat(s.length);
}
function mdCells(line) {                              // a table row's cells; \| is a literal pipe, as is one in `code`
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells = [];
  let cur = "", code = false;
  for (let k = 0; k < s.length; k++) {
    const ch = s[k];
    if (ch === "\\" && s[k + 1] === "|") { cur += "|"; k++; continue; }
    if (ch === "`") code = !code;
    if (ch === "|" && !code) { cells.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  cells.push(cur.trim());
  return cells;
}
function mdTableAt(lines, i) {                        // a header row with a pipe, then a rule row of as many cells
  if (!lines[i].includes("|") || i + 1 >= lines.length || !lines[i + 1].includes("-")) return false;
  const rule = mdCells(lines[i + 1]);
  return rule.every((c) => /^:?-+:?$/.test(c)) && rule.length === mdCells(lines[i]).length;
}
// only http(s), only what new URL() accepts, and no whitespace: a target with a space is not a link, so an unclosed
// "[x](https://..." in prose cannot swallow the words up to some later ")"; the text then stays as written
function mdHref(raw) {
  const u = raw.trim().replace(/^<(.*)>$/, "$1");
  if (!u || /\s/.test(u)) return null;
  try {
    const url = new URL(u);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch { return null; }
}

function mdParse(text, depth = 0) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [], i = 0, m;
  const flush = () => { if (para.length) { out.push({ t: "p", c: mdInline(para.join("\n")) }); para = []; } };
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { flush(); i++; continue; }
    if ((m = MD_FENCE.exec(line))) {                  // runs to its closing fence, or to the end when there is none
      flush();
      const ch = m[1][0], body = [];
      for (i++; i < lines.length; i++) {
        const c = lines[i].trim();
        if (c.length >= m[1].length && c === ch.repeat(c.length)) { i++; break; }
        body.push(lines[i]);
      }
      out.push({ t: "code", lang: m[2].trim(), text: body.join("\n") });
      continue;
    }
    if (mdIsRule(line)) { flush(); out.push({ t: "hr" }); i++; continue; }
    if ((m = MD_HEAD.exec(line))) {
      flush();
      let h = m[2].trimEnd(), j = h.length;           // a closing run of # goes, when a space comes before it
      while (j > 0 && h[j - 1] === "#") j--;
      if (j === 0 || h[j - 1] === " " || h[j - 1] === "\t") h = h.slice(0, j).trimEnd();
      out.push({ t: "h", level: m[1].length, c: mdInline(h) });
      i++;
      continue;
    }
    if (MD_QUOTE.test(line)) {
      flush();
      const inner = [];
      for (; i < lines.length && MD_QUOTE.test(lines[i]); i++) inner.push(lines[i].replace(/^ {0,3}> ?/, ""));
      out.push(depth >= MD_MAX_DEPTH ? { t: "p", c: mdInline(inner.join("\n")) } : { t: "quote", c: mdParse(inner.join("\n"), depth + 1) });
      continue;
    }
    if (mdTableAt(lines, i)) {
      flush();
      const head = mdCells(lines[i]), rows = [];
      for (i += 2; i < lines.length && lines[i].trim() && lines[i].includes("|"); i++) {
        const cells = mdCells(lines[i]);
        while (cells.length < head.length) cells.push("");   // a short row is padded; a long one keeps its extra cells
        rows.push(cells.map((c) => mdInline(c)));
      }
      out.push({ t: "table", head: head.map((c) => mdInline(c)), rows });
      continue;
    }
    if (MD_ITEM.test(line)) {
      flush();
      const [lists, next] = mdList(lines, i);
      out.push(...lists);
      i = next;
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  return out;
}

// a run of list lines: items, lines indented under an item (its continuation), and blank lines between items
function mdList(lines, i) {
  const items = [];
  while (i < lines.length) {
    const line = lines[i], m = MD_ITEM.exec(line);
    if (m && !mdIsRule(line)) { items.push({ indent: mdIndent(m[1]), ordered: /\d/.test(m[2]), marker: m[2], text: m[3] }); i++; continue; }
    if (!line.trim()) {
      let k = i + 1;
      while (k < lines.length && !lines[k].trim()) k++;
      if (k < lines.length && ((MD_ITEM.test(lines[k]) && !mdIsRule(lines[k])) || mdIndent(lines[k]) > items[items.length - 1].indent)) { i = k; continue; }
      break;
    }
    if (mdIndent(line) > items[items.length - 1].indent) { items[items.length - 1].text += "\n" + line.trim(); i++; continue; }
    break;
  }
  const root = { sub: [] }, stack = [{ indent: -1, node: root }];
  for (const it of items) {
    while (stack.length > 1 && stack[stack.length - 1].indent >= it.indent) stack.pop();
    if (stack.length > MD_MAX_DEPTH) stack.length = MD_MAX_DEPTH;   // deeper items become siblings at the limit
    const parent = stack[stack.length - 1].node;
    let list = parent.sub[parent.sub.length - 1];
    if (!list || list.ordered !== it.ordered) { list = { t: "list", ordered: it.ordered, items: [] }; parent.sub.push(list); }
    const node = { marker: it.marker, c: mdInline(it.text), sub: [] };
    list.items.push(node);
    stack.push({ indent: it.indent, node });
  }
  return [root.sub, i];
}

// inline: `code`, **bold** / __bold__, *italic* / _italic_ (an underscore inside a word stays literal, for
// snake_case paths), [text](http-url), ![alt](http-url) as a link, \-escapes, and a newline as a break
function mdInline(s, depth = 0) {
  const out = [], seen = new Map();
  let buf = "", i = 0, badClose = -1;
  const push = (node) => { if (buf) { out.push({ t: "text", v: buf }); buf = ""; } out.push(node); };
  // `from` only grows for a given tok, so a miss stays a miss and a hit at or after `from` is still the first one:
  // each tok's string is scanned once overall, however many openers ask (a run of "[" before one "]" stays linear)
  const find = (tok, from) => {
    let k = seen.get(tok);
    if (k === -1) return -1;
    if (k === undefined || k < from) { k = s.indexOf(tok, from); seen.set(tok, k); }
    return k;
  };
  const word = (c) => !!c && /[A-Za-z0-9]/.test(c);
  while (i < s.length) {
    const ch = s[i];
    if (ch === "\\" && i + 1 < s.length && MD_PUNCT.includes(s[i + 1])) { buf += s[i + 1]; i += 2; continue; }
    if (ch === "\n") { push({ t: "br" }); i++; continue; }
    if (ch === "`") {
      let n = 1;
      while (s[i + n] === "`") n++;
      const tick = "`".repeat(n);
      let k = find(tick, i + n);
      while (k >= 0 && s[k + n] === "`") {            // a longer run is not the closer: skip past it
        let e = k;
        while (s[e] === "`") e++;
        k = s.indexOf(tick, e);
      }
      if (k >= 0) {
        let v = s.slice(i + n, k).replace(/\n/g, " ");
        if (v.length > 2 && v[0] === " " && v[v.length - 1] === " ") v = v.slice(1, -1);
        push({ t: "code", v });
        i = k + n;
        continue;
      }
      buf += tick; i += n;
      continue;
    }
    if (ch === "*" || ch === "_") {
      const L = s[i + 1] === ch ? 2 : 1, tok = ch.repeat(L), after = s[i + L];
      if (depth < MD_MAX_DEPTH && (ch === "*" || !word(s[i - 1])) && after && after !== " " && after !== "\n") {
        const k = find(tok, i + L + 1);               // and the content is more than * and _ (a run of them is text)
        if (k >= 0 && s[k - 1] !== " " && s[k - 1] !== "\n" && (ch === "*" || !word(s[k + L])) && /[^*_]/.test(s.slice(i + L, k))) {
          push({ t: L === 2 ? "b" : "i", c: mdInline(s.slice(i + L, k), depth + 1) });
          i = k + L;
          continue;
        }
      }
      buf += ch; i++;
      continue;
    }
    if (ch === "[" || (ch === "!" && s[i + 1] === "[")) {
      const o = ch === "!" ? i + 1 : i, close = find("]", o + 1);
      if (close >= 0 && close !== badClose && s[close + 1] === "(") {   // a "](" already refused is not tried again
        const end = find(")", close + 2);
        const href = end >= 0 ? mdHref(s.slice(close + 2, end)) : null;
        if (href && depth < MD_MAX_DEPTH) {           // an image is only ever a link: nothing is fetched
          const label = s.slice(o + 1, close);
          push({ t: "a", href, c: label ? mdInline(label, depth + 1) : [{ t: "text", v: href }] });
          i = end + 1;
          continue;
        }
        badClose = close;
      }
      buf += s.slice(i, o + 1); i = o + 1;
      continue;
    }
    buf += ch; i++;
  }
  if (buf) out.push({ t: "text", v: buf });
  return out;
}
// md:end

// The DOM side: elements and text nodes only (el() sets textContent); the only attribute input reaches is a link's
// href, after mdHref() allowed it. Rendered trees are cached by text and cloned, as the feeds repaint every 2 s.
function mdInlineDom(nodes, parent) {
  for (const n of nodes) {
    if (n.t === "text") parent.append(n.v);
    else if (n.t === "br") parent.append(el("br"));
    else if (n.t === "code") parent.append(el("code", { text: n.v }));
    else {
      const node = n.t === "a" ? el("a", { href: n.href, rel: "noopener noreferrer", target: "_blank" }) : el(n.t === "b" ? "strong" : "em");
      mdInlineDom(n.c, node);
      parent.append(node);
    }
  }
}
function mdListDom(list) {
  const l = el(list.ordered ? "ol" : "ul");
  for (const it of list.items) {
    const li = el("li");
    if (list.ordered) li.append(el("span", { cls: "md-n", text: it.marker }));   // the number as written
    mdInlineDom(it.c, li);
    for (const sub of it.sub) li.append(mdListDom(sub));
    l.append(li);
  }
  return l;
}
function mdBlockDom(blocks, parent) {
  for (const b of blocks) {
    let node;
    if (b.t === "p" || b.t === "h") { node = el(b.t === "p" ? "p" : `h${b.level}`); mdInlineDom(b.c, node); }
    else if (b.t === "code") node = el("pre", {}, el("code", { text: b.text }));
    else if (b.t === "hr") node = el("hr");
    else if (b.t === "quote") { node = el("blockquote"); mdBlockDom(b.c, node); }
    else if (b.t === "list") node = mdListDom(b);
    else if (b.t === "table") {
      const row = (cells, tag) => el("tr", {}, ...cells.map((c) => { const td = el(tag); mdInlineDom(c, td); return td; }));
      node = el("div", { cls: "md-table" }, el("table", {}, el("thead", {}, row(b.head, "th")), el("tbody", {}, ...b.rows.map((r) => row(r, "td")))));
    }
    if (node) parent.append(node);
  }
}
const mdCache = new Map();
// renderMd(text): a <div class="md"> holding the text as markdown
function renderMd(text) {
  const key = String(text ?? "");
  let tpl = mdCache.get(key);
  if (!tpl) {
    if (mdCache.size >= 600) mdCache.clear();         // ponytail: drop-all cap; an LRU if repaints ever show it
    tpl = el("div", { cls: "md" });
    mdBlockDom(mdParse(key), tpl);
    mdCache.set(key, tpl);
  }
  return tpl.cloneNode(true);
}
// transcript events: a person's or an agent's words render as markdown; tool calls, results and errors stay raw.
// fleet.prose cuts at 4000 characters and saveGone at 2000, each ending in "…": a cut text stays plain, as a fence
// cut in half would render as garbage.
const cutText = (t, n) => (t.length > n ? `${t.slice(0, n - 1)}…` : t);
const MD_KINDS = new Set(["you", "claude", "gemini", "peer_in", "peer_out"]);
const proseNode = (kind, text) => (MD_KINDS.has(kind) && !(text.length >= 1999 && text.endsWith("…")) ? renderMd(text) : text);

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

// ------------------------------------------------------------------ header alerts (every tab)
// setAlert(id, {level: "warn"|"crit", text, title, onClick}) / clearAlert(id): one chip per open problem.
const alerts = new Map();
let alertsSig = "";
function setAlert(id, a) { alerts.set(id, a); paintAlerts(); }
function clearAlert(id) { if (alerts.delete(id)) paintAlerts(); }
function paintAlerts() {
  const sig = JSON.stringify([...alerts].map(([id, a]) => [id, a.level, a.text, a.title]));
  if (sig === alertsSig) return;                 // polled: do not rebuild (and drop focus) when nothing changed
  alertsSig = sig;
  $("alerts").replaceChildren(...[...alerts].map(([id, a]) => {
    const chip = el("button", { type: "button", cls: `alert-chip ${a.level === "crit" ? "crit" : "warn"}`,
      text: a.text, title: a.title || a.text, "data-alert": id });
    chip.addEventListener("click", () => (alerts.get(id)?.onClick || openAlertBox)(alerts.get(id) || a));
    return chip;
  }));
}
function openAlertBox(a) {
  $("alert-box-text").textContent = a.title || a.text;
  $("alert-box").hidden = false;
  $("alert-box-close").focus();
}
function closeAlertBox() { $("alert-box").hidden = true; }
$("alert-box-close").addEventListener("click", closeAlertBox);

// runners Almanac launches agents with; re-probed by the server, polled here every 15 s
const health = { doc: null, sig: "" };
const runnerMissing = (kind) => health.doc?.runners?.[kind]?.ok === false;
function runnerHelp(kind, r) {
  const what = kind === "gemini" ? "gemini runner (agy)" : "claude runner";
  if (r.reason) return `${what}: ${r.reason}.\nAlmanac looked in:\n  ${(r.searched || []).join("\n  ")}`;
  return `${what} not found. Almanac looked in:\n  ${(r.searched || []).join("\n  ")}\nInstall it, or set "${kind}" in config.json; `
    + "Almanac finds it on the next spawn without a restart.";
}
let healthOnce = null;
const firstHealth = () => (healthOnce ??= loadHealth());   // boot and startup share the first look
async function loadHealth() {
  try { health.doc = await api("/api/health"); } catch { return null; }
  for (const kind of ["claude", "gemini"]) {
    const r = health.doc.runners?.[kind];
    if (r && !r.ok) setAlert(`runner-${kind}`, { level: "warn", text: r.reason ? `▲ ${kind} runner refused (.cmd shim)` : `▲ ${kind} runner missing`,
      title: runnerHelp(kind, r) });
    else clearAlert(`runner-${kind}`);
  }
  const sig = `${runnerMissing("claude")}|${runnerMissing("gemini")}`;
  if (sig !== health.sig) { health.sig = sig; syncRunnerLabels(); }
  return health.doc;
}
// a Gemini option reads "(runner missing)" while agy cannot be found
function roleOptionText(r, long = true) {
  const base = `[${r.provider === "gemini" ? "G" : "C"}] ${r.title}${long ? ` (${r.id})` : ""}`;
  return runnerMissing(r.provider) ? `${base} (runner missing)` : base;
}
function syncRunnerLabels() {
  for (const sel of [$("spawn-provider"), $("role-provider")]) {
    const opt = sel && [...sel.options].find((o) => o.value === "gemini");
    if (!opt) continue;
    opt.dataset.text ??= opt.textContent;
    opt.textContent = runnerMissing("gemini") ? "gemini (runner missing)" : opt.dataset.text;
  }
  syncRoleControls();
  document.querySelectorAll("#squad-members select[data-field='role_id'] option, #strip-role option, #strip-add-role option").forEach((o) => {
    const r = state.roles.find((x) => x.id === o.value);
    if (r) o.textContent = roleOptionText(r, false);
  });
}

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
  const fleet = api("/api/fleet").catch(() => null);
  const checked = firstHealth();
  const runner = async (kind) => {
    const h = await checked;
    return !h ? "?" : h.runners?.[kind]?.ok ? "OK" : "MISSING";
  };
  // [text, what to print after it]; the last line waits for the runner check
  const lines = [["ALMANAC MISSION CONTROL v3 // CROSS-MODEL MESH"], ["(c) " + new Date().getFullYear()], [""],
    ["> door " + location.port + " ............ ".slice(location.port.length), async () => "OK"],
    ["> claude runner ......... ", () => runner("claude")],
    ["> gemini runner (agy) ... ", () => runner("gemini")],
    ["> scanning claude+gemini ", async () => { const f = await fleet; return f ? `${f.processes.length} found` : "FAIL"; }],
    ["> uds-messaging bus .... ", async () => "OK"], ["> telemetry ............ ", async () => "OK"], [""],
    [async () => (runnerMissing("claude") || runnerMissing("gemini") ? "READY, WITH WARNINGS." : "READY.")]];
  for (const [text, after] of lines) {
    if (typeof text === "function") await checked;
    const line = typeof text === "function" ? await text() : text;
    for (const ch of line) { if (skip) break; screen.textContent += ch; await new Promise((r) => setTimeout(r, 8)); }
    if (skip) break;
    if (after) screen.textContent += await after();
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
    refillRoleModels(multiTicked("role-models"));
    refillRoleEfforts();
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

// ROLES: models and efforts are each one multi-pick, a <details> of checkboxes. Saving stores every tick in
// models / efforts and the most capable tick as model / effort, the fallback when nobody picks (a member launched
// from the roster). A single tick saves an empty list, exactly as a fixed role was saved before.
// ponytail: capability order is a heuristic (model family, then the higher version; an alias counts as the newest).
// Serve a rank from /api/models if it ever picks the wrong default.
const MODEL_FAMILIES = ["fable", "mythos", "opus", "carbon", "argon", "sonnet", "pro", "flash", "haiku", "gpt-oss"];
function modelRank(m) {
  const s = String(m).toLowerCase(), fam = MODEL_FAMILIES.findIndex((f) => s.includes(f));
  const v = s.match(/(\d+)[.-](\d+)/);
  return [fam < 0 ? MODEL_FAMILIES.length : fam, v ? -(Number(v[1]) + Number(v[2]) / 100) : -1e9];
}
const byCapability = (a, b) => { const x = modelRank(a), y = modelRank(b); return x[0] - y[0] || x[1] - y[1]; };
const EFFORT_DESC = ["max", "xhigh", "high", "medium", "low"];
const byEffort = (a, b) => (EFFORT_DESC.indexOf(a) + 1 || 99) - (EFFORT_DESC.indexOf(b) + 1 || 99);

const multiTicked = (id) => [...$(`${id}-list`).querySelectorAll("input:checked")].map((i) => i.value);
function paintMulti(id, fmt) {
  const t = multiTicked(id);
  $(`${id}-sum`).textContent = t.length ? t.map(fmt).join(", ") : "runner default";   // nothing ticked saves empty
}
function fillMulti(id, options, ticked, fmt) {           // a ticked value outside the options stays, ticked
  const keep = new Set(ticked);
  $(`${id}-list`).replaceChildren(...[...new Set([...options, ...ticked])].map((v) => el("label", { cls: "chk" },
    el("input", { type: "checkbox", value: v, checked: keep.has(v) }), ` ${fmt(v)}`)));
  paintMulti(id, fmt);
}
const roleProv = () => $("role-provider").value || "claude";
const refillRoleModels = (ticked) => fillMulti("role-models", modelsForProvider(roleProv()), ticked, String);
function refillRoleEfforts(ticked = multiTicked("role-efforts")) {      // what the ticked models allow, highest first
  const models = multiTicked("role-models");
  const opts = [...new Set((models.length ? models : [""]).flatMap((m) => effortsForModel(roleProv(), m)))].sort(byEffort);
  fillMulti("role-efforts", opts, ticked, fmtEffortLabel);
}
$("role-models-list").addEventListener("change", () => { paintMulti("role-models", String); refillRoleEfforts(); });
$("role-efforts-list").addEventListener("change", () => paintMulti("role-efforts", fmtEffortLabel));
$("role-models-add").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();                                    // inside the role form: Enter adds, it does not save
  const v = e.target.value.trim();
  if (!v) return;
  refillRoleModels([...multiTicked("role-models"), v]);
  refillRoleEfforts();
  e.target.value = "";
});
$("role-provider").addEventListener("change", () => {
  const opts = modelsForProvider(roleProv());
  refillRoleModels(multiTicked("role-models").filter((m) => opts.includes(m)));
  refillRoleEfforts();
});
document.addEventListener("click", (e) => {             // a multi-pick closes on a click outside it
  for (const d of document.querySelectorAll("details.multi[open]")) if (!d.contains(e.target)) d.open = false;
});
refillRoleModels([]);
refillRoleEfforts(["high"]);

// a role's context_policy: none | fresh-per-task | compact-at-<tokens>; the form splits it into a select and a token field
const fmtContextPolicy = (p) => (p || "none") === "fresh-per-task" ? "fresh per task"
  : /^compact-at-\d+$/.test(p || "") ? `compact at ${Number(p.slice(11)).toLocaleString("en-US")}` : "none";
function setRoleContext(p) {
  const compact = /^compact-at-/.test(p || "");
  $("role-context").value = compact ? "compact" : p === "fresh-per-task" ? p : "none";
  $("role-context-n").value = compact ? p.slice(11) : "";
  $("role-context-n-field").hidden = !compact;
}
$("role-context").addEventListener("change", () => {
  const compact = $("role-context").value === "compact";
  $("role-context-n-field").hidden = !compact;
  if (compact && !$("role-context-n").value) $("role-context-n").value = "300000";
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
    ...state.roles.map((r) => el("option", { value: r.id, text: roleOptionText(r) })),
  );
  if (cur && state.roles.some((r) => r.id === cur)) spawnRole.value = cur;
  else if (state.roles.length && !cur) spawnRole.value = state.roles[0].id;
  syncSpawnModelField();

  // Refresh any squad member role selects; each row then re-fills the fields the operator has not edited
  if (state.roles.length) document.querySelectorAll("#squad-members .member-row").forEach((row) => {   // not before roles load: the fallback rows stay
    const sel = row.querySelector("select[data-field='role_id']");
    const v = sel.value;
    sel.replaceChildren(...state.roles.map((r) => el("option", { value: r.id, text: roleOptionText(r, false) })));
    if (v && state.roles.some((r) => r.id === v)) sel.value = v;
    row.syncFromRole();
  });

  // the launch form's "architect may add" choice
  const spawnSel = $("squad-spawn-role"), keep = spawnSel.value;
  spawnSel.replaceChildren(el("option", { value: "", text: "(nobody)" }), el("option", { value: "*", text: "any role", title: ANY_ROLE_NOTE }),
    ...state.roles.map((r) => el("option", { value: r.id, text: roleOptionText(r, false) })));
  spawnSel.value = keep === "*" || state.roles.some((r) => r.id === keep) ? keep : "";

  // Render roles table inside Mesh drawer
  const tbody = $("roles-table")?.querySelector("tbody");
  if (tbody) {
    tbody.replaceChildren(...state.roles.map((r) => {
      const tr = el("tr", {},
        el("td", {}, r.id, r.may_page_operator ? el("span", { cls: "amber", text: " ✉", title: "may page the operator" }) : null,
          r.may_spawn ? el("span", { cls: "cyan", text: " ⊕", title: "may add squad members" }) : null,
          r.may_restart ? el("span", { cls: "red", text: " ⟳", title: "may restart Almanac" }) : null),
        el("td", {}, providerBadge(r.provider)),
        el("td", { text: r.title }),
        el("td", { cls: "model", text: roleOptions(r.models, r.model).map(fmtModel).join(", ") || "–" }),
        el("td", {}, effortBadge(r.effort), elasticAlt(r.efforts, r.effort).length
          ? el("span", { cls: "dim", text: `, ${elasticAlt(r.efforts, r.effort).map(fmtEffortLabel).join(", ")}` }) : null),
        el("td", { cls: "dim", text: fmtContextPolicy(r.context_policy) }),
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

// member-defaults:begin. A pure function, no DOM: tests/test_member_defaults.py runs this part under node.
const ROLE_SHORT = { architect: "arch", implementer: "impl", reviewer: "rev", orchestrator: "orch" };   // mirrors spawner.ROLE_SHORT
function memberDefaults(role, usedHandles = []) {
  const first = role.id.split("-")[0];
  const base = `${ROLE_SHORT[first] || first.slice(0, 6) || "agent"}-${role.provider}`;
  let handle = base;
  for (let n = 2; usedHandles.includes(handle); n++) handle = `${base}-${n}`;
  return {
    handle,
    model: role.model || "",
    effort: role.effort === "med" ? "medium" : role.effort || "",
    workspace_mode: role.workspace_mode || "shared",
  };
}
// member-defaults:end

const FALLBACK_ROLES = [
  { id: "architect-claude", provider: "claude", title: "System Architect (Claude)", model: "opus", effort: "high" },
  { id: "implementer-gemini", provider: "gemini", title: "Implementer (Gemini)", model: "", effort: "high", workspace_mode: "worktree" },
  { id: "reviewer-gemini", provider: "gemini", title: "Peer Reviewer (Gemini)", model: "", effort: "high" },
];

// A row's handle / model / effort / isolation follow its role until the operator edits that field (dataset.touched).
function addSquadMemberRow(roleId = "") {
  const roles = state.roles.length ? state.roles : FALLBACK_ROLES;
  const roleSel = el("select", { "data-field": "role_id", "aria-label": "Member role" },
    ...roles.map((r) => el("option", { value: r.id, text: roleOptionText(r, false) })));
  if (roleId && roles.some((r) => r.id === roleId)) roleSel.value = roleId;
  const getRole = () => (state.roles.length ? state.roles : roles).find((x) => x.id === roleSel.value) || roles[0];

  const nameInp = el("input", {
    "data-field": "agent_name", placeholder: "handle (auto)",
    autocorrect: "off", autocapitalize: "none", spellcheck: "false", "aria-label": "Member handle",
  });
  const modelInp = el("input", {
    "data-field": "model", placeholder: "model (auto)",
    autocorrect: "off", autocapitalize: "none", spellcheck: "false", "aria-label": "Member model",
  });
  const effSel = el("select", { "data-field": "effort", "aria-label": "Reasoning effort" });
  const wsSel = el("select", { "data-field": "workspace_mode", "aria-label": "Workspace isolation" },
    el("option", { value: "", text: "(role default)" }),
    el("option", { value: "shared", text: "shared" }),
    el("option", { value: "worktree", text: "worktree" }),
  );
  const rmBtn = el("button", { type: "button", cls: "btn danger", text: "[✕]", title: "remove member" });
  const row = el("div", { cls: "member-row field" }, roleSel, nameInp, modelInp, effSel, wsSel, rmBtn);

  row.syncFromRole = () => {
    const r = getRole();
    if (!r) return;
    const used = [...document.querySelectorAll("#squad-members .member-row")].filter((x) => x !== row)
      .map((x) => x.querySelector("input[data-field='agent_name']").value.trim());
    const d = memberDefaults(r, used);
    const prevProv = row.dataset.provider;
    row.dataset.provider = r.provider;
    if (prevProv && prevProv !== r.provider) modelInp.dataset.touched = effSel.dataset.touched = "";   // model and effort are per provider: the new role's replace the operator's
    modelInp.placeholder = r.model ? `(role: ${r.model})` : "model (auto)";
    const free = (f) => f.dataset.touched !== "1" && document.activeElement !== f;   // never rewrite a field the operator is in
    if (free(nameInp)) nameInp.value = d.handle;
    if (free(modelInp)) modelInp.value = d.model;
    if (document.activeElement !== effSel) {
      syncEffortSelect(effSel, r.provider || "claude", modelInp.value.trim() || r.model || "",
        r.effort ? `(role: ${fmtEffortLabel(r.effort)})` : "(role)");
      if (free(effSel)) effSel.value = [...effSel.options].some((o) => o.value === d.effort) ? d.effort : "";
    }
    if (free(wsSel)) wsSel.value = d.workspace_mode;
  };

  const follow = (input, ev, after) => input.addEventListener(ev, () => {
    input.dataset.touched = input.value.trim() ? "1" : "";
    after?.();
  });
  follow(nameInp, "input");
  follow(modelInp, "input", () => row.syncFromRole());
  for (const inp of [nameInp, modelInp]) inp.addEventListener("blur", () => { if (!inp.value.trim()) row.syncFromRole(); });   // a cleared field follows the role again once left
  follow(effSel, "change");
  follow(wsSel, "change");
  attachModelPicker(modelInp, {
    getProvider: () => getRole()?.provider || "claude",
    onPick: () => { modelInp.dataset.touched = modelInp.value.trim() ? "1" : ""; row.syncFromRole(); },
  });
  roleSel.addEventListener("change", () => row.syncFromRole());
  rmBtn.addEventListener("click", () => { if ($("squad-members").children.length > 1) row.remove(); });
  $("squad-members").append(row);
  row.syncFromRole();
}

function seedDefaultSquadMembers() {
  $("squad-members").replaceChildren();
  for (const id of ["architect-claude", "implementer-gemini", "reviewer-gemini"]) addSquadMemberRow(id);
}

$("squad-add-member").addEventListener("click", () => addSquadMemberRow("implementer-gemini"));

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
    const r = await post("/api/squads", { name, cwd, brief, members, spawn_role: $("squad-spawn-role").value || null,
      spawn_limit: Number($("squad-spawn-limit").value || 0), plan_first: $("squad-plan-first").checked });
    // a member the server refused (no runner, a launch that died) says why
    const refused = (r.squad?.agents || []).filter((a) => !a.ok).map((a) => `${a.agent_name}: ${a.error || "failed"}`);
    if (r.ok) {
      msg.textContent = `squad ${r.squad.name} live: ${r.launched}/${members.length} agents on #squad`
        + (refused.length ? `; refused ${refused.join("; ")}` : "");
      setTimeout(() => { loadFleet(); setMeshSquad(r.squad.id); show("mesh"); }, refused.length ? 4000 : 1000);
    } else {
      msg.textContent = `partial/failed: launched ${r.launched || 0}/${members.length}${refused.length ? `; ${refused.join("; ")}` : ""}`;
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
  if (p.stall) {
    // since / ts are the transcript's ISO timestamps (fleet.read_tail); epoch seconds are taken as well
    const at = [p.stall.since, p.stall.ts].map((v) => (typeof v === "number" ? v : Date.parse(v) / 1000)).find(Number.isFinite);
    return el("span", { cls: "flag stalled", text: `▲ STALLED${p.stall.error ? ` · ${p.stall.error}` : ""}`,
      title: [p.stall.text, at ? `since ${age(at)} ago` : ""].filter(Boolean).join("\n") || "stopped on an API error" });
  }
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
  if (doc.context_warn) state.ctxWarn = doc.context_warn;
  trackSessions();       // before any early return: every fleet refresh notices sessions leaving, whatever the tab
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
      el("td", { cls: `r${ctxWarn(p.context)}`, text: p.role === "session" ? ctxText(p.context, p.context_total) : "", title: p.context ? `${ctxTitle(p.context_total)} (${p.context})` : null }),
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
  const kindsQ = state.fKinds ? `&kinds=${encodeURIComponent(state.fKinds)}` : "";
  try { doc = await api(`/api/session?pid=${state.sel}${kindsQ}`); } catch (err) { $("detail-head").replaceChildren(el("span", { cls: "red", text: err.message })); return; }
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
  if (u && u.turns) add("worked", `${fmtDur(u.work_s)} work · ${fmtDur(u.wait_s)} waiting · ${u.turns} turn${u.turns === 1 ? "" : "s"}`);
  if (u && u.messages) add("tokens", `${num(u.output, 0)} out · ${num(u.input + u.cache_create + u.cache_read, 0)} context · ${num(u.calls, 0)} tool calls · ${num(u.read_back_kb, 0)} KB back`);
  $("detail-head").replaceChildren(el("div", { cls: "title", text: rowName(p) }), kv);

  const feed = $("feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  if (!doc.events.length) feed.replaceChildren(el("li", { cls: "result", text: !p.session_id ? "no session transcript for this process" : state.fKinds ? "nothing of this kind yet" : "no transcript yet" }));
  else {
    const items = doc.events.map((e, i) => {
      const key = `${e.ts}|${e.kind}|${e.text.slice(0, 40)}|${i - doc.events.length}`;
      const li = el("li", { cls: `${e.kind}${state.feedKeys.size && !state.feedKeys.has(key) && !reduced ? " new" : ""}`, title: e.ts ? new Date(e.ts).toLocaleString() : null }, proseNode(e.kind, e.text));
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
  peerFromProcess(p);
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

const GONE_MAX = 12;                       // ended tiles kept at most, the oldest dropped first
const gallerySquad = { id: get("g-squad", ""), tried: 0 };
const galleryPrev = new Map();             // key -> the last fleet row seen, to notice a session leaving whatever the filter

const liveSessions = () => (state.fleet?.processes || []).filter((p) => p.role === "session");
const galleryKeeps = (p) => state.pins.has(keyOf(p)) || !!p.squad_id;
function galleryShows(p) {
  switch (state.gMode) {
    case "pinned": return state.pins.has(keyOf(p));
    case "busy": return active(p);
    case "squad": return gallerySquad.id ? p.squad_id === gallerySquad.id : !!p.squad_id;
    default: return true;
  }
}
function gallerySessions() { return liveSessions().filter(galleryShows); }

// Run on every fleet refresh, whatever the tab: a pinned or squad session that left the fleet keeps its tile,
// ended; the rest just go. This is also the one place pins are pruned: a pin lives while its session is live or
// while an ended tile holds it ([DISMISS ✕] and trimGone drop the tile, and the pin goes on the next refresh).
function trackSessions() {
  if (!state.fleet || state.fleet.error) return;
  const live = liveSessions();
  const liveKeys = new Set(live.map(keyOf));
  let changed = false;
  for (const [k, p] of galleryPrev) {
    if (liveKeys.has(k) || !galleryKeeps(p)) continue;
    let t = state.tiles.get(k);
    if (!t) { t = makeTile(p); state.tiles.set(k, t); }
    if (!t.gone) { endTile(t, p); changed = true; }
  }
  for (const [k, t] of state.tiles) if (t.gone && liveKeys.has(k)) { t.gone = 0; t.goneQ = ""; t.feedSig = "x"; changed = true; }
  galleryPrev.clear();
  for (const p of live) galleryPrev.set(keyOf(p), p);
  if (changed) { trimGone(); saveGone(); }
  const stale = [...state.pins].filter((k) => !liveKeys.has(k) && !state.tiles.get(k)?.gone);
  if (stale.length) { stale.forEach((k) => state.pins.delete(k)); put("pins", JSON.stringify([...state.pins])); }
}

// the last two segments of the folder; the title has the full path and, for a worktree, the repo it belongs to
function folderLabel(p) {
  const full = p.cwd || p.worktree_path || "";
  const sep = full.includes("\\") ? "\\" : "/";
  const tail = full.split(/[\\/]+/).filter(Boolean).slice(-2).join(sep);
  const norm = (s) => String(s || "").replace(/[\\/]+/g, "/").replace(/\/$/, "").toLowerCase();
  const lines = [full];
  if (p.worktree_path) {
    if (norm(p.worktree_path) !== norm(full)) lines.push(`worktree ${p.worktree_path}`);
    lines.push(`worktree of ${p.worktree_of || "an unknown repo"}`);
  }
  return el("span", { cls: "dim where", text: `${p.worktree_path ? "⑂ " : ""}${tail}`, title: lines.filter(Boolean).join("\n") || null });
}

function saveGone() {
  const recs = [...state.tiles.values()].filter((t) => t.gone).sort((a, b) => a.gone - b.gone).map((t) => ({
    p: t.p, gone: t.gone,
    events: (t.events || []).slice(-60).map((e) => ({ ts: e.ts, kind: e.kind, text: cutText(String(e.text ?? ""), 2000) })),
  }));
  put("g-gone", JSON.stringify(recs));
}
function trimGone() {
  const gone = [...state.tiles.values()].filter((t) => t.gone).sort((a, b) => a.gone - b.gone);
  for (const t of gone.slice(0, Math.max(0, gone.length - GONE_MAX))) { t.box.remove(); state.tiles.delete(keyOf(t.p)); }
}
function endTile(t, p) {
  t.gone = Date.now(); t.goneQ = ""; t.armed = 0;
  t.p = { ...p, status: "ended" };
}
// one fetch of the final output once a session has ended: /api/mesh/context falls back to the stored transcript
async function endedFeed(t, q) {
  t.goneQ = q;
  const sid = t.p.session_id;
  if (!sid) return;
  let doc;
  try {
    const res = await fetch(`/api/mesh/context?peer=${encodeURIComponent(sid)}&${q}`, { credentials: "same-origin" });
    if (!res.ok) return;                   // a 404 keeps the feed the tile already had
    doc = await res.json();
  } catch { return; }
  if (!t.gone || state.tiles.get(keyOf(t.p)) !== t) return;
  paintTile(t, { process: t.p, events: doc.events || [], meta: doc.meta, usage: doc.usage });
  saveGone();
}
function restoreGone() {
  let recs;
  try { recs = JSON.parse(get("g-gone", "[]")); } catch { recs = []; }
  if (!Array.isArray(recs)) return;
  for (const r of recs.slice(-GONE_MAX)) {
    if (!r || typeof r.p !== "object" || r.p === null || r.p.pid === undefined || state.tiles.has(keyOf(r.p))) continue;
    const t = makeTile(r.p);
    t.box.classList.remove("tile-enter");
    t.gone = Number(r.gone) || Date.now(); t.goneQ = "";
    state.tiles.set(keyOf(r.p), t);
    const events = (Array.isArray(r.events) ? r.events : []).filter((e) => e && typeof e === "object")
      .map((e) => ({ ts: e.ts, kind: String(e.kind || "result"), text: String(e.text ?? "") }));
    paintTile(t, { process: t.p, events });
  }
}
async function fillGallerySquads() {
  if (!state.squads.length && Date.now() - gallerySquad.tried > 30000) {
    gallerySquad.tried = Date.now();
    try { state.squads = (await api("/api/squads?limit=50")).squads || []; } catch { /* the filter can wait */ }
  }
  const sel = $("g-squad");
  const ids = state.squads.map((s) => s.id);
  if (gallerySquad.id && !ids.includes(gallerySquad.id)) ids.push(gallerySquad.id);
  const sig = ids.join(",");
  if (sel.dataset.sig !== sig) {
    sel.replaceChildren(el("option", { value: "", text: "any squad" }), ...ids.map((id) => {
      const s = state.squads.find((x) => x.id === id);
      return el("option", { value: id, text: s ? `${s.name} (${s.id})` : id });
    }));
    sel.dataset.sig = sig;
  }
  sel.value = gallerySquad.id;
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
  const chatBtn = el("button", { type: "button", cls: "btn", text: "[CHAT ▸]", title: "open its transcript in the chats view", hidden: true });
  const dismiss = el("button", { type: "button", cls: "btn", text: "[DISMISS ✕]", title: "forget this ended tile", hidden: true });
  box.append(head, feed, form, el("div", { cls: "actions pad" }, remote, pin, wide, meshBtn, open, killBtn, chatBtn, dismiss, msg));
  const t = { box, head, feed, input, form, msg, remote, pin, wide, meshBtn, open, kill: killBtn, chat: chatBtn, dismiss, p,
    feedSig: "", armed: 0, gone: 0, goneQ: "", events: [] };

  chatBtn.addEventListener("click", () => {
    if (!t.p.session_id) return;
    show("chats"); openChat(t.p.session_id);
  });
  dismiss.addEventListener("click", () => {
    t.box.remove(); state.tiles.delete(key);
    if (state.pins.delete(key)) put("pins", JSON.stringify([...state.pins]));
    saveGone(); loadGallery();
  });

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
    peerFromProcess(t.p);
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
  const ended = !!t.gone;
  t.box.classList.toggle("gone", ended);
  t.box.classList.toggle("busy", !ended && active(p));
  t.box.classList.toggle("wide", state.wide.has(key));
  const u = doc?.usage || t.usage;
  t.usage = u;
  const m = meta.model || p.model;
  t.head.replaceChildren(...[
    providerBadge(p.provider),
    ended ? el("span", { cls: "amber", text: `ended ${hhmm(t.gone / 1000)}`, title: new Date(t.gone).toLocaleString() }) : rowState(p),
    p.agent_role ? el("span", { cls: "role-tag", text: p.agent_role }) : null,
    el("span", { cls: "model", text: fmtModel(m), title: m || "no assistant reply yet" }), effortBadge(meta.effort || p.effort),
    ended ? null : udsBadge(p.has_uds, p.pipe),
    ended ? null : el("span", { cls: "dim", text: `cpu ${p.cpu === null ? "–" : num(p.cpu) + "%"} · ${p.mem_mb}M · up ${age(p.started)}` }),
    u && u.messages ? el("span", { cls: "dim", text: `· ${num(u.output, 0)} out tok` }) : null,
    workShort(u) ? el("span", { cls: "dim", text: `· ${workShort(u)}` }) : null,
    folderLabel(p)].filter(Boolean));
  t.pin.textContent = state.pins.has(key) ? "[UNPIN ◆]" : "[PIN ◇]";
  t.wide.textContent = state.wide.has(key) ? "[NARROW ⇥]" : "[WIDE ⇔]";
  for (const n of [t.form, t.kill, t.pin, t.wide, t.meshBtn, t.open]) n.hidden = ended;
  t.chat.hidden = t.dismiss.hidden = !ended;
  t.chat.disabled = !p.session_id;
  t.remote.hidden = ended || !p.remote_url;
  if (p.remote_url && !ended) t.remote.href = p.remote_url;
  t.input.placeholder = active(p) ? "busy: queues until free" : `send via ${p.has_uds ? "uds-messaging" : "console"}`;
  if (!doc || !Array.isArray(doc.events)) return;      // a process-only paint leaves the feed as it is
  const events = doc.events;
  t.events = events;
  const sig = `${events.length}|${events.length ? events[events.length - 1].ts + events[events.length - 1].text.slice(0, 30) : ""}`;
  if (sig === t.feedSig) return;
  const fresh = t.feedSig !== "";
  t.feedSig = sig;
  const atBottom = t.feed.scrollHeight - t.feed.scrollTop - t.feed.clientHeight < 40;
  const prev = new Set([...t.feed.children].map((li) => li.dataset.k));
  t.feed.replaceChildren(...(events.length ? events.map((e) => {
    const k = `${e.ts}|${e.kind}|${e.text.slice(0, 40)}`;
    const li = el("li", { cls: `${e.kind}${fresh && !prev.has(k) && !reduced ? " new" : ""}`, title: e.ts ? new Date(e.ts).toLocaleString() : null }, proseNode(e.kind, e.text));
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
  $("g-squad").hidden = state.gMode !== "squad";
  if (state.gMode === "squad") await fillGallerySquads();
  const live = liveSessions();     // ended tiles were made by trackSessions() in the loadFleet() before this
  const list = live.filter(galleryShows);
  const ended = [...state.tiles.values()].filter((t) => t.gone).sort((a, b) => b.gone - a.gone);
  const endedShown = ended.filter((t) => galleryShows(t.p));
  const sq = state.squads.find((s) => s.id === gallerySquad.id);
  const adj = state.gMode === "pinned" ? "pinned " : state.gMode === "busy" ? "busy " : "";
  const inSquad = state.gMode !== "squad" ? "" : gallerySquad.id ? ` in ${sq ? sq.name : gallerySquad.id}` : " in a squad";
  $("g-count").textContent = `${list.length} ${adj}tile${list.length === 1 ? "" : "s"}${inSquad}`
    + `${endedShown.length ? ` + ${endedShown.length} ended` : ""} · ${state.pins.size} pinned · ${live.length} sessions running`;
  $("g-empty").hidden = list.length + endedShown.length > 0;
  $("g-empty").textContent = state.gMode === "pinned" ? "nothing pinned: click ◇ on a fleet row, press p, or use [PIN ◇] on a tile"
    : state.gMode === "busy" ? "no session is busy right now"
    : state.gMode === "squad" ? (gallerySquad.id ? "no session of this squad is running or recently ended" : "no squad session is running or recently ended")
    : "no Claude or Gemini sessions running";
  const keys = new Set(list.map(keyOf));
  for (const [k, t] of state.tiles) if (!t.gone && !keys.has(k)) { t.box.remove(); state.tiles.delete(k); }
  for (const t of ended) if (!endedShown.includes(t)) t.box.remove();     // filtered out, still remembered
  for (const p of list) if (!state.tiles.has(keyOf(p))) state.tiles.set(keyOf(p), makeTile(p));
  const order = [...list.map((p) => state.tiles.get(keyOf(p)).box), ...endedShown.map((t) => t.box)];
  if (order.length !== grid.children.length || order.some((box, i) => grid.children[i] !== box)) grid.append(...order);
  for (const p of list) paintTile(state.tiles.get(keyOf(p)), { process: p });
  const kindsQ = state.gKinds ? `&kinds=${encodeURIComponent(state.gKinds)}` : "";
  const endedQ = `limit=${LIMITS[state.gHeight] || 30}${kindsQ}`;
  for (const t of endedShown) { paintTile(t, null); if (t.goneQ !== endedQ) endedFeed(t, endedQ); }
  if (!list.length) return;
  let doc;
  try { doc = await api(`/api/sessions?pids=${list.map((p) => p.pid).join(",")}&limit=${LIMITS[state.gHeight] || 30}${kindsQ}`); } catch { return; }
  for (const d of doc.sessions) {
    if (d.gone) continue;
    const t = state.tiles.get(keyOf(d.process));
    if (t && !t.gone) paintTile(t, d);
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
fillKindSeg("g-kinds", state.gKinds);
$("g-kinds").addEventListener("click", (e) => {
  if (e.target.dataset.v === undefined) return;
  state.gKinds = e.target.dataset.v; put("g-kinds", state.gKinds);
  pressKindSeg("g-kinds", state.gKinds);
  for (const t of state.tiles.values()) t.feedSig = "x";     // one choice for every tile; repaint them all
  loadGallery();
});
$("g-squad").addEventListener("change", (e) => {
  gallerySquad.id = e.target.value; put("g-squad", gallerySquad.id);
  loadGallery();
});
queueMicrotask(restoreGone);    // after the whole script has run: paintTile uses helpers declared further down
fillKindSeg("fleet-kinds", state.fKinds);
$("fleet-kinds").addEventListener("click", (e) => {
  if (e.target.dataset.v === undefined) return;
  state.fKinds = e.target.dataset.v; put("f-kinds", state.fKinds);
  pressKindSeg("fleet-kinds", state.fKinds);
  state.feedKeys = new Set();
  $("feed").dataset.pid = "";                                // scroll to the newest of the new selection
  loadDetail();
});

// ------------------------------------------------------------------ mesh: peer roster + context stream + contact stream
function populateRoleForm(r) {
  const prov = r ? (r.provider || "claude") : "claude";
  const mod = r ? (r.model || "") : "";
  // a stored role shows its effort as stored (empty = runner default); a new role starts at high
  const eff = r ? ((r.effort === "med" ? "medium" : r.effort) || "") : "high";
  $("role-id").value = r ? r.id : "";
  $("role-title").value = r ? r.title : "";
  $("role-provider").value = prov;
  refillRoleModels([...new Set([mod, ...(r?.models || [])])].filter(Boolean));
  refillRoleEfforts([...new Set([eff, ...(r?.efforts || [])])].filter(Boolean));
  $("role-workspace").value = r ? (r.workspace_mode || "shared") : "shared";
  setRoleContext(r ? r.context_policy : "none");
  $("role-prompt").value = r ? (r.system_prompt || "") : "";
  $("role-page").checked = !!r?.may_page_operator;
  $("role-grow").checked = !!r?.may_spawn;
  $("role-restart").checked = !!r?.may_restart;
  $("role-channels").value = (r?.channels || []).join(", ");
  $("role-commands").value = (r?.allowed_commands || []).join("\n");
  $("role-dirs").value = (r?.add_dirs || []).join("\n");
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
  const models = multiTicked("role-models").sort(byCapability), efforts = multiTicked("role-efforts").sort(byEffort);
  btn.disabled = true;
  try {
    const r = await post("/api/roles", {
      id: $("role-id").value.trim(),
      title: $("role-title").value.trim(),
      provider: $("role-provider").value,
      model: models[0] || null,                         // the most capable tick, used when nobody picks; none = runner default
      effort: efforts[0] || null,
      workspace_mode: $("role-workspace").value,
      system_prompt: $("role-prompt").value.trim(),
      may_page_operator: $("role-page").checked,
      may_spawn: $("role-grow").checked,
      may_restart: $("role-restart").checked,          // a server without the flag ignores the key
      channels: $("role-channels").value.split(",").map((s) => s.trim()).filter(Boolean),
      // every tick, most capable first; a single tick saves [] (the role is fixed at it, as before)
      models: models.length > 1 ? models : [],
      efforts: efforts.length > 1 ? efforts : [],
      context_policy: $("role-context").value === "compact" ? `compact-at-${$("role-context-n").value.trim()}` : $("role-context").value,
      allowed_commands: $("role-commands").value,
      add_dirs: $("role-dirs").value,
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
$("mesh-refresh").addEventListener("click", () => { planState.stale = true; backlogState.rev = undefined; loadMesh(); if (meshState.selPeer) loadPeerContext(); });

// The selected peer is held by session id when there is one (a handle can be reused across squads, and
// /api/mesh/context?peer=<handle> opens the newest live session of that name); the handle is kept beside it
// for display, the "selected peer" scope and #mesh-to.
const isSelPeer = (p) => !!meshState.selPeer
  && (p.session_id === meshState.selPeer || p.name === meshState.selPeer || String(p.pid) === meshState.selPeer);
function selPeerName() {
  const p = meshState.peers.find(isSelPeer);
  return p ? p.name : meshState.selName || meshState.selPeer;
}
// #mesh-to: what the page writes there is replaced on a squad change; what the operator typed is left alone
function setMeshTo(v) { $("mesh-to").value = v; delete $("mesh-to").dataset.typed; }
$("mesh-to").addEventListener("input", (e) => { e.target.dataset.typed = "1"; });
function selectPeer(name, sid, setTo = true) {
  meshState.selPeer = sid || name || null;
  meshState.selName = name || sid || null;
  meshState.ctxSig = "";
  closeTask();
  if (setTo && name && document.activeElement !== $("mesh-to")) setMeshTo(name);
}
function pickPeer(name, sid) { selectPeer(name, sid); loadMesh(); loadPeerContext(); }
// a jump from FLEET or GALLERY lands on the peer's own squad, so the roster, topology and heatmap include it
function peerFromProcess(p) {
  setMeshSquad(p.squad_id || "");
  selectPeer(p.name || String(p.pid), p.session_id);
}
// every squad change goes through here: the selection and #mesh-to are re-checked against the new roster
// in loadMesh(), and an edge pair filter from the old squad is dropped
function setMeshSquad(id) {
  meshState.squadFilter = id || "";
  meshState.squadChosen = true;
  meshState.revalidate = true;
  closeTask();
  setPairFilter(null, null);
}
function clearPeerContext() {
  $("mesh-ctx-box").setAttribute("data-title", "PEER CONTEXT STREAM");
  $("mesh-ctx-head").replaceChildren(el("span", { cls: "dim", text: "no peer selected" }));
  $("mesh-ctx-feed").replaceChildren();
}
$("mesh-squad-filter").addEventListener("change", (e) => {
  setMeshSquad(e.target.value);
  loadMesh();
});
fillKindSeg("mesh-ctx-kinds", meshState.ctxKinds);
$("mesh-ctx-kinds").addEventListener("click", (e) => {
  if (e.target.dataset.v === undefined) return;
  meshState.ctxKinds = e.target.dataset.v;
  meshState.ctxSig = "";
  pressKindSeg("mesh-ctx-kinds", meshState.ctxKinds);
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
  if (sq !== meshState.squadFilter) return;       // the squad changed while this was in flight: the newer call paints
  meshState.peers = doc.peers || [];
  if (doc.context_warn) state.ctxWarn = doc.context_warn;
  meshState.messages = doc.messages || [];
  meshState.open = doc.open || [];
  if (doc.roles?.length) { state.roles = doc.roles; syncRoleControls(); }
  state.squads = doc.squads || [];
  if (!meshState.squadFilter && !meshState.squadChosen) {
    meshState.squadChosen = true;                 // once: "all peers" chosen later stays chosen
    const live = new Set(meshState.peers.map((p) => p.squad_id).filter(Boolean));
    const pick = state.squads.find((s) => live.has(s.id));   // squads come newest first
    if (pick) { meshState.squadFilter = pick.id; meshState.revalidate = true; return loadMesh(); }
  }
  if (meshState.revalidate) {
    // a squad change: a selection from the old squad goes (the first peer is picked below, as on first load),
    // and #mesh-to stops addressing the old peer unless the operator typed it
    meshState.revalidate = false;
    let dropped = false;
    if (meshState.selPeer && !meshState.peers.some(isSelPeer)) {
      meshState.selPeer = meshState.selName = null;
      meshState.ctxSig = "";
      dropped = true;
      if (!meshState.peers.length) clearPeerContext();
    }
    // a page-set handle goes with a dropped selection (the same handle may name someone else in the new squad)
    const to = $("mesh-to"), v = to.value.trim();
    if (!to.dataset.typed && v && !/^[@#]/.test(v) && (dropped || !meshState.peers.some((p) => p.name === v || String(p.pid) === v))) setMeshTo("@all");
  }

  const cCount = meshState.peers.filter((p) => p.provider === "claude").length;
  const gCount = meshState.peers.filter((p) => p.provider === "gemini").length;
  $("mesh-summary").replaceChildren(
    el("span", { cls: "hi", text: `${meshState.peers.length} peers` }),
    el("span", { cls: "dim", text: ` (${cCount} claude · ${gCount} gemini)` }),
    " · ",
    el("span", { cls: doc.hub_pipe ? "hi" : "dim", text: doc.hub_pipe ? "uds hub ▲" : "uds hub ▽" }),
    ` · ${state.squads.length} squad${state.squads.length === 1 ? "" : "s"}`,
    ...(health.doc ? [" · ", el("span", {
      cls: runnerMissing("gemini") ? "run-missing" : "hi",
      text: runnerMissing("gemini") ? "agy ▽ missing" : "agy ▲",   // ▲ up, as "uds hub ▲"
      title: runnerMissing("gemini") ? runnerHelp("gemini", health.doc.runners.gemini) : `gemini runner: ${health.doc.runners?.gemini?.path}`,
    })] : []),
  );

  // Sync squad selector
  const sqSel = $("mesh-squad-filter");
  const prevSq = meshState.squadFilter;
  sqSel.replaceChildren(
    el("option", { value: "", text: "all peers" }),
    ...state.squads.map((s) => el("option", { value: s.id, text: `${s.name} (${s.id})` })),
  );
  if (prevSq && state.squads.some((s) => s.id === prevSq)) sqSel.value = prevSq;
  paintSquadStrip();

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
    const isSel = isSelPeer(p);
    const lastTxt = p.last_event ? `[${p.last_event.kind}] ${p.last_event.text}`.replace(/\s+/g, " ").slice(0, 44) : "–";
    // passive badge: an idle peer that owes someone an answer (replaces a stall watchdog)
    const owed = active(p) ? [] : meshState.open.filter((m) => m.to_name === p.name);
    const owedBlocking = owed.some((m) => m.needs === "blocking");
    const tr = el("tr", { cls: isSel ? "sel" : "" },
      el("td", {}, providerBadge(p.provider)),
      el("td", { title: `pid ${p.pid} · ${p.cwd || ""}${p.spawned_by ? ` · added by ${p.spawned_by}` : ""}` }, p.name.slice(0, 24), owed.length ? el("span", {
        cls: `flag ${owedBlocking ? "blocking" : "reply"}`, text: `✉${owed.length}`,
        title: `idle with ${owed.length} unanswered: ${owed.map((m) => `${m.from_name} (${m.needs}, ${age(m.ts)})`).join(", ")}`,
      }) : null),
      el("td", { cls: "role-tag", text: (p.agent_role || "–").slice(0, 18) }),
      el("td", { cls: "cyan", text: (p.squad_id || "–").slice(0, 16) }),
      el("td", {}, udsBadge(p.has_uds, p.pipe)),
      el("td", {}, rowState({ ...p, role: "session" })),
      el("td", { cls: "dim", text: lastTxt, title: p.last_event?.text || "" }),
    );
    tr.addEventListener("click", () => pickPeer(p.name || String(p.pid), p.session_id));
    return tr;
  }));

  if (!meshState.selPeer && !meshState.selTask && meshState.peers.length) {   // an open task keeps the pane
    const p0 = meshState.peers[0];
    selectPeer(p0.name || String(p0.pid), p0.session_id, false);
    loadPeerContext();
  }
  paintMeshMessages();
  paintPlan();                 // owner chips follow the roster on every poll
  loadPlan(doc.objects_rev);   // the rows are refetched only when objects_rev (or the squad) changes
  paintBacklog();              // the age moves and a selection shows
  loadBacklog(doc.objects_rev); // every project's, whatever squad is selected
  loadHeat();      // only when the scope changed; the 30 s loop refreshes it
  loadTopo(false, doc.latest_id);
}

// elastic squads: what the architect may add (and how much is used), the operator's limit editor and [+ ADD MEMBER].
// The tools row is rebuilt only when the squad changes, so a half-typed task survives the 2 s poll.
// plan first: shown until a plan of this squad is accepted. It reads the plan rows already loaded and never fetches;
// hidden until this squad's rows have arrived (no flash on a switch), repainted when they do (no lag after ACCEPT)
function paintPlanChip(squad = state.squads.find((s) => s.id === meshState.squadFilter)) {
  $("strip-plan").hidden = !squad?.plan_first || planState.loadedKey !== squad.id
    || planState.rows.some((o) => o.status === "accepted" && o.data?.plan?.accepted_ts);
}
function paintStripTools(squad) {
  const target = state.roles.find((r) => r.id === squad.spawn_role);
  $("strip-grow").textContent = squad.spawn_role
    ? `added ${squad.spawn_used || 0}/${squad.spawn_limit || 0} · ${squad.spawn_role === "*" ? "any role" : squad.spawn_role}`
    : "agents add nobody (no spawn role)";
  $("strip-grow").title = squad.spawn_role === "*" ? `an agent whose role may add members can add up to ${squad.spawn_limit} members of any role`
    : target ? `an agent whose role may add members can add up to ${squad.spawn_limit} ${target.title}` : "";
  paintPlanChip(squad);
  const strip = $("mesh-strip");
  const rolesSig = state.roles.map((r) => `${r.id}:${r.title}`).join(",");      // a renamed or swapped role rebuilds too
  if (strip.dataset.squad === squad.id && strip.dataset.roles === rolesSig) return;
  strip.dataset.squad = squad.id; strip.dataset.roles = rolesSig;
  const opts = (blank) => [...(blank ? [el("option", { value: "", text: "(nobody)" }), el("option", { value: "*", text: "any role", title: ANY_ROLE_NOTE })] : []),
    ...state.roles.map((r) => el("option", { value: r.id, text: roleOptionText(r, false) }))];
  $("strip-role").replaceChildren(...opts(true));
  $("strip-role").value = squad.spawn_role || "";
  $("strip-limit").value = String(squad.spawn_limit || 0);
  $("strip-add-role").replaceChildren(...opts(false));
  // the squad's spawn role, else an implementer, else the first role: never "" while there is a role to pick
  const want = [squad.spawn_role !== "*" && squad.spawn_role, state.roles.find((r) => r.id.startsWith("implementer"))?.id, state.roles[0]?.id]
    .find((id) => id && state.roles.some((r) => r.id === id));
  $("strip-add-role").value = want || "";
  for (const [form, btn] of [["strip-edit-form", "strip-edit"], ["strip-add-form", "strip-add"]]) {
    $(form).hidden = true; $(btn).setAttribute("aria-expanded", "false");
  }
  $("strip-msg").textContent = "";
}
for (const [btn, form, focus] of [["strip-edit", "strip-edit-form", "strip-role"], ["strip-add", "strip-add-form", "strip-add-task"]]) {
  $(btn).addEventListener("click", () => {
    const open = $(form).hidden;
    $(form).hidden = !open; $(btn).setAttribute("aria-expanded", String(open));
    if (open) $(focus).focus();
  });
}
$("strip-edit-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    const r = await post("/api/squad", { id: meshState.squadFilter, spawn_role: $("strip-role").value || null,
      spawn_limit: Number($("strip-limit").value || 0) });
    $("strip-msg").textContent = r.ok ? `saved: agents may add ${r.squad.spawn_role === "*" ? "any role" : r.squad.spawn_role || "nobody"} up to ${r.squad.spawn_limit} ✓` : `refused: ${r.error}`;
    if (r.ok) { $("strip-edit-form").hidden = true; $("strip-edit").setAttribute("aria-expanded", "false"); loadMesh(); }
  } catch (err) { $("strip-msg").textContent = `failed: ${err.message}`; }
  btn.disabled = false;
});
$("strip-add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button[type=submit]");
  if (btn.disabled) return;                         // Enter pressed again while the first add is still launching
  const task = $("strip-add-task").value.trim(), role = $("strip-add-role").value;
  if (!task) return;
  if (!role) { $("strip-msg").textContent = "pick a role for the new member first (no roles loaded?)"; $("strip-add-role").focus(); return; }
  btn.disabled = true;
  $("strip-msg").textContent = "launching…";
  try {
    const r = await post("/api/squad/member", { squad_id: meshState.squadFilter, role_id: role,
      agent_name: $("strip-add-name").value.trim() || null, task });
    $("strip-msg").textContent = r.ok ? `${r.agent_name} joined (${r.provider}, pid ${r.pid}) ✓` : `refused: ${r.error}`;
    if (r.ok) { $("strip-add-task").value = ""; $("strip-add-name").value = ""; setTimeout(() => { loadFleet(); loadMesh(); }, 900); }
  } catch (err) { $("strip-msg").textContent = `failed: ${err.message}`; }
  btn.disabled = false;
});

// one line above the roster for the selected squad: its brief, and who is waiting on whom
function paintSquadStrip() {
  const strip = $("mesh-strip");
  const squad = state.squads.find((s) => s.id === meshState.squadFilter);
  strip.hidden = !squad;
  if (!squad) return;
  const brief = (squad.brief || "").replace(/\s+/g, " ").trim();
  const waits = meshState.open.map((m) => {
    const toOp = OPERATOR.has(m.to_name);
    const text = toOp && m.needs === "blocking" ? `${msgFrom(m)} blocked on operator for ${age(m.ts)}`
      : `${msgFrom(m)} waits on ${toOp ? "operator" : m.to_name} ${age(m.ts)} (${m.needs})`;
    return el("span", { cls: m.needs === "blocking" ? "flag blocking" : "flag reply", text, title: m.body.slice(0, 400) });
  });
  paintStripTools(squad);
  $("mesh-strip-info").replaceChildren(
    el("span", { cls: "cyan", text: squad.name }), " ",
    el("span", { cls: "dim", text: brief.length > 160 ? `${brief.slice(0, 160)}…` : brief || "(no brief)", title: squad.brief || "" }),
    ...(waits.length ? [el("span", { cls: "grow" }), ...waits] : [el("span", { cls: "grow" }), el("span", { cls: "dim", text: "nobody waiting" })]),
  );
}

function paintMeshMessages() {
  const feed = $("mesh-feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  let list = meshState.messages;
  if (meshState.pair) {          // an edge's pair filter replaces the scope until its chip is dismissed
    const [a, b] = meshState.pair, norm = (n) => (OPERATOR.has(n) ? "operator" : n);
    list = list.filter((m) => (norm(m.from_name) === a && norm(m.to_name) === b) || (norm(m.from_name) === b && norm(m.to_name) === a));
  } else if (meshState.msgScope === "peer" && meshState.selPeer) {
    const target = selPeerName();
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
        needsBadge(m),
      );
      const body = el("div", { cls: "msg-body" }, renderMd(m.body));
      return el("li", { cls: fresh ? "new" : "", "data-id": m.id }, head, body);
    }));
  }
  meshState.messages.forEach((m) => meshState.seenMsgIds.add(m.id));
  if (atBottom || first) feed.scrollTop = feed.scrollHeight;
}

async function loadPeerContext() {
  const want = meshState.selPeer;
  if (!want || meshState.selTask) return;          // a task holds the pane until a peer is picked
  let doc;
  try {
    const kindsQ = meshState.ctxKinds ? `&kinds=${encodeURIComponent(meshState.ctxKinds)}` : "";
    doc = await api(`/api/mesh/context?peer=${encodeURIComponent(want)}&limit=60${kindsQ}`);
  } catch (err) {
    if (meshState.selPeer === want && !meshState.selTask) $("mesh-ctx-head").replaceChildren(el("span", { cls: "red", text: err.message }));
    return;
  }
  if (meshState.selPeer !== want || meshState.selTask) return;   // another peer or a task was picked meanwhile
  const p = doc.peer, u = doc.usage;
  $("mesh-ctx-box").setAttribute("data-title", `CONTEXT STREAM // ${p.name}`.slice(0, 56));
  $("mesh-ctx-head").replaceChildren(...[    // replaceChildren would print a null as "null"
    providerBadge(p.provider), " ",
    el("span", { cls: "title", text: p.name }),
    p.agent_role ? el("span", { cls: "role-tag", text: ` · ${p.agent_role}` }) : null,
    p.model ? el("span", { cls: "model", text: ` · ${fmtModel(p.model)}` }) : null,
    el("span", { cls: "dim", text: ` · ${doc.live ? `live pid ${p.pid}` : "ended"}` }),
    u && u.messages ? el("span", { cls: "dim", text: ` · ${num(u.output, 0)} out tok · ${num(u.calls, 0)} tools` }) : null,
    workShort(u) ? el("span", { cls: "dim", text: ` · ${workShort(u)}` }) : null,
  ].filter((n) => n !== null));
  const events = doc.events || [];
  const sig = `${p.session_id || p.name}|${events.length}|${events.length ? events[events.length - 1].ts + events[events.length - 1].text.slice(0, 30) : ""}`;
  if (sig === meshState.ctxSig) return;
  meshState.ctxSig = sig;
  const feed = $("mesh-ctx-feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  feed.replaceChildren(...(events.length
    ? events.map((e) => el("li", { cls: e.kind, title: e.ts ? new Date(e.ts).toLocaleString() : null }, proseNode(e.kind, e.text)))
    : [el("li", { cls: "result", text: "no matching context events yet" })]));
  if (atBottom) feed.scrollTop = feed.scrollHeight;
}

// ------------------------------------------------------------------ mesh: plan (the squad's tasks, who owns which)
// Read-only. Titles, bodies and log lines are written by agents: set with textContent only (el()).
const PLAN_STATUSES = ["queued", "in progress", "in review", "accepted", "blocked"];
const PLAN_EMPTY = "No tasks filed. The architect files them with mesh_file.";
const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z]+/g, "-");

// fetch, not api(): a server older than the plan answers 404 for these routes, which must neither flag the LINK as down
// nor be asked again every poll; it returns null and the plan says "this server predates tasks: restart Almanac"
async function planGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  const doc = await res.json().catch(() => ({}));
  if (res.status === 404 && doc.error === "Not found") return null;
  if (!res.ok) throw new Error(doc.error || `${res.status}`);
  return doc;
}

async function loadPlan(rev) {
  const sq = meshState.squadFilter;
  if (!planState.stale && sq === planState.key && rev === planState.rev) return;
  const seq = ++planState.seq;                         // only the newest fetch paints
  if (sq !== planState.key) { planState.rows = []; planState.loadedKey = null; planState.branches = ""; planState.note = "loading plan…"; paintPlan(); }
  planState.stale = false; planState.key = sq; planState.rev = rev;
  if (!sq) { planState.note = "pick a squad above to see its plan"; paintPlan(); return; }
  let doc, br;
  try {
    // the branches ride with the plan's fetch (never the 2 s poll); a failure there only hides the line
    [doc, br] = await Promise.all([planGet(`/api/mesh/objects?squad=${encodeURIComponent(sq)}&kind=task&limit=500`),
      planGet(`/api/mesh/branches?squad=${encodeURIComponent(sq)}`).catch(() => null)]);
  } catch (err) {
    if (seq !== planState.seq || sq !== meshState.squadFilter) return;
    planState.stale = true;                            // retried on the next poll, only while it fails
    planState.note = `plan unavailable: ${err.message}`;
    paintPlan();
    return;
  }
  if (seq !== planState.seq || sq !== meshState.squadFilter) return;   // a newer fetch or another squad
  planState.rows = doc?.objects || [];
  planState.loadedKey = doc ? sq : null;
  paintPlanChip();
  planState.branches = branchesLine(br?.branches);
  planState.note = doc ? "" : "this server predates tasks: restart Almanac";
  paintPlan();
  if (meshState.selTask) loadTask(meshState.selTask);
}

// the roster decides on every poll, as it is fresher than the fetched rows: a live owner is stalled (stopped on an API
// error), busy or idle, and an absent one has ended. The rows are only fetched after a roster poll, so the door's own
// owner_state is never needed here.
// A step's re-read, in k. Σ: only agy's running total for the conversation is known (a Gemini record without its own step figure).
const ctxText = (n, total) => n ? `${ktok(n)}${total ? "Σ" : ""}` : "";
const ctxWarn = (n) => n && state.ctxWarn && n >= state.ctxWarn ? " ctx-warn" : "";
const ctxTitle = (total) => total ? "tokens agy reports for the whole conversation so far (not one step's re-read)" : "tokens its last step re-read";
function planOwnerCtx(o) {      // the live peer's, else what the doc carried (an ended owner's last stored)
  const peer = meshState.peers.find((q) => o.owner_session && q.session_id === o.owner_session);
  return peer ? [peer.context, !!peer.context_total] : [o.owner_context, !!o.owner_context_total];
}
function planOwnerState(o) {
  if (!o.owner_name) return "none";
  const p = meshState.peers.find((q) => (o.owner_session ? q.session_id === o.owner_session : q.name === o.owner_name));
  return !p ? "ended" : p.stall ? "stalled" : active(p) ? "busy" : "idle";   // p.stall: {error, text, ts} while stopped
}
// plan-sha:begin
// a task's commit chip: null without a sha; merged true/false/null (null: git could not tell, shown as "?")
function shaChip(o) {
  if (!o?.sha) return null;
  const state = o.merged === true ? "merged" : o.merged === false ? "unmerged" : "?";
  const full = `${o.sha}${o.branch ? `\nbranch ${o.branch}` : ""}${o.base ? `\nbase ${o.base}` : ""}`;
  return { text: `${o.sha.slice(0, 7)} ${state}`, title: state === "?" ? `${full}\ngit could not tell whether this is merged` : full,
    cls: state === "merged" ? "st-accepted" : state === "unmerged" ? "st-in-progress" : "st-queued" };
}
// "unmerged: impl-x (2 commits), impl-y (git: boom)"; "" when there is nothing to say
function branchesLine(list) {
  if (!list?.length) return "";
  return "unmerged: " + list.map((b) => `${b.agent_name} (${b.error ? `git: ${b.error}` : `${b.ahead} commit${b.ahead === 1 ? "" : "s"}`})`).join(", ");
}
// shown tasks whose merge state can still change: a sha that is not known merged
const planPending = (rows) => rows.some((o) => o.sha && o.merged !== true);
// plan-sha:end
const shaEl = (o) => { const c = shaChip(o); return c ? el("span", { cls: `flag ${c.cls}`, text: c.text, title: c.title }) : null; };
const statusPill = (s) => el("span", { cls: `flag st-${slug(s)}`, text: s || "–" });
function ownerChip(o) {
  const own = planOwnerState(o);
  const plain = ["none", "busy", "idle", "ended"].includes(own);   // colour says these; any other state is spelled out
  const [n, total] = planOwnerCtx(o);
  const ctx = ctxText(n, total);
  const base = plain ? o.owner_name || "unowned" : `${o.owner_name} · ${own}`;
  return el("span", { cls: `flag own-${slug(own)}${ctxWarn(n)}`, text: ctx && own !== "none" ? `${base} · ${ctx}` : base,
    title: own === "none" ? "nobody owns this task" : `${o.owner_name}: ${own}${ctx ? ` · context ${ctx}: ${ctxTitle(total)}` : ""}` });
}
function planLast(l) {
  if (!l) return "";
  const body = (l.body || "").replace(/\s+/g, " ").trim();
  // a create or assign row has no body; a "dead" entry whose body already starts "dead:" is not prefixed twice
  const what = l.kind === "status" && l.status_to ? l.status_to
    : l.kind === "create" ? body || "filed" : l.kind === "assign" ? body || "assigned"
    : l.kind === "parent" ? body : l.kind === "amend" ? body.split(" Before:")[0]     // "parent T-1 → T-2", "amended title"
    : l.kind === "note" || body.toLowerCase().startsWith(`${l.kind}:`) ? body : `${l.kind}: ${body}`;
  return `${l.agent_name} · ${what} · ${age(l.ts)}`;
}

function planRow(o, depth, byOwner) {
  const task = el("td", { cls: "task", title: o.title },
    byOwner ? (o.parent ? el("span", { cls: "dim", text: `${o.parent} › ` }) : null) : (depth ? el("span", { cls: "dim", text: "└ " }) : null),
    o.title,
    o.children?.total ? el("span", { cls: "dim", text: ` · accepted ${o.children.accepted}/${o.children.total}` }) : null);
  if (!byOwner && depth) task.style.paddingLeft = `${8 + Math.min(depth, 6) * 16}px`;   // CSSOM: the CSP allows no style attribute
  const tr = el("tr", { tabindex: "0", "data-ref": o.ref, cls: meshState.selTask === o.ref ? "sel" : "" },
    el("td", { cls: "cyan", text: o.ref }),
    task,
    el("td", {}, statusPill(o.status), shaEl(o) ? " " : null, shaEl(o)),
    el("td", {}, ownerChip(o)),
    el("td", { cls: "r", text: String(o.files?.length || 0), title: (o.files || []).join("\n") }),
    el("td", { cls: "last", text: planLast(o.last_log), title: o.last_log?.body || "" }),
  );
  tr.addEventListener("click", () => openTask(o.ref));
  tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTask(o.ref); } });
  return tr;
}

function paintPlan() {
  const squad = state.squads.find((s) => s.id === meshState.squadFilter);
  const brief = squad?.brief || "", mp = $("plan-mission"), tog = $("plan-mission-toggle");
  $("plan-mission-row").hidden = !brief;
  if (mp.textContent !== brief) {
    mp.textContent = brief;
    mp.classList.add("clamp"); tog.setAttribute("aria-expanded", "false"); tog.textContent = "[more]";
  }
  if (brief) tog.hidden = mp.classList.contains("clamp") && mp.scrollHeight <= mp.clientHeight + 1;   // fits in two lines

  const rows = planState.rows, byOwner = planState.group === "owner";
  $("plan-branches").hidden = !planState.branches;     // cheap and idempotent: safe on every poll
  if ($("plan-branches").textContent !== planState.branches) $("plan-branches").textContent = planState.branches;
  const sig = JSON.stringify([planState.group, meshState.selTask, planState.note, rows, rows.map(planOwnerState), rows.map((o) => planLast(o.last_log))]);
  // what the owner chips show (rounded k and the warn class), not the raw count: that moves on every step
  const ctxSig = JSON.stringify(rows.map((o) => { const [n, t] = planOwnerCtx(o); return [ctxText(n, t), ctxWarn(n)]; }));
  if (sig === planState.sig) {                         // polled: do not rebuild (and drop focus) when nothing changed
    if (ctxSig !== planState.ctxSig) {                 // only a chip's size or warning moved: swap the chips, leave the rows
      planState.ctxSig = ctxSig;
      for (const o of rows) $("plan-table").querySelector(`tr[data-ref="${CSS.escape(o.ref)}"] td:nth-child(4)`)?.replaceChildren(ownerChip(o));
    }
    return;
  }
  planState.sig = sig; planState.ctxSig = ctxSig;
  $("plan-counts").replaceChildren(
    el("span", { cls: "hi", text: `${rows.length} task${rows.length === 1 ? "" : "s"}` }),
    ...PLAN_STATUSES.flatMap((s) => {
      const n = rows.filter((o) => o.status === s).length;
      return n ? [" · ", el("span", { cls: `st-${slug(s)}`, text: `${n} ${s}` })] : [];
    }));
  $("plan-empty").hidden = rows.length > 0;
  $("plan-empty").textContent = planState.note || PLAN_EMPTY;
  $("plan-table").hidden = !rows.length;
  let trs;
  if (byOwner) {
    const groups = new Map();
    for (const o of rows) { const k = o.owner_name || ""; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(o); }
    const keys = [...groups.keys()].filter(Boolean).sort();
    if (groups.has("")) keys.push("");                 // unowned last
    trs = keys.flatMap((k) => [
      el("tr", { cls: "group" }, el("th", { colspan: "6", text: `${k || "unowned"} · ${groups.get(k).length}` })),
      ...groups.get(k).map((o) => planRow(o, 0, true))]);
  } else {
    const depth = new Map();                           // rows arrive in tree order: a parent before its children
    for (const o of rows) depth.set(o.ref, o.parent && depth.has(o.parent) ? depth.get(o.parent) + 1 : 0);
    trs = rows.map((o) => planRow(o, depth.get(o.ref), false));
  }
  const focused = document.activeElement?.closest?.("#plan-table tr")?.dataset.ref;
  $("plan-table").querySelector("tbody").replaceChildren(...trs);
  if (focused) $("plan-table").querySelector(`tr[data-ref="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });   // a polled repaint must not scroll the page back to the row
}

$("plan-group").addEventListener("click", (e) => {
  if (!e.target.dataset.v) return;
  planState.group = e.target.dataset.v;
  put("plan-group", planState.group);
  pressKindSeg("plan-group", planState.group);
  paintPlan();
});
pressKindSeg("plan-group", planState.group);
$("plan-mission-toggle").addEventListener("click", (e) => {
  const open = !$("plan-mission").classList.toggle("clamp");
  e.target.setAttribute("aria-expanded", String(open));
  e.target.textContent = open ? "[less]" : "[more]";
});

// a task opens in the peer context pane and holds it until a peer is picked or the squad changes
function openTask(ref) {
  meshState.selTask = ref;
  if ($("mesh-ctx-decide").dataset.ref !== ref) paintDecide(null);   // another task's bar goes while this one loads
  paintPlan(); paintBacklog();
  $("mesh-ctx-box").setAttribute("data-title", `TASK // ${ref}`.slice(0, 56));
  $("mesh-ctx-kinds").hidden = true;
  $("mesh-ctx-head").replaceChildren(el("span", { cls: "dim", text: `loading ${ref}…` }));
  $("mesh-ctx-feed").replaceChildren();
  loadTask(ref);
}
function closeTask() {
  if (!meshState.selTask) return;
  meshState.selTask = null;
  paintDecide(null);
  meshState.ctxSig = "";
  $("mesh-ctx-kinds").hidden = false;
  clearPeerContext();                                  // the next loadPeerContext() repaints the peer
  paintPlan(); paintBacklog();
}

// [RESPAWN] ends the owner's session and starts a fresh one on the task (POST /api/mesh/object/respawn): the first
// click arms it, a second within 5 s respawns
const respawnState = { ref: null, armed: false, at: 0, busy: false, msg: "", bad: false };
function respawnButton(o) {
  const armed = respawnState.ref === o.ref && respawnState.armed;
  const btn = el("button", { type: "button", cls: `btn ${armed ? "confirm" : "hi"}`, disabled: respawnState.busy,
    text: armed ? "[RESPAWN? click again]" : "[RESPAWN]",
    title: `end ${o.owner_name}'s session and start a fresh one on ${o.ref}, in the same worktree, from the task's log` });
  btn.addEventListener("click", (e) => { e.stopPropagation(); respawnOwner(o); });
  return el("span", {}, " ", btn);
}
async function respawnOwner(o) {
  if (respawnState.busy) return;
  if (!(respawnState.ref === o.ref && respawnState.armed)) {
    Object.assign(respawnState, { ref: o.ref, armed: true, at: Date.now(), bad: false,
      msg: `ends ${o.owner_name}'s session; click again within 5 s` });
    loadTask(o.ref);
    setTimeout(() => {
      if (respawnState.ref === o.ref && respawnState.armed && Date.now() - respawnState.at >= 5000) {
        Object.assign(respawnState, { armed: false, msg: "not respawned" });
        if (meshState.selTask === o.ref) loadTask(o.ref);
      }
    }, 5100);
    return;
  }
  Object.assign(respawnState, { armed: false, busy: true, msg: `respawning ${o.owner_name}…` });
  loadTask(o.ref);
  let r;
  try { r = await post("/api/mesh/object/respawn", { ref: o.ref }); } catch (err) { r = { error: err.message }; }
  Object.assign(respawnState, { busy: false, bad: !r.ok,
    msg: r.ok ? `${r.agent_name} respawned (session ${String(r.session_id).slice(0, 8)})` : `not respawned: ${r.error}` });
  if (meshState.selTask === o.ref) loadTask(o.ref);
  setTimeout(() => { loadFleet(); loadMesh(); }, 900);
}

// plan-decide:begin
// the operator's decision on a task in review: accepted takes any comment, sending back needs one
function decideBody(status, comment) {
  const body = (comment || "").trim();
  if (status === "in progress" && !body) return { error: "say what to change" };
  return { status, body };
}
// plan-decide:end
// [ACCEPT] / [SEND BACK] on a task in review (POST /api/mesh/object/log, the operator's).
// It lives in its own slot (#mesh-ctx-decide), outside the feed that loadTask rebuilds: the slot is only touched when the
// ref or the in-review state changes, so a refresh never blurs the textarea or drops a half-typed comment.
function paintDecide(o) {
  const slot = $("mesh-ctx-decide"), want = o && o.status === "in review" ? o.ref : "";
  if ((slot.dataset.ref || "") === want) return;
  slot.dataset.ref = want;
  slot.hidden = !want;
  slot.replaceChildren(...(want ? [decideBar(o)] : []));
}
function decideBar(o) {
  const box = el("textarea", { rows: 2, placeholder: "comment (needed to send back)", "aria-label": "comment (needed to send back)" });
  const msg = el("span", { cls: "red" });
  const btns = ["accepted", "in progress"].map((status) =>
    el("button", { type: "button", cls: "btn hi", text: status === "accepted" ? "[ACCEPT]" : "[SEND BACK]" }));
  btns.forEach((btn, i) => btn.addEventListener("click", async () => {
    const d = decideBody(i ? "in progress" : "accepted", box.value);
    if (d.error) { msg.textContent = d.error; return; }
    btns.forEach((b) => { b.disabled = true; }); msg.textContent = "";
    let r;
    try { r = await post("/api/mesh/object/log", { ref: o.ref, ...d }); } catch (err) { r = { error: err.message }; }
    btns.forEach((b) => { b.disabled = false; });
    if (r?.error || r?.ok === false) { msg.textContent = r.error || "not recorded"; return; }   // the comment stays
    planState.stale = true; loadMesh();
    if (meshState.selTask === o.ref) loadTask(o.ref);   // the status moved on: the slot empties itself
  }));
  return el("div", { cls: "field" }, box, el("div", { cls: "bar-line" }, ...btns, msg));
}

async function loadTask(ref) {
  let doc;
  try {
    doc = await planGet(`/api/mesh/object?ref=${encodeURIComponent(ref)}&log_limit=500`);
  } catch (err) {
    if (meshState.selTask === ref) $("mesh-ctx-head").replaceChildren(el("span", { cls: "red", text: `${ref}: ${err.message}` }));
    return;
  }
  if (meshState.selTask !== ref) return;
  if (!doc) { $("mesh-ctx-head").replaceChildren(el("span", { cls: "dim", text: "this server predates tasks: restart Almanac" })); return; }
  const o = doc.object, log = doc.log || [];
  paintDecide(o);
  $("mesh-ctx-head").replaceChildren(...[            // replaceChildren would print a null as "null"
    el("span", { cls: "title", text: `${o.ref} ${o.title}` }), " ", statusPill(o.status), shaEl(o) ? " " : null, shaEl(o), " ", ownerChip(o),
    el("span", { cls: "dim", text: ` · filed by ${o.created_by_name} ${age(o.created_ts)} ago${o.parent ? ` · part of ${o.parent}` : ""}` }),
    o.children?.total ? el("span", { cls: "dim", text: ` · accepted ${o.children.accepted}/${o.children.total}` }) : null,
    o.squad_id && o.owner_name && o.status !== "accepted" ? respawnButton(o) : null,
    respawnState.ref === o.ref && respawnState.msg ? el("span", { cls: respawnState.bad ? "red" : "dim", text: ` ${respawnState.msg}` }) : null,
  ].filter((n) => n !== null));
  const logHead = (l) => `${clock(l.ts)} ${l.agent_name} [${l.kind}]`
    + `${!l.status_to ? "" : l.kind === "create" ? ` as ${l.status_to}` : ` ${l.status_from || "–"} → ${l.status_to}`}`
    + `${l.sha ? ` ${l.sha.slice(0, 7)}` : ""}`
    + `${l.owner_to ? ` owner → ${l.owner_to}` : ""}`;
  const logCls = (k) => (k === "dead" ? "error" : k === "message" ? "peer_out" : k === "status" || k === "assign" || k === "dispatch" || k === "respawn" || k === "retired" || k === "compact" || k === "parent" || k === "amend" ? "tool" : "claude");
  // a refresh (every objects_rev change re-runs this for the open task) keeps the reader's place, measured from the
  // bottom because new log entries arrive at the top; only a newly opened task, or a reader at the top, starts at the top
  const feed = $("mesh-ctx-feed"), fromBottom = feed.scrollTop ? feed.scrollHeight - feed.scrollTop : 0;
  feed.replaceChildren(
    el("li", { cls: "you" }, o.body ? renderMd(o.body) : "(no body)"),
    el("li", { cls: "result", text: o.files?.length ? `files: ${o.files.join(", ")}` : "files: none" }),
    ...(log.length
      ? log.map((l) => el("li", { cls: logCls(l.kind), title: new Date(l.ts * 1000).toLocaleString() }, logHead(l), l.body ? renderMd(l.body) : null))
      : [el("li", { cls: "result", text: "no log entries yet" })]),
  );
  feed.scrollTop = fromBottom ? feed.scrollHeight - fromBottom : 0;   // the log is newest first
}

// ------------------------------------------------------------------ mesh: backlog (tasks no squad has taken yet)
// Every project's, whatever squad is selected; refetched when objects_rev changes. [DISPATCH] launches a squad on one
// (POST /api/squads/dispatch, the operator's only), after a second click that names the roster and the limit.
// [ASSIGN] moves one into a running squad of its project (POST /api/squads/assign): pick the squad, click twice.
const backlogState = { rev: undefined, rows: [], note: "", msg: "", armed: null, armedAt: 0, busy: false, sig: "",
  squads: [], squadsAt: 0, pick: {}, armedAssign: null, open: {} };   // open: {ref: true} for the parents expanded; a parent starts collapsed
const BACKLOG_EMPTY = "No backlog. Agents and the operator file one with mesh_file(backlog=true).";
const DEFAULT_SQUAD = [{ role: "orchestrator-claude" }, { role: "reviewer-gemini" }];   // the door's DEFAULT_ROSTER

function backlogRoster(o) {
  const r = o.data?.roster?.length ? o.data.roster : DEFAULT_SQUAD;
  return r.map((m, i) => `${m.name ? `${m.name} ` : ""}${m.role}${i ? "" : " (lead)"}`).join(", ")
    + (o.data?.roster?.length ? "" : " · default");
}
function backlogLimit(o) {
  const n = o.data?.spawn_limit ?? 5, role = o.data?.spawn_role || "*";
  return n ? `the lead may add ${n} × ${role === "*" ? "any role" : role}` : "the lead adds nobody";
}

async function loadBacklogSquads() {                    // the active squads, each with its `project`; every 20 s at most
  if (Date.now() - backlogState.squadsAt < 20000) return;
  backlogState.squadsAt = Date.now();
  try { backlogState.squads = ((await api("/api/squads?limit=50")).squads || []).filter((q) => q.status === "active"); }
  catch { backlogState.squadsAt = 0; }
}

async function loadBacklog(rev) {
  const stale = Date.now() - backlogState.squadsAt >= 20000;
  if (rev === backlogState.rev && !stale) return;
  await loadBacklogSquads();
  if (rev === backlogState.rev) return paintBacklog();     // only the squads moved
  backlogState.rev = rev;
  try {
    const doc = await planGet("/api/mesh/objects?backlog=1&kind=task&status=queued&limit=500");   // what can dispatch
    backlogState.rows = doc?.objects || [];
    backlogState.note = doc ? "" : "this server predates the backlog: restart Almanac";
  } catch (err) {
    backlogState.rev = undefined;                      // retried on the next poll, only while it fails
    backlogState.note = `backlog unavailable: ${err.message}`;
  }
  paintBacklog();
}

// a parent's own count covers its slices wherever they are (still in the backlog or already in a squad)
function slicesText(c) { return `${c.total} slice${c.total === 1 ? "" : "s"}, ${c.accepted} accepted`; }

function backlogRow(o, depth, listedKids) {
  const armed = backlogState.armed === o.ref;
  const btn = el("button", { type: "button", cls: `btn ${armed ? "confirm" : "hi"}`, disabled: backlogState.busy,
    text: armed ? "[LAUNCH? click again]" : "[DISPATCH ▸]",
    title: `${backlogRoster(o)}; ${backlogLimit(o)}` });
  btn.addEventListener("click", (e) => { e.stopPropagation(); dispatchBacklog(o); });
  btn.addEventListener("keydown", (e) => e.stopPropagation());   // Enter on the button is the button's, not the row's
  const squads = backlogState.squads.filter((q) => q.project === o.project);
  const assign = [];
  if (squads.length) {                                  // not offered when its project has no active squad
    const picked = squads.find((q) => q.id === backlogState.pick[o.ref]) || squads[0];
    const sel = el("select", { cls: "inline-effort-select", title: picked.name, "aria-label": `squad to assign ${o.ref} to`, disabled: backlogState.busy },
      ...squads.map((q) => el("option", { value: q.id, text: q.name, selected: q.id === picked.id })));
    sel.addEventListener("change", () => { backlogState.pick[o.ref] = sel.value; backlogState.armedAssign = null; paintBacklog(); });
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("keydown", (e) => e.stopPropagation());
    const armedA = backlogState.armedAssign === `${o.ref}:${picked.id}`;   // the squad it names, not just the task
    const ab = el("button", { type: "button", cls: `btn ${armedA ? "confirm" : ""}`, disabled: backlogState.busy,
      text: armedA ? "[ASSIGN? click again]" : "[ASSIGN ▸]", title: `move ${o.ref} into squad ${picked.name}; its lead is told` });
    ab.addEventListener("click", (e) => { e.stopPropagation(); assignBacklog(o, picked); });
    ab.addEventListener("keydown", (e) => e.stopPropagation());
    assign.push(sel, " ", ab, " ");
  }
  const project = String(o.project || "").split(/[\\/]/).filter(Boolean).pop() || "";
  let toggle = null;
  if (listedKids) {                                     // its slices still in the backlog sit under it, collapsed until opened
    const open = !!backlogState.open[o.ref];
    toggle = el("button", { type: "button", cls: "btn", "data-toggle": o.ref, "aria-expanded": String(open),
      "aria-label": `${open ? "collapse" : "expand"} the slices of ${o.ref}`, text: open ? "▾" : "▸" });
    toggle.addEventListener("click", (e) => { e.stopPropagation(); backlogState.open[o.ref] = !open; paintBacklog(); });
    toggle.addEventListener("keydown", (e) => e.stopPropagation());
  }
  const task = el("td", { cls: "task", title: `${o.title}\n${o.project || ""}` }, toggle, toggle ? " " : null,
    depth && !toggle ? el("span", { cls: "dim", text: "└ " }) : null, o.title,
    project ? el("span", { cls: "dim", text: ` · ${project}` }) : null,
    o.children?.total ? el("span", { cls: "dim", text: ` · ${slicesText(o.children)}` }) : null);
  if (depth) task.style.paddingLeft = `${8 + Math.min(depth, 6) * 16}px`;   // CSSOM: the CSP allows no style attribute
  const tr = el("tr", { tabindex: "0", "data-ref": o.ref, cls: meshState.selTask === o.ref ? "sel" : "" },
    el("td", { cls: "cyan", text: o.ref }),
    task,
    el("td", { cls: "last", text: backlogRoster(o), title: `${backlogRoster(o)}\n${backlogLimit(o)}` }),
    el("td", { cls: "r", text: age(o.created_ts), title: `filed by ${o.created_by_name}` }),
    el("td", {}, ...assign, btn),
  );
  tr.addEventListener("click", () => openTask(o.ref));
  tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTask(o.ref); } });
  return tr;
}

function paintBacklog() {
  const rows = backlogState.rows;
  const sig = JSON.stringify([rows, backlogState.armed, backlogState.busy, backlogState.note, backlogState.msg,
    meshState.selTask, rows.map((o) => age(o.created_ts)), backlogState.squads.map((q) => [q.id, q.name, q.project]),
    backlogState.pick, backlogState.armedAssign, backlogState.open]);
  if (sig === backlogState.sig) return;               // polled: do not rebuild (and drop focus) when nothing changed
  backlogState.sig = sig;
  $("backlog-table").hidden = !rows.length;
  $("backlog-msg").textContent = backlogState.msg || backlogState.note || (rows.length ? "" : BACKLOG_EMPTY);
  // rows arrive in tree order (a parent before its slices); a slice shows only when every parent above it is open
  const byRef = new Map(rows.map((o) => [o.ref, o])), depth = new Map(), listedKids = new Set();
  for (const o of rows) {
    const up = o.parent && byRef.has(o.parent);
    depth.set(o.ref, up ? depth.get(o.parent) + 1 : 0);
    if (up) listedKids.add(o.parent);
  }
  const shown = rows.filter((o) => { for (let p = o.parent; p && byRef.has(p); p = byRef.get(p).parent) if (!backlogState.open[p]) return false; return true; });
  const active = document.activeElement;
  const focused = active?.closest?.("#backlog-table tr")?.dataset.ref, toggled = active?.dataset?.toggle;
  $("backlog-table").querySelector("tbody").replaceChildren(...shown.map((o) => backlogRow(o, depth.get(o.ref), listedKids.has(o.ref))));
  const back = toggled ? `button[data-toggle="${CSS.escape(toggled)}"]` : focused ? `tr[data-ref="${CSS.escape(focused)}"]` : null;
  if (back) $("backlog-table").querySelector(back)?.focus({ preventScroll: true });   // a polled repaint must not scroll the page back to the row
}

function movedText(r) {                                  // what a parent's move did to its slices
  const c = r.children || {}, out = [];
  if (c.moved?.length) out.push(`${c.moved.length} slice${c.moved.length === 1 ? "" : "s"} moved with it (${c.moved.join(", ")})`);
  if (c.left?.length) out.push(`left in place: ${c.left.map((x) => `${x.ref} ${x.why}`).join(", ")}`);
  return out;
}

async function dispatchBacklog(o) {
  if (backlogState.busy) return;
  if (backlogState.armed !== o.ref) {                  // the first click arms; the second, within 5 s, launches
    backlogState.armed = o.ref; backlogState.armedAssign = null; backlogState.armedAt = Date.now();
    backlogState.msg = `${o.ref}: launch ${backlogRoster(o)}; ${backlogLimit(o)}. Click [LAUNCH?] again within 5 s.`;
    paintBacklog();
    setTimeout(() => {
      if (backlogState.armed === o.ref && Date.now() - backlogState.armedAt >= 5000) {
        backlogState.armed = null; backlogState.msg = `${o.ref}: not launched`; paintBacklog();
      }
    }, 5100);
    return;
  }
  backlogState.armed = null; backlogState.busy = true; backlogState.msg = `dispatching ${o.ref}…`;
  paintBacklog();
  let r;
  try {
    r = await post("/api/squads/dispatch", { ref: o.ref });   // 409 (not dispatched) comes back as a doc
  } catch (err) {
    r = { error: err.message };
  }
  const failed = (r.squad?.agents || []).filter((a) => !a.ok).map((a) => `${a.agent_name} failed: ${a.error}`);
  backlogState.msg = r.dispatched
    ? [`${o.ref} → squad ${r.squad.name}: ${r.launched} launched`, ...movedText(r), ...failed].join("; ")
    : [`${o.ref} not dispatched${r.error ? `: ${r.error}` : ""}`, ...failed].join("; ");
  backlogState.busy = false; backlogState.rev = undefined;
  paintBacklog();
  if (r.dispatched) {
    setMeshSquad(r.squad.id);
    openTask(o.ref);                                   // now the lead's task in the new squad's plan
  }
  setTimeout(() => { loadFleet(); loadMesh(); }, 900);
}

async function assignBacklog(o, squad) {
  if (backlogState.busy) return;
  const key = `${o.ref}:${squad.id}`;
  if (backlogState.armedAssign !== key) {             // the first click arms; the second, within 5 s, assigns
    backlogState.armedAssign = key; backlogState.armed = null; backlogState.armedAt = Date.now();
    backlogState.msg = `${o.ref}: move into squad ${squad.name}; its lead is told. Click [ASSIGN?] again within 5 s.`;
    paintBacklog();
    setTimeout(() => {
      if (backlogState.armedAssign === key && Date.now() - backlogState.armedAt >= 5000) {
        backlogState.armedAssign = null; backlogState.msg = `${o.ref}: not assigned`; paintBacklog();
      }
    }, 5100);
    return;
  }
  backlogState.armedAssign = null; backlogState.busy = true; backlogState.msg = `assigning ${o.ref}…`;
  paintBacklog();
  let r;
  try {
    r = await post("/api/squads/assign", { ref: o.ref, squad_id: squad.id });   // a refusal comes back as { error }
  } catch (err) {
    r = { error: err.message };
  }
  backlogState.msg = r.ok
    ? `${o.ref} → squad ${squad.name}${r.note ? `: ${r.note}` : `: ${r.lead} told`}${movedText(r).map((t) => `; ${t}`).join("")}`
    : `${o.ref} not assigned${r.error ? `: ${r.error}` : ""}`;
  backlogState.busy = false; backlogState.rev = undefined; backlogState.squadsAt = 0;
  paintBacklog();
  if (r.ok) { setMeshSquad(squad.id); openTask(o.ref); }
  setTimeout(() => loadMesh(), 900);
}

// ------------------------------------------------------------------ mesh: topology (who talks to whom)
// Deterministic SVG, no physics: columns operator | architect | implementer | reviewer | other, rows in spawn order,
// channels on a rail underneath. One curved edge per direction, width by count, coloured by open flags.
const topoState = { doc: null, key: "", at: 0, latest: -1 };
const TOPO_COLS = ["operator", "architect", "implementer", "reviewer", "other"];
const TOPO_MIN_COL = 136, TOPO_MIN_CH_SLOT = 96;     // px: a node box is then >= 114 wide, a channel box >= 86
const TOPO_CH = 6.7;                                 // px per character of the 11px monospace the svg is set in
// shorten to fit `px`, ending in an ellipsis; estimated by character count, as the face is monospace
function fitText(s, px) {
  const n = Math.max(1, Math.floor(px / TOPO_CH));
  return s.length <= n ? s : `${s.slice(0, Math.max(1, n - 1))}…`;
}

async function loadTopo(force = false, latestId = -1) {
  const sq = meshState.squadFilter;
  const key = sq || "";
  // the graph moves with messages: refetch when the scope or the newest message id changes, else every 10 s
  if (!force && key === topoState.key && latestId === topoState.latest && Date.now() - topoState.at < 10000) return;
  if (topoState.unsupported) return;
  topoState.key = key; topoState.latest = latestId; topoState.at = Date.now();
  // fetch, not api(): a server older than the topology answers 404 for the route, which must neither flag the LINK
  // as down nor be asked again every few seconds
  let res, doc;
  try {
    res = await fetch(`/api/mesh/graph${sq ? `?squad=${encodeURIComponent(sq)}` : ""}`, { credentials: "same-origin" });
    doc = await res.json().catch(() => ({}));
  } catch { return; }
  if (res.status === 404 && doc.error === "Not found") {
    topoState.unsupported = true;                       // until the next page load
    $("topo-scope").textContent = "";
    $("topo").replaceChildren(el("p", { cls: "dim pad", text: "topology needs an Almanac restart: this server predates it" }));
    return;
  }
  if (!res.ok) { $("topo-scope").textContent = `topology unavailable: ${doc.error || res.status}`; return; }
  topoState.doc = doc;
  paintTopo();
}

function topoEdgeClass(e) {
  if (e.open?.blocking) return "blocking";
  if (e.open?.reply) return "reply";
  return Date.now() / 1000 - (e.last_ts || 0) < 60 ? "recent" : "quiet";
}

function paintTopo() {
  const doc = topoState.doc, box = $("topo");
  if (!doc) return;
  const agents = doc.nodes.filter((n) => n.kind === "agent");
  const sq = state.squads.find((s) => s.id === doc.squad_id);
  if (!doc.squad_id && agents.length > 20) {
    $("topo-scope").textContent = `${agents.length} live peers`;
    box.replaceChildren(el("p", { cls: "dim pad", text: "pick a squad above to draw its topology: more than 20 unrelated peers make no readable graph" }));
    return;
  }
  const openFlags = doc.edges.reduce((n, e) => n + (e.open?.reply || 0) + (e.open?.blocking || 0), 0);
  $("topo-scope").textContent = `${sq ? `squad ${sq.name}` : "live peers (no squad)"} · ${agents.length} agent${agents.length === 1 ? "" : "s"}`
    + ` · ${doc.edges.length} path${doc.edges.length === 1 ? "" : "s"}${openFlags ? ` · ${openFlags} open` : ""}`;
  // layout: only the columns in use (the operator always), each node a fixed box
  // A column is never narrower than TOPO_MIN_COL: past that the svg keeps its width and .topo scrolls sideways
  const used = TOPO_COLS.filter((c) => c === "operator" || agents.some((n) => n.role_class === c));
  const avail = box.clientWidth ? box.clientWidth - 20 : 640;          // .topo has 10px of padding either side
  const pad = 14, W = Math.max(420, Math.round(avail), 2 * pad + used.length * TOPO_MIN_COL), colW = (W - 2 * pad) / used.length;
  const nodeW = Math.min(176, colW - 22), nodeH = 40, rowH = 58, top = 22;
  const pos = new Map();
  const perCol = Object.fromEntries(used.map((c) => [c, 0]));
  for (const n of doc.nodes) {
    if (n.kind === "channel") continue;
    const c = n.kind === "operator" ? "operator" : n.role_class;
    const i = used.indexOf(c), r = perCol[c]++;
    pos.set(n.id, { x: pad + i * colW + (colW - nodeW) / 2, y: top + r * rowH, w: nodeW, h: nodeH });
  }
  const rows = Math.max(1, ...Object.values(perCol));
  const channels = doc.nodes.filter((n) => n.kind === "channel");
  // channels: as many per rail row as fit at TOPO_MIN_CH_SLOT each, further rows underneath
  const railY = top + rows * rowH + 8, span = W - 2 * pad, railRowH = 32;
  const perRail = Math.max(1, Math.min(channels.length, Math.floor(span / TOPO_MIN_CH_SLOT)));
  const slot = span / perRail, chW = Math.min(120, slot - 10);
  channels.forEach((n, i) => {
    const r = Math.floor(i / perRail), c = i % perRail;
    pos.set(n.id, { x: pad + c * slot + (slot - chW) / 2, y: railY + r * railRowH, w: chW, h: 24 });
  });
  const railRows = Math.ceil(channels.length / perRail);
  const H = (channels.length ? railY + (railRows - 1) * railRowH + 24 : top + rows * rowH) + 12;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "group",
    "aria-label": `Topology: ${agents.length} agents, ${doc.edges.length} message paths, ${openFlags} open flags. Tab to a node or an edge, Enter to open it.` });
  if (W > avail) { svg.style.width = `${W}px`; svg.style.maxWidth = "none"; }    // CSSOM: the CSP allows no style attribute
  const defs = svgEl("defs");
  for (const k of ["quiet", "recent", "reply", "blocking"]) {
    const m = svgEl("marker", { id: `topo-arrow-${k}`, viewBox: "0 0 8 8", refX: "7", refY: "4", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
    m.append(svgEl("path", { d: "M0,0 L8,4 L0,8 z", class: `topo-arrow ${k}` }));
    defs.append(m);
  }
  svg.append(defs);
  // column heads
  used.forEach((c, i) => svg.append(svgEl("text", { x: pad + i * colW + colW / 2, y: 12, "text-anchor": "middle", class: "topo-col" }, c.toUpperCase())));
  // edges first, so nodes sit on top
  const center = (p) => ({ x: p.x + p.w / 2, y: p.y + p.h / 2 });
  const clip = (c, toward, p) => {          // where the line from the centre toward `toward` leaves the box
    const dx = toward.x - c.x, dy = toward.y - c.y;
    const t = Math.min(dx ? (p.w / 2) / Math.abs(dx) : Infinity, dy ? (p.h / 2) / Math.abs(dy) : Infinity, 1);
    return { x: c.x + dx * t, y: c.y + dy * t };
  };
  for (const e of doc.edges) {
    const a = pos.get(e.from), b = pos.get(e.to);
    if (!a || !b || e.from === e.to) continue;
    const ca = center(a), cb = center(b);
    const dx = cb.x - ca.x, dy = cb.y - ca.y, len = Math.hypot(dx, dy) || 1;
    const bend = Math.min(40, 14 + len * 0.08);            // to the left of the direction: a->b and b->a part
    const ctl = { x: (ca.x + cb.x) / 2 - (dy / len) * bend, y: (ca.y + cb.y) / 2 + (dx / len) * bend };
    const p0 = clip(ca, ctl, a), p1 = clip(cb, ctl, b);
    const k = topoEdgeClass(e), width = Math.min(6, 1 + Math.log2(Math.max(1, e.count)));
    const open = (e.open?.reply || 0) + (e.open?.blocking || 0);
    const label = `${e.from} → ${e.to}: ${e.count} message${e.count === 1 ? "" : "s"}, last ${age(e.last_ts)} ago`
      + (open ? `, ${e.open.blocking ? `${e.open.blocking} blocking` : ""}${e.open.blocking && e.open.reply ? ", " : ""}${e.open.reply ? `${e.open.reply} waiting for a reply` : ""}` : "");
    const g = svgEl("g", { class: `topo-edge ${k}`, tabindex: "0", role: "button", "aria-label": `${label}; show these messages` });
    g.append(svgEl("title", {}, label));
    const d = `M${p0.x.toFixed(1)},${p0.y.toFixed(1)} Q${ctl.x.toFixed(1)},${ctl.y.toFixed(1)} ${p1.x.toFixed(1)},${p1.y.toFixed(1)}`;
    g.append(svgEl("path", { d, class: "hit" }), svgEl("path", { d, class: "line", "stroke-width": width.toFixed(1), "marker-end": `url(#topo-arrow-${k})` }));
    const mid = { x: 0.25 * p0.x + 0.5 * ctl.x + 0.25 * p1.x, y: 0.25 * p0.y + 0.5 * ctl.y + 0.25 * p1.y };
    g.append(svgEl("text", { x: mid.x, y: mid.y - 3, "text-anchor": "middle", class: "topo-count" },
      open ? `${e.count} · ${open}${e.open.blocking ? " ▲" : " ?"}` : String(e.count)));
    const act = () => setPairFilter(e.from, e.to);
    g.addEventListener("click", act);
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); act(); } });
    svg.append(g);
  }
  // nodes
  for (const n of doc.nodes) {
    const p = pos.get(n.id);
    if (!p) continue;
    const busy = n.live && n.status && n.status !== "idle" && n.status !== "ended";
    const cls = `topo-node ${n.kind}${n.kind === "agent" && !n.live ? " ended" : ""}${busy && !reduced ? " busy" : ""}`
      + `${(n.session_id ? isSelPeer({ name: n.id, session_id: n.session_id }) : n.id === selPeerName()) ? " sel" : ""}`;
    const last = n.last_event ? `[${n.last_event.kind}] ${n.last_event.text}`.replace(/\s+/g, " ") : n.kind === "agent" && !n.live ? "ended" : "";
    const title = [n.id, n.role_id, n.provider, n.kind === "agent" ? (n.live ? n.status || "idle" : "ended") : null,
      n.worktree ? `worktree ${n.worktree}` : null, n.spawned_by ? `added by ${n.spawned_by}` : null, last].filter(Boolean).join(" · ");
    const g = svgEl("g", { class: cls, tabindex: n.kind === "agent" ? "0" : null, role: n.kind === "agent" ? "button" : null,
      "aria-label": n.kind === "agent" ? `${title}; open its context stream` : title });
    g.append(svgEl("title", {}, title));
    g.append(svgEl("rect", { x: p.x, y: p.y, width: p.w, height: p.h, rx: 3 }));
    const letter = n.kind === "operator" ? "◉" : n.kind === "channel" ? "#" : (n.provider === "gemini" ? "G" : "C");
    const head = svgEl("text", { x: p.x + 6, y: p.y + (n.kind === "channel" ? 16 : 15), class: "topo-name" });
    // labels fit the box (full name and last event stay in <title>): 6px inset either side, 2 chars of provider
    // letter, 2 more for the worktree mark
    const nameRoom = p.w - 12 - 2 * TOPO_CH - (n.worktree ? 2 * TOPO_CH : 0);
    head.append(svgEl("tspan", { class: `topo-prov ${n.provider || n.kind}` }, `${letter} `), document.createTextNode(fitText(n.id, nameRoom)));
    if (n.worktree) head.append(svgEl("tspan", { class: "topo-wt" }, " ⑂"));
    g.append(head);
    if (n.kind === "agent") g.append(svgEl("text", { x: p.x + 6, y: p.y + 31, class: "topo-last" }, fitText(last, p.w - 12)));
    if (n.kind === "agent") {
      const pick = () => pickPeer(n.id, n.session_id);
      g.addEventListener("click", pick);
      g.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); pick(); } });
    }
    svg.append(g);
  }
  box.replaceChildren(svg);
}

// an edge click filters the contact stream to that pair; a chip beside the scope seg undoes it
function setPairFilter(a, b) {
  meshState.pair = a && b ? [a, b] : null;
  const chip = $("mesh-pair-chip");
  chip.hidden = !meshState.pair;
  const seg = $("mesh-msg-scope");
  seg.classList.toggle("overridden", !!meshState.pair);
  if (meshState.pair) seg.title = "an edge filter is on: dismiss its chip to use this scope again"; else seg.removeAttribute("title");
  if (meshState.pair) $("mesh-pair-text").textContent = `${a} ⇄ ${b}`;
  paintMeshMessages();
  if (meshState.pair) $("mesh-feed").scrollTop = $("mesh-feed").scrollHeight;
}
$("mesh-pair-clear").addEventListener("click", () => setPairFilter(null, null));

// jump from the swimlane to a message in the contact stream
function flashMessage(id) {
  const li = $("mesh-feed").querySelector(`li[data-id="${Number(id)}"]`);
  if (!li) return false;
  li.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
  li.classList.remove("flash"); void li.offsetWidth; li.classList.add("flash");
  return true;
}

// ------------------------------------------------------------------ mesh: squad timeline // work heatmap
// One row per agent, one column per time bucket: work in a 5-step single-hue ramp by intensity, waiting (mesh_wait)
// in a quieter hue, idle left as background. A PARALLEL row above steps by how many agents worked at once.
const heatState = { doc: null, mode: get("heat-mode", "events"), layers: get("heat-layers", "heat"), key: "", geo: null };
const SVG_NS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, text) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}
function fmtDur(sec) {
  const s = Math.round(sec || 0);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.floor(s / 3600)}h${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m`;
}
const workShort = (u) => (u && u.turns ? `${fmtDur(u.work_s)} work` : null);

function heatQuery() {
  if (meshState.squadFilter) return `squad=${encodeURIComponent(meshState.squadFilter)}`;
  const sids = meshState.peers.map((p) => p.session_id).filter(Boolean).slice(0, 20);
  return sids.length ? `sessions=${sids.map(encodeURIComponent).join(",")}` : "";
}
// fetched when the scope changes and every 30 s while MESH shows (the data moves at ingest pace), never per 2 s poll
async function loadHeat(force = false) {
  const q = heatQuery();
  if (!force && q === heatState.key) return;
  if (q !== heatState.key) heatState.view = null;      // another squad or set of peers: its whole run
  heatState.key = q;
  if (!q) { heatState.doc = null; paintHeat(); return; }
  try { heatState.doc = await api(`/api/worktime?${q}`); }
  catch (err) { $("heat-scope").textContent = `worktime unavailable: ${err.message}`; return; }
  paintHeat();
}

// fold k adjacent buckets into one column so a column is never narrower than ~4px
function heatColumns(doc, maxCols) {
  const k = Math.max(1, Math.ceil(doc.range.n / maxCols)), cols = Math.ceil(doc.range.n / k);
  const fold = (arr, f) => Array.from({ length: cols }, (_, c) => f(arr.slice(c * k, c * k + k)));
  const sum = (xs) => xs.reduce((a, b) => a + b, 0);
  return {
    k, cols, bucket: doc.range.bucket_s * k,
    agents: doc.agents.map((a) => ({ ...a, work: fold(a.work, sum), wait: fold(a.wait, sum), events: fold(a.events, sum), output: fold(a.output, sum) })),
    maxWorking: fold(doc.overlap.max_working, (xs) => Math.max(0, ...xs)),
  };
}

function heatSummary(doc, names) {
  const ov = doc.overlap, wall = doc.range.end - doc.range.start, by = ov.by_count || {};
  const parallelPct = wall > 0 ? Math.round((ov.parallel_s / wall) * 100) : 0;
  const line = `wall ${fmtDur(wall)} · someone working ${fmtDur(ov.any_s)} · parallel (2+) ${fmtDur(ov.parallel_s)} ${parallelPct}%`
    + ` · solo ${fmtDur(by[1] || by["1"] || 0)} · all idle ${fmtDur(by[0] || by["0"] || 0)} · peak ${ov.peak}`;
  const pairs = (ov.pairs || []).slice(0, 4).map((p) => `${names[p.a] || p.a.slice(0, 8)} × ${names[p.b] || p.b.slice(0, 8)} ${fmtDur(p.overlap_s)}`);
  return { line, pairs };
}

// heat-view:begin
// The zoomed time window {t0, t1} in seconds (null: the whole run), kept in time so a refresh that adds buckets or
// changes their size does not move it. heatWindow: the window after zooming by `f` (<1 in, >1 out) around the fraction
// `at` of the shown span and panning by `dx` spans; never under 4 buckets, never outside the run, null once it is all.
function heatWindow(r, view, f, at = 0.5, dx = 0) {
  const end = r.start + r.n * r.bucket_s, v = view || { t0: r.start, t1: end }, was = v.t1 - v.t0;
  const span = Math.min(end - r.start, Math.max(4 * r.bucket_s, was * f));
  if (span >= end - r.start) return null;
  const t0 = Math.max(r.start, Math.min(end - span, v.t0 + (was - span) * at + span * dx));
  return { t0, t1: t0 + span };
}
// the worktime doc cut to a window: bucket arrays, marks and messages inside it; every total stays the whole run's
function heatView(doc, view) {
  if (!view) return doc;
  const r = doc.range, min = Math.min(4, r.n), clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const i0 = clamp(Math.floor((view.t0 - r.start) / r.bucket_s), 0, r.n - min);
  const i1 = clamp(Math.ceil((view.t1 - r.start) / r.bucket_s), i0 + min, r.n);
  const cut = (xs) => xs.slice(i0, i1), start = r.start + i0 * r.bucket_s, end = r.start + i1 * r.bucket_s;
  const within = (m) => m.ts >= start && m.ts < end;
  return { ...doc, range: { ...r, start, end, n: i1 - i0 },
    agents: doc.agents.map((a) => ({ ...a, work: cut(a.work), wait: cut(a.wait), events: cut(a.events), output: cut(a.output) })),
    overlap: { ...doc.overlap, max_working: cut(doc.overlap.max_working) },
    marks: (doc.marks || []).filter(within), messages: (doc.messages || []).filter(within) };
}
// heat-view:end
function heatZoom(f, at = 0.5, dx = 0) {
  const r = heatState.doc?.range;
  if (!r?.n) return;
  heatState.view = heatWindow(r, heatState.view, f, at, dx);
  paintHeat();
}

function paintHeat() {
  const full = heatState.doc, box = $("heat");
  const sq = state.squads.find((s) => s.id === meshState.squadFilter);
  if (!full || !full.agents?.length || !full.range?.n) {
    $("heat-scope").textContent = sq ? `squad ${sq.name}` : "peers shown";
    box.replaceChildren(el("p", { cls: "dim", text: full?.agents?.length ? "no turns recorded yet for these sessions (transcripts are read every 30 s while agents run)" : "no sessions to chart" }));
    $("heat-legend").replaceChildren(); $("heat-summary").textContent = "";
    $("heat-table").querySelector("tbody").replaceChildren();
    return;
  }
  const doc = heatView(full, heatState.view);      // what is drawn; the stats and the summary stay the whole run's
  const names = Object.fromEntries(doc.agents.map((a) => [a.session_id, a.agent_name]));
  const W = Math.max(360, Math.round(box.clientWidth || 640)), labelW = 150, statW = 178, gridX = labelW, gridW = W - labelW - statW;
  const geo = heatColumns(doc, Math.max(10, Math.floor(gridW / 4)));
  const cw = gridW / geo.cols, rowH = 16, gap = 3, parY = 2, rowsY = parY + rowH + 8;
  const axisY = rowsY + geo.agents.length * (rowH + gap) + 2, H = axisY + 18;
  heatState.geo = { ...geo, gridX, cw, W, t0: doc.range.start };
  // intensity: per-minute rate in each worked column, scaled to the squad's 95th percentile so one burst
  // does not flatten the rest
  const per = (a, i) => (heatState.mode === "output" ? a.output[i] : a.events[i]) / (geo.bucket / 60);
  const rates = geo.agents.flatMap((a) => a.work.map((w, i) => (w > 0 ? per(a, i) : null))).filter((v) => v !== null && v > 0).sort((x, y) => x - y);
  const p95 = rates.length ? rates[Math.min(rates.length - 1, Math.floor(rates.length * 0.95))] : 0;
  const step = (a, i) => (p95 > 0 ? Math.min(5, Math.max(1, Math.ceil((per(a, i) / p95) * 5))) : 1);
  const summary = heatSummary(full, names);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Work heatmap. ${summary.line}. ${summary.pairs.join(", ")}` });
  const cell = (x, y, fill) => svgEl("rect", { x: x.toFixed(2), y, width: Math.max(1, cw - (cw > 4 ? 1 : 0)).toFixed(2), height: rowH, fill, rx: cw > 6 ? 1 : 0 });
  // PARALLEL row
  svg.append(svgEl("text", { x: 0, y: parY + 12, class: "lbl" }, "PARALLEL"));
  geo.maxWorking.forEach((m, i) => { if (m > 0) svg.append(cell(gridX + i * cw, parY, `var(--par-${Math.min(4, m)})`)); });
  svg.append(svgEl("text", { x: gridX + gridW + 8, y: parY + 12 }, `peak ${doc.overlap.peak}`));
  // agent rows
  geo.agents.forEach((a, r) => {
    const y = rowsY + r * (rowH + gap);
    const role = (a.role_id || "").split("-")[0];
    const label = svgEl("text", { x: 0, y: y + 12, class: "lbl" }, a.agent_name.slice(0, 15));
    if (role) label.append(svgEl("tspan", { class: "lbl-role" }, ` ${role.slice(0, 5)}`));
    if (!a.live) label.setAttribute("opacity", ".6");
    svg.append(label);
    a.work.forEach((w, i) => {
      if (w > 0) svg.append(cell(gridX + i * cw, y, `var(--heat-${step(a, i)})`));
      else if (a.wait[i] > 0) svg.append(cell(gridX + i * cw, y, "var(--heat-wait)"));
    });
    const idlePct = a.wall_s > 0 ? Math.round((a.idle_s / a.wall_s) * 100) : 0;
    svg.append(svgEl("text", { x: gridX + gridW + 8, y: y + 12 }, `work ${fmtDur(a.work_s)} · wait ${fmtDur(a.wait_s)} · idle ${idlePct}%`));
  });
  // time axis
  svg.append(svgEl("line", { x1: gridX, x2: gridX + gridW, y1: axisY, y2: axisY, class: "axis" }));
  const span = geo.cols * geo.bucket;
  const tickStep = [60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400].find((s) => span / s <= 7) || 86400;
  const t0 = doc.range.start;
  for (let t = Math.ceil(t0 / tickStep) * tickStep; t <= t0 + span; t += tickStep) {
    const x = gridX + ((t - t0) / geo.bucket) * cw;
    svg.append(svgEl("line", { x1: x, x2: x, y1: axisY, y2: axisY + 4, class: "axis" }));
    svg.append(svgEl("text", { x, y: axisY + 15, "text-anchor": "middle" }, hhmm(t)));
  }
  // hit rows: hover for a tooltip, click / Enter to select the peer
  const rowAt = (r) => (r < 0 ? null : geo.agents[r]);
  [-1, ...geo.agents.keys()].forEach((r) => {
    const a = rowAt(r), y = r < 0 ? parY : rowsY + r * (rowH + gap);
    const hit = svgEl("rect", { x: gridX, y: y - 1, width: gridW, height: rowH + 2, class: "row-hit",
      tabindex: a ? "0" : null, role: a ? "button" : null,
      "aria-label": a ? `${a.agent_name}: work ${fmtDur(a.work_s)}, waiting ${fmtDur(a.wait_s)}, ${a.turns} turns; select this peer` : null });
    hit.addEventListener("mousemove", (e) => showTip(e, heatTip(a, e, svg)));
    hit.addEventListener("mouseleave", hideTip);
    if (a) {
      const pick = () => pickPeer(a.agent_name, a.session_id);
      hit.addEventListener("click", pick);
      hit.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); pick(); } });
    }
    svg.append(hit);
  });
  if (heatState.layers === "events") drawSwimlane(svg, doc, geo, { gridX, cw, rowsY, rowH, gap });
  box.replaceChildren(svg);
  const when = heatState.view ? `${hhmm(doc.range.start)}–${hhmm(doc.range.end)} of ${fmtDur(full.range.n * full.range.bucket_s)}` : `since ${hhmm(doc.range.start)}`;
  $("heat-scope").textContent = `${sq ? `squad ${sq.name}` : `${doc.agents.length} peers shown`} · ${when} · ${fmtDur(geo.bucket)} columns`;
  // CSSOM, not a style attribute: the page's CSP (style-src 'self') blocks inline style attributes
  const sw = (v) => { const n = el("span", { cls: "sw" }); n.style.background = `var(${v})`; return n; };
  $("heat-legend").replaceChildren(
    el("span", {}, `work (${heatState.mode === "output" ? "output tokens" : "events"} / min) low `, sw("--heat-1"), sw("--heat-2"), sw("--heat-3"), sw("--heat-4"), sw("--heat-5"), " high"),
    el("span", {}, sw("--heat-wait"), " waiting (mesh_wait)"),
    el("span", {}, "parallel ", sw("--par-1"), " solo ", sw("--par-2"), " 2 ", sw("--par-3"), " 3 ", sw("--par-4"), " 4+"),
    el("span", {}, "blank = idle"),
    heatState.layers === "events" && !("marks" in full) ? el("span", { text: "events need an Almanac restart: this server predates them" })
    : heatState.layers === "events" ? el("span", {}, "events: ", el("span", { cls: "red", text: "│" }), " tool error · ",
      el("span", { text: "ˌ" }), " file edit · ", el("span", { cls: "cyan", text: "●" }), " message (",
      el("span", { cls: "amber", text: "reply" }), " / ", el("span", { cls: "red", text: "blocking" }), ", ○ once answered)") : "");
  $("heat-summary").replaceChildren(el("span", { text: summary.line }),
    summary.pairs.length ? el("span", { cls: "dim", text: ` · ${summary.pairs.join(" · ")}` }) : "");
  $("heat-table").querySelector("tbody").replaceChildren(...doc.agents.map((a) => el("tr", {},
    el("th", { scope: "row", text: a.agent_name }), el("td", { text: fmtDur(a.work_s) }), el("td", { text: fmtDur(a.wait_s) }),
    el("td", { text: fmtDur(a.idle_s) }), el("td", { text: String(a.turns) }), el("td", { text: String(a.calls) }))));
}

// swimlane on the heatmap, same x axis: per agent, a tick for each tool error and a notch for each file edit; each
// message a thin line from the sender's row to the recipient's with a dot at the recipient (a broadcast, or one to
// the operator, is a dot on the sender's row), coloured by its flag and hollow once answered
function drawSwimlane(svg, doc, geo, { gridX, cw, rowsY, rowH, gap }) {
  const rowOf = new Map(geo.agents.map((a, r) => [a.session_id, r]));
  const rowByName = new Map(geo.agents.map((a, r) => [a.agent_name, r]));
  const xAt = (ts) => gridX + ((ts - doc.range.start) / geo.bucket) * cw;
  const top = (r) => rowsY + r * (rowH + gap), mid = (r) => top(r) + rowH / 2;
  const lane = svgEl("g", { class: "lane" });
  for (const m of doc.marks || []) {
    const r = rowOf.get(m.session);
    if (r === undefined) continue;
    const x = xAt(m.ts).toFixed(1);
    lane.append(m.kind === "error"
      ? svgEl("line", { x1: x, x2: x, y1: top(r), y2: top(r) + rowH, class: "lane-err" })
      : svgEl("line", { x1: x, x2: x, y1: top(r) + rowH - 5, y2: top(r) + rowH, class: "lane-edit" }));
  }
  for (const m of doc.messages || []) {
    const from = rowByName.get(m.from), to = rowByName.get(m.to);
    const at = to !== undefined && !String(m.to).match(/^[@#]/) ? to : from;
    if (at === undefined) continue;
    const x = xAt(m.ts);
    const cls = `lane-msg ${m.needs || "plain"}${m.needs && m.answered_ts ? " answered" : ""}`;
    const label = `${clock(m.ts)} ${m.from} → ${m.to}${m.needs ? ` [${m.needs}${m.answered_ts ? ", answered" : ", open"}]` : ""}: ${m.preview}`;
    const g = svgEl("g", { class: cls, tabindex: "0", role: "button", "aria-label": `${label}; show it in the contact stream` });
    if (from !== undefined && to !== undefined && from !== to) g.append(svgEl("line", { x1: x, x2: x, y1: mid(from), y2: mid(to) }));
    g.append(svgEl("circle", { cx: x, cy: mid(at), r: 2.8, class: "dot" }), svgEl("circle", { cx: x, cy: mid(at), r: 7, class: "hit" }));
    g.addEventListener("mouseenter", (e) => showTip(e, label));
    g.addEventListener("mouseleave", hideTip);
    const go = () => {
      if (flashMessage(m.id)) return;
      const r = g.getBoundingClientRect();
      showTip({ clientX: r.left, clientY: r.top }, "older than the contact stream shows (its newest 150)");
    };
    g.addEventListener("click", go);
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); go(); } });
    lane.append(g);
  }
  svg.append(lane);
}

$("heat-layers").addEventListener("click", (e) => {
  if (!e.target.dataset.v) return;
  heatState.layers = e.target.dataset.v; put("heat-layers", heatState.layers);
  document.querySelectorAll("#heat-layers button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === heatState.layers)));
  paintHeat();
});
document.querySelectorAll("#heat-layers button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === heatState.layers)));

function heatTip(a, e, svg) {
  const g = heatState.geo, box = svg.getBoundingClientRect();
  const x = ((e.clientX - box.left) / box.width) * g.W;
  const i = Math.min(g.cols - 1, Math.max(0, Math.floor((x - g.gridX) / g.cw)));
  const t0 = g.t0 + i * g.bucket, span = `${clock(t0)}–${clock(t0 + g.bucket)}`;
  if (!a) return `PARALLEL · ${span}\nat most ${g.maxWorking[i]} agent${g.maxWorking[i] === 1 ? "" : "s"} working at once`;
  return `${a.agent_name} · ${span}\nwork ${fmtDur(a.work[i])} · waiting ${fmtDur(a.wait[i])}\n${a.events[i]} events · ${num(a.output[i], 0)} output tokens`;
}

$("heat-mode").addEventListener("click", (e) => {
  if (!e.target.dataset.v) return;
  heatState.mode = e.target.dataset.v; put("heat-mode", heatState.mode);
  document.querySelectorAll("#heat-mode button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === heatState.mode)));
  paintHeat();
});
document.querySelectorAll("#heat-mode button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === heatState.mode)));
// zoom and pan: the buttons, Ctrl + wheel (zoom at the pointer) and Shift + wheel or a sideways wheel (pan). A plain
// wheel is left to the page, so scrolling past the heatmap never zooms it.
$("heat-zoom").addEventListener("click", (e) => {
  const z = e.target.dataset.z;
  if (z === "all") { heatState.view = null; paintHeat(); }
  else if (z) heatZoom(z === "in" ? 0.5 : z === "out" ? 2 : 1, 0.5, z === "left" ? -0.5 : z === "right" ? 0.5 : 0);
});
$("heat").addEventListener("wheel", (e) => {
  const g = heatState.geo, svg = $("heat").querySelector("svg");
  const zoom = e.ctrlKey || e.metaKey, pan = !zoom && heatState.view && (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY));
  if (!g || !svg || !(zoom || pan)) return;
  e.preventDefault();
  const box = svg.getBoundingClientRect(), x = ((e.clientX - box.left) / box.width) * g.W;
  if (zoom) heatZoom(e.deltaY < 0 ? 0.8 : 1.25, Math.max(0, Math.min(1, (x - g.gridX) / (g.cols * g.cw))));
  else heatZoom(1, 0.5, Math.sign(e.deltaX || e.deltaY) * 0.1);
}, { passive: false });
let heatResize = 0;
addEventListener("resize", () => { clearTimeout(heatResize); heatResize = setTimeout(() => { if (state.tab === "mesh") { paintHeat(); paintTopo(); } }, 200); });

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
      needs: $("mesh-needs").value || null,
    });
    if (r.ok) {
      $("mesh-text").value = "";
      $("mesh-needs").value = "";
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
const chatState = { list: [], sel: null, armed: 0, timer: 0, nameTimer: 0 };
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
  feed.replaceChildren(...(doc.events.length ? doc.events.map((e) => el("li", { cls: e.kind, title: e.ts ? new Date(e.ts).toLocaleString() : null }, proseNode(e.kind, e.text)))
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
// one follow-up timer at most, and only while CHATS is the tab and the page is visible; show("chats") and the
// page becoming visible again restart it
const chatsShown = () => state.tab === "chats" && !document.hidden;
function scheduleNameStatus(ms, withChats) {
  clearTimeout(chatState.nameTimer);
  chatState.nameTimer = setTimeout(() => {
    chatState.nameTimer = 0;
    if (!chatsShown()) return;
    nameStatus(); if (withChats) loadChats();
  }, ms);
}
async function nameStatus() {
  try {
    const n = await api("/api/names");
    $("name-status").textContent = n.running ? `naming… ${n.done} named, ${n.failed} failed, $${(n.usd || 0).toFixed(3)} so far`
      : n.done || n.failed ? `last run: ${n.done} named, ${n.failed} failed, $${(n.usd || 0).toFixed(3)}` : "";
    if (n.running && chatsShown()) scheduleNameStatus(3000, true);
  } catch { /* the link light already shows trouble */ }
}
document.addEventListener("visibilitychange", () => { if (chatsShown()) nameStatus(); });
$("name-all").addEventListener("click", async () => {
  try { const r = await post("/api/name", { all: true }); $("name-status").textContent = `${r.queued} chats to name…`; }
  catch (err) { $("name-status").textContent = `failed: ${err.message}`; return; }
  scheduleNameStatus(1500, false);
});

// ------------------------------------------------------------------ telemetry
state.provider = ["all", "claude", "gemini"].includes(get("provider", "all")) ? get("provider", "all") : "all";
const pct1 = (v) => v === null || v === undefined ? "–" : `${num(v * 100, 1)}%`;
const tokens = (v) => v === null || v === undefined ? "–" : v >= 1e6 ? `${num(v / 1e6, 1)}M` : v >= 1e4 ? `${num(v / 1e3, 0)}k` : num(v, 0);
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
    const fmt = (v) => v === null || v === undefined ? "–" : t.id === "error_rate" ? pct1(v) : t.unit === "%" ? pct(v) : t.unit === "×" ? `${num(v, 0)}×` : `${num(v, 2)} ${t.unit}`;
    const big = el("div", { cls: "big" });
    countUp(big, t.value, fmt);
    let delta = el("div", { cls: "dim", text: t.value === null && t.id === "error_rate" ? "no tool results yet" : "no earlier period yet" });
    if (t.value !== null && t.previous === 0) {
      const worse = t.value > 0 && t.better === "down";
      delta = el("div", {}, el("span", { cls: t.value === 0 ? "dim" : worse ? "bad" : "good", text: t.value === 0 ? "= flat" : `▲ up from zero` }),
        el("span", { cls: "dim", text: ` vs ${fmt(0)}` }));
    } else if (t.value !== null && t.previous) {
      const ch = (t.value - t.previous) / Math.abs(t.previous), better = t.better === "down" ? ch < 0 : ch > 0, flat = Math.abs(ch) < 0.02;
      delta = el("div", {}, el("span", { cls: flat ? "dim" : better ? "good" : "bad", text: flat ? "= flat" : `${ch > 0 ? "▲" : "▼"} ${Math.abs(Math.round(ch * 100))}% ${better ? "better" : "worse"}` }),
        el("span", { cls: "dim", text: ` vs ${fmt(t.previous)}` }));
    }
    return el("div", { cls: "box tile", "data-title": t.label.toUpperCase() }, el("div", { cls: "pad" }, big, delta));
  }));

  const trend = [["KB back / call", r.series.map((s) => s.kb_per_call), (v) => `${num(v, 2)} KB`],
    ["context / output", r.series.map((s) => s.ctx_per_out), (v) => `${num(v, 0)}×`],
    ["shell wait (min)", r.series.map((s) => s.shell_min), (v) => `${num(v, 1)} min`],
    ["tool error rate", r.series.map((s) => s.error_rate), pct1],
    ["tool calls", r.series.map((s) => s.calls), (v) => num(v, 0)],
    ["output tokens", r.series.map((s) => s.output_tokens), (v) => num(v, 0)]];
  const rows = el("div", { cls: "rows" });
  for (const [name, vals, fmt] of trend) {
    const last = [...vals].reverse().find((v) => v !== null && v !== undefined);
    rows.append(el("span", { cls: "lbl", text: name }), el("span", { cls: "val", text: fmt(last ?? null) }), cells(vals, fmt, labels), el("span", { cls: "dim", text: "latest" }));
  }
  $("trends").replaceChildren(rows, tableView(["bucket", ...trend.map((t) => t[0])], labels.map((b, i) => [b, ...trend.map(([, v, f]) => v[i] === null ? "–" : f(v[i]))])));

  drawModels(r);

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

// BY MODEL: one row per (provider, model); the error-rate mark compares with the period before
function changeMark(now, before, better = "down") {
  if (now === null || now === undefined || before === null || before === undefined) return null;
  const diff = now - before;
  if (Math.abs(diff) < 0.005) return el("span", { cls: "dim", text: " =", title: `previous period: ${pct1(before)}` });
  const good = better === "down" ? diff < 0 : diff > 0;
  return el("span", { cls: good ? "good" : "bad", text: diff > 0 ? " ▲" : " ▼", title: `previous period: ${pct1(before)}` });
}
function drawModels(r) {
  const head = ["", "model", "sessions", "output", "ctx/out", "KB/call", "calls", "error rate"];
  const right = (text, title) => el("td", { cls: "r", text, title });
  const body = el("tbody");
  const models = r.models || [];
  for (const m of models) {
    if (m.kind === "legacy") {
      body.append(el("tr", { cls: "dim side" }, el("td"), el("td", { text: "legacy", title: "calls seeded from an inventory of pruned transcripts" }),
        right("–"), right("–"), right("–"), right("–"), right(num(m.calls, 0)), right("–")));
      continue;
    }
    const side = m.kind === "unattributed";
    const err = m.error_rate === null ? "–" : `${pct1(m.error_rate)} (${num(m.tool_errors, 0)})`;
    body.append(el("tr", { cls: side ? "dim side" : null },
      el("td", {}, side ? null : providerBadge(m.provider)),
      el("td", { cls: "model", text: side ? "unattributed" : fmtModel(m.model), title: side ? "no model recorded on these calls or messages" : m.model }),
      right(num(m.sessions, 0)), right(tokens(m.output_tokens), `${num(m.output_tokens, 0)} output tokens`),
      right(m.ctx_per_out === null ? "–" : `${num(m.ctx_per_out, 0)}×`, m.previous.ctx_per_out === null ? null : `previous period: ${num(m.previous.ctx_per_out, 0)}×`),
      right(m.kb_per_call === null ? "–" : num(m.kb_per_call, 2), m.previous.kb_per_call === null ? null : `previous period: ${num(m.previous.kb_per_call, 2)} KB`),
      right(num(m.calls, 0)),
      el("td", { cls: "r" }, err, changeMark(m.error_rate, m.previous.error_rate))));
  }
  const kids = [];
  if (!models.length) kids.push(el("p", { cls: "dim", text: "no model activity in this period" + (state.provider === "all" ? "" : ` for ${state.provider}`) }));
  else {
    if (state.provider === "all" && (r.providers || []).length > 1) {
      kids.push(el("div", { cls: "dim prov-sum" }, ...r.providers.flatMap((p) => [providerBadge(p.provider),
        ` ${tokens(p.output_tokens)} out · ${num(p.calls, 0)} calls · ${pct1(p.error_rate)} errors   `])));
    }
    kids.push(el("div", { cls: "scroll" }, el("table", { cls: "models" }, el("thead", {}, el("tr", {}, ...head.map((h, i) => el("th", { cls: i > 1 ? "r" : null, text: h })))), body)));
    if (models.some((m) => m.kind !== "model")) {
      kids.push(el("p", { cls: "dim", text: "dimmed: unattributed = calls or replies with no model recorded; legacy = calls seeded from pruned transcripts (count only, no sizes, results or tokens). error rate = errors / calls whose result arrived; ▲▼ vs the period before" }));
    }
  }
  $("models").replaceChildren(...kids);
}

async function loadReport() {
  document.querySelectorAll("#days button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state.days)));
  document.querySelectorAll("#bucket button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state.bucket)));
  document.querySelectorAll("#provider button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state.provider)));
  const prov = state.provider === "all" ? "" : `&provider=${state.provider}`;
  try { state.report = await api(`/api/report?days=${state.days}&bucket=${state.bucket}${prov}`); drawReport(state.report); }
  catch (err) { $("ingest-status").textContent = `report unavailable: ${err.message}`; }
}
$("days").addEventListener("click", (e) => {
  if (!e.target.dataset.v) return;
  state.days = e.target.dataset.v; put("days", state.days);
  if (state.days === "90" || state.days === "0") { state.bucket = "week"; put("bucket", "week"); }
  loadReport();
});
$("bucket").addEventListener("click", (e) => { if (e.target.dataset.v) { state.bucket = e.target.dataset.v; put("bucket", state.bucket); loadReport(); } });
$("provider").addEventListener("click", (e) => { if (e.target.dataset.v) { state.provider = e.target.dataset.v; put("provider", state.provider); loadReport(); } });
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

// ------------------------------------------------------------------ operator inbox: what agents send the human
const inboxState = {
  counts: null, messages: [], threads: [], sel: null, squad: "", sig: "", feedSig: "", notifySig: "",
  notify: get("notify", "0") === "1", notified: new Set(JSON.parse(get("notified", "[]"))),
};
const canNotify = () => "Notification" in window;

// header badge, the blocking alert and notifications: polled every 5 s on every tab
async function loadInboxCounts() {
  let c;
  try { c = await api("/api/operator/inbox?limit=0"); } catch { return; }
  inboxState.counts = c;
  const btn = $("inbox-btn");
  btn.textContent = `[✉ ${c.unread}]`;
  btn.className = `inbox-btn${c.open.blocking ? " crit" : c.open.reply ? " amber" : c.unread ? " hi" : ""}`;
  btn.setAttribute("aria-label", `operator inbox, ${c.unread} unread, ${c.open.reply} waiting for a reply, ${c.open.blocking} blocking`);
  if (c.open.blocking) {
    setAlert("operator-blocking", {
      level: "crit", text: `▲ blocked on operator ${age(c.oldest_blocking_ts)}`,
      title: `${c.open.blocking} agent message${c.open.blocking === 1 ? "" : "s"} cannot go on until you answer: open the inbox (7)`,
      onClick: () => show("inbox"),
    });
    document.title = "(!) ALMANAC";
    const notifySig = `${c.latest_id}|${c.open.blocking}`;
    if (notifySig !== inboxState.notifySig) { inboxState.notifySig = notifySig; notifyBlocking(); }
  } else {
    clearAlert("operator-blocking");
    document.title = "Almanac";
  }
}

// one browser notification per blocking message id, only once the operator turned them on from a click
async function notifyBlocking() {
  if (!inboxState.notify || !canNotify() || Notification.permission !== "granted") return;
  let doc;
  try { doc = await api("/api/operator/inbox?limit=300"); } catch { return; }
  for (const m of doc.messages) {
    if (m.needs !== "blocking" || m.answered_ts || !OPERATOR.has(m.to_name) || inboxState.notified.has(m.id)) continue;
    inboxState.notified.add(m.id);
    const n = new Notification(isSystem(m) ? `${msgFrom(m)} is stalled` : `${m.from_name} is blocked on you`, { body: m.body.slice(0, 200), tag: `almanac-${m.id}` });
    n.onclick = () => { window.focus(); show("inbox"); selectThread(threadKey(m)); n.close(); };
  }
  put("notified", JSON.stringify([...inboxState.notified].slice(-500)));
}

function paintNotifyToggle() {
  const b = $("inbox-notify");
  b.disabled = !canNotify();
  b.title = !canNotify() ? "this browser has no notifications"
    : canNotify() && Notification.permission === "denied" ? "notifications are blocked for this page in the browser's site settings"
    : "a browser notification when an agent is blocked on you";
  const on = inboxState.notify && canNotify() && Notification.permission === "granted";
  b.textContent = on ? "[NOTIFY ◆]" : "[NOTIFY ◇]";
  b.setAttribute("aria-pressed", String(on));
}
$("inbox-notify").addEventListener("click", async () => {
  if (!canNotify()) return;
  if (inboxState.notify) inboxState.notify = false;
  else inboxState.notify = (await Notification.requestPermission()) === "granted";   // asked only from this click
  put("notify", inboxState.notify ? "1" : "0");
  paintNotifyToggle();
  inboxState.notifySig = "";
  if (inboxState.notify) loadInboxCounts();
});

// a thread is everything between the operator and one other party in one squad
const threadKey = (m) => `${isMine(m) ? m.to_name : msgFrom(m)}\u0000${m.squad_id || ""}`;
function buildThreads(messages) {
  const by = new Map();
  for (const m of messages) {
    const k = threadKey(m);
    const [other, squad] = k.split("\u0000");
    const t = by.get(k) || { key: k, other, squad, session: null, msgs: [], unread: 0, open: [], last: 0 };
    t.msgs.push(m);
    // handles repeat across squads: address the other party by its newest session id, never by name alone
    const sid = isMine(m) ? m.to_session : isSystem(m) ? null : m.from_session;   // a notice's session is the hub's
    if (sid && m.ts >= (t.sessionTs || 0)) { t.session = sid; t.sessionTs = m.ts; }
    t.last = Math.max(t.last, m.ts);
    if (OPERATOR.has(m.to_name) && !m.read_ts) t.unread += 1;
    if (m.needs && !m.answered_ts) t.open.push(m);
    by.set(k, t);
  }
  return [...by.values()].sort((a, b) => b.last - a.last);
}

async function loadInbox() {
  const sq = inboxState.squad;
  let doc;
  try { doc = await api(`/api/operator/inbox?limit=300${sq ? `&squad=${encodeURIComponent(sq)}` : ""}`); }
  catch (err) { $("inbox-summary").textContent = `inbox unavailable: ${err.message}`; return; }
  // squads are re-read on entering INBOX (show() sets the flag), so a squad launched since shows up in the filter
  if (inboxState.squadsStale || !state.squads.length) {
    try { state.squads = (await api("/api/squads?limit=50")).squads || []; inboxState.squadsStale = false; } catch { /* the filter can wait */ }
  }
  if (!state.roles.length) await loadRoles();
  const sqSel = $("inbox-squad");
  const ids = state.squads.map((s) => s.id);
  if (sq && !ids.includes(sq)) ids.push(sq);             // the chosen squad stays choosable while it is filtered on
  const sqSig = ids.join(",") + "|" + state.squads.map((s) => s.name).join(",");
  if (sqSel.dataset.sig !== sqSig) {
    sqSel.replaceChildren(el("option", { value: "", text: "all squads" }), ...ids.map((id) => {
      const s = state.squads.find((x) => x.id === id);
      return el("option", { value: id, text: s ? `${s.name} (${s.id})` : id });
    }));
    sqSel.dataset.sig = sqSig;
  }
  if (document.activeElement !== sqSel) sqSel.value = sq;
  inboxState.messages = doc.messages;
  inboxState.threads = buildThreads(doc.messages);
  $("inbox-summary").textContent = `${inboxState.threads.length} thread${inboxState.threads.length === 1 ? "" : "s"} · `
    + `${doc.unread} unread · ${doc.open.blocking} blocking · ${doc.open.reply} waiting for a reply`;
  const sig = JSON.stringify(inboxState.threads.map((t) => [t.key, t.msgs.length, t.unread, t.open.length, t.msgs.at(-1)?.answered_ts]))
    + inboxState.sel + Math.floor(Date.now() / 30000);    // ages on badges move: repaint at least every 30 s
  if (sig === inboxState.sig) return;
  inboxState.sig = sig;
  paintThreads();
  paintThread();
}

function paintThreads() {
  const list = $("inbox-threads");
  const empty = !inboxState.threads.length;
  $("inbox-empty").hidden = !empty;
  if (empty) {
    const pagers = state.roles.filter((r) => r.may_page_operator).map((r) => r.id);
    $("inbox-empty").textContent = `nothing to or from the operator yet. Roles that may page you: ${pagers.join(", ") || "none"}; `
      + "any agent may answer a message you send it.";
  }
  list.replaceChildren(...inboxState.threads.map((t) => {
    const blocking = t.open.filter((m) => m.needs === "blocking" && OPERATOR.has(m.to_name));
    const replying = t.open.filter((m) => m.needs === "reply" && OPERATOR.has(m.to_name));
    const oldest = blocking.length ? Math.min(...blocking.map((m) => m.ts)) : 0;
    const last = t.msgs.at(-1);
    const btn = el("button", { type: "button", cls: `thread${t.key === inboxState.sel ? " sel" : ""}`, "aria-current": t.key === inboxState.sel ? "true" : null },
      el("span", { cls: "thread-head" },
        el("span", { cls: isMine(last) ? "cyan" : "hi", text: t.other }),
        t.squad ? el("span", { cls: "dim", text: ` · ${t.squad}` }) : null,
        el("span", { cls: "grow" }),
        t.unread ? el("span", { cls: "unread", text: `${t.unread} new` }) : null,
        blocking.length ? el("span", { cls: "flag blocking", text: `BLOCKING ${age(oldest)}` })
          : replying.length ? el("span", { cls: "flag reply", text: "REPLY" }) : null,
        el("span", { cls: "dim", text: ` ${when(last.ts)}` })),
      el("span", { cls: "thread-last dim", text: `${isMine(last) ? "you: " : isSystem(last) ? "system: " : ""}${last.body}`.replace(/\s+/g, " ").slice(0, 90) }));
    btn.addEventListener("click", () => selectThread(t.key));
    return el("li", {}, btn);
  }));
}

function selectThread(key) {
  inboxState.sel = key;
  inboxState.sig = ""; inboxState.feedSig = "";
  paintThreads(); paintThread();
  $("inbox-text").focus();
}

async function markThreadRead(t) {
  const ids = t.msgs.filter((m) => OPERATOR.has(m.to_name) && !m.read_ts).map((m) => m.id);
  if (!ids.length) return;
  try { await post("/api/operator/read", { ids }); } catch { return; }
  const now = Date.now() / 1000;
  t.msgs.forEach((m) => { if (ids.includes(m.id)) m.read_ts = now; });
  t.unread = 0;
  loadInboxCounts();
}

function paintThread() {
  const t = inboxState.threads.find((x) => x.key === inboxState.sel);
  const form = $("inbox-form"), feed = $("inbox-feed");
  form.hidden = !t || t.other === "system";             // a notice naming no agent has nobody to answer
  if (!t) {
    $("inbox-head").replaceChildren(el("span", { cls: "dim", text: inboxState.threads.length ? "select a thread on the left" : "" }));
    feed.replaceChildren();
    return;
  }
  $("inbox-thread-box").setAttribute("data-title", `THREAD // ${t.other}`.slice(0, 56));
  const peer = (meshState.peers || []).find((p) => t.session && p.session_id === t.session)
    || (t.session ? null : (meshState.peers || []).find((p) => p.name === t.other && (!t.squad || p.squad_id === t.squad)));
  $("inbox-head").replaceChildren(...[      // replaceChildren would print a null as "null"
    el("span", { cls: "title", text: t.other }),
    t.squad ? el("span", { cls: "dim", text: ` · squad ${t.squad}` }) : null,
    t.session ? el("span", { cls: "dim", text: ` · session ${t.session.slice(0, 8)}`, title: t.session }) : null,
    el("span", { cls: "dim", text: ` · ${t.msgs.length} message${t.msgs.length === 1 ? "" : "s"}` }),
    peer ? el("span", { cls: peer.stall ? "amber" : "dim", text: ` · ${peer.stall ? `stalled (${peer.stall.error || "API error"})` : peer.status || "idle"}` }) : null].filter(Boolean));
  $("inbox-to").textContent = t.session ? `${t.other} (${t.session.slice(0, 8)})` : t.other;
  const sig = JSON.stringify(t.msgs.map((m) => [m.id, m.answered_ts])) + Math.floor(Date.now() / 30000);
  if (sig !== inboxState.feedSig) {
    inboxState.feedSig = sig;
    const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
    feed.replaceChildren(...t.msgs.map((m) => {
      const mine = isMine(m), sys = isSystem(m);
      const resolve = m.needs && !m.answered_ts
        ? el("button", { type: "button", cls: "btn", text: "[RESOLVE ✓]", title: "close this flag by hand (no reply needed)" }) : null;
      resolve?.addEventListener("click", async () => {
        resolve.disabled = true;
        try {
          const r = await post("/api/operator/resolve", { id: m.id });
          if (r.ok === false) $("inbox-msg").textContent = `refused: ${r.error}`;
        } catch (err) { $("inbox-msg").textContent = `failed: ${err.message}`; }
        inboxState.sig = ""; await loadInbox(); loadInboxCounts();
      });
      return el("li", { cls: mine ? "from-op" : sys ? "from-sys" : "" },
        el("div", { cls: "msg-head" },
          el("span", { cls: "dim", text: clock(m.ts) }),
          el("span", { cls: mine ? "cyan" : sys ? "amber" : "hi", text: mine ? "you" : sys ? "system" : m.from_name,
            title: sys ? "a notice from Almanac itself, not from you" : null }),
          el("span", { cls: "dim", text: "──▸" }),
          el("span", { cls: "dim", text: OPERATOR.has(m.to_name) ? "you" : m.to_name }),
          m.channel ? el("span", { cls: "violet", text: m.channel }) : null,
          needsBadge(m), resolve),
        el("div", { cls: "msg-body" }, renderMd(m.body)));
    }));
    if (atBottom || feed.dataset.key !== t.key) feed.scrollTop = feed.scrollHeight;
    feed.dataset.key = t.key;
  }
  if (t.unread && state.tab === "inbox" && !document.hidden) markThreadRead(t);
}

$("inbox-squad").addEventListener("change", (e) => { inboxState.squad = e.target.value; inboxState.sig = ""; loadInbox(); });
$("inbox-refresh").addEventListener("click", () => { inboxState.sig = ""; loadInbox(); loadInboxCounts(); });
$("inbox-btn").addEventListener("click", () => show("inbox"));
$("inbox-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (e.isComposing || e.keyCode === 229) return;
    $("inbox-form").requestSubmit();
  }
});
$("inbox-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const t = inboxState.threads.find((x) => x.key === inboxState.sel);
  const text = $("inbox-text").value.trim(), msg = $("inbox-msg"), btn = $("inbox-send");
  if (!t || !text) return;
  btn.disabled = true; msg.textContent = `sending to ${t.other}…`;
  try {
    const r = await post("/api/mesh/send", { to: t.session || t.other, text, squad_id: t.squad || null, needs: $("inbox-needs").value || null });
    if (r.ok) {
      $("inbox-text").value = ""; $("inbox-needs").value = "";
      msg.textContent = (r.delivered || []).length ? `delivered to ${r.delivered.join(", ")} ✓` : `nobody received it: ${t.other} is not live`;
      inboxState.sig = ""; await loadInbox(); loadInboxCounts();
    } else {
      msg.textContent = `not delivered: ${r.error || "refused"}${/no live|not found|no session|LookupError/i.test(r.error || "") ? " (the agent is no longer live)" : ""}`;
    }
  } catch (err) { msg.textContent = `failed: ${err.message}`; }
  btn.disabled = false;
  $("inbox-text").focus();
});

// ------------------------------------------------------------------ tabs, keys, loops
function show(tab) {
  state.tab = tab;
  document.querySelectorAll(".tabs [role=tab]").forEach((b) => {
    const sel = b.dataset.tab === tab;
    b.setAttribute("aria-selected", String(sel));
    if (sel) b.scrollIntoView({ block: "nearest", inline: "nearest" });
  });
  for (const id of ["fleet", "gallery", "mesh", "chats", "telemetry", "log", "inbox"]) $(id).hidden = id !== tab;
  if (tab === "chats") { loadChats(); nameStatus(); }
  if (tab === "telemetry") loadReport();
  if (tab === "log") loadLog();
  if (tab === "inbox") { inboxState.sig = ""; inboxState.squadsStale = true; loadInbox(); paintNotifyToggle(); }
  if (tab === "fleet") loadFleet();
  if (tab === "gallery") loadFleet().then(loadGallery);
  if (tab === "mesh") { planState.stale = true; loadMesh().then(() => { loadHeat(true); loadTopo(true); }); if (meshState.selPeer) loadPeerContext(); }
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
    case "7": show("inbox"); break;
    case "m": if (state.tab === "fleet" && state.detail?.process?.role === "session") { e.preventDefault(); editSelectedSessionModel(); } break;
    case "s": if (state.tab === "fleet") { e.preventDefault(); setDrawer("spawn-drawer", "toggle-spawn", $("spawn-drawer").hidden); } break;
    case "p": if (state.tab === "fleet" && state.detail?.process?.role === "session") { togglePin(state.detail.process); loadDetail(); loadFleet(); } break;
    case "?": $("help").hidden = !$("help").hidden; break;
    case "Escape":
      $("help").hidden = true;
      setDrawer("spawn-drawer", "toggle-spawn", false);
      setDrawer("squad-drawer", "toggle-squad", false);
      $("roles-drawer").hidden = true;
      closeAlertBox();
      break;
    case "r":
      state.tab === "chats" ? loadChats()
        : state.tab === "telemetry" ? loadReport()
        : state.tab === "log" ? loadLog()
        : state.tab === "inbox" ? ((inboxState.sig = ""), (inboxState.squadsStale = true), loadInbox(), loadInboxCounts())
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
      else if (state.tab === "inbox" && !$("inbox-form").hidden) { e.preventDefault(); $("inbox-text").focus(); }
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
// the 2 s poll skips a tick while the previous tick's requests are still out (a slow server would otherwise stack
// them); a tick stuck past POLL_STUCK_MS no longer blocks the next. On the other tabs the fleet is still read every
// 10 s, so a pinned or squad session that starts and exits there gets its ended gallery tile (trackSessions).
const POLL_STUCK_MS = 20000;
const poll = { busy: 0, tick: 0 };
setInterval(async () => {
  if (document.hidden || (poll.busy && Date.now() - poll.busy < POLL_STUCK_MS)) return;
  const tab = state.tab, jobs = [];
  poll.tick += 1;
  if (tab === "fleet") jobs.push(loadFleet(), loadDetail());
  else if (tab === "gallery") jobs.push(loadFleet().then(loadGallery));
  else if (poll.tick % 5 === 0) jobs.push(loadFleet());
  if (tab === "mesh") { jobs.push(loadMesh()); if (meshState.selPeer) jobs.push(loadPeerContext()); }
  if (tab === "inbox") jobs.push(loadInbox());
  if (!jobs.length) return;
  const mine = poll.busy = Date.now();
  try { await Promise.allSettled(jobs); } finally { if (poll.busy === mine) poll.busy = 0; }
}, 2000);
setInterval(() => { if (!document.hidden) loadInboxCounts(); }, 5000);
setInterval(() => { if (!document.hidden && state.tab === "mesh") loadHeat(true); }, 30000);
// a merge does not bump objects_rev: while a shown task is unmerged or unknown, refetch the plan (and branches) every 30 s
setInterval(() => {
  if (!document.hidden && state.tab === "mesh" && planPending(planState.rows)) { planState.stale = true; loadPlan(planState.rev); }
}, 30000);
setInterval(() => { if (!document.hidden && state.tab === "log") loadLog(); }, 5000);
setInterval(() => { if (!document.hidden) loadHealth(); }, 15000);
setInterval(() => { if (!document.hidden && state.tab === "telemetry") loadReport(); }, 5 * 60 * 1000);

boot();
firstHealth();
loadInboxCounts();
paintNotifyToggle();
loadModels();
loadRoles();
loadFleet().then(() => {
  const busy = state.fleet?.processes.find((p) => p.role === "session");
  if (busy && !state.sel) select(busy.pid);
});
