/* Headless integration harness for the hermes-chat-dashboard bundle.
 *
 * Loads bundle/index.js with a synthetic window/document/SDK, a fake REST
 * backend implementing the plugin's own routes, and a scriptable fake
 * gateway WebSocket, then drives realistic user flows. Any render-path
 * exception in the bundle crashes the scenario here with a stack trace —
 * this is how runtime crashes are reproduced without a browser.
 *
 * Usage: node tests/harness/run.js [scenarioName...]
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const mini = require("./miniReact.js");

// ── fake backend state ───────────────────────────────────────────────

const state = {
  capabilities: {
    modes: [
      { id: "fast", emoji: "⚡", label: "Fast", description: "Quick answers.", strategy: { fast: true } },
      { id: "reasoning", emoji: "🧠", label: "Reasoning", description: "Deep reasoning.", strategy: {} },
    ],
    models: [
      { id: "prov:m-1", model: "m-1", provider: "prov", name: "m-1", context_window: 128000 },
      { id: "prov:m-2", model: "m-2", provider: "prov", name: "m-2", context_window: 32000 },
    ],
    current_model: "prov:m-1",
    toolsets: [{ id: "web", name: "web", label: "Web", description: "web tools", tool_count: 2, enabled: true }],
    agents: [{ id: "auto", label: "Auto", description: "" }],
    features: {},
  },
  settings: {},
  metadata: {},
  shares: {},
  sessions: [],
  messages: {},   // session_id -> rows
  sessionsByTitle: {},
};

function seedSession(id, title, ago, msgs) {
  const rows = msgs.map((m, i) => ({
    id: `${id}-${i}`,
    role: m.role,
    content: m.content,
    tool_name: m.tool_name || "",
    tool_call_id: m.tool_call_id || "",
    tool_calls: m.tool_calls || null,
    timestamp: (Date.now() / 1000) - (ago || 3600) + i,
  }));
  state.sessions.push({
    id, title, preview: (msgs.find((m) => m.role === "user") || {}).content || "",
    started_at: (Date.now() / 1000) - (ago || 3600), message_count: msgs.length, source: "web", snippet: "",
  });
  state.messages[id] = rows;
}

seedSession("s-older", "Older chat", 90000, [
  { role: "user", content: "hello there" },
  { role: "assistant", content: "hi!" },
]);
seedSession("s-today", "Today chat", 600, [
  { role: "user", content: "search the web for cats" },
  { role: "tool", content: "did 2 searches", tool_name: "web_search" },
  { role: "assistant", content: "found **cats**" },
]);

// ── REST router (mirrors plugin_api.py shapes) ──────────────────────

function rest(url, opts) {
  opts = opts || {};
  const u = new URL(url, "https://dashboard.test");
  const p = u.pathname;
  const body = opts.body ? JSON.parse(opts.body) : {};
  const json = (v) => ({ __json: true, value: v });

  if (p === "/api/plugins/hermes-chat-dashboard/capabilities") return json(state.capabilities);
  if (p === "/api/plugins/hermes-chat-dashboard/settings") {
    if (opts.method === "PUT") { state.settings = { ...state.settings, ...body }; return json({ ok: true, settings: state.settings }); }
    return json({ density: "comfortable", messageWidth: "wide", fontSize: "medium", showTimestamps: true, showUsage: true, confirmDelete: false, enterToSend: true, autoScroll: true, autoTitle: true, autoTools: true, saveHistory: true, memoryEnabled: true, temporaryDefault: false, defaultMode: "fast", defaultModel: "", defaultAgent: "auto", defaultTools: [], pinnedModels: [], ...state.settings });
  }
  if (p === "/api/plugins/hermes-chat-dashboard/metadata") return json(state.metadata);
  const metaPut = p.match(/^\/api\/plugins\/hermes-chat-dashboard\/metadata\/(.+)$/);
  if (metaPut && opts.method === "PUT") {
    state.metadata[metaPut[1]] = { ...(state.metadata[metaPut[1]] || {}), ...body, updated_at: Date.now() / 1000 };
    return json({ ok: true, metadata: state.metadata[metaPut[1]] });
  }
  if (p === "/api/plugins/hermes-chat-dashboard/sessions") {
    const q = u.searchParams.get("q") || "";
    let ids = state.sessions.map((s) => s.id);
    if (q && q.length >= 2) ids = ids.filter((id) => { const s = state.sessions.find((x) => x.id === id); return (s.title + s.preview).includes(q); });
    const limit = Number(u.searchParams.get("limit") || 200);
    const offset = Number(u.searchParams.get("offset") || 0);
    const rows = ids.slice(offset, offset + limit).map((id) => state.sessions.find((s) => s.id === id));
    return json({ sessions: rows, has_more: false });
  }
  const sess = p.match(/^\/api\/plugins\/hermes-chat-dashboard\/sessions\/([^/]+)$/);
  if (sess) {
    const id = decodeURIComponent(sess[1]);
    if (!state.messages[id]) return Promise.reject(new Error("404 session not found"));
    const all = state.messages[id];
    let limit = Number(u.searchParams.get("limit") || 0);
    let offset = Number(u.searchParams.get("offset") || 0);
    if (!limit) limit = all.length;
    if (offset < 0) offset = Math.max(0, all.length + offset); // "newest page" form
    const page = all.slice(offset, offset + limit);
    return json({ session: state.sessions.find((s) => s.id === id) || { id }, messages: page, count: page.length, total: all.length, offset, has_more: offset + page.length < all.length });
  }
  const tree = p.match(/^\/api\/plugins\/hermes-chat-dashboard\/sessions\/([^/]+)\/tree$/);
  if (tree) return json({ session: { id: tree[1] }, parent: null, children: [{ id: "b1", title: "Branch child", started_at: 1000, message_count: 2 }] });
  const ren = p.match(/^\/api\/plugins\/hermes-chat-dashboard\/sessions\/([^/]+)\/rename$/);
  if (ren && opts.method === "POST") {
    const row = state.sessions.find((x) => x.id === ren[1]);
    if (row) row.title = body.title;
    return json({ ok: true, session_id: ren[1], title: body.title });
  }
  const del = p.match(/^\/api\/plugins\/hermes-chat-dashboard\/sessions\/([^/]+)$/);
  if (del && opts.method === "DELETE") {
    state.sessions = state.sessions.filter((x) => x.id !== del[1]);
    delete state.messages[del[1]];
    return json({ ok: true, deleted: del[1] });
  }
  if (p === "/api/plugins/hermes-chat-dashboard/folders") return json({ folders: [{ name: "Work", count: 1 }] });
  const folder = p.match(/^\/api\/plugins\/hermes-chat-dashboard\/folders\/([^/]+)$/);
  if (folder) return json({ ok: true, folders: ["Work"] });
  if (p === "/api/plugins/hermes-chat-dashboard/metadata/bulk" && opts.method === "POST") {
    (body.ids || []).forEach((id) => { state.metadata[id] = { ...(state.metadata[id] || {}), ...(body.patch || {}) }; });
    return json({ ok: true, updated: body.ids || [], deleted: [], skipped: [] });
  }
  if (p === "/api/plugins/hermes-chat-dashboard/usage") return json({ days: 14, per_day: [{ key: "2026-09-03", input: 100, output: 50, sessions: 2 }], per_model: [{ key: "gpt-x", input: 100, output: 50, sessions: 2 }] });
  if (p === "/api/plugins/hermes-chat-dashboard/shares") return json({ shares: [{ token: "t0", session_id: "s-today", session_title: "Today chat", created_at: 1000, revoked: false }] });
  const share = p.match(/^\/api\/plugins\/hermes-chat-dashboard\/share\/([^/]+)$/);
  if (share && opts.method === "POST") return json({ ok: true, token: "tk1", url: `/api/plugins/hermes-chat-dashboard/shared/tk1` });
  if (share && opts.method === "DELETE") return json({ ok: true });
  if (p === "/api/plugins/hermes-chat-dashboard/attachments" && opts.method === "POST") return json({ ok: true, id: "a1", name: "f.txt", path: "/tmp/f.txt", size: 3, content_type: "text/plain", is_image: false, prompt_reference: "@/tmp/f.txt" });
  if (p === "/api/plugins/hermes-chat-dashboard/branch" && opts.method === "POST") {
    seedSession("br-1", "Branched", Date.now() / 1000, [{ role: "user", content: "hello" }]);
    return json({ ok: true, session_id: "br-1", parent: body.session_id, title: "Branched" });
  }
  return Promise.reject(new Error(`harness REST: no route ${opts.method || "GET"} ${p}`));
}

// ── fake gateway WebSocket ───────────────────────────────────────────

let wsCurrent = null;

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this._listeners = {};
    this.sent = [];
    wsCurrent = this;
    queueMicrotask(() => {
      if (FakeWebSocket.connectFails) {
        this.readyState = 3;
        this.dispatch("error", {});
        this.dispatch("close", {});
        return;
      }
      this.readyState = 1;
      this.dispatch("open", {});
      this._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "gateway.ready", payload: {} } });
    });
  }
  addEventListener(t, fn) { (this._listeners[t] || (this._listeners[t] = [])).push(fn); }
  removeEventListener(t, fn) { const l = this._listeners[t]; if (l) { const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); } }
  dispatch(t, ev) { ev.target = this; for (const fn of (this._listeners[t] || []).slice()) fn(ev); }
  send(data) {
    const req = JSON.parse(data);
    this.sent.push(req);
    if (this.onServerRequest) this.onServerRequest(req);
    if (wsMode === "ok") {
      queueMicrotask(() => {
        const h = gatewayHandlers[req.method];
        if (h) this._respond(req.id, h(req.params || {}));
        else this._respondErr(req.id, `unknown method: ${req.method}`);
      });
    }
  }
  close() { this.readyState = 3; this.dispatch("close", {}); }
  _serverFrame(obj) { this.dispatch("message", { data: JSON.stringify(obj) }); }
  _respond(id, result) { this._serverFrame({ jsonrpc: "2.0", id, result }); }
  _respondErr(id, message) { this._serverFrame({ jsonrpc: "2.0", id, error: { code: 1, message } }); }
}

const failConnect = () => {
  FakeWebSocket.connectFails = true;
};

// gateway RPC behaviour, scripted per scenario (default: happy path)
const gatewayHandlers = {
  "session.create": () => ({ session_id: "live-1", info: {} }),
  "session.resume": () => ({ session_id: "live-2", resumed: "s-today", messages: [] }),
  "prompt.submit": () => ({ status: "streaming" }),
  "prompt.background": () => ({ task_id: "bg-1" }),
  "session.title": () => ({ title: "t", session_key: "s-today" }),
  "session.close": () => ({ closed: true }),
  "config.set": () => ({ key: "k", value: "v" }),
  "tools.configure": () => ({ changed: [] }),
  "session.interrupt": () => ({ status: "interrupted" }),
  "session.usage": () => ({ input: 10, output: 5, total: 15, calls: 1, context_used: 1000, context_max: 128000, context_percent: 1 }),
  "approval.respond": () => ({ resolved: true }),
  "clarify.respond": () => ({ status: "ok" }),
  "sudo.respond": () => ({ status: "ok" }),
  "secret.respond": () => ({ status: "ok" }),
  "session.delete": () => ({ deleted: "x" }),
  "session.undo": () => ({ removed: 2 }),
  "session.info": () => ({ session_id: "live-1", title: "Live", model: "m1", enabled_toolsets: ["web"] }),
  "session.steer": () => ({ status: "queued" }),
  "session.compress": () => ({ before: 10000, after: 3000, removed: 4, summary: "kept recent turns" }),
  "config.get": () => ({ key: "mtime", value: 1 }),
  "commands.catalog": () => ({ categories: [{ name: "General", commands: [{ name: "goal", description: "goal mode" }, { name: "retry", description: "regenerate" }] }] }),
  "command.dispatch": () => ({ type: "exec", output: "ok" }),
  "spawn_tree.save": () => ({ saved: true }),
  "spawn_tree.list": () => ({ trees: [{ id: "tree-1", label: "run 1", created_at: 1700000000 }] }),
  "spawn_tree.load": () => ({ subagents: [{ id: "sa-1", parent_id: "", depth: 0, goal: "research", status: "complete", startedAt: 1 }] }),
  "delegation.status": () => ({ paused: false, agents: [{ status: "idle", goal: "bg agent" }] }),
  "delegation.pause": () => ({ paused: true }),
  "subagent.interrupt": () => ({ interrupted: true }),
  "image.attach": () => ({ attached: true }),
};
let wsMode = "ok"; // ok | fail | script

// ── SDK + window globals ─────────────────────────────────────────────

function makeWindow() {
  const win = {
    __HERMES_SESSION_TOKEN__: "tok",
    __HERMES_DASHBOARD_TUI__: true,
    __HERMES_DASHBOARD_EMBEDDED_CHAT__: true,
    setTimeout, clearTimeout, setInterval, clearInterval,
    queueMicrotask,
    console,
    Date, Math, JSON, URL, URLSearchParams, Promise, Object, Array, String, Number, Boolean, Error, RegExp, Map, Set, Symbol, Headers,
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
    location: mini.locationShim,
    localStorage: mini.storageShim(),
    sessionStorage: mini.storageShim(),
    document: mini.documentShim,
    history: {
      pushState: function () {}, replaceState: function () {},
    },
    addEventListener(t, fn) { (this._winListeners || (this._winListeners = {})); ((this._winListeners[t] || (this._winListeners[t] = [])).push(fn)); },
    removeEventListener(t, fn) { const l = this._winListeners && this._winListeners[t]; if (l) { const i = l.indexOf(fn); if (i >= 0) l.splice(i, 1); } },
    dispatchEvent(t, ev) { for (const fn of ((this._winListeners || {})[t] || []).slice()) fn(ev); },
    WebSocket: FakeWebSocket,
    fetch: undefined,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
  };
  win.window = win;
  win.globalThis = win;
  const React = { createElement: mini.createElement, Fragment: mini.Fragment, Component: mini.Component };
  win.__HERMES_PLUGIN_SDK__ = {
    React,
    hooks: mini.hooks,
    fetchJSON: (url, opts) => {
      const r = rest(url, opts);
      if (r && typeof r.then === "function") return r;
      if (r && r.__json) return Promise.resolve(r.value);
      return Promise.resolve(r);
    },
    api: {},
    utils: { timeAgo: (t) => "5m ago" },
    components: {},
  };
  const registered = {};
  win.__HERMES_PLUGINS__ = {
    register: (name, C) => { registered[name] = C; },
    registerSlot: () => {},
    get: (name) => registered[name],
  };
  win.__HERMES_TEST__ = { registered, state };
  return win;
}

// ── harness driver ───────────────────────────────────────────────────

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function flush(ms) {
  await sleep(ms == null ? 30 : ms);
}

function loadBundle(win) {
  const src = fs.readFileSync(path.join(__dirname, "..", ".. " .trim(), "dashboard-plugins", "hermes-chat-dashboard", "dashboard", "bundle", "index.js"), "utf8");
  vm.runInNewContext(src, win, { filename: "bundle/index.js" });
}

let win, ChatDashboard;

function mount() {
  ChatDashboard = win.__HERMES_TEST__.registered["hermes-chat-dashboard"];
  if (!ChatDashboard) throw new Error("bundle did not register hermes-chat-dashboard");
  mini.resetRenderer(ChatDashboard, {});
  mini.flushEffects(mini.invokeRender());
}

function rerender() { mini.flushEffects(mini.invokeRender()); }

function q(pred) { return mini.findAll(pred); }
function byText(str) {
  return mini.findAll((n) => {
    if (!n.children) return false;
    return n.children.some((c) => c && c.text === str);
  });
}
function clickNode(entry) {
  const onClick = entry.node.props && entry.node.props.onClick;
  if (!onClick) throw new Error(`click target has no onClick: ${JSON.stringify({ type: entry.node.type, text: mini.textOf(entry.node) })}`);
  onClick({ stopPropagation: () => {}, preventDefault: () => {}, target: {}, currentTarget: {}, key: "click" });
  rerender();
}

// ── scenarios ────────────────────────────────────────────────────────

const scenarios = {};

scenarios.offline = async () => {
  FakeWebSocket.connectFails = true;
  await flush(80);
  // history must still be browsable
  const titles = mini.findAll((n) => (n.children || []).some((c) => c && c.text === "Older chat"));
  if (!titles.length) throw new Error("offline: session list did not render from REST");
  // open a session while the gateway is down
  const row = mini.findAll((n) => n.props && n.props.className && String(n.props.className).includes("hcd-conv") && mini.textOf(n).includes("Today chat"));
  if (!row.length) throw new Error("offline: today chat row not found");
  const btn = mini.findAll((n) => n.props && typeof n.props.onClick === "function" && String(n.props.className || "").startsWith("hcd-conv") && !String(n.props.className).includes("more")).filter((e) => mini.textOf(e.node).includes("Today chat"))[0];
  if (!btn) throw new Error("offline: open button not found in row");
  clickNode(btn);
  await flush(150);
  const bubble = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-markdown"));
  if (!bubble.length) throw new Error("offline: transcript did not render");
};

scenarios.online = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  // find the composer textarea and send
  const ta = mini.findAll((n) => n.type === "textarea" && n.props.onChange);
  if (!ta.length) throw new Error("online: composer textarea missing");
  ta[0].node.props.onChange({ target: { value: "hello hermes" } });
  rerender();
  const sendBtn = byText("Send");
  if (!sendBtn.length) throw new Error("online: send button missing");
  clickNode(sendBtn[0]);
  await flush(150);
  // gateway should have received prompt.submit
  if (!wsCurrent || !wsCurrent.sent.some((m) => m.method === "prompt.submit")) throw new Error("online: prompt.submit never sent");
  // stream a reply
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.start", session_id: "live-1", payload: {} } });
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.delta", session_id: "live-1", payload: { text: "hi " } } });
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.delta", session_id: "live-1", payload: { text: "there" } } });
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.complete", session_id: "live-1", payload: { text: "hi there", status: "complete", usage: { input: 1, output: 2, total: 3, calls: 1 } } } });
  await flush(80);
  const html = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-markdown")).map((e) => (e.node.props && e.node.props.dangerouslySetInnerHTML && e.node.props.dangerouslySetInnerHTML.__html) || "").join("\n");
  if (!html.includes("hi there")) {
    const texts = []; mini.walkTree(mini.lastTree, (n) => { if (n && n.text != null) texts.push(n.text); });
    console.log("ONLINE HTML:", JSON.stringify(html.slice(0, 400)));
    throw new Error("online: streamed reply not rendered");
  }
};

scenarios.sessionFiltering = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  const ta = mini.findAll((n) => n.type === "textarea" && n.props.onChange);
  ta[0].node.props.onChange({ target: { value: "mine" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(100);
  // events for a different session id must not paint
  const before = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-message")).length;
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.start", session_id: "other-session", payload: {} } });
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.delta", session_id: "other-session", payload: { text: "LEAK" } } });
  await flush(60);
  const html = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-markdown")).map((e) => (e.node.props && e.node.props.dangerouslySetInnerHTML && e.node.props.dangerouslySetInnerHTML.__html) || "").join("");
  if (html.includes("LEAK")) throw new Error("session filter failed: other-session delta leaked into view");
};

scenarios.toolsAndApproval = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  const ta = mini.findAll((n) => n.type === "textarea" && n.props.onChange);
  ta[0].node.props.onChange({ target: { value: "run a tool" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(100);
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "tool.start", session_id: "live-1", payload: { tool_id: "t1", name: "web_search", context: "query=cats" } } });
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "tool.complete", session_id: "live-1", payload: { tool_id: "t1", name: "web_search", summary: "Did 2 searches", duration_s: 1.2 } } });
  await flush(60);
  const cards = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-tool"));
  if (!cards.length) throw new Error("tool card not rendered");
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "approval.request", session_id: "live-1", payload: { request_id: "r1", command: "rm -rf /tmp/x", description: "dangerous" } } });
  await flush(60);
  const approve = byText("Allow once");
  if (!approve.length) throw new Error("approval card not rendered");
  wsCurrent.onServerRequest = (req) => {
    if (req.method === "approval.respond") wsCurrent._respond(req.id, { resolved: true });
  };
  clickNode(approve[0]);
  await flush(60);
  const sent = wsCurrent.sent.find((m) => m.method === "approval.respond");
  if (!sent) { console.log("SENT:", JSON.stringify(wsCurrent.sent.map((m) => m.method))); throw new Error("approval.respond never sent"); }
};

scenarios.openSession = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  const row = mini.findAll((n) => n.props && typeof n.props.onClick === "function" && String(n.props.className || "").includes("hcd-conv") && !String(n.props.className).includes("more") && mini.textOf(n).includes("Today chat"));
  if (!row.length) throw new Error("session row not found");
  clickNode({ node: row[0].node });
  await flush(200);
  const resume = wsCurrent && wsCurrent.sent.find((m) => m.method === "session.resume");
  if (!resume) throw new Error("session.resume not sent");
  await flush(60);
  const html = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-markdown")).map((e) => (e.node.props && e.node.props.dangerouslySetInnerHTML && e.node.props.dangerouslySetInnerHTML.__html) || "").join("");
  if (!html.includes("cats")) throw new Error("opened session transcript missing");
};

scenarios.settings = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  const gear = mini.findAll((n) => (n.node || n) && false); // find via title attr
  const btn = mini.findAll((n) => n.props && n.props.title === "Settings");
  if (!btn.length) throw new Error("settings button missing");
  clickNode({ node: btn[0].node });
  await flush(40);
  if (!mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-settings")).length) throw new Error("settings modal did not open");
  const x = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-x"));
  clickNode({ node: x[0].node });
  await flush(40);
};


// ── stress scenarios (appended) ──────────────────────────────────────

scenarios.stress = {};
scenarios.stress.setup = () => {
  const msgs = [];
  for (let i = 0; i < 1200; i++) msgs.push({ role: i % 2 ? "assistant" : "user", content: `message number ${i} with **markdown** and a link https://example.com/x${i}` });
  seedSession("s-huge", "Huge chat", 60, msgs);
};
scenarios.stress.run = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  const btn = mini.findAll((n) => n.props && typeof n.props.onClick === "function" && String(n.props.className || "").startsWith("hcd-conv")).filter((e) => mini.textOf(e.node).includes("Huge chat"))[0];
  if (!btn) throw new Error("stress: huge session row missing");
  clickNode(btn);
  await flush(300);
  // rapid-fire 300 tool events + deltas
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.start", session_id: "live-2", payload: {} } });
  for (let i = 0; i < 300; i++) {
    wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "tool.start", session_id: "live-2", payload: { tool_id: "t" + i, name: "web_search", context: "q" + i } } });
    wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.delta", session_id: "live-2", payload: { text: "x" } } });
  }
  await flush(300);
  // rapid session switching
  for (let k = 0; k < 6; k++) {
    const rows = mini.findAll((n) => n.props && typeof n.props.onClick === "function" && String(n.props.className || "").startsWith("hcd-conv"));
    if (rows.length) { clickNode(rows[rows.length - 1]); await flush(10); }
  }
  await flush(200);
};

scenarios.errorPaths = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  // gateway returns errors for everything
  wsMode = "script";
  wsCurrent.onServerRequest = (req) => wsCurrent._respondErr(req.id, "boom");
  const ta = mini.findAll((n) => n.type === "textarea" && n.props.onChange);
  ta[0].node.props.onChange({ target: { value: "trigger failure" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(150);
  // FAILED submit must restore the composer draft and drop the optimistic bubble
  const taAfter = mini.findAll((n) => n.type === "textarea" && n.props.onChange);
  const restored = String((taAfter[0] && taAfter[0].node.props.value) || "");
  if (!restored.includes("trigger failure")) throw new Error(`errorPaths: draft not restored after failed send (got ${JSON.stringify(restored)})`);
  const bubbles = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-message"));
  if (bubbles.some((e) => mini.textOf(e.node).includes("trigger failure"))) throw new Error("errorPaths: optimistic user message not rolled back on failure");
  wsMode = "ok";
  wsCurrent.onServerRequest = null;
  // gateway error event (unscoped — no session yet)
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "error", session_id: "", payload: { message: "agent exploded" } } });
  await flush(80);
  // malformed / weird markdown must not throw
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.start", session_id: "", payload: {} } });
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.complete", session_id: "", payload: { text: "> quote\n\n```py\ncode\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- [ ] task\n- [x] done\n\n$x^2$ <script>alert(1)</script>", status: "complete" } } });
  await flush(80);
  const html = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-markdown")).map((e) => (e.node.props.dangerouslySetInnerHTML && e.node.props.dangerouslySetInnerHTML.__html) || "").join("");
  if (html.includes("<script>")) throw new Error("errorPaths: raw HTML was not escaped");
  if (!html.includes("<table")) { console.log("ERRPATH HTML:", JSON.stringify(html.slice(0, 500))); throw new Error("errorPaths: table did not render"); }
};


// ── v1.2 feature scenarios ───────────────────────────────────────────

scenarios.respondParams = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  // establish a live session first so scoped events pass the ownership filter
  const ta0 = mini.findAll((n) => n.type === "textarea" && n.props.onChange)[0];
  ta0.node.props.onChange({ target: { value: "hi" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(100);
  // clarify: answer param
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "clarify.request", session_id: "live-1", payload: { request_id: "c1", question: "Which region?", choices: ["eu", "us"] } } });
  await flush(40);
  const choiceBtn = byText("eu");
  if (!choiceBtn.length) throw new Error("respondParams: clarify choices missing");
  clickNode(choiceBtn[0]);
  await flush(40);
  let sent = wsCurrent.sent.find((m) => m.method === "clarify.respond");
  if (!sent || sent.params.answer !== "eu") throw new Error(`respondParams: clarify.respond params wrong: ${JSON.stringify(sent && sent.params)}`);
  if (sent.params.response !== undefined) throw new Error("respondParams: legacy 'response' param sent");
  // sudo: password param
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "sudo.request", session_id: "live-1", payload: { request_id: "s1" } } });
  await flush(40);
  const inputs = mini.findAll((n) => n.type === "input" && n.props.type === "password");
  if (!inputs.length) throw new Error("respondParams: sudo password input missing");
  inputs[0].node.props.onChange({ target: { value: "hunter2" } });
  rerender();
  clickNode(byText("Submit")[0]);
  await flush(40);
  sent = wsCurrent.sent.find((m) => m.method === "sudo.respond");
  if (!sent || sent.params.password !== "hunter2") throw new Error(`respondParams: sudo.respond params wrong: ${JSON.stringify(sent && sent.params)}`);
  // secret: value param
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "secret.request", session_id: "live-1", payload: { request_id: "k1", prompt: "API key", env_var: "API_KEY" } } });
  await flush(40);
  const sinputs = mini.findAll((n) => n.type === "input" && n.props.placeholder && String(n.props.placeholder).includes("API_KEY"));
  if (!sinputs.length) throw new Error("respondParams: secret input missing");
  sinputs[0].node.props.onChange({ target: { value: "sk-123" } });
  rerender();
  clickNode(byText("Submit")[0]);
  await flush(40);
  sent = wsCurrent.sent.find((m) => m.method === "secret.respond");
  if (!sent || sent.params.value !== "sk-123") throw new Error(`respondParams: secret.respond params wrong: ${JSON.stringify(sent && sent.params)}`);
  // approval: choice param with allow-always scope
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "approval.request", session_id: "live-1", payload: { request_id: "a1", command: "rm -rf /tmp/y" } } });
  await flush(40);
  clickNode(byText("Allow always")[0]);
  await flush(40);
  sent = wsCurrent.sent.find((m) => m.method === "approval.respond");
  if (!sent || sent.params.choice !== "always") throw new Error(`respondParams: approval.respond params wrong: ${JSON.stringify(sent && sent.params)}`);
};

scenarios.steerCompact = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  const ta = mini.findAll((n) => n.type === "textarea" && n.props.onChange)[0];
  ta.node.props.onChange({ target: { value: "long task" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(100);
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.start", session_id: "live-1", payload: {} } });
  await flush(40);
  // while generating the composer becomes a steer bar
  const steerBtn = byText("Steer");
  if (!steerBtn.length) throw new Error("steerCompact: Steer button missing while generating");
  const ta2 = mini.findAll((n) => n.type === "textarea" && n.props.onChange)[0];
  ta2.node.props.onChange({ target: { value: "focus on performance" } });
  rerender();
  clickNode(byText("Steer")[0]);
  await flush(60);
  const st = wsCurrent.sent.find((m) => m.method === "session.steer");
  if (!st || st.params.text !== "focus on performance") throw new Error(`steerCompact: session.steer wrong: ${JSON.stringify(st && st.params)}`);
  // compact via slash command + modal
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.complete", session_id: "live-1", payload: { text: "done", status: "complete" } } });
  await flush(80);
  const ta3 = mini.findAll((n) => n.type === "textarea" && n.props.onChange)[0];
  ta3.node.props.onChange({ target: { value: "/compress keep auth notes" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(60);
  const cmpBtn = byText("Compact");
  if (!cmpBtn.length) throw new Error("steerCompact: compact modal missing");
  clickNode(cmpBtn[0]);
  await flush(60);
  const cp = wsCurrent.sent.find((m) => m.method === "session.compress");
  if (!cp || cp.params.focus_topic !== "keep auth notes") throw new Error(`steerCompact: session.compress wrong: ${JSON.stringify(cp && cp.params)}`);
  const sysHtml = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-markdown")).map((e) => (e.node.props.dangerouslySetInnerHTML && e.node.props.dangerouslySetInnerHTML.__html) || "").join("");
  if (!sysHtml.includes("compacted")) throw new Error("steerCompact: compaction system line missing");
};

scenarios.slashCommands = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  // catalog should have been fetched once the gateway opened
  await flush(40);
  const cat = wsCurrent.sent.find((m) => m.method === "commands.catalog");
  if (!cat) throw new Error("slashCommands: commands.catalog never fetched");
  const ta = mini.findAll((n) => n.type === "textarea" && n.props.onChange)[0];
  ta.node.props.onChange({ target: { value: "/go" } });
  rerender();
  if (!mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-cmdbar")).length) throw new Error("slashCommands: command bar not shown for '/'");
  ta.node.props.onChange({ target: { value: "/goal status" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(120);
  const disp = wsCurrent.sent.find((m) => m.method === "command.dispatch");
  if (!disp || disp.params.name !== "goal" || disp.params.arg !== "status") throw new Error(`slashCommands: dispatch wrong: ${JSON.stringify(disp && disp.params)}`);
  // retry rides the same bridge
  ta.node.props.onChange({ target: { value: "/retry" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(120);
  const retries = wsCurrent.sent.filter((m) => m.method === "command.dispatch" && m.params.name === "retry");
  if (!retries.length) throw new Error("slashCommands: /retry not dispatched");
};

scenarios.subagents = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  const ta = mini.findAll((n) => n.type === "textarea" && n.props.onChange)[0];
  ta.node.props.onChange({ target: { value: "delegate this" } });
  rerender();
  clickNode(byText("Send")[0]);
  await flush(100);
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "subagent.start", session_id: "live-1", payload: { subagent_id: "sa-1", parent_id: "", depth: 0, goal: "research topic", model: "gpt-x" } } });
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "subagent.tool", session_id: "live-1", payload: { subagent_id: "sa-1", tool_preview: "web_search q=1" } } });
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "subagent.start", session_id: "live-1", payload: { subagent_id: "sa-2", parent_id: "sa-1", depth: 1, goal: "summarize" } } });
  await flush(80);
  const tree = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-agent"));
  if (!tree.length) throw new Error("subagents: agent tree not rendered");
  if (!mini.textOf(mini.lastTree).includes("research topic")) throw new Error("subagents: root agent goal missing");
  // nested child rendered under parent
  const kids = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-agent-children"));
  if (!kids.length) throw new Error("subagents: nested children not rendered");
  // activity panel polls delegation + lists saved trees
  await flush(80);
  if (!wsCurrent.sent.some((m) => m.method === "delegation.status")) throw new Error("subagents: delegation.status not polled");
  if (!wsCurrent.sent.some((m) => m.method === "spawn_tree.list")) throw new Error("subagents: spawn_tree.list not called");
  const replay = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-tree-replay"));
  if (!replay.length) throw new Error("subagents: replay buttons missing");
  clickNode(replay[0]);
  await flush(80);
  if (!wsCurrent.sent.some((m) => m.method === "spawn_tree.load")) throw new Error("subagents: spawn_tree.load not called");
  // turn completes → tree saved
  wsCurrent._serverFrame({ jsonrpc: "2.0", method: "event", params: { type: "message.complete", session_id: "live-1", payload: { text: "done", status: "complete" } } });
  await flush(80);
  if (!wsCurrent.sent.some((m) => m.method === "spawn_tree.save")) throw new Error("subagents: spawn_tree.save not called on turn end");
};

scenarios.foldersTagsBulk = async () => {
  FakeWebSocket.connectFails = false;
  state.metadata["s-today"] = { pinned: true, tags: ["alpha", "beta"], folder: "" };
  state.metadata["s-yesterday"] = { tags: ["alpha"], folder: "Work" };
  await flush(120);
  // tag chips + pinned group
  const chip = byText("#alpha");
  if (!chip.length) throw new Error("foldersTagsBulk: tag chip missing");
  if (!mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-side-group")).length) throw new Error("foldersTagsBulk: pinned group missing");
  clickNode(chip[0]);
  await flush(80);
  // only tagged sessions visible now
  const rows = mini.findAll((n) => n.props && String(n.props.className || "").startsWith("hcd-conv"));
  if (!rows.length) throw new Error("foldersTagsBulk: filtered rows empty");
  clickNode(chip[0]); // clear filter
  await flush(60);
  // bulk select mode
  const selBtn = byText("☐ Select");
  if (!selBtn.length) throw new Error("foldersTagsBulk: select-mode button missing");
  clickNode(selBtn[0]);
  await flush(60);
  const boxes = mini.findAll((n) => n.type === "input" && n.props.type === "checkbox" && typeof n.props.onChange === "function");
  if (!boxes.length) throw new Error("foldersTagsBulk: bulk checkboxes missing");
  boxes[0].node.props.onChange({ target: { checked: true } });
  await flush(40);
  clickNode(byText("Pin")[0]);
  await flush(60);
  if (!state.metadata["s-today"] || state.metadata["s-today"].pinned !== true) throw new Error(`foldersTagsBulk: bulk patch not applied: ${JSON.stringify(state.metadata["s-today"])}`);
};

scenarios.palette = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  win.dispatchEvent("keydown", { key: "k", metaKey: true, preventDefault() {}, stopPropagation() {} });
  rerender();
  const pal = mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-palette-row"));
  if (!pal.length) throw new Error("palette: rows missing");
  const input = mini.findAll((n) => n.type === "input" && n.props.placeholder && String(n.props.placeholder).includes("Search commands"));
  if (!input.length) throw new Error("palette: input missing");
  // fuzzy filter
  input[0].node.props.onChange({ target: { value: "share" } });
  rerender();
  if (!mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-palette-row")).some((e) => mini.textOf(e.node).toLowerCase().includes("share"))) throw new Error("palette: fuzzy filter failed");
  // esc closes
  win.dispatchEvent("keydown", { key: "Escape", preventDefault() {}, stopPropagation() {} });
  rerender();
  if (mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-palette-modal")).length) throw new Error("palette: did not close on Escape");
};

scenarios.drafts = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  const ta = () => mini.findAll((n) => n.type === "textarea" && n.props.onChange)[0];
  ta().node.props.onChange({ target: { value: "unsent new-chat thought" } });
  rerender();
  await flush(40);
  if (win.localStorage.getItem("hcd-draft-new") !== "unsent new-chat thought") throw new Error("drafts: new-chat draft not persisted");
  // open a session → its own (empty) draft; type one
  const row = mini.findAll((n) => n.props && (" " + String(n.props.className || "") + " ").includes(" hcd-conv ") && typeof n.props.onClick === "function").filter((e) => mini.textOf(e.node).includes("Today chat"))[0];
  if (!row) throw new Error("drafts: Today chat row missing");
  clickNode(row);
  await flush(200);
  if (String(ta().node.props.value) !== "") throw new Error("drafts: session draft not swapped in");
  ta().node.props.onChange({ target: { value: "half-finished follow-up" } });
  rerender();
  await flush(40);
  if (win.localStorage.getItem("hcd-draft-s-today") !== "half-finished follow-up") throw new Error("drafts: session draft not persisted");
  // back to new chat → draft restored
  clickNode(byText("＋ New conversation")[0]);
  await flush(60);
  if (String(ta().node.props.value) !== "unsent new-chat thought") throw new Error("drafts: new-chat draft not restored");
  if (win.localStorage.getItem("hcd-draft-s-today") !== "half-finished follow-up") throw new Error("drafts: session draft clobbered on swap back");
};

scenarios.errorBoundary = async () => {
  FakeWebSocket.connectFails = false;
  await flush(80);
  // inject a render error into the boundary (simulates a throwing child)
  const eb = Array.from(mini._instances()).find(([k]) => k.includes("ErrorBoundary"));
  if (!eb) throw new Error("errorBoundary: boundary instance not found");
  eb[1].classInst.state = { error: new Error("synthetic payload explosion") };
  rerender();
  if (!mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-crash")).length) throw new Error("errorBoundary: crash card not shown");
  if (!mini.textOf(mini.lastTree).includes("rest of the dashboard is fine")) throw new Error("errorBoundary: recovery copy missing");
  // Try again clears it without unmounting the app
  clickNode(byText("Try again")[0]);
  await flush(60);
  if (mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-crash")).length) throw new Error("errorBoundary: crash card did not clear");
  if (!mini.findAll((n) => n.props && String(n.props.className || "").includes("hcd-welcome")).length) throw new Error("errorBoundary: chat did not recover");
};

// ── runner ───────────────────────────────────────────────────────────

process.on("unhandledRejection", (e) => {
  console.log("UNHANDLED REJECTION:", e && e.stack ? e.stack.split("\n").slice(0, 6).join("\n") : e);
});

(async () => {
  const picked = process.argv.slice(2);
  const names = picked.length ? picked.filter((n) => scenarios[n]) : Object.keys(scenarios);
  let failed = 0;
  for (const name of names) {
    // fresh world per scenario
    win = makeWindow();
    FakeWebSocket.connectFails = false;
    try {
      if (scenarios[name].setup) scenarios[name].setup(win);
      loadBundle(win);
      mount();
      if (process.env.HCD_DUMP) {
        console.log('DUMP root type:', typeof mini.lastTree && mini.lastTree && mini.lastTree.type, 'keys:', mini.lastTree && Object.keys(mini.lastTree));
        try { console.log('DUMP raw:', JSON.stringify(mini.lastTree, (k,v)=> typeof v === 'function' ? '[fn]' : v).slice(0, 800)); } catch (e) { console.log('DUMP err', e); }
      }
      await (scenarios[name].run || scenarios[name])();
      console.log(`PASS ${name}`);
    } catch (e) {
      failed++;
      console.log(`FAIL ${name}: ${e && e.stack ? e.stack.split("\n").slice(0, 8).join("\n") : e}`);
      if (process.env.HCD_DEBUG) {
        const texts = [];
        mini.walkTree(mini.lastTree, (n) => { if (n && n.text != null) texts.push(n.text); });
        console.log("TREE TEXT:", JSON.stringify(texts.slice(0, 80)));
      }
    }
  }
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
