(() => {
  const sdk = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!sdk || !registry) return;
  const React = sdk.React;
  const { useCallback, useEffect, useMemo, useRef, useState } = sdk.hooks;
  const { fetchJSON } = sdk;
  const h = React.createElement;
  const BASE_PATH = (window.__HERMES_BASE_PATH__ || "").replace(/\/+$/, "");
  const BASE = "/api/plugins/hermes-chat-dashboard";
  const LS_KEY = "hcd-ui-v2";

  // ── Gateway (JSON-RPC over the dashboard's /api/ws) ─────────────────
  class HermesGateway {
    constructor() { this.ws = null; this.id = 0; this.pending = new Map(); this.listeners = new Map(); this.state = "idle"; this.onState = () => {}; }
    on(type, cb) { const set = this.listeners.get(type) || new Set(); set.add(cb); this.listeners.set(type, set); return () => set.delete(cb); }
    emit(ev) { (this.listeners.get(ev.type) || []).forEach(cb => { try { cb(ev); } catch (e) { console.error("[hcd] listener", e); } }); (this.listeners.get("*") || []).forEach(cb => cb(ev)); }
    setState(s) { this.state = s; try { this.onState(s); } catch {} }
    async connect() {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) return;
      const token = window.__HERMES_SESSION_TOKEN__ || "";
      if (!token) throw new Error("Session token unavailable. Open Hermes through the dashboard server.");
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}${BASE_PATH}/api/ws?token=${encodeURIComponent(token)}`);
      this.ws = ws; this.setState("connecting");
      ws.addEventListener("message", ev => {
        let msg; try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.id && this.pending.has(msg.id)) {
          const p = this.pending.get(msg.id); this.pending.delete(msg.id); clearTimeout(p.timer);
          msg.error ? p.reject(new Error(msg.error.message || "request failed")) : p.resolve(msg.result);
          return;
        }
        if (msg.method === "event" && msg.params && msg.params.type) this.emit(msg.params);
      });
      ws.addEventListener("close", () => { this.setState("closed"); for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(new Error("Gateway connection closed")); } this.pending.clear(); });
      await new Promise((resolve, reject) => {
        ws.addEventListener("open", () => { this.setState("open"); resolve(); }, { once: true });
        ws.addEventListener("error", () => reject(new Error("Gateway WebSocket failed — is HERMES_DASHBOARD_TUI=1 set?")), { once: true });
      });
    }
    close() { try { this.ws && this.ws.close(); } catch {} this.ws = null; }
    get open() { return !!this.ws && this.ws.readyState === WebSocket.OPEN; }
    request(method, params = {}, timeout = 120000) {
      if (!this.open) return Promise.reject(new Error("gateway is not connected"));
      const id = `chat-${++this.id}`;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`Request timed out: ${method}`)); }, timeout);
        this.pending.set(id, { resolve, reject, timer });
        this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      });
    }
  }

  // ── Helpers ───────────────────────────────────────────────────────────
  const nowId = () => `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const safe = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
  function markdownToHtml(md) {
    let text = safe(md || "");
    const blocks = [];
    text = text.replace(/```([\w.+-]*)[ \t]*\n([\s\S]*?)```/g, (_, lang, code) => { const i = blocks.push(`<div class="hcd-code"><div class="hcd-code-head"><span>${safe(lang || "code")}</span><button type="button" data-copy-code="1">Copy</button></div><pre><code>${code.replace(/\n$/, "")}</code></pre></div>`) - 1; return `\n§CODE${i}§\n`; });
    text = text.replace(/`([^`\n]+)`/g, (_, c) => { const i = blocks.push(`<code>${c}</code>`) - 1; return `§CODE${i}§`; });
    text = text.replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h1>$1</h1>");
    text = text.replace(/^\s*(?:---|\*\*\*)\s*$/gm, "<hr/>");
    text = text.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
    text = text.replace(/^&gt; ?(.*)$/gm, "<blockquote>$1</blockquote>").replace(/<\/blockquote>\n<blockquote>/g, "<br/>");
    text = text.replace(/((?:^\s*\d+\. .*(?:\n|$))+)/gm, m => `<ol>${m.trim().split(/\n/).map(l => `<li>${l.replace(/^\s*\d+\. /, "")}</li>`).join("")}</ol>\n`);
    text = text.replace(/((?:^\s*[-*] .*(?:\n|$))+)/gm, m => `<ul>${m.trim().split(/\n/).map(l => `<li>${l.replace(/^\s*[-*] /, "")}</li>`).join("")}</ul>\n`);
    text = text.replace(/((?:^\|.*\|\s*(?:\n|$))+)/gm, m => {
      const rows = m.trim().split(/\n/).map(r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim()));
      if (rows.length < 2 || !/^:?-{2,}:?$/.test(rows[1][0] || "")) return m;
      const head = `<tr>${rows[0].map(c => `<th>${c}</th>`).join("")}</tr>`;
      const body = rows.slice(2).map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("");
      return `<div class="hcd-table"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>\n`;
    });
    text = text.split(/\n{2,}/).map(p => (/^\s*<(h\d|ul|ol|div|pre|hr|blockquote|table)/.test(p) || /^\s*§CODE\d+§\s*$/.test(p) ? p : `<p>${p.trim().replace(/\n/g, "<br/>")}</p>`)).join("\n");
    text = text.replace(/§CODE(\d+)§/g, (_, i) => blocks[Number(i)] || "");
    return text;
  }
  function fmtTime(ts) { if (!ts) return ""; try { const d = new Date(ts * 1000); return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); } catch { return ""; } }
  function fmtDateTime(ts) { if (!ts) return ""; try { return new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); } catch { return ""; } }
  function timeAgo(ts) {
    if (!ts) return "";
    const d = Date.now() / 1000 - ts;
    if (d < 60) return "just now"; if (d < 3600) return `${Math.floor(d / 60)}m ago`; if (d < 86400) return `${Math.floor(d / 3600)}h ago`; if (d < 172800) return "yesterday"; return `${Math.floor(d / 86400)}d ago`;
  }
  function dayGroup(ts) {
    if (!ts) return "Older";
    const now = new Date(); const d = new Date(ts * 1000);
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const diff = (start - new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()) / 86400000;
    if (diff <= 0) return "Today"; if (diff === 1) return "Yesterday"; if (diff < 7) return "Previous 7 days"; if (diff < 30) return "Previous 30 days"; return "Older";
  }
  function fmtDuration(s) { if (s == null) return ""; if (s < 10) return `${s.toFixed(1)}s`; if (s < 60) return `${Math.round(s)}s`; const m = Math.floor(s / 60); const r = Math.round(s % 60); return r ? `${m}m ${r}s` : `${m}m`; }
  function toolIcon(name = "", type) {
    if (type === "agent") return "🤖";
    const n = name.toLowerCase();
    if (n.includes("web") || n.includes("search") || n.includes("fetch")) return "🔎";
    if (n.includes("browser")) return "🌐";
    if (n.includes("terminal") || n.includes("shell") || n.includes("bash") || n.includes("exec")) return "💻";
    if (n.includes("read") || n.includes("write") || n.includes("edit") || n.includes("patch") || n.includes("file")) return "📄";
    if (n.includes("memory") || n.includes("recall")) return "🧠";
    if (n.includes("todo") || n.includes("plan")) return "📋";
    if (n.includes("mcp_render")) return "☁️";
    if (n.includes("delegate") || n.includes("agent") || n.includes("spawn")) return "🤖";
    if (n.includes("image") || n.includes("vision")) return "🖼️";
    return "🛠️";
  }
  function sessionTitle(s) { return (s && (s.title || s.preview)) || "Untitled chat"; }
  function loadLocal() { try { return JSON.parse(localStorage.getItem(LS_KEY) || "{}") || {}; } catch { return {}; } }
  function saveLocal(patch) { try { localStorage.setItem(LS_KEY, JSON.stringify({ ...loadLocal(), ...patch })); } catch {} }
  function copyText(text) { return navigator.clipboard && navigator.clipboard.writeText ? navigator.clipboard.writeText(text || "") : Promise.resolve(); }
  function useAutoScroll(dep, enabled) {
    const ref = useRef(null); const [stuck, setStuck] = useState(true);
    const onScroll = useCallback(() => { const el = ref.current; if (!el) return; setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < 80); }, []);
    useEffect(() => { const el = ref.current; if (!el || !enabled || !stuck) return; el.scrollTop = el.scrollHeight; }, [dep, enabled, stuck]);
    const scrollToBottom = useCallback(() => { const el = ref.current; if (el) { el.scrollTo({ top: el.scrollHeight, behavior: "smooth" }); setStuck(true); } }, []);
    return { ref, onScroll, stuck, scrollToBottom };
  }

  // ── Small components ─────────────────────────────────────────────────
  function Icon({ name }) {
    const paths = {
      plus: "M12 5v14M5 12h14", search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-3.5-3.5", menu: "M4 6h16M4 12h16M4 18h16",
      panel: "M4 5h16v14H4zM15 5v14", send: "M22 2L11 13M22 2l-7 20-4-9-9-4 20-7", stop: "M6 6h12v12H6z", down: "M6 9l6 6 6-6",
      x: "M6 6l12 12M18 6L6 18", gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z",
      clip: "M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48", more: "M12 6h.01M12 12h.01M12 18h.01",
      pin: "M12 17v5M5 8l7-5 7 5-2 6H7z", trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6", edit: "M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z",
      check: "M20 6L9 17l-5-5", refresh: "M21 12a9 9 0 1 1-3-6.7M21 3v6h-6", copy: "M9 9h11v11H9zM5 15H4V4h11v1", branch: "M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9",
      archive: "M3 4h18v4H3zM5 8v12h14V8M10 12h4", download: "M12 3v12M6 11l6 6 6-6M4 21h16", chevron: "M9 18l6-6-6-6", spark: "M12 2l2.4 6.6L21 11l-6.6 2.4L12 20l-2.4-6.6L3 11l6.6-2.4z", clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2", warn: "M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
    };
    return h("svg", { className: "hcd-ico", viewBox: "0 0 24 24", width: 16, height: 16, fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true }, h("path", { d: paths[name] || "" }));
  }
  function IconBtn({ icon, label, onClick, className = "", active, disabled, danger }) {
    return h("button", { type: "button", className: `hcd-iconbtn ${className} ${active ? "on" : ""} ${danger ? "danger" : ""}`, title: label, "aria-label": label, onClick, disabled }, h(Icon, { name: icon }));
  }
  function Menu({ items, onClose, align = "right" }) {
    const ref = useRef(null);
    useEffect(() => { const onDoc = e => { if (ref.current && !ref.current.contains(e.target)) onClose(); }; const onKey = e => { if (e.key === "Escape") onClose(); }; document.addEventListener("mousedown", onDoc); document.addEventListener("keydown", onKey); return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); }; }, [onClose]);
    return h("div", { ref, className: `hcd-menu ${align}`, role: "menu" }, items.filter(Boolean).map((it, i) => it === "-" ? h("hr", { key: i }) : h("button", { key: it.label, type: "button", role: "menuitem", className: it.danger ? "danger" : "", onClick: () => { onClose(); it.onClick(); } }, it.icon && h(Icon, { name: it.icon }), h("span", null, it.label), it.hint && h("kbd", null, it.hint))));
  }

  // ── Tool step (one tool call, chronological within its turn) ─────────
  function ToolStep({ tool, defaultOpen }) {
    const [open, setOpen] = useState(!!defaultOpen);
    const status = tool.status || "running";
    const running = status === "running";
    const name = tool.title || tool.name || "Tool";
    const detail = tool.context || tool.preview || "";
    return h("div", { className: `hcd-step ${status}` },
      h("button", { type: "button", className: "hcd-step-head", onClick: () => setOpen(o => !o), "aria-expanded": open },
        h("span", { className: "hcd-step-state" }, running ? h("span", { className: "hcd-spinner" }) : status === "error" ? h(Icon, { name: "warn" }) : h(Icon, { name: "check" })),
        h("span", { className: "hcd-step-icon" }, toolIcon(name, tool.type)),
        h("span", { className: "hcd-step-name" }, name),
        detail && h("span", { className: "hcd-step-detail", title: detail }, detail),
        h("span", { className: "hcd-step-right" },
          tool.summary && !open && h("span", { className: "hcd-step-summary" }, tool.summary),
          tool.duration_s != null && h("span", { className: "hcd-step-time" }, fmtDuration(tool.duration_s)),
          h(Icon, { name: "chevron" }))),
      open && h("div", { className: "hcd-step-body" },
        tool.summary && h("div", { className: "hcd-step-row" }, h("b", null, "Result"), h("span", null, tool.summary)),
        tool.reasoning && h("details", { className: "hcd-step-reasoning" }, h("summary", null, "Reasoning"), h("pre", null, tool.reasoning)),
        tool.inline_diff && h("pre", { className: "hcd-diff" }, tool.inline_diff),
        tool.result && h("pre", { className: "hcd-step-out" }, tool.result),
        tool.todos && h("ul", { className: "hcd-todos" }, tool.todos.map((t, i) => h("li", { key: i, className: (t.status || "").toString() }, h("span", null, t.status === "completed" || t.status === "done" ? "☑" : t.status === "in_progress" ? "◐" : "☐"), " ", t.content || t.title || String(t)))),
        !tool.summary && !tool.result && !tool.inline_diff && !tool.todos && h("p", { className: "hcd-muted" }, running ? "Running…" : "No output captured."),
        h("div", { className: "hcd-step-foot" }, tool.started_at && h("span", null, "Started ", fmtTime(tool.started_at)), tool.tool_id && h("span", { className: "hcd-muted" }, "id ", String(tool.tool_id).slice(0, 12)))));
  }
  function ToolSteps({ tools, live, expand }) {
    if (!tools || !tools.length) return null;
    const [collapsed, setCollapsed] = useState(false);
    const running = tools.filter(t => (t.status || "running") === "running").length;
    const total = tools.reduce((a, t) => a + (t.duration_s || 0), 0);
    return h("div", { className: `hcd-steps ${collapsed ? "collapsed" : ""}` },
      h("button", { type: "button", className: "hcd-steps-head", onClick: () => setCollapsed(c => !c) },
        h(Icon, { name: "chevron" }),
        h("span", null, `${tools.length} step${tools.length === 1 ? "" : "s"}`),
        running ? h("span", { className: "hcd-steps-live" }, h("span", { className: "hcd-spinner" }), ` ${running} running`) : total ? h("span", { className: "hcd-muted" }, fmtDuration(total)) : null),
      !collapsed && h("div", { className: "hcd-steps-list" }, tools.map((t, i) => h(ToolStep, { key: t.tool_id || i, tool: t, defaultOpen: !!expand }))));
  }

  // ── Message ───────────────────────────────────────────────────────────
  function Message({ msg, index, onAction, showTime, isLast, readOnly, expandTools }) {
    const bodyRef = useRef(null); const [menu, setMenu] = useState(false); const [copied, setCopied] = useState(false);
    useEffect(() => {
      const root = bodyRef.current; if (!root) return;
      root.querySelectorAll("button[data-copy-code]").forEach(btn => { btn.onclick = () => { copyText(btn.closest(".hcd-code").querySelector("code").innerText || ""); btn.textContent = "Copied"; setTimeout(() => { btn.textContent = "Copy"; }, 1200); }; });
    }, [msg.content]);
    const isHermes = msg.role === "assistant";
    const isSystem = msg.role === "system";
    const doCopy = () => { copyText(msg.content || ""); setCopied(true); setTimeout(() => setCopied(false), 1200); };
    if (isSystem) return h("div", { className: `hcd-system ${msg.kind || ""}`, id: msg.id }, h("span", null, msg.content));
    return h("article", { className: `hcd-message hcd-${msg.role || "assistant"} ${msg.streaming ? "streaming" : ""}`, id: msg.id || undefined },
      h("div", { className: "hcd-avatar" }, isHermes ? h(Icon, { name: "spark" }) : "You"),
      h("div", { className: "hcd-body" },
        h("div", { className: "hcd-meta" }, h("strong", null, isHermes ? "Hermes" : "You"), showTime && msg.timestamp ? h("time", { title: fmtDateTime(msg.timestamp) }, fmtTime(msg.timestamp)) : null, msg.model && h("span", { className: "hcd-chip" }, msg.model)),
        msg.attachments && msg.attachments.length ? h("div", { className: "hcd-attachments" }, msg.attachments.map(a => h("span", { key: a.id || a.path }, a.is_image ? "🖼️ " : "📎 ", a.name))) : null,
        isHermes && h(ToolSteps, { tools: msg.tools, live: msg.streaming, expand: expandTools }),
        isHermes && msg.reasoning && h("details", { className: "hcd-reasoning" }, h("summary", null, "Reasoning"), h("pre", null, msg.reasoning)),
        msg.error ? h("div", { className: "hcd-error" }, h(Icon, { name: "warn" }), h("div", null, h("b", null, "Something went wrong"), h("pre", null, msg.error))) :
          (msg.content || msg.streaming) ? h("div", { ref: bodyRef, className: "hcd-markdown", dangerouslySetInnerHTML: { __html: markdownToHtml(msg.content || "") + (msg.streaming && !msg.content ? '<p class="hcd-thinking"><span></span><span></span><span></span></p>' : msg.streaming ? '<span class="hcd-caret">▍</span>' : "") } }) :
            msg.incomplete ? h("p", { className: "hcd-muted" }, "Turn ended without a visible reply.") : null,
        msg.status && msg.status !== "complete" && !msg.streaming && h("div", { className: "hcd-status-note" }, msg.status === "interrupted" ? "⏹ Stopped by user" : msg.status === "error" ? "⚠ Turn failed" : msg.status),
        msg.warning && h("div", { className: "hcd-status-note" }, msg.warning),
        !msg.streaming && h("div", { className: "hcd-actions" },
          h(IconBtn, { icon: copied ? "check" : "copy", label: "Copy", onClick: doCopy }),
          !readOnly && msg.role === "user" && h(IconBtn, { icon: "edit", label: "Edit & resend", onClick: () => onAction("edit", msg, index) }),
          !readOnly && isHermes && isLast && h(IconBtn, { icon: "refresh", label: "Regenerate", onClick: () => onAction("regenerate", msg, index) }),
          h("span", { className: "hcd-rel" }, h(IconBtn, { icon: "more", label: "More", onClick: () => setMenu(m => !m) }),
            menu && h(Menu, { onClose: () => setMenu(false), items: [
              !readOnly && isHermes && { icon: "chevron", label: "Continue from here", onClick: () => onAction("continue", msg, index) },
              { icon: "branch", label: "Branch conversation here", onClick: () => onAction("branch", msg, index) },
              "-", { icon: "trash", label: "Hide from view", danger: true, onClick: () => onAction("delete", msg, index) },
            ] })))));
  }

  // ── Approval / clarification prompts ─────────────────────────────────
  function PromptCard({ p, onAnswer }) {
    const [text, setText] = useState("");
    if (p.kind === "approval") {
      return h("div", { className: "hcd-prompt approval" },
        h("div", { className: "hcd-prompt-head" }, h(Icon, { name: "warn" }), h("b", null, "Approval required"), p.description && h("span", null, p.description)),
        p.command && h("pre", null, p.command),
        h("div", { className: "hcd-prompt-actions" },
          h("button", { type: "button", className: "primary", onClick: () => onAnswer(p, "once") }, "Allow once"),
          h("button", { type: "button", onClick: () => onAnswer(p, "session") }, "Allow for session"),
          h("button", { type: "button", onClick: () => onAnswer(p, "always") }, "Always allow"),
          h("button", { type: "button", className: "danger", onClick: () => onAnswer(p, "deny") }, "Deny")));
    }
    const label = p.kind === "sudo" ? "Hermes needs a sudo password" : p.kind === "secret" ? "Hermes needs a secret value" : "Hermes has a question";
    return h("div", { className: "hcd-prompt" },
      h("div", { className: "hcd-prompt-head" }, h(Icon, { name: "spark" }), h("b", null, label)),
      p.question && h("p", null, p.question),
      p.choices && p.choices.length ? h("div", { className: "hcd-prompt-actions" }, p.choices.map(c => h("button", { key: c, type: "button", onClick: () => onAnswer(p, c) }, c))) : null,
      h("form", { className: "hcd-prompt-form", onSubmit: e => { e.preventDefault(); onAnswer(p, text); } },
        h("input", { type: p.kind === "sudo" || p.kind === "secret" ? "password" : "text", value: text, onChange: e => setText(e.target.value), placeholder: p.choices && p.choices.length ? "Or type a custom answer…" : "Type your answer…", autoFocus: true }),
        h("button", { type: "submit", className: "primary" }, "Send")));
  }

  // ── Composer ──────────────────────────────────────────────────────────
  function Composer({ disabled, connected, generating, selected, setSelected, cap, attachments, setAttachments, onSend, onStop, onSteer, draft, setDraft, readOnly, onExitReadOnly }) {
    const [drag, setDrag] = useState(false); const [showTools, setShowTools] = useState(false);
    const ta = useRef(null);
    useEffect(() => { if (ta.current) { ta.current.style.height = "auto"; ta.current.style.height = Math.min(260, ta.current.scrollHeight) + "px"; } }, [draft]);
    const upload = async (files) => {
      for (const file of Array.from(files || [])) {
        const local = { id: nowId(), name: file.name, size: file.size, status: "uploading" };
        setAttachments(a => [...a, local]);
        const fd = new FormData(); fd.append("file", file); fd.append("conversation_id", selected.sessionKey || selected.sessionId || "");
        try { const res = await fetchJSON(`${BASE}/attachments`, { method: "POST", body: fd }); setAttachments(a => a.map(x => x.id === local.id ? { ...res, id: local.id, status: "ready" } : x)); }
        catch (e) { setAttachments(a => a.map(x => x.id === local.id ? { ...local, status: "error", error: e.message } : x)); }
      }
    };
    const canSend = (draft.trim().length > 0 || attachments.some(a => a.status === "ready")) && !attachments.some(a => a.status === "uploading");
    const submit = () => { if (!canSend) return; if (generating) { onSteer(draft.trim()); setDraft(""); return; } onSend(draft.trim()); setDraft(""); };
    const mode = cap.modes.find(m => m.id === selected.mode);
    const model = selected.model === "auto" ? null : cap.models.find(m => m.id === selected.model);
    return h("div", { className: `hcd-composer-wrap` },
      readOnly && h("div", { className: "hcd-readonly-bar" }, h(Icon, { name: "clock" }), h("span", null, "Viewing saved transcript. Sending a message will resume this conversation."), h("button", { type: "button", onClick: onExitReadOnly }, "Resume now")),
      h("div", { className: `hcd-composer ${drag ? "drag" : ""} ${disabled ? "disabled" : ""}`, onDragOver: e => { e.preventDefault(); setDrag(true); }, onDragLeave: () => setDrag(false), onDrop: e => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files); } },
        attachments.length ? h("div", { className: "hcd-attach-row" }, attachments.map(a => h("span", { key: a.id, className: `hcd-attach ${a.status}`, title: a.error || a.path || "" }, a.is_image ? "🖼️" : "📎", " ", a.name, a.status === "uploading" ? " …" : a.status === "error" ? " ✗" : "", h("button", { type: "button", "aria-label": "Remove", onClick: () => setAttachments(xs => xs.filter(x => x.id !== a.id)) }, "×")))) : null,
        h("textarea", { ref: ta, value: draft, rows: 1, placeholder: !connected ? "Connecting to Hermes…" : generating ? "Type to steer the running turn (delivered with the next tool result)…" : selected.temporary ? "Temporary chat — Hermes is asked not to memorise this" : "Message Hermes…  (Shift+Enter for newline)",
          disabled: !connected, onPaste: e => { const fs = [...(e.clipboardData?.files || [])]; if (fs.length) { e.preventDefault(); upload(fs); } }, onChange: e => setDraft(e.target.value),
          onKeyDown: e => { if (e.key === "Enter" && !e.shiftKey && selected.enterToSend !== false && !e.isComposing) { e.preventDefault(); submit(); } } }),
        h("div", { className: "hcd-controls" },
          h("div", { className: "hcd-controls-left" },
            h("label", { className: "hcd-upload", title: "Attach files" }, h(Icon, { name: "clip" }), h("input", { type: "file", multiple: true, onChange: e => { upload(e.target.files); e.target.value = ""; }, hidden: true })),
            h("div", { className: "hcd-pill-select" }, h("span", null, mode ? `${mode.emoji || ""} ${mode.label}` : "Mode"), h("select", { value: selected.mode, "aria-label": "Mode", onChange: e => setSelected(s => ({ ...s, mode: e.target.value })) }, cap.modes.map(m => h("option", { value: m.id, key: m.id }, `${m.emoji || ""} ${m.label} — ${m.description || ""}`)))),
            h("div", { className: "hcd-pill-select" }, h("span", { title: model ? model.id : "Use the gateway default model" }, model ? model.name || model.model : "Default model"), h("select", { value: selected.model, "aria-label": "Model", onChange: e => setSelected(s => ({ ...s, model: e.target.value })) }, h("option", { value: "auto" }, `Default${cap.current_model ? ` (${cap.current_model})` : ""}`), cap.models.slice(0, 400).map(m => h("option", { value: m.id, key: m.id }, `${m.provider || ""} / ${m.name || m.model}`)))),
            h("span", { className: "hcd-rel" },
              h("button", { type: "button", className: `hcd-pill ${!selected.autoTools ? "on" : ""}`, onClick: () => setShowTools(v => !v) }, "🛠 ", selected.autoTools ? "Tools: auto" : `Tools: ${selected.tools.length}`),
              showTools && h("div", { className: "hcd-popover" },
                h("label", { className: "hcd-check" }, h("input", { type: "checkbox", checked: !!selected.autoTools, onChange: e => setSelected(s => ({ ...s, autoTools: e.target.checked })) }), " Let Hermes pick tools automatically"),
                h("div", { className: `hcd-toollist ${selected.autoTools ? "dim" : ""}` }, cap.toolsets.map(t => { const id = t.id || t.name; return h("label", { key: id, className: "hcd-check" }, h("input", { type: "checkbox", disabled: selected.autoTools, checked: (selected.tools || []).includes(id), onChange: e => setSelected(s => ({ ...s, tools: e.target.checked ? [...(s.tools || []), id] : (s.tools || []).filter(x => x !== id) })) }), " ", t.label || t.name, t.tool_count ? h("small", null, ` ${t.tool_count}`) : null, t.description && h("small", { className: "hcd-block" }, t.description)); })),
                h("button", { type: "button", className: "hcd-popover-close", onClick: () => setShowTools(false) }, "Done"))),
            h("button", { type: "button", className: `hcd-pill ${selected.temporary ? "on" : ""}`, title: "Ask Hermes not to store memories from this chat", onClick: () => setSelected(s => ({ ...s, temporary: !s.temporary })) }, "⏱ Temporary")),
          h("div", { className: "hcd-controls-right" },
            generating ? h("button", { type: "button", className: "hcd-send danger", onClick: onStop, title: "Stop (Esc)" }, h(Icon, { name: "stop" }), " Stop") : null,
            h("button", { type: "button", className: "hcd-send", onClick: submit, disabled: !canSend || !connected, title: generating ? "Steer the running turn" : "Send (Enter)" }, h(Icon, { name: "send" }), generating ? " Steer" : " Send")))),
      h("div", { className: "hcd-composer-foot" }, "Hermes can run tools and change things. Review approvals carefully."));
  }

  // ── Sidebar ───────────────────────────────────────────────────────────
  function SessionRow({ s, active, onOpen, onMenu }) {
    return h("div", { className: `hcd-conv ${active ? "active" : ""} ${s.archived ? "archived" : ""}`, role: "button", tabIndex: 0, onClick: () => onOpen(s.id), onKeyDown: e => { if (e.key === "Enter") onOpen(s.id); } },
      h("div", { className: "hcd-conv-main" },
        h("span", { className: "hcd-conv-title" }, s.pinned && h("i", { className: "hcd-pinmark", title: "Pinned" }, "📌"), sessionTitle(s), s.is_active && h("i", { className: "hcd-live", title: "Active in the last 5 minutes" })),
        h("small", null, s.snippet ? h("span", { dangerouslySetInnerHTML: { __html: String(s.snippet).replace(/<(?!\/?(b|mark)>)[^>]*>/g, "") } }) : (s.title && s.preview ? s.preview : ""))),
      h("div", { className: "hcd-conv-side" }, h("small", { title: fmtDateTime(s.last_active || s.started_at) }, timeAgo(s.last_active || s.started_at)), h("small", { className: "hcd-conv-count" }, `${s.message_count || 0} msg${s.source && s.source !== "web" && s.source !== "tui" ? ` · ${s.source}` : ""}`)),
      h("button", { type: "button", className: "hcd-conv-menu", "aria-label": "Conversation actions", onClick: e => { e.stopPropagation(); onMenu(s, e.currentTarget); } }, h(Icon, { name: "more" })));
  }
  function Sidebar({ sessions, current, query, setQuery, onNew, onOpen, onAction, loading, showArchived, setShowArchived, onRefresh, collapsed, setCollapsed, total }) {
    const [menuFor, setMenuFor] = useState(null);
    const visible = useMemo(() => sessions.filter(s => showArchived ? true : !s.archived), [sessions, showArchived]);
    const groups = useMemo(() => {
      const pinned = visible.filter(s => s.pinned); const rest = visible.filter(s => !s.pinned);
      const out = []; if (pinned.length) out.push(["Pinned", pinned]);
      const buckets = new Map(); rest.forEach(s => { const g = query ? "Results" : dayGroup(s.last_active || s.started_at); if (!buckets.has(g)) buckets.set(g, []); buckets.get(g).push(s); });
      for (const [k, v] of buckets) out.push([k, v]);
      return out;
    }, [visible, query]);
    return h("aside", { className: `hcd-left ${collapsed ? "collapsed" : ""}` },
      h("div", { className: "hcd-side-head" },
        h("button", { type: "button", className: "hcd-new", onClick: onNew, title: "New chat (Ctrl+Shift+O)" }, h(Icon, { name: "plus" }), h("span", null, "New chat")),
        h(IconBtn, { icon: "panel", label: "Hide sidebar (Ctrl+Shift+B)", onClick: () => setCollapsed(true) })),
      h("div", { className: "hcd-search-wrap" }, h(Icon, { name: "search" }), h("input", { className: "hcd-search", value: query, onChange: e => setQuery(e.target.value), placeholder: "Search chats (Ctrl+K)" }), query && h("button", { type: "button", className: "hcd-clear", "aria-label": "Clear search", onClick: () => setQuery("") }, h(Icon, { name: "x" }))),
      h("div", { className: "hcd-side-list" },
        loading && !sessions.length ? h("div", { className: "hcd-empty-small" }, "Loading conversations…") :
          !visible.length ? h("div", { className: "hcd-empty-small" }, query ? "No matching chats." : showArchived ? "No conversations yet." : "No conversations yet. Start a new chat.") :
            groups.map(([label, arr]) => h("section", { key: label }, h("h4", null, label), arr.map(s => h(SessionRow, { key: s.id, s, active: current === s.id, onOpen, onMenu: (sess, el) => setMenuFor({ s: sess, rect: el.getBoundingClientRect() }) }))))),
      h("div", { className: "hcd-side-foot" },
        h("label", { className: "hcd-check small" }, h("input", { type: "checkbox", checked: showArchived, onChange: e => setShowArchived(e.target.checked) }), " Show archived"),
        h("span", { className: "hcd-muted" }, total != null ? `${total} total` : ""),
        h(IconBtn, { icon: "refresh", label: "Refresh list", onClick: onRefresh, className: loading ? "spin" : "" })),
      menuFor && h("div", { className: "hcd-menu-anchor", style: { top: Math.min(menuFor.rect.bottom + 4, window.innerHeight - 260), left: Math.min(menuFor.rect.left, window.innerWidth - 240) } },
        h(Menu, { align: "left", onClose: () => setMenuFor(null), items: [
          { icon: "edit", label: "Rename", onClick: () => onAction("rename", menuFor.s) },
          { icon: "pin", label: menuFor.s.pinned ? "Unpin" : "Pin", onClick: () => onAction("pin", menuFor.s) },
          { icon: "archive", label: menuFor.s.archived ? "Unarchive" : "Archive", onClick: () => onAction("archive", menuFor.s) },
          { icon: "download", label: "Export as Markdown", onClick: () => onAction("export", menuFor.s) },
          { icon: "copy", label: "Copy session id", onClick: () => copyText(menuFor.s.id) },
          "-", { icon: "trash", label: "Delete", danger: true, onClick: () => onAction("delete", menuFor.s) },
        ] })));
  }

  // ── Welcome ───────────────────────────────────────────────────────────
  function Welcome({ onQuick, connected }) {
    const qs = [["💻 Write code", "Help me implement or debug this. Inspect the context first, then propose and apply a safe plan:\n"], ["🔎 Research", "Research this thoroughly, compare sources, and summarise with citations:\n"], ["☁️ Check my Render services", "List my Render services and summarise their current status, recent deploys and any errors in the logs."], ["📄 Analyse a file", "I will attach a file. Read it, identify the important structure and findings, then suggest next actions."], ["🧠 Explain", "Explain this clearly with an example and a short summary:\n"], ["✍️ Write", "Help me draft and refine this text:\n"]];
    return h("div", { className: "hcd-welcome" }, h("div", { className: "hcd-welcome-logo" }, h(Icon, { name: "spark" })), h("h1", null, "How can Hermes help?"), h("p", null, connected ? "Same agent, tools, memory and sessions as your Telegram / CLI Hermes — in the browser." : "Connecting to the Hermes gateway…"), h("div", { className: "hcd-quick" }, qs.map(([label, prompt]) => h("button", { key: label, type: "button", onClick: () => onQuick(prompt) }, label))));
  }

  // ── Details panel ─────────────────────────────────────────────────────
  function DetailsPanel({ session, info, selected, tasks, onClose, messages }) {
    const toolCount = messages.reduce((a, m) => a + ((m.tools || []).length), 0);
    const usage = info?.usage || {};
    const row = (k, v) => v == null || v === "" ? null : h("div", { className: "hcd-kv", key: k }, h("span", null, k), h("b", null, String(v)));
    return h("aside", { className: "hcd-context" },
      h("div", { className: "hcd-context-head" }, h("strong", null, "Details"), h(IconBtn, { icon: "x", label: "Close", onClick: onClose })),
      h("div", { className: "hcd-context-body" },
        h("h4", null, "Conversation"),
        row("Session", session?.id || selected.sessionKey || "not saved yet"),
        row("Source", session?.source), row("Started", session?.started_at ? fmtDateTime(session.started_at) : null),
        row("Messages", messages.filter(m => m.role !== "system").length), row("Tool calls", toolCount),
        session?.parent_session_id && row("Branched from", session.parent_session_id),
        h("h4", null, "Model"),
        row("Active", info?.model || session?.model || (selected.model === "auto" ? "gateway default" : selected.model)),
        info?.reasoning_effort && row("Reasoning", info.reasoning_effort),
        usage.context_max ? h("div", { className: "hcd-ctxbar" }, h("div", { className: "hcd-kv" }, h("span", null, "Context"), h("b", null, `${usage.context_percent || 0}%`)), h("progress", { value: usage.context_used || 0, max: usage.context_max })) : null,
        usage.total ? row("Tokens this session", `${(usage.input || 0).toLocaleString()} in / ${(usage.output || 0).toLocaleString()} out`) : null,
        usage.cost_display && row("Est. cost", usage.cost_display),
        h("h4", null, "Settings for next message"),
        row("Mode", selected.mode), row("Tools", selected.autoTools ? "auto" : (selected.tools.join(", ") || "none")), row("Temporary", selected.temporary ? "yes" : "no"),
        tasks.length ? h(React.Fragment, null, h("h4", null, "Background tasks"), tasks.map(t => h("div", { className: "hcd-task", key: t.id }, h("b", null, t.done ? "✓ " : "⏳ ", t.id), h("p", null, t.text)))) : null));
  }

  // ── Settings ──────────────────────────────────────────────────────────
  function SettingsModal({ settings, setSettings, cap, onClose }) {
    const update = (patch) => { const next = { ...settings, ...patch }; setSettings(next); fetchJSON(`${BASE}/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }).catch(() => {}); };
    const check = (key, label, hint) => h("label", { className: "hcd-check row" }, h("input", { type: "checkbox", checked: settings[key] !== false, onChange: e => update({ [key]: e.target.checked }) }), h("span", null, label, hint && h("small", { className: "hcd-block" }, hint)));
    return h("div", { className: "hcd-modal", onMouseDown: e => { if (e.target === e.currentTarget) onClose(); } }, h("div", { className: "hcd-settings", role: "dialog", "aria-label": "Chat settings" }, h("div", { className: "hcd-settings-head" }, h("h2", null, "Chat settings"), h(IconBtn, { icon: "x", label: "Close", onClick: onClose })),
      h("h3", null, "Appearance"),
      h("label", { className: "hcd-field" }, "Density", h("select", { value: settings.density || "comfortable", onChange: e => update({ density: e.target.value }) }, h("option", { value: "comfortable" }, "Comfortable"), h("option", { value: "compact" }, "Compact"))),
      h("label", { className: "hcd-field" }, "Message width", h("select", { value: settings.messageWidth || "wide", onChange: e => update({ messageWidth: e.target.value }) }, h("option", { value: "narrow" }, "Narrow"), h("option", { value: "wide" }, "Wide"), h("option", { value: "full" }, "Full"))),
      check("showTimestamps", "Show timestamps"),
      check("expandTools", "Expand tool steps by default", "Otherwise each turn's steps are collapsed to a one-line summary."),
      h("h3", null, "Behaviour"),
      check("enterToSend", "Enter sends", "Shift+Enter always inserts a newline."),
      check("autoScroll", "Follow new messages"),
      check("autoTools", "Let Hermes pick tools automatically"),
      h("label", { className: "hcd-field" }, "Default mode", h("select", { value: settings.defaultMode || "fast", onChange: e => update({ defaultMode: e.target.value }) }, cap.modes.map(m => h("option", { key: m.id, value: m.id }, `${m.emoji || ""} ${m.label}`)))),
      h("h3", null, "Privacy"),
      check("memoryEnabled", "Allow memory use", "When off, Hermes is asked not to read or write long-term memory in this chat."),
      check("temporaryDefault", "Start new chats as temporary")));
  }

  // ── Main app ──────────────────────────────────────────────────────────
  function ChatDashboard() {
    const local = useMemo(loadLocal, []);
    const gwRef = useRef(null); const activeSidRef = useRef(null); const streamRef = useRef({ sid: null });
    const [gwState, setGwState] = useState("idle"); const [error, setError] = useState(""); const [toast, setToast] = useState("");
    const [cap, setCap] = useState({ modes: [], models: [], agents: [], toolsets: [], features: {} });
    const [settings, setSettings] = useState({ enterToSend: true, autoTools: true, memoryEnabled: true, defaultMode: "fast", autoScroll: true, showTimestamps: true, density: "comfortable", messageWidth: "wide" });
    const [selected, setSelected] = useState({ sessionId: "", sessionKey: "", mode: "fast", model: "auto", tools: [], autoTools: true, temporary: false, memoryEnabled: true, enterToSend: true });
    const [sessions, setSessions] = useState([]); const [total, setTotal] = useState(null); const [query, setQuery] = useState(""); const [loadingSessions, setLoadingSessions] = useState(false); const [showArchived, setShowArchived] = useState(false);
    const [messages, setMessages] = useState([]); const [generating, setGenerating] = useState(false); const [activity, setActivity] = useState(""); const [readOnly, setReadOnly] = useState(false); const [viewing, setViewing] = useState(null); const [loadingChat, setLoadingChat] = useState(false);
    const [prompts, setPrompts] = useState([]); const [info, setInfo] = useState(null);
    const [attachments, setAttachments] = useState([]); const [tasks, setTasks] = useState([]); const [showDetails, setShowDetails] = useState(!!local.details); const [showSettings, setShowSettings] = useState(false);
    const [sideCollapsed, setSideCollapsed] = useState(!!local.sideCollapsed); const [mobileSide, setMobileSide] = useState(false);
    const [draft, setDraft] = useState(""); const [topMenu, setTopMenu] = useState(false); const [renaming, setRenaming] = useState(null);
    const selRef = useRef(selected); selRef.current = selected;
    const messagesRef = useRef(messages); messagesRef.current = messages;
    const scroller = useAutoScroll(messages, settings.autoScroll !== false);
    useEffect(() => saveLocal({ sideCollapsed, details: showDetails }), [sideCollapsed, showDetails]);
    const flash = (t) => { setToast(t); setTimeout(() => setToast(""), 2200); };
    const fail = (e) => { const m = e && e.message ? e.message : String(e); setError(m); console.error("[hcd]", e); };

    const refreshSessions = useCallback(async (opts = {}) => {
      if (!opts.silent) setLoadingSessions(true);
      try { const r = await fetchJSON(`${BASE}/sessions?limit=150`); setSessions(r.sessions || []); setTotal(r.total); }
      catch (e) { try { const s = await sdk.api.getSessions(150, 0); setSessions((s.sessions || []).map(x => ({ ...x, last_active: x.last_active || x.started_at }))); setTotal(s.total); } catch (e2) { fail(e2); } }
      finally { setLoadingSessions(false); }
    }, []);

    // Boot: capabilities + settings + sessions, then the gateway socket.
    useEffect(() => {
      let cancelled = false;
      (async () => {
        try { const [c, st] = await Promise.all([fetchJSON(`${BASE}/capabilities`), fetchJSON(`${BASE}/settings`)]); if (cancelled) return; setCap(c); setSettings(s => ({ ...s, ...st })); setSelected(s => ({ ...s, mode: st.defaultMode || "fast", autoTools: st.autoTools !== false, temporary: !!st.temporaryDefault, memoryEnabled: st.memoryEnabled !== false, enterToSend: st.enterToSend !== false, tools: st.defaultTools || [] })); } catch (e) { fail(e); }
        refreshSessions();
        const gw = new HermesGateway(); gwRef.current = gw; gw.onState = setGwState;
        try { await gw.connect(); } catch (e) { fail(e); }
      })();
      return () => { cancelled = true; const sid = activeSidRef.current; if (sid && gwRef.current) { try { gwRef.current.request("session.close", { session_id: sid }, 5000).catch(() => {}); } catch {} } gwRef.current && gwRef.current.close(); };
    }, [refreshSessions]);

    // Reconnect when the socket drops.
    useEffect(() => {
      if (gwState !== "closed") return;
      let attempt = 0; let timer;
      const tryConnect = async () => { attempt++; try { await gwRef.current.connect(); activeSidRef.current = null; setSelected(s => ({ ...s, sessionId: "" })); if (selRef.current.sessionKey) setReadOnly(true); setGenerating(false); flash("Reconnected to Hermes"); } catch { timer = setTimeout(tryConnect, Math.min(15000, 1000 * 2 ** attempt)); } };
      timer = setTimeout(tryConnect, 800);
      return () => clearTimeout(timer);
    }, [gwState]);

    // Gateway events → UI state. Tools attach to the streaming assistant turn in arrival order.
    useEffect(() => {
      const gw = gwRef.current; if (!gw) return;
      const patchLast = (fn) => setMessages(ms => { const i = ms.map(m => m.role).lastIndexOf("assistant"); if (i < 0 || !ms[i].streaming) return ms; const copy = [...ms]; copy[i] = fn(copy[i]); return copy; });
      const ownEvent = (ev) => !ev.session_id || !activeSidRef.current || ev.session_id === activeSidRef.current;
      const offs = [
        gw.on("message.start", ev => { if (!ownEvent(ev)) return; setGenerating(true); setActivity("Thinking…"); setMessages(ms => { if (ms.length && ms[ms.length - 1].role === "assistant" && ms[ms.length - 1].streaming) return ms; return [...ms, { id: nowId(), role: "assistant", content: "", tools: [], streaming: true, timestamp: Date.now() / 1000 }]; }); }),
        gw.on("message.delta", ev => { if (!ownEvent(ev)) return; const txt = ev.payload?.text || ""; if (!txt) return; setActivity("Responding…"); patchLast(m => ({ ...m, content: (m.content || "") + txt })); }),
        gw.on("thinking.delta", ev => { if (ownEvent(ev)) setActivity("Thinking…"); }),
        gw.on("reasoning.delta", ev => { if (!ownEvent(ev)) return; const txt = ev.payload?.text || ""; if (txt) patchLast(m => ({ ...m, reasoning: (m.reasoning || "") + txt })); }),
        gw.on("message.complete", ev => {
          if (!ownEvent(ev)) return;
          setGenerating(false); setActivity(""); const p = ev.payload || {};
          setMessages(ms => { const i = ms.map(m => m.role).lastIndexOf("assistant"); if (i < 0) return ms; const copy = [...ms]; const cur = copy[i]; copy[i] = { ...cur, content: p.text || cur.content || "", streaming: false, status: p.status, warning: p.warning, reasoning: p.reasoning || cur.reasoning, tools: (cur.tools || []).map(t => t.status === "running" ? { ...t, status: p.status === "interrupted" ? "error" : "complete" } : t), incomplete: !(p.text || cur.content) && !!(cur.tools || []).length }; return copy; });
          if (p.usage) setInfo(i => ({ ...(i || {}), usage: p.usage }));
          setPrompts([]);
          const s = selRef.current;
          if (s.temporary && s.sessionKey) fetchJSON(`${BASE}/sessions/${encodeURIComponent(s.sessionKey)}`, { method: "DELETE" }).catch(() => {});
          setTimeout(() => refreshSessions({ silent: true }), 600);
        }),
        gw.on("error", ev => { if (!ownEvent(ev)) return; setGenerating(false); setActivity(""); const msg = ev.payload?.message || "Unknown error"; setMessages(ms => { const i = ms.map(m => m.role).lastIndexOf("assistant"); if (i >= 0 && ms[i].streaming) { const copy = [...ms]; copy[i] = { ...copy[i], streaming: false, error: msg, tools: (copy[i].tools || []).map(t => t.status === "running" ? { ...t, status: "error" } : t) }; return copy; } return [...ms, { id: nowId(), role: "assistant", content: "", error: msg, timestamp: Date.now() / 1000 }]; }); }),
        gw.on("tool.generating", ev => { if (ownEvent(ev)) setActivity(`Preparing ${ev.payload?.name || "tool"}…`); }),
        gw.on("tool.start", ev => { if (!ownEvent(ev)) return; const p = ev.payload || {}; setActivity(`Running ${p.name || "tool"}…`); patchLast(m => ({ ...m, tools: [...(m.tools || []), { ...p, status: "running", type: "tool", started_at: Date.now() / 1000 }] })); }),
        gw.on("tool.progress", ev => { if (!ownEvent(ev)) return; const p = ev.payload || {}; if (p.preview) setActivity(`${p.name || "Tool"}: ${p.preview}`); patchLast(m => { const tools = [...(m.tools || [])]; for (let i = tools.length - 1; i >= 0; i--) { if (tools[i].name === p.name && tools[i].status === "running") { tools[i] = { ...tools[i], preview: p.preview || tools[i].preview }; break; } } return { ...m, tools }; }); }),
        gw.on("tool.complete", ev => { if (!ownEvent(ev)) return; const p = ev.payload || {}; setActivity("Thinking…"); patchLast(m => { const tools = [...(m.tools || [])]; let idx = tools.findIndex(t => t.tool_id && t.tool_id === p.tool_id); if (idx < 0) idx = tools.findIndex(t => t.name === p.name && t.status === "running"); if (idx < 0) tools.push({ ...p, status: "complete", type: "tool" }); else tools[idx] = { ...tools[idx], ...p, status: "complete" }; return { ...m, tools }; }); }),
        gw.on("status.update", ev => { if (ownEvent(ev)) setActivity(ev.payload?.text || ev.payload?.kind || "Working…"); }),
        gw.on("session.info", ev => { if (ownEvent(ev)) setInfo(ev.payload || null); }),
        gw.on("session.usage", ev => { if (ownEvent(ev) && ev.payload?.usage) setInfo(i => ({ ...(i || {}), usage: ev.payload.usage })); }),
        gw.on("session.title", ev => { if (ownEvent(ev)) refreshSessions({ silent: true }); }),
        gw.on("background.complete", ev => { setTasks(ts => ts.map(t => t.id === ev.payload?.task_id ? { ...t, done: true } : t)); setMessages(ms => [...ms, { id: nowId(), role: "assistant", content: `**Background task ${ev.payload?.task_id || ""} finished**\n\n${ev.payload?.text || ""}`, timestamp: Date.now() / 1000 }]); }),
        gw.on("approval.request", ev => { if (!ownEvent(ev)) return; const p = ev.payload || {}; setActivity("Waiting for your approval…"); setPrompts(ps => [...ps, { id: nowId(), kind: "approval", command: p.command, description: p.description, pattern_key: p.pattern_key }]); }),
        gw.on("clarify.request", ev => { if (!ownEvent(ev)) return; const p = ev.payload || {}; setActivity("Waiting for your answer…"); setPrompts(ps => [...ps, { id: p.request_id || nowId(), kind: "clarify", request_id: p.request_id, question: p.question, choices: p.choices || [] }]); }),
        gw.on("sudo.request", ev => { if (!ownEvent(ev)) return; const p = ev.payload || {}; setPrompts(ps => [...ps, { id: p.request_id || nowId(), kind: "sudo", request_id: p.request_id, question: p.prompt || p.question || "" }]); }),
        gw.on("secret.request", ev => { if (!ownEvent(ev)) return; const p = ev.payload || {}; setPrompts(ps => [...ps, { id: p.request_id || nowId(), kind: "secret", request_id: p.request_id, question: p.prompt || p.question || p.name || "" }]); }),
      ];
      return () => offs.forEach(off => off());
    }, [gwState, refreshSessions]);

    // Search (FTS) with debounce; falls back to the plain list when cleared.
    useEffect(() => {
      const q = query.trim();
      const timer = setTimeout(async () => {
        if (q.length < 2) { if (!q) refreshSessions({ silent: true }); return; }
        try {
          const [search, listed] = await Promise.all([sdk.api.searchSessions(q), fetchJSON(`${BASE}/sessions?limit=300`)]);
          const byId = new Map((listed.sessions || []).map(x => [x.id, x]));
          const local = (listed.sessions || []).filter(s => `${s.title || ""} ${s.preview || ""} ${s.id}`.toLowerCase().includes(q.toLowerCase()));
          const merged = new Map();
          local.forEach(s => merged.set(s.id, s));
          (search.results || []).forEach(r => { const base = byId.get(r.session_id) || { id: r.session_id, title: "", preview: r.snippet || "", started_at: r.session_started, last_active: r.session_started, source: r.source, message_count: 0 }; merged.set(r.session_id, { ...base, snippet: r.snippet }); });
          setSessions([...merged.values()]);
        } catch (e) { fail(e); }
      }, 250);
      return () => clearTimeout(timer);
    }, [query, refreshSessions]);

    // Keyboard shortcuts.
    useEffect(() => {
      const onKey = e => {
        const mod = e.ctrlKey || e.metaKey; const k = e.key.toLowerCase();
        if (mod && k === "k") { e.preventDefault(); setSideCollapsed(false); setTimeout(() => document.querySelector(".hcd-search")?.focus(), 0); }
        else if (mod && e.shiftKey && k === "o") { e.preventDefault(); newChat(); }
        else if (mod && e.shiftKey && k === "b") { e.preventDefault(); if (window.innerWidth < 900) setMobileSide(s => !s); else setSideCollapsed(s => !s); }
        else if (mod && e.shiftKey && k === "i") { e.preventDefault(); setShowDetails(s => !s); }
        else if (e.key === "Escape" && generating) stop();
      };
      window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey);
    }, [generating]);

    // ── Session lifecycle ──
    const releaseSession = (sid) => { if (!sid) return; if (activeSidRef.current === sid) activeSidRef.current = null; try { gwRef.current && gwRef.current.request("session.close", { session_id: sid }, 10000).catch(() => {}); } catch {} };
    const ensureSession = async () => {
      const s = selRef.current; const gw = gwRef.current;
      if (!gw || !gw.open) throw new Error("Not connected to the Hermes gateway yet.");
      if (s.sessionId) { activeSidRef.current = s.sessionId; return s.sessionId; }
      if (s.sessionKey) {
        // Viewing a saved transcript → resume it lazily on first send.
        setActivity("Resuming conversation…");
        const res = await gw.request("session.resume", { session_id: s.sessionKey, cols: 100 }, 180000);
        activeSidRef.current = res.session_id; setSelected(x => ({ ...x, sessionId: res.session_id, sessionKey: res.resumed || s.sessionKey })); setReadOnly(false); if (res.info) setInfo(res.info);
        return res.session_id;
      }
      const res = await gw.request("session.create", { cols: 100 });
      activeSidRef.current = res.session_id; setSelected(x => ({ ...x, sessionId: res.session_id })); if (res.info) setInfo(res.info);
      return res.session_id;
    };
    const applySelectors = async (sid) => {
      const s = selRef.current; const gw = gwRef.current; const mode = cap.modes.find(m => m.id === s.mode); const strategy = mode?.strategy || {};
      try { if (strategy.fast !== undefined) await gw.request("config.set", { session_id: sid, key: "fast", value: strategy.fast ? "fast" : "normal" }, 30000); } catch {}
      try { if (strategy.yolo) await gw.request("config.set", { session_id: sid, key: "yolo", value: "on" }, 30000); } catch {}
      if (s.model && s.model !== "auto") { const m = cap.models.find(x => x.id === s.model); if (m) { try { await gw.request("config.set", { session_id: sid, key: "model", value: `${m.provider}:${m.model || m.name}` }, 90000); } catch (e) { fail(new Error(`Model switch failed: ${e.message}`)); } } }
      if (!s.autoTools && s.tools.length) { try { await gw.request("tools.configure", { session_id: sid, action: "enable", names: s.tools }, 60000); } catch {} }
      return mode;
    };
    const composePrompt = (text, atts) => {
      const s = selRef.current; const mode = cap.modes.find(m => m.id === s.mode); const prefixes = [];
      if (mode?.strategy?.prompt) prefixes.push(`Mode instruction (${mode.label}): ${mode.strategy.prompt}`);
      if (s.temporary) prefixes.push("Privacy instruction: this is a temporary chat. Do not intentionally save facts from this turn to long-term memory or the user profile.");
      if (s.memoryEnabled === false) prefixes.push("Memory instruction: avoid using or updating long-term memory unless explicitly requested.");
      const refs = atts.map(a => a.prompt_reference || (a.path ? `@${a.path}` : "")).filter(Boolean);
      return `${prefixes.length ? prefixes.join("\n") + "\n\n" : ""}${refs.length ? `Attachments:\n${refs.join("\n")}\n\n` : ""}${text || "Please analyse the attached file(s)."}`;
    };
    const send = async (text) => {
      setError("");
      const ready = attachments.filter(a => a.status === "ready");
      const userMsg = { id: nowId(), role: "user", content: text || "Please analyse the attached file(s).", attachments: ready, timestamp: Date.now() / 1000 };
      setMessages(ms => [...ms, userMsg]); setAttachments([]); setGenerating(true); setActivity("Starting…");
      try {
        const sid = await ensureSession(); await applySelectors(sid);
        await gwRef.current.request("prompt.submit", { session_id: sid, text: composePrompt(text, ready) });
        setTimeout(async () => { try { const t = await gwRef.current.request("session.title", { session_id: sid }); if (t.session_key) setSelected(s => ({ ...s, sessionKey: t.session_key })); refreshSessions({ silent: true }); } catch {} }, 2500);
      } catch (e) { setGenerating(false); setActivity(""); fail(e); }
    };
    const steer = async (text) => { if (!text || !selRef.current.sessionId) return; try { await gwRef.current.request("session.steer", { session_id: selRef.current.sessionId, text }, 15000); setMessages(ms => [...ms, { id: nowId(), role: "system", kind: "steer", content: `Steer sent: “${text}”`, timestamp: Date.now() / 1000 }]); } catch (e) { fail(e); } };
    const stop = () => { const sid = selRef.current.sessionId; if (sid && gwRef.current) gwRef.current.request("session.interrupt", { session_id: sid }, 10000).catch(() => {}); setPrompts([]); setActivity("Stopping…"); };
    const answerPrompt = async (p, answer) => {
      setPrompts(ps => ps.filter(x => x.id !== p.id)); const sid = selRef.current.sessionId; if (!sid) return;
      try {
        if (p.kind === "approval") { await gwRef.current.request("approval.respond", { session_id: sid, choice: answer }, 15000); setMessages(ms => [...ms, { id: nowId(), role: "system", kind: answer === "deny" ? "deny" : "ok", content: `${answer === "deny" ? "Denied" : "Approved"}: ${(p.command || "").slice(0, 160)}` }]); }
        else if (p.kind === "clarify") await gwRef.current.request("clarify.respond", { session_id: sid, request_id: p.request_id, answer }, 15000);
        else if (p.kind === "sudo") await gwRef.current.request("sudo.respond", { session_id: sid, request_id: p.request_id, password: answer }, 15000);
        else if (p.kind === "secret") await gwRef.current.request("secret.respond", { session_id: sid, request_id: p.request_id, value: answer }, 15000);
        setActivity("Working…");
      } catch (e) { fail(e); }
    };

    // Open a saved session: read-only transcript instantly, resume on first send.
    const openSession = async (id) => {
      if (generating) { if (!confirm("A response is still generating. Leave this conversation?")) return; stop(); }
      setError(""); setLoadingChat(true); setMobileSide(false); setPrompts([]);
      const prev = selRef.current.sessionId; if (prev) releaseSession(prev);
      setSelected(s => ({ ...s, sessionId: "", sessionKey: id })); setReadOnly(true); setInfo(null); setViewing(null); setMessages([]);
      try {
        const r = await fetchJSON(`${BASE}/transcript/${encodeURIComponent(id)}`);
        if (selRef.current.sessionKey !== id) return;
        setViewing(r.session || null); if (r.session?.id && r.session.id !== id) setSelected(s => ({ ...s, sessionKey: r.session.id }));
        setMessages((r.messages || []).map(m => ({ ...m, id: `${id}-${m.id}` })));
        setTimeout(() => { const el = scroller.ref.current; if (el) el.scrollTop = el.scrollHeight; }, 0);
      } catch (e) {
        // Older upstream without /transcript support → fall back to a live resume.
        try { const res = await gwRef.current.request("session.resume", { session_id: id, cols: 100 }, 180000); activeSidRef.current = res.session_id; setSelected(s => ({ ...s, sessionId: res.session_id, sessionKey: res.resumed || id })); setReadOnly(false); if (res.info) setInfo(res.info); setMessages((res.messages || []).map((m, i) => ({ id: `${id}-${i}`, role: m.role === "tool" ? "system" : m.role, content: m.role === "tool" ? `🛠 ${m.name}${m.context ? ` — ${m.context}` : ""}` : m.text || m.content || "" }))); }
        catch (e2) { fail(e2); }
      } finally { setLoadingChat(false); }
    };
    const resumeNow = async () => { try { setLoadingChat(true); await ensureSession(); flash("Conversation resumed"); } catch (e) { fail(e); } finally { setLoadingChat(false); } };
    const newChat = () => { const prev = selRef.current.sessionId; if (prev) releaseSession(prev); setSelected(s => ({ ...s, sessionId: "", sessionKey: "", temporary: !!settings.temporaryDefault })); setMessages([]); setAttachments([]); setPrompts([]); setInfo(null); setViewing(null); setReadOnly(false); setGenerating(false); setActivity(""); setError(""); setMobileSide(false); setTimeout(() => document.querySelector(".hcd-composer textarea")?.focus(), 30); };
    const sessionAction = async (action, s) => {
      try {
        if (action === "rename") { setRenaming({ id: s.id, title: s.title || "" }); return; }
        if (action === "pin" || action === "archive") { const key = action === "pin" ? "pinned" : "archived"; const val = !s[key]; setSessions(ss => ss.map(x => x.id === s.id ? { ...x, [key]: val } : x)); await fetchJSON(`${BASE}/metadata/${encodeURIComponent(s.id)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [key]: val, ...(key === "pinned" ? { starred: val } : {}) }) }); return; }
        if (action === "export") return exportChat("markdown", s.id);
        if (action === "delete") {
          if (!confirm(`Delete “${sessionTitle(s)}”? This removes the transcript permanently.`)) return;
          if (selRef.current.sessionKey === s.id) { newChat(); }
          await fetchJSON(`${BASE}/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" }).catch(() => sdk.api.deleteSession(s.id));
          setSessions(ss => ss.filter(x => x.id !== s.id)); flash("Conversation deleted");
        }
      } catch (e) { fail(e); }
    };
    const commitRename = async () => { const r = renaming; setRenaming(null); if (!r) return; try { await fetchJSON(`${BASE}/sessions/${encodeURIComponent(r.id)}/title`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: r.title }) }); setSessions(ss => ss.map(x => x.id === r.id ? { ...x, title: r.title } : x)); } catch (e) { fail(e); } };
    const msgAction = async (action, msg, idx) => {
      if (action === "delete") return setMessages(ms => ms.filter((_, i) => i !== idx));
      if (action === "continue") return send("Please continue from where you left off.");
      if (action === "regenerate") { const prev = [...messagesRef.current].slice(0, idx).reverse().find(m => m.role === "user"); if (prev) { setMessages(ms => ms.filter((_, i) => i !== idx)); return send(prev.content || "Please regenerate your previous response."); } }
      if (action === "edit") { setDraft(msg.content || ""); setTimeout(() => document.querySelector(".hcd-composer textarea")?.focus(), 30); return; }
      if (action === "branch") {
        const key = selRef.current.sessionKey;
        if (!key) return fail(new Error("Branching is available once the conversation has been saved."));
        try { const r = await fetchJSON(`${BASE}/branch`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: key, message_index: idx, title: `Branch of ${sessionTitle(sessions.find(s => s.id === key)).slice(0, 40)}` }) }); await refreshSessions({ silent: true }); await openSession(r.session_id); flash("Branch created"); } catch (e) { fail(e); }
      }
    };
    const exportChat = async (fmt, id) => {
      const key = id || selRef.current.sessionKey; if (!key) return fail(new Error("Export is available after the conversation is saved."));
      try {
        const headers = new Headers(); if (window.__HERMES_SESSION_TOKEN__) headers.set("X-Hermes-Session-Token", window.__HERMES_SESSION_TOKEN__);
        const res = await fetch(`${BASE_PATH}${BASE}/export/${encodeURIComponent(key)}?format=${encodeURIComponent(fmt)}`, { headers });
        if (!res.ok) throw new Error(await res.text());
        const blob = await res.blob(); const url = URL.createObjectURL(blob); const a = document.createElement("a");
        a.href = url; a.download = `hermes-chat-${key}.${fmt === "json" ? "json" : fmt === "txt" ? "txt" : "md"}`; a.click(); URL.revokeObjectURL(url);
      } catch (e) { fail(e); }
    };
    const shareChat = async () => { const key = selRef.current.sessionKey; if (!key) return fail(new Error("Share is available after the conversation is saved.")); try { const r = await fetchJSON(`${BASE}/share/${encodeURIComponent(key)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); const url = `${location.origin}${BASE_PATH}${r.url}`; await copyText(url); flash("Read-only share link copied"); } catch (e) { fail(e); } };
    const setQuick = (promptText) => { newChat(); setDraft(promptText); };

    const current = sessions.find(s => s.id === selected.sessionKey) || null;
    const title = current ? sessionTitle(current) : viewing ? (viewing.title || "Conversation") : selected.sessionKey ? "Conversation" : "New chat";
    const connected = gwState === "open";
    const lastAssistantIdx = messages.map(m => m.role).lastIndexOf("assistant");
    const statusText = activity || (generating ? "Working…" : !connected ? (gwState === "closed" ? "Disconnected — reconnecting…" : "Connecting to Hermes…") : readOnly && selected.sessionKey ? "Saved transcript" : selected.sessionId ? (info?.model ? `Live · ${info.model}` : "Live session") : "Ready");

    return h("div", { className: `hcd-root ${settings.density === "compact" ? "compact" : ""} width-${settings.messageWidth || "wide"} ${sideCollapsed ? "side-collapsed" : ""} ${showDetails ? "with-details" : ""}` },
      !sideCollapsed && h(Sidebar, { sessions, current: selected.sessionKey, query, setQuery, onNew: newChat, onOpen: openSession, onAction: sessionAction, loading: loadingSessions, showArchived, setShowArchived, onRefresh: () => refreshSessions(), collapsed: false, setCollapsed: setSideCollapsed, total }),
      h("main", { className: "hcd-main" },
        h("header", { className: "hcd-topbar" },
          h("div", { className: "hcd-topbar-left" },
            h("button", { type: "button", className: "hcd-iconbtn hcd-mobile", "aria-label": "Conversations", onClick: () => setMobileSide(true) }, h(Icon, { name: "menu" })),
            sideCollapsed && h(IconBtn, { icon: "panel", label: "Show sidebar (Ctrl+Shift+B)", className: "hcd-desktop", onClick: () => setSideCollapsed(false) }),
            sideCollapsed && h(IconBtn, { icon: "plus", label: "New chat", className: "hcd-desktop", onClick: newChat }),
            h("div", { className: "hcd-title" },
              renaming && renaming.id === selected.sessionKey ? h("input", { className: "hcd-rename", autoFocus: true, value: renaming.title, onChange: e => setRenaming(r => ({ ...r, title: e.target.value })), onBlur: commitRename, onKeyDown: e => { if (e.key === "Enter") commitRename(); if (e.key === "Escape") setRenaming(null); } })
                : h("strong", { onDoubleClick: () => selected.sessionKey && setRenaming({ id: selected.sessionKey, title: current?.title || "" }), title: selected.sessionKey ? "Double-click to rename" : "" }, title),
              h("small", { className: `hcd-status ${generating ? "busy" : ""} ${!connected ? "off" : ""}` }, h("i", { className: "hcd-dot" }), statusText))),
          h("div", { className: "hcd-topbar-right" },
            selected.temporary && h("span", { className: "hcd-temp-pill" }, "Temporary"),
            h(IconBtn, { icon: "panel", label: "Details (Ctrl+Shift+I)", className: "hcd-flip", active: showDetails, onClick: () => setShowDetails(v => !v) }),
            h("span", { className: "hcd-rel" }, h(IconBtn, { icon: "more", label: "Conversation menu", onClick: () => setTopMenu(m => !m) }),
              topMenu && h(Menu, { onClose: () => setTopMenu(false), items: [
                selected.sessionKey && { icon: "edit", label: "Rename", onClick: () => setRenaming({ id: selected.sessionKey, title: current?.title || "" }) },
                selected.sessionKey && { icon: "pin", label: current?.pinned ? "Unpin" : "Pin", onClick: () => sessionAction("pin", current || { id: selected.sessionKey }) },
                selected.sessionKey && "-",
                { icon: "download", label: "Export Markdown", onClick: () => exportChat("markdown") },
                { icon: "download", label: "Export JSON", onClick: () => exportChat("json") },
                { icon: "copy", label: "Copy share link", onClick: shareChat },
                "-",
                { icon: "gear", label: "Settings", onClick: () => setShowSettings(true) },
                selected.sessionKey && "-",
                selected.sessionKey && { icon: "trash", label: "Delete conversation", danger: true, onClick: () => sessionAction("delete", current || { id: selected.sessionKey, title }) },
              ] })))),
        error && h("div", { className: "hcd-errorbar", role: "alert" }, h(Icon, { name: "warn" }), h("span", null, error), h("button", { type: "button", onClick: () => setError("") }, "Dismiss")),
        renaming && renaming.id !== selected.sessionKey && h("div", { className: "hcd-modal", onMouseDown: e => { if (e.target === e.currentTarget) setRenaming(null); } }, h("form", { className: "hcd-settings small", onSubmit: e => { e.preventDefault(); commitRename(); } }, h("h2", null, "Rename conversation"), h("input", { className: "hcd-rename", autoFocus: true, value: renaming.title, onChange: e => setRenaming(r => ({ ...r, title: e.target.value })), placeholder: "Title" }), h("div", { className: "hcd-prompt-actions" }, h("button", { type: "submit", className: "primary" }, "Save"), h("button", { type: "button", onClick: () => setRenaming(null) }, "Cancel")))),
        h("div", { className: "hcd-scroll", ref: scroller.ref, onScroll: scroller.onScroll },
          h("div", { className: "hcd-thread" },
            loadingChat && !messages.length ? h("div", { className: "hcd-loading" }, h("span", { className: "hcd-spinner" }), " Loading conversation…") :
              messages.length === 0 ? h(Welcome, { onQuick: setQuick, connected }) :
                h(React.Fragment, null,
                  viewing && h("div", { className: "hcd-thread-head" }, h("span", null, `Started ${fmtDateTime(viewing.started_at)}`), viewing.source && h("span", null, `via ${viewing.source}`), viewing.model && h("span", null, viewing.model)),
                  messages.map((m, i) => h(Message, { key: m.id || i, msg: m, index: i, onAction: msgAction, showTime: settings.showTimestamps !== false, isLast: i === lastAssistantIdx, readOnly: false, expandTools: settings.expandTools === true })),
                  prompts.map(p => h(PromptCard, { key: p.id, p, onAnswer: answerPrompt })),
                  generating && activity && h("div", { className: "hcd-activity" }, h("span", { className: "hcd-spinner" }), " ", activity)))),
        !scroller.stuck && messages.length > 0 && h("button", { type: "button", className: "hcd-jump", onClick: scroller.scrollToBottom, "aria-label": "Jump to latest" }, h(Icon, { name: "down" })),
        h(Composer, { connected, generating, selected, setSelected, cap, attachments, setAttachments, onSend: send, onStop: stop, onSteer: steer, draft, setDraft, readOnly: readOnly && !!selected.sessionKey && !selected.sessionId, onExitReadOnly: resumeNow })),
      showDetails && h(DetailsPanel, { session: viewing || current, info, selected, tasks, messages, onClose: () => setShowDetails(false) }),
      showSettings && h(SettingsModal, { settings, setSettings, cap, onClose: () => setShowSettings(false) }),
      toast && h("div", { className: "hcd-toast" }, toast),
      h("div", { className: `hcd-mobile-drawer ${mobileSide ? "open" : ""}`, onClick: e => { if (e.target === e.currentTarget) setMobileSide(false); } },
        mobileSide && h(Sidebar, { sessions, current: selected.sessionKey, query, setQuery, onNew: newChat, onOpen: openSession, onAction: sessionAction, loading: loadingSessions, showArchived, setShowArchived, onRefresh: () => refreshSessions(), collapsed: false, setCollapsed: () => setMobileSide(false), total })));
  }

  registry.register("hermes-chat-dashboard", ChatDashboard);

  // The dashboard only wires a /chat route -- built-in or plugin override --
  // when the server injected __HERMES_DASHBOARD_EMBEDDED_CHAT__=true (that is,
  // the dashboard process saw HERMES_DASHBOARD_TUI=1). With it false, this
  // plugin's manifest is still listed and its bundle still loads, but /chat
  // has no route: the sidebar shows no Chat entry, the Plugins page's "Open
  // tab" link points at /chat, and the catch-all silently redirects to
  // /sessions. Nothing in the UI says why. Register a banner that explains
  // the state and how to fix it, so the failure is diagnosable from the
  // browser instead of looking like a broken link.
  const embeddedChatEnabled = window.__HERMES_DASHBOARD_EMBEDDED_CHAT__ === true || window.__HERMES_DASHBOARD_TUI__ === true;
  if (!embeddedChatEnabled && typeof registry.registerSlot === "function") {
    function ChatDisabledBanner() {
      const [dismissed, setDismissed] = useState(() => { try { return sessionStorage.getItem("hcd-disabled-banner") === "1"; } catch { return false; } });
      const [visible, setVisible] = useState(() => location.pathname.replace(/\/$/, "") === "/chat" || location.pathname.replace(/\/$/, "") === "/sessions");
      useEffect(() => {
        const update = () => { const p = location.pathname.replace(/\/$/, ""); setVisible(p === "/chat" || p === "/sessions" || p === "/plugins"); };
        const origPush = history.pushState, origReplace = history.replaceState;
        history.pushState = function () { const r = origPush.apply(this, arguments); update(); return r; };
        history.replaceState = function () { const r = origReplace.apply(this, arguments); update(); return r; };
        window.addEventListener("popstate", update);
        update();
        return () => { history.pushState = origPush; history.replaceState = origReplace; window.removeEventListener("popstate", update); };
      }, []);
      if (dismissed || !visible) return null;
      return h("div", { className: "hcd-disabled-banner", role: "alert" },
        h("strong", null, "Chat tab is installed but switched off on this server."),
        h("span", null,
          " The dashboard only mounts /chat when it starts with ",
          h("code", null, "HERMES_DASHBOARD_TUI=1"),
          ", so the Chat link redirects here. Set ", h("code", null, "HERMES_DASHBOARD_TUI=1"),
          " in Render's Environment tab (it may be overridden by a stale copy in ", h("code", null, "/opt/data/.env"),
          " on images older than this fix -- redeploy on the current image and it is cleaned up automatically), then restart the service."),
        h("button", { type: "button", onClick: () => { setDismissed(true); try { sessionStorage.setItem("hcd-disabled-banner", "1"); } catch {} } }, "Dismiss")
      );
    }
    registry.registerSlot("hermes-chat-dashboard", "header-banner", ChatDisabledBanner);
  }
})();
