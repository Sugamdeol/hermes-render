(() => {
  const sdk = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!sdk || !registry) return;
  const React = sdk.React;
  const { useCallback, useEffect, useMemo, useRef, useState } = sdk.hooks;
  const { Button, Input, Badge, Select, SelectOption } = sdk.components;
  const { api, fetchJSON } = sdk;
  const h = React.createElement;
  const BASE = "/api/plugins/hermes-chat-dashboard";

  class HermesGateway {
    constructor() { this.ws = null; this.id = 0; this.pending = new Map(); this.listeners = new Map(); this.state = "idle"; }
    on(type, cb) { const set = this.listeners.get(type) || new Set(); set.add(cb); this.listeners.set(type, set); return () => set.delete(cb); }
    emit(ev) { (this.listeners.get(ev.type) || []).forEach(cb => cb(ev)); (this.listeners.get("*") || []).forEach(cb => cb(ev)); }
    async connect() {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) return;
      const token = window.__HERMES_SESSION_TOKEN__ || "";
      if (!token) throw new Error("Session token unavailable. Open Hermes through the dashboard server.");
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/api/ws?token=${encodeURIComponent(token)}`);
      this.ws = ws; this.state = "connecting";
      ws.addEventListener("message", ev => {
        let msg; try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.id && this.pending.has(msg.id)) {
          const p = this.pending.get(msg.id); this.pending.delete(msg.id); clearTimeout(p.timer);
          msg.error ? p.reject(new Error(msg.error.message || "request failed")) : p.resolve(msg.result);
          return;
        }
        if (msg.method === "event" && msg.params && msg.params.type) this.emit(msg.params);
      });
      ws.addEventListener("close", () => { this.state = "closed"; for (const p of this.pending.values()) p.reject(new Error("Gateway connection closed")); this.pending.clear(); });
      await new Promise((resolve, reject) => { ws.addEventListener("open", () => { this.state = "open"; resolve(); }, { once: true }); ws.addEventListener("error", () => reject(new Error("Gateway WebSocket failed")), { once: true }); });
    }
    close() { try { this.ws && this.ws.close(); } catch {} this.ws = null; }
    request(method, params = {}, timeout = 120000) {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return Promise.reject(new Error("gateway is not connected"));
      const id = `chat-${++this.id}`;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`Request timed out: ${method}`)); }, timeout);
        this.pending.set(id, { resolve, reject, timer });
        this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      });
    }
  }

  const nowId = () => `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  const safe = (s) => String(s || "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  function markdownToHtml(md) {
    let text = safe(md || "");
    const blocks = [];
    text = text.replace(/```([\w.+-]*)\n([\s\S]*?)```/g, (_, lang, code) => { const i = blocks.push(`<div class="hcd-code"><div><span>${safe(lang || "code")}</span><button data-copy-code="${blocks.length}">Copy</button></div><pre><code>${code}</code></pre></div>`) - 1; return `\n§CODE${i}§\n`; });
    text = text.replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>");
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/\*([^*]+)\*/g, "<em>$1</em>").replace(/`([^`]+)`/g, "<code>$1</code>");
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    text = text.replace(/^\s*[-*] (.*)$/gm, "<li>$1</li>").replace(/(<li>[\s\S]*?<\/li>)(\n<li>)/g, "$1$2").replace(/((?:<li>[\s\S]*?<\/li>\n?)+)/g, "<ul>$1</ul>");
    text = text.replace(/\$\$([\s\S]*?)\$\$/g, '<div class="hcd-math">$1</div>').replace(/\$([^$\n]+)\$/g, '<span class="hcd-math-inline">$1</span>');
    text = text.split(/\n{2,}/).map(p => (/^\s*<(h\d|ul|div|pre)/.test(p) ? p : `<p>${p.replace(/\n/g, "<br/>")}</p>`)).join("\n");
    blocks.forEach((b, i) => { text = text.replace(`§CODE${i}§`, b); });
    return text;
  }
  function timeLabel(ts) { if (!ts) return ""; try { return new Date(ts * 1000).toLocaleString(); } catch { return ""; } }
  function roleName(role) { return role === "assistant" ? "Hermes" : role === "tool" ? "Tool" : role === "system" ? "System" : "You"; }

  function Message({ msg, index, active, onAction }) {
    const bodyRef = useRef(null);
    useEffect(() => {
      const root = bodyRef.current; if (!root) return;
      root.querySelectorAll("button[data-copy-code]").forEach(btn => {
        btn.onclick = () => navigator.clipboard.writeText(btn.closest(".hcd-code").querySelector("code").innerText || "");
      });
    }, [msg.content]);
    const isHermes = msg.role === "assistant";
    return h("article", { className: `hcd-message hcd-${msg.role || "assistant"}`, id: msg.id || undefined },
      h("div", { className: "hcd-avatar" }, isHermes ? "H" : msg.role === "tool" ? "⚙" : "U"),
      h("div", { className: "hcd-bubble" },
        h("div", { className: "hcd-meta" }, h("strong", null, roleName(msg.role)), msg.timestamp && h("span", null, timeLabel(msg.timestamp))),
        msg.attachments && msg.attachments.length ? h("div", { className: "hcd-attachments" }, msg.attachments.map(a => h("span", { key: a.id || a.path }, a.is_image ? "🖼️ " : "📎 ", a.name))) : null,
        msg.error ? h("div", { className: "hcd-error" }, "Something went wrong while generating the response.", h("details", null, h("summary", null, "View details"), h("pre", null, msg.error))) :
          h("div", { ref: bodyRef, className: "hcd-markdown", dangerouslySetInnerHTML: { __html: markdownToHtml(msg.content || (active ? "▌" : "")) } }),
        h("div", { className: "hcd-actions" },
          h("button", { onClick: () => onAction("copy", msg, index) }, "Copy"),
          msg.role === "user" && h("button", { onClick: () => onAction("edit", msg, index) }, "Edit"),
          isHermes && h("button", { onClick: () => onAction("regenerate", msg, index) }, "Regenerate"),
          isHermes && h("button", { onClick: () => onAction("continue", msg, index) }, "Continue"),
          h("button", { onClick: () => onAction("branch", msg, index) }, "Branch"),
          h("button", { onClick: () => onAction("delete", msg, index) }, "Delete")
        )
      )
    );
  }

  function ToolCard({ tool }) {
    const icon = tool.type === "agent" ? "🤖" : tool.name && tool.name.includes("web") ? "🔎" : tool.name && tool.name.includes("terminal") ? "💻" : "🛠️";
    return h("details", { className: `hcd-tool ${tool.status || "running"}` },
      h("summary", null, h("span", null, icon, " ", tool.title || tool.name || "Tool"), h("em", null, tool.summary || tool.context || tool.preview || (tool.status === "complete" ? "Completed" : "Running…"))),
      h("div", null,
        tool.context && h("p", null, tool.context),
        tool.preview && h("pre", null, tool.preview),
        tool.duration_s && h("small", null, `Duration: ${tool.duration_s.toFixed(1)}s`),
        tool.todos && h("ul", null, tool.todos.map((t, i) => h("li", { key: i }, `${t.status || ""} ${t.content || t}`)))
      )
    );
  }

  function Composer({ disabled, modes, models, agents, toolsets, selected, setSelected, attachments, setAttachments, onSend, onStop, generating, onBackground, draftSeed }) {
    const [text, setText] = useState("");
    useEffect(() => { if (draftSeed) setText(draftSeed); }, [draftSeed]);
    const [drag, setDrag] = useState(false);
    const ta = useRef(null);
    useEffect(() => { if (ta.current) { ta.current.style.height = "auto"; ta.current.style.height = Math.min(220, ta.current.scrollHeight) + "px"; } }, [text]);
    const upload = async (files) => {
      for (const file of files) {
        const local = { id: nowId(), name: file.name, size: file.size, status: "uploading" };
        setAttachments(a => [...a, local]);
        const fd = new FormData(); fd.append("file", file); fd.append("conversation_id", selected.sessionKey || selected.sessionId || "");
        try { const res = await fetchJSON(`${BASE}/attachments`, { method: "POST", body: fd }); setAttachments(a => a.map(x => x.id === local.id ? { ...res, status: "ready" } : x)); }
        catch (e) { setAttachments(a => a.map(x => x.id === local.id ? { ...local, status: "error", error: e.message } : x)); }
      }
    };
    const submit = (background=false) => { const value = text.trim(); if (!value && !attachments.length) return; background ? onBackground(value) : onSend(value); setText(""); };
    return h("div", { className: `hcd-composer ${drag ? "drag" : ""}`, onDragOver: e => { e.preventDefault(); setDrag(true); }, onDragLeave: () => setDrag(false), onDrop: e => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files); } },
      attachments.length ? h("div", { className: "hcd-attach-row" }, attachments.map(a => h("span", { key: a.id, className: `hcd-attach ${a.status}` }, a.is_image ? "🖼️" : "📎", " ", a.name, " ", a.status === "uploading" ? "…" : "", h("button", { onClick: () => setAttachments(xs => xs.filter(x => x.id !== a.id)) }, "×")))) : null,
      h("textarea", { ref: ta, value: text, placeholder: selected.temporary ? "Temporary chat — history and memory disabled where Hermes supports it" : "Message Hermes…", disabled, onPaste: e => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) upload(fs); }, onChange: e => setText(e.target.value), onKeyDown: e => { if (e.key === "Enter" && !e.shiftKey && selected.enterToSend) { e.preventDefault(); submit(false); } } }),
      h("div", { className: "hcd-controls" },
        h("label", null, "Mode", h("select", { value: selected.mode, onChange: e => setSelected(s => ({ ...s, mode: e.target.value })) }, modes.map(m => h("option", { value: m.id, key: m.id }, `${m.emoji || ""} ${m.label}`)))),
        h("label", null, "Model", h("select", { value: selected.model, onChange: e => setSelected(s => ({ ...s, model: e.target.value })) }, h("option", { value: "auto" }, "Auto"), models.slice(0, 250).map(m => h("option", { value: m.id, key: m.id }, `${m.provider || ""}/${m.name || m.model}`)))),
        h("label", null, "Agent", h("select", { value: selected.agent, onChange: e => setSelected(s => ({ ...s, agent: e.target.value })) }, agents.map(a => h("option", { value: a.id, key: a.id }, a.label)))),
        h("details", { className: "hcd-tool-menu" }, h("summary", null, "Tools"), h("div", null, toolsets.map(t => h("label", { key: t.id || t.name }, h("input", { type: "checkbox", checked: (selected.tools || []).includes(t.id || t.name), onChange: e => setSelected(s => ({ ...s, tools: e.target.checked ? [...(s.tools || []), (t.id || t.name)] : (s.tools || []).filter(x => x !== (t.id || t.name)) })) }), " ", t.label || t.name, h("small", null, t.description || ""))))),
        h("button", { className: selected.autoTools ? "on" : "", onClick: () => setSelected(s => ({ ...s, autoTools: !s.autoTools })) }, "Auto Tools"),
        h("button", { className: selected.temporary ? "on" : "", onClick: () => setSelected(s => ({ ...s, temporary: !s.temporary })) }, "Temporary"),
        h("label", { className: "hcd-upload" }, "📎", h("input", { type: "file", multiple: true, onChange: e => upload(e.target.files), hidden: true })),
        generating ? h("button", { className: "danger", onClick: onStop }, "Stop") : h("button", { className: "secondary", onClick: () => submit(true), disabled }, "Background"),
        h("button", { className: "send", onClick: () => submit(false), disabled }, "Send")
      ),
      selected.tools && selected.tools.length ? h("div", { className: "hcd-toolchips" }, selected.tools.map(t => h("span", { key: t }, "🛠️ ", t))) : null
    );
  }

  function Sidebar({ sessions, meta, current, query, setQuery, onNew, onOpen, onMeta, loading }) {
    const visible = useMemo(() => sessions.filter(s => !query || `${s.title || ""} ${s.preview || ""} ${s.snippet || ""} ${s.id}`.toLowerCase().includes(query.toLowerCase())), [sessions, query]);
    const pinned = visible.filter(s => meta[s.id]?.pinned || meta[s.id]?.starred);
    const recent = visible.filter(s => !(meta[s.id]?.pinned || meta[s.id]?.starred) && !meta[s.id]?.archived);
    const archived = visible.filter(s => meta[s.id]?.archived);
    const group = (title, arr) => h("section", null, h("h4", null, title), arr.length ? arr.map(s => h("button", { key: s.id, className: `hcd-conv ${current === s.id ? "active" : ""}`, onClick: () => onOpen(s.id) }, h("span", null, s.title || s.preview || "Untitled chat"), h("small", null, s.snippet || s.preview || s.id), h("em", null, meta[s.id]?.starred ? "★" : ""))) : h("p", { className: "hcd-empty-small" }, "None"));
    return h("aside", { className: "hcd-left" },
      h("div", { className: "hcd-side-head" }, h("strong", null, "💬 Chat"), h("button", { onClick: onNew }, "New")),
      h("input", { className: "hcd-search", value: query, onChange: e => setQuery(e.target.value), placeholder: "Search conversations…" }),
      loading ? h("div", { className: "hcd-empty-small" }, "Loading…") : h(React.Fragment, null, group("Pinned", pinned), group("Recent", recent), group("Archived", archived)),
      current && h("div", { className: "hcd-side-actions" },
        h("button", { onClick: () => onMeta(current, { pinned: !meta[current]?.pinned }) }, meta[current]?.pinned ? "Unpin" : "Pin"),
        h("button", { onClick: () => onMeta(current, { starred: !meta[current]?.starred }) }, meta[current]?.starred ? "Unstar" : "Star"),
        h("button", { onClick: () => onMeta(current, { archived: !meta[current]?.archived }) }, meta[current]?.archived ? "Unarchive" : "Archive")
      )
    );
  }

  function Welcome({ onQuick }) {
    const qs = [
      ["🧠 Ask Hermes", "Answer this question with the right level of detail:\n"], ["💻 Write Code", "Help me implement or debug this code task. First inspect context, then propose and apply a safe plan:\n"], ["🔎 Research", "Research this thoroughly, gather sources, compare findings, and summarize with citations:\n"], ["📄 Analyse File", "I will attach a file. Read it, identify the important structure and findings, then suggest next actions."], ["🧪 Run Experiment", "Design and run a small experiment to test this hypothesis:\n"], ["🤖 Spawn Agent", "Use Hermes agents to plan and execute this multi-step objective:\n"], ["⚙️ Manage System", "Inspect and help manage this Hermes/Render system task safely:\n"], ["📊 Analyse Data", "Analyse this dataset or data question and produce clear conclusions:\n"], ["🔌 Use Plugin", "Use the relevant Hermes plugin or tool for this task:\n"]
    ];
    return h("div", { className: "hcd-welcome" }, h("h1", null, "What can Hermes do for you?"), h("p", null, "Chat directly with the same Hermes brain that powers Telegram, tools, memory, plugins, agents and background tasks."), h("div", { className: "hcd-quick" }, qs.map(([label, prompt]) => h("button", { key: label, onClick: () => onQuick(prompt) }, label))));
  }

  function ContextPanel({ capabilities, selected, tools, memories, sources, tasks, collapsed, setCollapsed }) {
    return h("aside", { className: `hcd-context ${collapsed ? "collapsed" : ""}` },
      h("button", { className: "hcd-collapse", onClick: () => setCollapsed(!collapsed) }, collapsed ? "Context ◀" : "Context ▶"),
      !collapsed && h("div", null,
        selected.temporary && h("div", { className: "hcd-temp" }, "Temporary Chat"),
        h("h4", null, "Files"), h("p", null, "Uploaded files appear in the current message and are referenced through Hermes context handling."),
        h("h4", null, "Memory"), h("p", null, selected.memoryEnabled ? "Memory enabled. Relevant memories are selected server-side by Hermes." : "Memory disabled for this chat where supported."),
        h("h4", null, "Tools"), h("div", { className: "hcd-chipwrap" }, (selected.autoTools ? ["Auto"] : selected.tools || []).map(t => h("span", { key: t }, t))),
        h("h4", null, "Agents"), h("p", null, selected.agent || "auto"),
        h("h4", null, "Tasks"), tasks.length ? tasks.map(t => h("div", { className: "hcd-task", key: t.id }, h("strong", null, "🤖 Task running"), h("p", null, t.text), h("progress", { value: t.done ? 100 : 60, max: 100 }))) : h("p", null, "No background tasks."),
        h("h4", null, "Sources"), sources.length ? sources.map(s => h("a", { key: s, href: s, target: "_blank", rel: "noreferrer" }, s)) : h("p", null, "Sources appear after research tools run.")
      )
    );
  }

  function SettingsModal({ settings, setSettings, onClose }) {
    const update = (patch) => { const next = { ...settings, ...patch }; setSettings(next); fetchJSON(`${BASE}/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) }).catch(() => {}); };
    return h("div", { className: "hcd-modal" }, h("div", { className: "hcd-settings" }, h("button", { className: "hcd-x", onClick: onClose }, "×"), h("h2", null, "Chat Settings"),
      h("h3", null, "Appearance"), h("label", null, "Density", h("select", { value: settings.density, onChange: e => update({ density: e.target.value }) }, h("option", { value: "comfortable" }, "Comfortable"), h("option", { value: "compact" }, "Compact"))), h("label", null, h("input", { type: "checkbox", checked: !!settings.showTimestamps, onChange: e => update({ showTimestamps: e.target.checked }) }), " Show timestamps"),
      h("h3", null, "Behaviour"), h("label", null, h("input", { type: "checkbox", checked: !!settings.enterToSend, onChange: e => update({ enterToSend: e.target.checked }) }), " Enter to send"), h("label", null, h("input", { type: "checkbox", checked: !!settings.autoScroll, onChange: e => update({ autoScroll: e.target.checked }) }), " Auto-scroll"), h("label", null, h("input", { type: "checkbox", checked: !!settings.autoTools, onChange: e => update({ autoTools: e.target.checked }) }), " Auto-select tools"),
      h("h3", null, "Privacy"), h("label", null, h("input", { type: "checkbox", checked: !!settings.saveHistory, onChange: e => update({ saveHistory: e.target.checked }) }), " Save history"), h("label", null, h("input", { type: "checkbox", checked: !!settings.memoryEnabled, onChange: e => update({ memoryEnabled: e.target.checked }) }), " Memory usage")
    ));
  }

  function ChatDashboard() {
    const gwRef = useRef(null); const listEnd = useRef(null); const activeSidRef = useRef(null);
    // Closes the server-side tui_gateway session for the conversation the
    // user is leaving. Kept in a ref so the unmount cleanup can reach it.
    const releaseSessionRef = useRef(null);
    useEffect(() => { releaseSessionRef.current = () => { const sid = activeSidRef.current; if (!sid) return; activeSidRef.current = null; try { gwRef.current && gwRef.current.request("session.close", { session_id: sid }, 10000).catch(() => {}); } catch {} }; });
    const [ready, setReady] = useState(false); const [error, setError] = useState("");
    const [cap, setCap] = useState({ modes: [], models: [], agents: [], toolsets: [], features: {} });
    const [settings, setSettings] = useState({ enterToSend: true, autoTools: true, memoryEnabled: true, defaultMode: "fast" });
    const [selected, setSelected] = useState({ sessionId: "", sessionKey: "", mode: "fast", model: "auto", agent: "auto", tools: [], autoTools: true, temporary: false, memoryEnabled: true, enterToSend: true });
    const [sessions, setSessions] = useState([]); const [meta, setMeta] = useState({}); const [query, setQuery] = useState(""); const [loadingSessions, setLoadingSessions] = useState(false);
    const [messages, setMessages] = useState([]); const [tools, setTools] = useState([]); const [generating, setGenerating] = useState(false); const [activity, setActivity] = useState("");
    const [attachments, setAttachments] = useState([]); const [tasks, setTasks] = useState([]); const [contextCollapsed, setContextCollapsed] = useState(false); const [showSettings, setShowSettings] = useState(false); const [mobileSide, setMobileSide] = useState(false);
    const [draftSeed, setDraftSeed] = useState(""); const [share, setShare] = useState("");

    const refreshSessions = useCallback(async () => { setLoadingSessions(true); try { const [s, m] = await Promise.all([api.getSessions(80,0), fetchJSON(`${BASE}/metadata`)]); setSessions(s.sessions || []); setMeta(m || {}); } catch (e) { setError(e.message); } finally { setLoadingSessions(false); } }, []);
    useEffect(() => { (async () => { try { const [c, st] = await Promise.all([fetchJSON(`${BASE}/capabilities`), fetchJSON(`${BASE}/settings`)]); setCap(c); setSettings(st); setSelected(s => ({ ...s, mode: st.defaultMode || "fast", autoTools: st.autoTools !== false, temporary: !!st.temporaryDefault, memoryEnabled: st.memoryEnabled !== false, enterToSend: st.enterToSend !== false })); } catch (e) { setError(e.message); } await refreshSessions(); const gw = new HermesGateway(); gwRef.current = gw; try { await gw.connect(); setReady(true); } catch (e) { setError(e.message); } })(); return () => { try { releaseSessionRef.current && releaseSessionRef.current(); } catch {} gwRef.current && gwRef.current.close(); }; }, []);

    useEffect(() => {
      const gw = gwRef.current; if (!gw) return;
      const offs = [
        gw.on("message.start", () => { setGenerating(true); setActivity("🧠 Thinking…"); setMessages(ms => [...ms, { id: nowId(), role: "assistant", content: "", streaming: true, timestamp: Date.now()/1000 }]); }),
        gw.on("message.delta", ev => { const txt = ev.payload?.text || ""; setActivity("✍️ Responding…"); setMessages(ms => { const copy = [...ms]; const i = copy.map(m => m.role).lastIndexOf("assistant"); if (i >= 0) copy[i] = { ...copy[i], content: (copy[i].content || "") + txt, streaming: true }; return copy; }); }),
        gw.on("message.complete", async ev => { setGenerating(false); setActivity(""); const text = ev.payload?.text || ""; setMessages(ms => { const copy = [...ms]; const i = copy.map(m => m.role).lastIndexOf("assistant"); if (i >= 0) copy[i] = { ...copy[i], content: text || copy[i].content, streaming: false, status: ev.payload?.status }; return copy; }); if (selected.temporary && selected.sessionKey) api.deleteSession(selected.sessionKey).catch(()=>{}); refreshSessions(); }),
        gw.on("error", ev => { setGenerating(false); setActivity(""); setMessages(ms => [...ms, { id: nowId(), role: "assistant", content: "", error: ev.payload?.message || "Unknown error", timestamp: Date.now()/1000 }]); }),
        gw.on("tool.start", ev => { setActivity(`🛠️ Running ${ev.payload?.name || "tool"}…`); setTools(ts => [{ ...(ev.payload||{}), status: "running", type: "tool" }, ...ts].slice(0, 30)); }),
        gw.on("tool.progress", ev => { setActivity(`🛠️ ${ev.payload?.preview || ev.payload?.name || "Tool running"}`); }),
        gw.on("tool.complete", ev => { setTools(ts => ts.map(t => t.tool_id === ev.payload?.tool_id ? { ...t, ...(ev.payload||{}), status: "complete" } : t)); }),
        gw.on("tool.generating", ev => setActivity(`🛠️ Preparing ${ev.payload?.name || "tool"}…`)),
        gw.on("status.update", ev => setActivity(ev.payload?.text || ev.payload?.kind || "Working…")),
        gw.on("background.complete", ev => { setTasks(ts => ts.map(t => t.id === ev.payload?.task_id ? { ...t, done: true } : t)); setMessages(ms => [...ms, { id: nowId(), role: "assistant", content: `Background task ${ev.payload?.task_id} completed:\n\n${ev.payload?.text || ""}`, timestamp: Date.now()/1000 }]); }),
        gw.on("clarify.request", ev => setMessages(ms => [...ms, { id: nowId(), role: "system", content: `Hermes needs clarification: ${ev.payload?.question || ""}`, timestamp: Date.now()/1000 }]))
      ];
      return () => offs.forEach(off => off());
    }, [selected.sessionKey, selected.temporary, refreshSessions]);
    useEffect(() => { if (settings.autoScroll !== false) listEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [messages, tools, activity]);

    useEffect(() => {
      const q = query.trim();
      const timer = setTimeout(async () => {
        if (q.length < 2) { refreshSessions(); return; }
        try {
          const [search, listed] = await Promise.all([api.searchSessions(q), api.getSessions(80,0)]);
          const byId = new Map((listed.sessions || []).map(x => [x.id, x]));
          setSessions((search.results || []).map(r => ({ ...(byId.get(r.session_id) || { id: r.session_id, title: r.session_id, started_at: r.session_started }), id: r.session_id, snippet: r.snippet })));
        } catch (e) { setError(e.message); }
      }, 250);
      return () => clearTimeout(timer);
    }, [query]);

    useEffect(() => { const onKey = e => { const mod = e.ctrlKey || e.metaKey; if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); document.querySelector(".hcd-search")?.focus(); } if (mod && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); } if (mod && e.key === "/") { e.preventDefault(); document.querySelector(".hcd-composer textarea")?.focus(); } if (e.key === "Escape" && generating) stop(); if (mod && e.shiftKey && e.key.toLowerCase() === "b") { e.preventDefault(); setMobileSide(s => !s); } }; window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey); }, [generating]);

    const ensureSession = async () => { if (selected.sessionId) { activeSidRef.current = selected.sessionId; return selected.sessionId; } const res = await gwRef.current.request("session.create", { cols: 100 }); activeSidRef.current = res.session_id; setSelected(s => ({ ...s, sessionId: res.session_id })); setMessages([]); return res.session_id; };
    const applySelectors = async (sid) => {
      const mode = cap.modes.find(m => m.id === selected.mode); const strategy = mode?.strategy || {};
      try { if (strategy.fast !== undefined) await gwRef.current.request("config.set", { session_id: sid, key: "fast", value: strategy.fast ? "fast" : "normal" }, 30000); } catch {}
      try { if (strategy.yolo) await gwRef.current.request("config.set", { session_id: sid, key: "yolo", value: "on" }, 30000); } catch {}
      if (selected.model && selected.model !== "auto") { const m = cap.models.find(x => x.id === selected.model); if (m) { try { await gwRef.current.request("config.set", { session_id: sid, key: "model", value: `${m.provider}:${m.model || m.name}` }, 60000); } catch (e) { setError(e.message); } } }
      if (!selected.autoTools && selected.tools.length) { try { await gwRef.current.request("tools.configure", { session_id: sid, action: "enable", names: selected.tools }, 60000); } catch {} }
      return mode;
    };
    const composePrompt = (text) => {
      const mode = cap.modes.find(m => m.id === selected.mode); const prefixes = [];
      if (mode?.strategy?.prompt) prefixes.push(`Mode instruction (${mode.label}): ${mode.strategy.prompt}`);
      if (selected.agent && selected.agent !== "auto") prefixes.push(`Agent preference: use the ${selected.agent} specialist or Hermes multi-agent equivalent when helpful.`);
      if (selected.temporary) prefixes.push("Privacy instruction: this is a temporary chat. Do not intentionally save facts from this turn to long-term memory or the user profile.");
      if (selected.memoryEnabled === false) prefixes.push("Memory instruction: avoid using or updating long-term memory unless explicitly requested.");
      const refs = attachments.filter(a => a.status === "ready").map(a => a.prompt_reference || (a.path ? `@${a.path}` : "")).filter(Boolean);
      return `${prefixes.length ? prefixes.join("\n") + "\n\n" : ""}${refs.length ? `Attachments:\n${refs.join("\n")}\n\n` : ""}${text || "Please analyse the attached file(s)."}`;
    };
    const send = async (text) => { setError(""); const sid = await ensureSession(); await applySelectors(sid); const readyAttachments = attachments.filter(a => a.status === "ready"); setMessages(ms => [...ms, { id: nowId(), role: "user", content: text || "Please analyse the attached file(s).", attachments: readyAttachments, timestamp: Date.now()/1000 }]); const prompt = composePrompt(text); setAttachments([]); await gwRef.current.request("prompt.submit", { session_id: sid, text: prompt }); setTimeout(async () => { try { const title = await gwRef.current.request("session.title", { session_id: sid }); if (title.session_key) setSelected(s => ({ ...s, sessionKey: title.session_key })); } catch {} }, 2000); };
    const background = async (text) => { const sid = await ensureSession(); const res = await gwRef.current.request("prompt.background", { session_id: sid, text: composePrompt(text) }); setTasks(ts => [{ id: res.task_id, text: text || "Background task", done: false }, ...ts]); setMessages(ms => [...ms, { id: nowId(), role: "system", content: `Task created successfully: ${res.task_id}`, timestamp: Date.now()/1000 }]); };
    // The dashboard keeps every created session resident (a full AIAgent +
    // LLM clients) until session.close; nothing evicts them on tab close. On
    // a small instance a handful of abandoned conversations can exhaust RAM,
    // so release the server-side session whenever we leave it. The persisted
    // transcript stays in the session DB and re-opens via session.resume.
    const releaseSession = (sid) => { if (!sid) return; if (activeSidRef.current === sid) activeSidRef.current = null; try { gwRef.current && gwRef.current.request("session.close", { session_id: sid }, 10000).catch(() => {}); } catch {} };
    const openSession = async (id) => { setError(""); try { const res = await gwRef.current.request("session.resume", { session_id: id, cols: 100 }, 120000); activeSidRef.current = res.session_id; setSelected(s => { if (s.sessionId && s.sessionId !== res.session_id) releaseSession(s.sessionId); return { ...s, sessionId: res.session_id, sessionKey: res.resumed || id }; }); const msgs = (res.messages || []).map((m, i) => ({ id: `${id}-${i}`, role: m.role, content: m.content, timestamp: m.timestamp })); setMessages(msgs); setTools([]); } catch (e) { setError(e.message); } };
    const newChat = () => { setSelected(s => { releaseSession(s.sessionId); return { ...s, sessionId: "", sessionKey: "" }; }); setMessages([]); setTools([]); setAttachments([]); setShare(""); };
    const stop = () => { if (selected.sessionId) gwRef.current.request("session.interrupt", { session_id: selected.sessionId }, 10000).catch(()=>{}); setGenerating(false); setActivity("Stopped"); };
    const onMeta = async (sid, patch) => { setMeta(m => ({ ...m, [sid]: { ...(m[sid] || {}), ...patch } })); await fetchJSON(`${BASE}/metadata/${encodeURIComponent(sid)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }).catch(e => setError(e.message)); };
    const msgAction = async (action, msg, idx) => {
      if (action === "copy") return navigator.clipboard.writeText(msg.content || "");
      if (action === "delete") return setMessages(ms => ms.filter((_, i) => i !== idx));
      if (action === "continue") return send("Please continue from where you left off.");
      if (action === "regenerate") { const prev = [...messages].slice(0, idx).reverse().find(m => m.role === "user"); if (prev) return send(prev.content || "Please regenerate your previous response."); }
      if (action === "edit") { const next = prompt("Edit message", msg.content || ""); if (next != null) { setMessages(ms => ms.map((m,i) => i===idx ? { ...m, content: next } : m)); return send(next); } }
      if (action === "branch") { if (selected.sessionKey) { try { const r = await fetchJSON(`${BASE}/branch`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: selected.sessionKey, message_index: idx, title: "Branch" }) }); await refreshSessions(); await openSession(r.session_id); } catch (e) { setError(e.message); } } else if (selected.sessionId) { try { const r = await gwRef.current.request("session.branch", { session_id: selected.sessionId, name: "Branch" }); setSelected(s => ({ ...s, sessionId: r.session_id, sessionKey: "" })); setMessages(messages.slice(0, idx + 1)); } catch (e) { setError(e.message); } } }
    };
    const exportChat = async (fmt) => {
      if (!selected.sessionKey) return setError("Export is available after the conversation is saved.");
      try {
        const headers = new Headers(); if (window.__HERMES_SESSION_TOKEN__) headers.set("X-Hermes-Session-Token", window.__HERMES_SESSION_TOKEN__);
        const res = await fetch(`${BASE}/export/${encodeURIComponent(selected.sessionKey)}?format=${encodeURIComponent(fmt)}`, { headers });
        if (!res.ok) throw new Error(await res.text());
        const blob = await res.blob(); const url = URL.createObjectURL(blob); const a = document.createElement("a");
        a.href = url; a.download = `hermes-chat-${selected.sessionKey}.${fmt === "json" ? "json" : fmt === "txt" ? "txt" : "md"}`; a.click(); URL.revokeObjectURL(url);
      } catch (e) { setError(e.message); }
    };
    const shareChat = async () => { if (!selected.sessionKey) return setError("Share is available after the conversation is saved."); try { const r = await fetchJSON(`${BASE}/share/${encodeURIComponent(selected.sessionKey)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); const url = `${location.origin}${r.url}`; setShare(url); await navigator.clipboard.writeText(url).catch(()=>{}); } catch (e) { setError(e.message); } };
    const setQuick = (promptText) => { newChat(); setDraftSeed(promptText); setTimeout(() => document.querySelector(".hcd-composer textarea")?.focus(), 50); };

    return h("div", { className: `hcd-root ${settings.density === "compact" ? "compact" : ""}` },
      h(Sidebar, { sessions, meta, current: selected.sessionKey, query, setQuery, onNew: newChat, onOpen: openSession, onMeta, loading: loadingSessions }),
      h("main", { className: "hcd-main" },
        h("header", { className: "hcd-topbar" }, h("button", { className: "hcd-mobile", onClick: () => setMobileSide(!mobileSide) }, "☰"), h("div", null, h("strong", null, selected.sessionKey ? (sessions.find(s => s.id === selected.sessionKey)?.title || "Hermes Chat") : "Hermes Chat"), h("small", null, activity || (ready ? "Connected to real Hermes gateway" : "Connecting…"))), h("div", null, selected.temporary && h("span", { className: "hcd-temp-pill" }, "Temporary"), h("button", { onClick: () => exportChat("markdown") }, "Export"), h("button", { onClick: shareChat }, "Share"), h("button", { onClick: () => setShowSettings(true) }, "Settings"))),
        error && h("div", { className: "hcd-errorbar" }, "Something went wrong. ", h("button", { onClick: () => setError("") }, "Dismiss"), h("details", null, h("summary", null, "View details"), h("pre", null, error))),
        share && h("div", { className: "hcd-sharebar" }, "Secure read-only share link copied: ", h("code", null, share)),
        h("div", { className: "hcd-scroll" }, messages.length === 0 ? h(Welcome, { onQuick: setQuick }) : h(React.Fragment, null, messages.map((m, i) => h(Message, { key: m.id || i, msg: m, index: i, active: m.streaming, onAction: msgAction })), tools.length ? h("div", { className: "hcd-tools-inline" }, tools.slice(0, 6).map((t, i) => h(ToolCard, { key: t.tool_id || i, tool: t }))) : null, activity && generating && h("div", { className: "hcd-activity" }, activity), h("div", { ref: listEnd }))),
        h(Composer, { disabled: !ready || generating, modes: cap.modes, models: cap.models, agents: cap.agents, toolsets: cap.toolsets, selected, setSelected, attachments, setAttachments, onSend: send, onStop: stop, generating, onBackground: background, draftSeed })
      ),
      h(ContextPanel, { capabilities: cap, selected, tools, memories: [], sources: [], tasks, collapsed: contextCollapsed, setCollapsed: setContextCollapsed }),
      showSettings && h(SettingsModal, { settings, setSettings, onClose: () => setShowSettings(false) }),
      h("div", { className: `hcd-mobile-drawer ${mobileSide ? "open" : ""}` }, h(Sidebar, { sessions, meta, current: selected.sessionKey, query, setQuery, onNew: newChat, onOpen: (id) => { setMobileSide(false); openSession(id); }, onMeta, loading: loadingSessions }))
    );
  }

  registry.register("hermes-chat-dashboard", ChatDashboard);
})();
