/* hermes-chat-dashboard — web chat workspace for the dashboard.
 *
 * A ChatGPT-style chat UI that drives the real Hermes ``tui_gateway`` over a
 * pure-Python WebSocket (``/api/ws``) and talks to this plugin's backend
 * routes (``/api/plugins/hermes-chat-dashboard/...``) for durable session
 * metadata, transcripts, uploads, shares, folders and exports.
 *
 * Plain IIFE — no build step. React + hooks come from the Plugin SDK
 * (window.__HERMES_PLUGIN_SDK__); styling lives in bundle/style.css because
 * plugin bundles load at runtime and Tailwind never compiles plugin classes.
 *
 * Notable behaviours:
 *  - Session sidebar runs off the plugin REST API, so history stays browsable
 *    even while the gateway is restarting; the WebSocket is only needed once
 *    you actually chat or resume a session for another turn.
 *  - Tool activity is kept in event order (oldest first) and grouped under
 *    the turn that produced it — nothing is prepended to the top.
 *  - Every gateway event is filtered by ``session_id`` so background turns
 *    in other tabs can never paint into the current conversation.
 *  - The whole tab is wrapped in an ErrorBoundary: a single bad payload can
 *    never blank the dashboard — the boundary catches, logs, and offers a
 *    one-click recovery instead of unmounting the host React tree.
 *  - Live agent control: steering mid-turn, retry/edit-and-retry, context
 *    compaction with a context meter, the slash-command bridge, goal-mode
 *    pills, and subagent/delegation observability all ride the pinned
 *    v2026.5.7 gateway RPC surface (feature-detected, degrade gracefully).
 */
(() => {
  "use strict";

  const sdk = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!sdk || !registry) return;

  const React = sdk.React;
  const { useState, useEffect, useMemo, useRef, useCallback } = sdk.hooks;
  const h = React.createElement;

  const fetchJSON = sdk.fetchJSON;
  const api = sdk.api || {};
  const utils = sdk.utils || {};
  const BASE = "/api/plugins/hermes-chat-dashboard";

  // Optional SDK surfaces — feature-detect, never assume (older dashboards
  // do not expose useTheme/useI18n; the CSS-variable theming keeps working).
  const useTheme = typeof sdk.useTheme === "function" ? sdk.useTheme : null;
  const useI18n = typeof sdk.useI18n === "function" ? sdk.useI18n : null;

  // ── tiny helpers ────────────────────────────────────────────────────

  const nowId = () =>
    `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 9)}`;

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));

  const cn = (...xs) => xs.filter(Boolean).join(" ");

  const str = (v, d = "") => (v == null ? d : typeof v === "string" ? v : String(v));

  const num = (v, d = 0) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : d;
  };

  const fmtTime = (ts) => {
    if (!num(ts)) return "";
    try {
      return new Date(num(ts) * 1000).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return "";
    }
  };

  const relTime = (ts) => {
    const t = num(ts);
    if (!t) return "";
    if (utils && typeof utils.timeAgo === "function") {
      try {
        return utils.timeAgo(t);
      } catch {
        /* fall through */
      }
    }
    const diff = Date.now() / 1000 - t;
    if (diff < 90) return "just now";
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    if (diff < 86400 * 7) return `${Math.round(diff / 86400)}d ago`;
    return new Date(t * 1000).toLocaleDateString();
  };

  const fmtTokens = (n) => {
    const v = num(n);
    if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
    if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
    return String(Math.round(v));
  };

  const fmtBytes = (n) => {
    const v = num(n);
    if (v >= 1024 * 1024) return `${(v / 1024 / 1024).toFixed(1)} MB`;
    if (v >= 1024) return `${(v / 1024).toFixed(1)} KB`;
    return `${Math.round(v)} B`;
  };

  const fmtDuration = (sec) => {
    const s = num(sec);
    if (s <= 0) return "";
    if (s < 10) return `${s.toFixed(1)}s`;
    if (s < 60) return `${Math.round(s)}s`;
    const m = Math.floor(s / 60);
    return `${m}m ${Math.round(s % 60)}s`;
  };

  const roleLabel = (role) =>
    role === "assistant"
      ? "Hermes"
      : role === "tool"
        ? "Tool"
        : role === "system"
          ? "System"
          : "You";

  const toolIcon = (name) => {
    const n = String(name || "").toLowerCase();
    if (n.includes("web_search") || n.includes("search")) return "🔎";
    if (n.includes("web_extract") || n.includes("fetch") || n.includes("url"))
      return "🌐";
    if (n.includes("terminal") || n.includes("shell") || n.includes("command"))
      return "💻";
    if (n.includes("edit") || n.includes("write_file") || n.includes("read_file"))
      return "📄";
    if (n.includes("code") || n.includes("python") || n.includes("exec"))
      return "🐍";
    if (n.includes("image") || n.includes("vision")) return "🖼️";
    if (n.includes("todo") || n.includes("plan")) return "📋";
    if (n.includes("memory")) return "🧠";
    if (n.includes("agent") || n.includes("delegate") || n.includes("spawn"))
      return "🤖";
    return "🛠️";
  };

  // Fuzzy score for the command palette / slash bar: subsequence match with
  // bonuses for word starts; returns null when there is no match at all.
  function fuzzyScore(needle, haystack) {
    const n = String(needle || "").toLowerCase().trim();
    const hs = String(haystack || "").toLowerCase();
    if (!n) return 0;
    let i = 0;
    let score = 0;
    let prevHit = -2;
    for (let j = 0; j < hs.length && i < n.length; j++) {
      if (hs[j] === n[i]) {
        score += j === prevHit + 1 ? 3 : 1;
        if (j === 0 || /[\s\-_/:]/.test(hs[j - 1] || "")) score += 2;
        prevHit = j;
        i++;
      }
    }
    return i === n.length ? score - hs.length * 0.01 : null;
  }

  // ── markdown → html (escape-first, safe) ────────────────────────────

  function inlineMd(text) {
    let s = esc(text);
    // bare https?:// URLs
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)"']+)/g, '$1<a href="$2" target="_blank" rel="noreferrer noopener">$2</a>');
    // [text](https://url)
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
    // inline code (before the other inline styles so their markers inside
    // backticks are not transformed)
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    // bold, then italic/underline-ish
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    s = s.replace(/(^|[^~])~~([^~]+)~~/g, "$1<del>$2</del>");
    return s;
  }

  // Tiny, dependency-free code tokenizer: comments, strings, numbers and a
  // common keyword set. Operates on RAW text and escapes every emitted span,
  // so highlighting can never introduce markup. Deliberately conservative.
  const HL_KEYWORDS = new Set(("break case catch class const continue def default delete do elif else except " +
    "finally for from func function if import in is lambda let new not null or pass print raise return super " +
    "switch this throw try typeof var void while with true false none nil and as assert async await del global " +
    "nonlocal yield struct impl fn mut pub use where match enum type interface static final").split(" "));

  function highlightCode(code, lang) {
    const src = str(code);
    const out = [];
    let i = 0;
    const n = src.length;
    const push = (cls, text) => out.push(cls ? `<span class="hl-${cls}">${esc(text)}</span>` : esc(text));
    while (i < n) {
      const rest = src.slice(i);
      let m;
      if ((m = rest.match(/^(#|\/\/|--)[^\n]*/)) && !/^\-\-?>/.test(rest)) {
        push("cmt", m[0]); i += m[0].length; continue;
      }
      if ((m = rest.match(/^\/\*[\s\S]*?(\*\/|$)/))) {
        push("cmt", m[0]); i += m[0].length; continue;
      }
      if ((m = rest.match(/^("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)/))) {
        push("str", m[0]); i += m[0].length; continue;
      }
      if ((m = rest.match(/^\d+(\.\d+)?([eE][+-]?\d+)?/))) {
        push("num", m[0]); i += m[0].length; continue;
      }
      if ((m = rest.match(/^[A-Za-z_$][\w$]*/))) {
        push(HL_KEYWORDS.has(m[0]) ? "kw" : "", m[0]); i += m[0].length; continue;
      }
      push("", src[i]); i += 1;
    }
    return out.join("");
  }

  function renderMarkdown(src) {
    const raw = String(src || "");
    const codeBlocks = [];
    const lines = raw.replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // fenced code
      const fence = line.match(/^```([\w.+-]*)\s*$/);
      if (fence) {
        const lang = fence[1] || "code";
        const buf = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        i++; // closing fence
        const idx = codeBlocks.push({
          lang,
          code: buf.join("\n"),
        }) - 1;
        out.push(`\u0000CODE${idx}\u0000`);
        continue;
      }

      // table
      if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:\-|]+\|?\s*$/.test(lines[i + 1])) {
        const rows = [];
        while (i < lines.length && lines[i].includes("|")) {
          rows.push(lines[i]);
          i++;
        }
        const cells = (r) =>
          r
            .replace(/^\s*\|/, "")
            .replace(/\|\s*$/, "")
            .split("|")
            .map((c) => c.trim());
        const head = cells(rows[0]);
        const body = rows.slice(2).map((r) => cells(r));
        out.push(
          `<div class="hcd-table-wrap"><table><thead><tr>${head
            .map((c) => `<th>${inlineMd(c)}</th>`)
            .join("")}</tr></thead><tbody>${body
            .map((r) => `<tr>${r.map((c) => `<td>${inlineMd(c)}</td>`).join("")}</tr>`)
            .join("")}</tbody></table><button type="button" class="hcd-copy-code hcd-copy-table" data-copy="${esc(
              rows.slice(2).map((r) => cells(r).join("\t")).join("\n"),
            ).replace(/"/g, "&quot;")}">Copy</button></div>`,
        );
        continue;
      }

      // blockquote group
      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        out.push(`<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`);
        continue;
      }

      // list group (loose: blank lines inside are kept) — with GFM task items
      const isList = (l) => /^\s*([-*+]|\d+[.)])\s+/.test(l);
      const taskOf = (l) => {
        const m = l.match(/^\s*([-*+]|\d+[.)])\s+\[([ xX])\]\s+(.*)$/);
        return m ? { done: m[2].toLowerCase() === "x", text: m[3] } : null;
      };
      if (isList(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const items = [];
        while (i < lines.length) {
          const l = lines[i];
          if (isList(l)) {
            const task = taskOf(l);
            const m = l.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
            items.push({ done: null, text: m[2], task });
            i++;
          } else if (/^\s*$/.test(l) && i + 1 < lines.length && isList(lines[i + 1])) {
            i++; // keep list going across a blank line
          } else {
            break;
          }
        }
        const tag = ordered ? "ol" : "ul";
        out.push(
          `<${tag}>${items
            .map((it) => {
              if (it.task) {
                return `<li class="hcd-task${it.task.done ? " done" : ""}"><span class="hcd-task-box" aria-hidden="true">${it.task.done ? "☑" : "☐"}</span> ${inlineMd(it.task.text)}</li>`;
              }
              return `<li>${inlineMd(it.text)}</li>`;
            })
            .join("")}</${tag}>`,
        );
        continue;
      }

      // heading
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        const lvl = heading[1].length;
        out.push(`<h${lvl}>${inlineMd(heading[2])}</h${lvl}>`);
        i++;
        continue;
      }

      // hr
      if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
        out.push("<hr/>");
        i++;
        continue;
      }

      // blank
      if (/^\s*$/.test(line)) {
        i++;
        continue;
      }

      // paragraph — absorb adjacent non-blank, non-special lines
      const buf = [line];
      i++;
      while (
        i < lines.length &&
        !/^\s*$/.test(lines[i]) &&
        !/^```/.test(lines[i]) &&
        !/^\s*>/.test(lines[i]) &&
        !isList(lines[i]) &&
        !/^(#{1,6})\s+/.test(lines[i])
      ) {
        if (lines[i].includes("|") && i + 1 < lines.length && /^\s*\|?[\s:\-|]+\|?\s*$/.test(lines[i + 1])) break;
        buf.push(lines[i]);
        i++;
      }
      out.push(`<p>${buf.map(inlineMd).join("<br/>")}</p>`);
    }

    let html = out.join("\n");
    codeBlocks.forEach((blk, idx) => {
      const safeLang = esc(blk.lang);
      const safeCode = esc(blk.code);
      const hl = highlightCode(blk.code, blk.lang);
      html = html.replace(
        `\u0000CODE${idx}\u0000`,
        `<div class="hcd-code"><div class="hcd-code-head"><span>${safeLang}</span><button type="button" class="hcd-copy-code" data-copy='${safeCode.replace(/'/g, "&#39;")}'>Copy</button></div><pre><code>${hl || safeCode}</code></pre></div>`,
      );
    });
    return html;
  }

  function bindCopyButtons(root) {
    if (!root) return;
    root.querySelectorAll("button.hcd-copy-code").forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => {
        const text = btn.dataset.copy || btn.closest(".hcd-code")?.querySelector("code")?.innerText || "";
        navigator.clipboard?.writeText(text).then(() => {
          const prev = btn.textContent;
          btn.textContent = "Copied ✓";
          setTimeout(() => (btn.textContent = prev), 1200);
        });
      });
    });
  }

  // ── drafts (localStorage, per conversation) ─────────────────────────

  const draftKey = (key) => `hcd-draft-${key || "new"}`;
  const readDraft = (key) => {
    try {
      return localStorage.getItem(draftKey(key)) || "";
    } catch {
      return "";
    }
  };
  const writeDraft = (key, text) => {
    try {
      if (text) localStorage.setItem(draftKey(key), text);
      else localStorage.removeItem(draftKey(key));
    } catch {
      /* private mode / quota — drafts are best-effort */
    }
  };

  // ── gateway client ─────────────────────────────────────────────────

  const CONNECT_TIMEOUT_MS = 12000;

  class Gateway {
    constructor() {
      this.ws = null;
      this.seq = 0;
      this.pending = new Map();
      this.listeners = new Map();
      this.status = "idle";
      this.retries = 0;
      this.stop = false;
      this.reconnectTimer = null;
      this.lastFrameAt = 0; // any inbound frame (event or response)
      this.heartbeatTimer = null;
    }

    on(type, cb) {
      let set = this.listeners.get(type);
      if (!set) {
        set = new Set();
        this.listeners.set(type, set);
      }
      set.add(cb);
      return () => set.delete(cb);
    }

    emit(ev) {
      (this.listeners.get(ev.type) || []).forEach((cb) => cb(ev));
      (this.listeners.get("*") || []).forEach((cb) => cb(ev));
    }

    setStatus(status) {
      this.status = status;
      this.emit({ type: "status", status });
    }

    connect() {
      // Dedupe concurrent connect() calls (boot + session open + retry can
      // race). Use numeric readyState constants — the WS globals are not
      // guaranteed to exist in every host scope.
      if (this.connectPromise) return this.connectPromise;
      if (this.ws && (this.ws.readyState === 1 || this.ws.readyState === 0)) {
        return Promise.resolve();
      }
      const token = window.__HERMES_SESSION_TOKEN__ || "";
      if (!token) return Promise.reject(new Error("Session token unavailable — open Hermes through the dashboard server."));
      this.setStatus("connecting");
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/api/ws?token=${encodeURIComponent(token)}`);
      this.ws = ws;

      this.connectPromise = new Promise((resolve, reject) => {
        let settled = false;
        const settle = (ok, err) => {
          if (settled) return;
          settled = true;
          this.connectPromise = null;
          clearTimeout(connectTimer);
          ws.removeEventListener("open", onOpen);
          ws.removeEventListener("error", onError);
          if (ok) resolve();
          else reject(err || new Error("Gateway WebSocket failed"));
        };
        const onOpen = () => {
          this.retries = 0;
          this.lastFrameAt = Date.now();
          this.setStatus("open");
          this.startHeartbeat();
          settle(true);
        };
        const onError = () => settle(false, new Error("Gateway WebSocket failed"));
        // A half-open socket that never opens nor errors would otherwise pin
        // connectPromise forever and leave the tab in "connecting" limbo.
        const connectTimer = setTimeout(() => {
          try { ws.close(); } catch { /* noop */ }
          settle(false, new Error("Gateway connection timed out"));
        }, CONNECT_TIMEOUT_MS);

        ws.addEventListener("message", (ev) => {
          this.lastFrameAt = Date.now();
          let msg;
          try {
            msg = JSON.parse(ev.data);
          } catch {
            return;
          }
          if (msg.id && this.pending.has(msg.id)) {
            const p = this.pending.get(msg.id);
            this.pending.delete(msg.id);
            clearTimeout(p.timer);
            if (msg.error) p.reject(new Error(msg.error.message || "gateway request failed"));
            else p.resolve(msg.result);
            return;
          }
          if (msg.method === "event" && msg.params && msg.params.type) this.emit(msg.params);
        });

        ws.addEventListener("close", () => {
          if (this.ws === ws) this.ws = null;
          settle(false, new Error("Gateway connection closed"));
          for (const p of this.pending.values()) p.reject(new Error("Gateway connection closed"));
          this.pending.clear();
          this.stopHeartbeat();
          if (!this.stop) {
            this.setStatus("reconnecting");
            const delay = Math.min(15000, 1000 * 2 ** Math.min(this.retries, 4));
            this.retries += 1;
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = setTimeout(() => this.connect().catch(() => {}), delay);
          } else {
            this.setStatus("closed");
          }
        });

        ws.addEventListener("error", onError);
        ws.addEventListener("open", onOpen);
      });
      return this.connectPromise;
    }

    // Cheap liveness probe: `config.get mtime` is a session-less, read-only
    // handler on the pinned gateway (verified against v2026.5.7 — there is no
    // gateway.ping). If no frame of any kind arrives for 60s the transport is
    // presumed dead: flag "stale" once, then force a reconnect.
    startHeartbeat() {
      this.stopHeartbeat();
      this.heartbeatTimer = setInterval(() => {
        if (this.stop || !this.ws || this.ws.readyState !== 1) return;
        const quietFor = Date.now() - (this.lastFrameAt || Date.now());
        if (quietFor > 60000) {
          this.setStatus("stale");
          try { this.ws.close(); } catch { /* noop */ }
          // the close handler schedules the reconnect
          return;
        }
        this.request("config.get", { key: "mtime" }, 10000).catch(() => {});
      }, 25000);
    }

    stopHeartbeat() {
      if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }

    dispose() {
      this.stop = true;
      this.stopHeartbeat();
      clearTimeout(this.reconnectTimer);
      try {
        this.ws && this.ws.close();
      } catch {
        /* noop */
      }
      this.ws = null;
    }

    request(method, params = {}, timeout = 120000) {
      if (!this.ws || this.ws.readyState !== 1)
        return Promise.reject(new Error("gateway is not connected"));
      const id = `chat-${++this.seq}`;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          this.pending.delete(id);
          reject(new Error(`Request timed out: ${method}`));
        }, timeout);
        this.pending.set(id, { resolve, reject, timer });
        this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      });
    }
  }

  // ── error boundary (a bad payload must never blank the dashboard) ───

  class ErrorBoundary extends (React.Component || class {}) {
    constructor(props) {
      super(props);
      this.state = { error: null };
    }
    static getDerivedStateFromError(error) {
      return { error };
    }
    componentDidCatch(error, info) {
      try {
        console.error("[hermes-chat-dashboard] render error:", error, info);
      } catch { /* noop */ }
    }
    render() {
      if (!this.state.error) return this.props.children;
      return h("div", { className: "hcd-crash", role: "alert" },
        h("div", { className: "hcd-crash-card" },
          h("div", { className: "hcd-crash-icon" }, "⚠️"),
          h("h2", null, "The chat tab hit an unexpected error"),
          h("p", null, "The rest of the dashboard is fine. Your conversation history is stored on the server and was not affected."),
          h("details", null,
            h("summary", null, "Technical details"),
            h("pre", null, String((this.state.error && this.state.error.stack) || this.state.error))),
          h("div", { className: "hcd-crash-actions" },
            h("button", {
              className: "primary",
              onClick: () => this.setState({ error: null }),
            }, "Try again"),
            h("button", {
              onClick: () => { try { location.reload(); } catch { /* noop */ } },
            }, "Reload dashboard"),
          ),
        ),
      );
    }
  }

  // ── shared small components ─────────────────────────────────────────

  function StatusDot({ status }) {
    const cls = status === "open" ? "ok" : status === "connecting" || status === "reconnecting" ? "busy" : status === "stale" ? "warn pulse" : "warn";
    return h("span", { className: `hcd-dot ${cls}`, title: `Gateway: ${status}` });
  }

  function ToastStack({ toasts, dismiss }) {
    return h(
      "div",
      { className: "hcd-toasts" },
      toasts.map((t) =>
        h(
          "div",
          { key: t.id, className: `hcd-toast ${t.kind || "info"}`, role: t.kind === "error" ? "alert" : "status" },
          h("span", null, t.text),
          h("button", { onClick: () => dismiss(t.id), title: "Dismiss", "aria-label": "Dismiss notification" }, "×"),
        ),
      ),
    );
  }

  function Empty({ icon, title, sub }) {
    return h(
      "div",
      { className: "hcd-empty" },
      icon && h("div", { className: "hcd-empty-icon" }, icon),
      h("strong", null, title),
      sub && h("p", null, sub),
    );
  }

  // Modal shell: focus trap, Esc to close, aria attributes. Every dialog in
  // the plugin routes through this — no window.prompt / window.confirm.
  function Modal({ title, onClose, children, wide }) {
    const ref = useRef(null);
    useEffect(() => {
      const node = ref.current;
      const focusables = () =>
        Array.from(
          (node &&
            node.querySelectorAll(
              'button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])',
            )) || [],
        ).filter((el) => el.offsetParent !== null || el === document.activeElement);
      const t = setTimeout(() => {
        const first = focusables()[0];
        try { first && first.focus(); } catch { /* noop */ }
      }, 0);
      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          onClose();
          return;
        }
        if (e.key === "Tab") {
          const items = focusables();
          if (!items.length) return;
          const first = items[0];
          const last = items[items.length - 1];
          const active = document.activeElement;
          if (e.shiftKey && (active === first || !node.contains(active))) {
            e.preventDefault();
            try { last.focus(); } catch { /* noop */ }
          } else if (!e.shiftKey && active === last) {
            e.preventDefault();
            try { first.focus(); } catch { /* noop */ }
          }
        }
      };
      document.addEventListener("keydown", onKey, true);
      return () => {
        clearTimeout(t);
        document.removeEventListener("keydown", onKey, true);
      };
    }, [onClose]);
    return h("div", {
      className: "hcd-modal", onClick: (e) => e.target === e.currentTarget && onClose(),
      role: "presentation",
    },
      h("div", { className: cn("hcd-modal-box", wide && "wide"), ref, role: "dialog", "aria-modal": "true", "aria-label": title || "Dialog" },
        h("div", { className: "hcd-modal-head" },
          h("h2", null, title || ""),
          h("button", { className: "hcd-x", onClick: onClose, "aria-label": "Close dialog" }, "×")),
        children,
      ),
    );
  }

  function ConfirmModal({ title, message, confirmLabel, danger, onConfirm, onCancel }) {
    return h(Modal, { title: title || "Are you sure?", onClose: onCancel },
      h("p", { className: "hcd-confirm-msg" }, message),
      h("div", { className: "hcd-modal-actions" },
        h("button", { onClick: onCancel }, "Cancel"),
        h("button", { className: danger ? "danger" : "primary", onClick: onConfirm }, confirmLabel || "Confirm"),
      ),
    );
  }

  // Small "prompt()" replacement: a modal with a single text input.
  function PromptModal({ title, label, initial, placeholder, confirmLabel, onConfirm, onCancel }) {
    const [value, setValue] = useState(initial || "");
    const ok = () => onConfirm(String(value || "").trim());
    return h(Modal, { title, onClose: onCancel },
      h("label", { className: "hcd-prompt-modal" },
        h("span", null, label || ""),
        h("input", {
          autoFocus: true,
          value,
          placeholder: placeholder || "",
          onChange: (e) => setValue(e.target.value),
          onKeyDown: (e) => { if (e.key === "Enter") { e.preventDefault(); ok(); } },
        })),
      h("div", { className: "hcd-modal-actions" },
        h("button", { onClick: onCancel }, "Cancel"),
        h("button", { className: "primary", onClick: ok }, confirmLabel || "Save"),
      ),
    );
  }

  // ── tool activity ───────────────────────────────────────────────────

  // Every user-visible string is coerced through `str()` — a structured
  // payload (list/dict) arriving where text is expected renders as harmless
  // text instead of crashing React with "objects are not valid as children".
  function ToolCard({ tool, defaultOpen }) {
    const status = tool.status || "running";
    const isRunning = status === "running" || status === "pending";
    const title = str(tool.title || tool.goal || tool.name || "Tool");
    const subRaw =
      tool.summary || tool.context || tool.preview ||
      (tool.error ? tool.error : isRunning ? "Working…" : "Done");
    const sub = str(subRaw);
    const inlineDiff = tool.inline_diff ? str(tool.inline_diff) : "";
    const preview = tool.preview && !inlineDiff ? str(tool.preview) : "";
    return h(
      "details",
      { className: `hcd-tool ${status}`, open: isRunning || !!defaultOpen },
      h(
        "summary",
        null,
        h(
          "span",
          { className: "hcd-tool-title" },
          tool.kind === "subagent" ? "🤖 " : toolIcon(tool.name),
          " ",
          title,
          tool.kind === "subagent" && tool.task_count
            ? h(
                "span",
                { className: "hcd-tool-meta" },
                ` (task ${num(tool.task_index, 1)}/${tool.task_count})`,
              )
            : null,
        ),
        h(
          "span",
          { className: "hcd-tool-right" },
          tool.duration_s || tool.duration_seconds
            ? h("em", null, fmtDuration(tool.duration_s || tool.duration_seconds))
            : null,
          h(
            "span",
            { className: `hcd-badge ${status}` },
            isRunning ? "running" : status === "error" ? "error" : "done",
          ),
        ),
      ),
      h(
        "div",
        { className: "hcd-tool-body" },
        sub && !inlineDiff ? h("p", { className: "hcd-tool-sub" }, sub) : null,
        tool.context && inlineDiff ? h("p", { className: "hcd-tool-sub" }, str(tool.context)) : null,
        Array.isArray(tool.todos) && tool.todos.length
          ? h(
              "ul",
              { className: "hcd-todos" },
              tool.todos.slice(0, 50).map((t, i) => {
                const done = t && t.status === "done";
                const label = t && (t.content || t.text) ? str(t.content || t.text) : str(t);
                return h("li", { key: i, className: done ? "done" : "" }, `${done ? "✓" : "○"} ${label}`);
              }),
            )
          : null,
        preview ? h("pre", null, preview) : null,
        inlineDiff
          ? h("div", { className: "hcd-diff" }, inlineDiff.split("\n").map((l, i) =>
              h(
                "div",
                { key: i, className: cn("hcd-diff-line", l.startsWith("+") && "add", l.startsWith("-") && "del") },
                l || " ",
              ),
            ))
          : null,
        tool.error ? h("p", { className: "hcd-tool-error" }, str(tool.error)) : null,
      ),
    );
  }

  // ── subagent / delegation observability ─────────────────────────────

  const SUBAGENT_OPENERS = new Set(["subagent.spawn_requested", "subagent.spawn", "subagent.start", "subagent.started"]);
  const SUBAGENT_CLOSERS = new Set(["subagent.complete", "subagent.completed", "subagent.summary", "subagent.finished"]);

  // events → {id: agentNode}; agentNode = {id, parent_id, depth, goal, model,
  // status, task_index, task_count, tokens, tools[], lastText}
  function applySubagentEvent(agents, ev) {
    const p = ev.payload || {};
    const id = str(p.subagent_id || ev.subagent_id || "");
    if (!id) return agents; // identity-less events render via tool cards only
    const type = ev.type;
    const prev = agents[id] || {
      id,
      parent_id: str(p.parent_id || ""),
      depth: num(p.depth, 0),
      goal: str(p.goal || ""),
      model: str(p.model || ""),
      status: "running",
      task_index: num(p.task_index, 0),
      task_count: num(p.task_count, 1),
      tokens: 0,
      tools: [],
      lastText: "",
      startedAt: Date.now() / 1000,
    };
    const next = { ...prev };
    if (p.parent_id !== undefined) next.parent_id = str(p.parent_id);
    if (p.depth !== undefined) next.depth = num(p.depth, prev.depth);
    if (p.goal) next.goal = str(p.goal);
    if (p.model) next.model = str(p.model);
    if (p.task_index !== undefined) next.task_index = num(p.task_index, prev.task_index);
    if (p.task_count !== undefined) next.task_count = num(p.task_count, prev.task_count);
    next.tokens = num(p.input_tokens) + num(p.output_tokens) || prev.tokens;
    if (p.cost_usd !== undefined) next.cost_usd = num(p.cost_usd);
    if (p.summary) next.summary = str(p.summary);
    if (p.status) next.status = str(p.status);

    if (SUBAGENT_OPENERS.has(type)) next.status = "running";
    else if (type === "subagent.thinking" || type === "subagent.reasoning") next.status = "thinking";
    else if (type === "subagent.tool") {
      next.status = "running";
      const line = str(p.tool_preview || p.text || p.tool_name || "tool");
      next.tools = [...next.tools, line].slice(-30);
    } else if (type === "subagent.progress") {
      next.status = "running";
      if (p.text || p.summary) next.lastText = str(p.text || p.summary);
    } else if (SUBAGENT_CLOSERS.has(type)) {
      next.status = "complete";
      if (p.output_tail && Array.isArray(p.output_tail)) next.tail = p.output_tail.slice(-5);
    }
    if (p.duration_seconds !== undefined) next.duration_s = num(p.duration_seconds);
    return { ...agents, [id]: next };
  }

  function agentsToTree(agents) {
    const byId = agents;
    const children = new Map();
    const roots = [];
    for (const a of Object.values(byId)) {
      const parent = a.parent_id && byId[a.parent_id] ? a.parent_id : "";
      if (parent) {
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(a);
      } else {
        roots.push(a);
      }
    }
    const sortAgents = (xs) => xs.slice().sort((x, y) => num(x.startedAt) - num(y.startedAt));
    return { roots: sortAgents(roots), children };
  }

  function AgentNode({ agent, children, onInterrupt, live }) {
    const kids = children.get(agent.id) || [];
    const status = agent.status === "complete" ? "complete" : agent.status === "thinking" ? "thinking" : "running";
    const label = agent.goal || `Subagent ${agent.id.slice(0, 6)}`;
    return h("li", { className: `hcd-agent ${status}` },
      h("div", { className: "hcd-agent-row" },
        h("span", { className: "hcd-agent-icon", title: agent.model || "" }, "🤖"),
        h("span", { className: "hcd-agent-label" },
          h("span", { className: "hcd-agent-name" }, label),
          h("small", { className: "hcd-agent-meta" },
            agent.model ? ` ${agent.model}` : "",
            agent.task_count > 1 ? ` · task ${agent.task_index + 1}/${agent.task_count}` : "",
            agent.tokens ? ` · ${fmtTokens(agent.tokens)} tok` : "",
            agent.duration_s ? ` · ${fmtDuration(agent.duration_s)}` : "",
          ),
          agent.lastText && status !== "complete" ? h("small", { className: "hcd-agent-last" }, ` ${agent.lastText}`) : null,
        ),
        h("span", { className: "hcd-agent-right" },
          live && status !== "complete" && h("button", {
            className: "hcd-agent-interrupt",
            title: "Interrupt this subagent",
            "aria-label": `Interrupt subagent ${label}`,
            onClick: () => onInterrupt(agent.id),
          }, "■"),
          h("span", { className: `hcd-badge ${status}` }, status)),
      ),
      agent.tools && agent.tools.length
        ? h("ul", { className: "hcd-agent-tools" }, agent.tools.map((t, i) => h("li", { key: i }, t)))
        : null,
      kids.length
        ? h("ul", { className: "hcd-agent-children" }, kids.map((k) =>
            h(AgentNode, { key: k.id, agent: k, children, onInterrupt, live }),
          ))
        : null,
    );
  }

  function SubagentTree({ agents, onInterrupt, live, title }) {
    const { roots, children } = useMemo(() => agentsToTree(agents), [agents]);
    if (!roots.length) return null;
    return h("div", { className: "hcd-agent-tree" },
      h("div", { className: "hcd-agent-tree-head" },
        h("strong", null, title || "🤖 Agent tree"),
        h("small", null, `${Object.keys(agents).length} agent${Object.keys(agents).length === 1 ? "" : "s"}`)),
      h("ul", { className: "hcd-agent-roots" },
        roots.map((a) => h(AgentNode, { key: a.id, agent: a, children, onInterrupt, live }))),
    );
  }

  // ── prompts (approval / clarify / sudo / secret) ────────────────────

  function PromptCard({ prompt, onRespond }) {
    const kind = prompt.kind || "clarify";
    const [value, setValue] = useState("");
    const question = str(prompt.question || prompt.message || prompt.prompt || prompt.description || prompt.command || "Hermes is waiting for input");
    const choices =
      Array.isArray(prompt.choices) && prompt.choices.length
        ? prompt.choices.map((c) => str(c))
        : Array.isArray(prompt.options)
          ? prompt.options.map((c) => str(c))
          : null;
    const allowPermanent = prompt.allow_permanent !== false;

    return h(
      "div",
      { className: `hcd-prompt ${kind}` },
      h("div", { className: "hcd-prompt-head" },
        h("strong", null, kind === "approval" ? "⚠️ Action approval" : kind === "clarify" ? "❓ Clarification" : kind === "sudo" ? "🔐 sudo password" : "🔑 Secret needed"),
        h("button", { className: "hcd-prompt-dismiss", onClick: () => onRespond(kind, prompt, "deny"), "aria-label": "Dismiss prompt" }, "×"),
      ),
      h("p", null, question),
      kind === "approval" && prompt.command
        ? h("pre", { className: "hcd-prompt-cmd" }, str(prompt.command))
        : null,
      prompt.description && kind === "approval"
        ? h("p", { className: "hcd-prompt-desc" }, str(prompt.description))
        : null,
      choices
        ? h(
            "div",
            { className: "hcd-prompt-choices" },
            choices.map((c) =>
              h(
                "button",
                { key: c, onClick: () => onRespond(kind, prompt, c) },
                c,
              ),
            ),
          )
        : null,
      kind === "clarify" && !choices
        ? h(
            "div",
            { className: "hcd-prompt-input" },
            h("input", { value, onChange: (e) => setValue(e.target.value), onKeyDown: (e) => { if (e.key === "Enter" && value.trim()) onRespond(kind, prompt, value.trim()); }, placeholder: "Type your answer…" }),
            h("button", { className: "primary", onClick: () => value.trim() && onRespond(kind, prompt, value.trim()) }, "Send"),
          )
        : null,
      (kind === "sudo" || kind === "secret")
        ? h(
            "div",
            { className: "hcd-prompt-input" },
            h("input", { type: kind === "sudo" ? "password" : "text", value, onChange: (e) => setValue(e.target.value), placeholder: kind === "sudo" ? "sudo password" : prompt.env_var ? `value for ${prompt.env_var}` : "value" }),
            h("button", { className: "primary", onClick: () => value && onRespond(kind, prompt, value) }, "Submit"),
          )
        : null,
      kind === "approval"
        ? h(
            "div",
            { className: "hcd-prompt-choices" },
            h("button", { className: "approve", onClick: () => onRespond(kind, prompt, "once") }, "Allow once"),
            h("button", { className: "approve", onClick: () => onRespond(kind, prompt, "session") }, "Allow this chat"),
            allowPermanent
              ? h("button", { className: "approve", title: "Add this pattern to the permanent allowlist", onClick: () => onRespond(kind, prompt, "always") }, "Allow always")
              : null,
            h("button", { className: "deny", onClick: () => onRespond(kind, prompt, "deny") }, "Deny"),
          )
        : null,
    );
  }

  // ── message ─────────────────────────────────────────────────────────

  function MessageView({ msg, index, active, onAction, showTime, showUsage }) {
    const bodyRef = useRef(null);
    const [reasoningOpen, setReasoningOpen] = useState(null); // null = auto
    useEffect(() => {
      bindCopyButtons(bodyRef.current);
    }, [msg.content, msg.streaming]);
    useEffect(() => {
      // auto: expanded while streaming, collapsed once the turn lands — the
      // user's own toggle (boolean) always wins
      if (msg.streaming) setReasoningOpen((v) => (v === null ? true : v));
      else setReasoningOpen((v) => (v === null ? false : v));
    }, [msg.streaming]);
    const isHermes = msg.role === "assistant";
    const isUser = msg.role === "user";
    const isTool = msg.role === "tool";
    const isSystem = msg.role === "system";
    const contentText = str(msg.content || "");
    // Oversized raw tool/system dumps (the agent's own tool transcripts are
    // huge JSON/blocks) are shown in a compact, scrollable, collapsible block
    // instead of being rendered as full-width markdown that buries the real
    // conversation. Live-streaming ones stay open; stored ones start closed.
    const compactRaw = (isTool || (isSystem && contentText.length > 600)) && !active;
    const reasoning = str(msg.reasoning || "");
    const thinking = str(msg.thinking || "");
    const hasStreamedThought = (reasoning || thinking).trim().length > 0;
    const bodyContent = renderMarkdown(contentText || (active ? "▌" : "")) + (active && contentText ? '<span class="hcd-cursor">▌</span>' : "");

    return h(
      "article",
      { className: cn("hcd-message", `hcd-${msg.role || "assistant"}`), id: msg.id || undefined },
      h("div", { className: "hcd-avatar", title: roleLabel(msg.role) }, isHermes ? "H" : isUser ? "🧑" : isTool ? "⚙️" : "ℹ️"),
      h(
        "div",
        { className: "hcd-bubble" },
        h(
          "div",
          { className: "hcd-meta" },
          h("strong", null, roleLabel(msg.role)),
          showTime && msg.timestamp ? h("span", null, fmtTime(msg.timestamp)) : null,
          msg.model ? h("span", { className: "hcd-model-pill" }, str(msg.model)) : null,
          msg.verbose ? h("span", { className: "hcd-model-pill", title: "verbose reasoning" }, "verbose") : null,
          msg.status === "interrupted" ? h("span", { className: "hcd-status-pill interrupted" }, "stopped") : null,
          msg.status === "error" ? h("span", { className: "hcd-status-pill error" }, "error") : null,
        ),
        msg.error
          ? h(
              "div",
              { className: "hcd-message-error" },
              h("strong", null, "Something went wrong"),
              h("details", null, h("summary", null, "View details"), h("pre", null, contentText)),
            )
          : compactRaw
            ? h("details", { className: "hcd-outline", open: !!msg.streaming },
                h("summary", null, isTool ? "⚙️ Tool output" : "Output"),
                h("pre", null, contentText || (active ? "▌" : "")),
              )
            : isSystem && !compactRaw
              ? h("div", { className: "hcd-markdown hcd-system-note", dangerouslySetInnerHTML: { __html: bodyContent } })
              : h("div", { ref: bodyRef, className: "hcd-markdown", dangerouslySetInnerHTML: { __html: bodyContent } }),
        hasStreamedThought
          ? h(
              "details",
              {
                className: cn("hcd-reasoning", msg.streaming && "live"),
                open: reasoningOpen === null ? undefined : !!reasoningOpen,
                onToggle: (e) => setReasoningOpen(e.target.open ? true : false),
              },
              h("summary", null, reasoningOpen === null && msg.streaming ? "🧠 Reasoning (streaming…)" : "🧠 Reasoning"),
              h("pre", null, thinking ? `${thinking}\n\n${reasoning}`.trim() : reasoning),
            )
          : null,
        msg.usage && msg.usage.total && showUsage !== false
          ? h("div", { className: "hcd-usage" },
              `${fmtTokens(msg.usage.input || 0)} in · ${fmtTokens(msg.usage.output || 0)} out · ${msg.usage.calls || 1} call${(msg.usage.calls || 1) === 1 ? "" : "s"} ${msg.usage.model ? `· ${str(msg.usage.model)}` : ""}`)
          : null,
        msg.warning ? h("div", { className: "hcd-warning" }, str(msg.warning)) : null,
        h(
          "div",
          { className: "hcd-actions" },
          h("button", { onClick: () => onAction("copy", msg, index) }, "Copy"),
          isUser && h("button", { onClick: () => onAction("edit", msg, index) }, "Edit"),
          isUser && h("button", { onClick: () => onAction("editRetry", msg, index), title: "Edit this message and resend it (truncates the turn)" }, "Edit & retry"),
          isHermes && h("button", { onClick: () => onAction("regenerate", msg, index) }, "Regenerate"),
          isHermes && !msg.streaming && h("button", { onClick: () => onAction("continue", msg, index) }, "Continue"),
          h("button", { onClick: () => onAction("branch", msg, index) }, "Branch"),
          h("button", { className: "danger", onClick: () => onAction("delete", msg, index) }, "Delete"),
        ),
      ),
    );
  }

  // ── context meter (header) ──────────────────────────────────────────

  function ContextMeter({ usage, contextWindow, onCompact, disabled }) {
    const used = num(usage && (usage.context_used || usage.total));
    const max = num(usage && usage.context_max) || num(contextWindow);
    if (!used || !max) return null;
    const pct = Math.max(0, Math.min(100, Math.round((used / max) * 100)));
    const warn = pct >= 80;
    return h("button", {
      className: cn("hcd-ctx-meter", warn && "warn"),
      title: `Context: ${fmtTokens(used)} / ${fmtTokens(max)} tokens (${pct}%)${warn ? " — click to compact" : ""}`,
      onClick: onCompact,
      disabled,
      "aria-label": `Context usage ${pct} percent. ${warn ? "Compact conversation." : ""}`,
    },
      h("span", { className: "hcd-ctx-label" }, warn ? "⚠" : "◇"),
      h("span", { className: "hcd-ctx-bar" },
        h("span", { className: "hcd-ctx-fill", style: { width: `${pct}%` } })),
      h("span", { className: "hcd-ctx-text" }, `${pct}%`),
    );
  }

  // ── model picker (searchable; pin to favourites) ────────────────────

  function ModelPicker({ models, pinned, value, onChange, onPinnedChange, disabled }) {
    const [open, setOpen] = useState(false);
    const [q, setQ] = useState("");
    const inputRef = useRef(null);
    const wrapRef = useRef(null);

    useEffect(() => {
      if (open) {
        setQ("");
        setTimeout(() => inputRef.current && inputRef.current.focus(), 0);
      }
    }, [open]);

    useEffect(() => {
      if (!open) return;
      const close = (e) => {
        if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
      };
      document.addEventListener("mousedown", close);
      return () => document.removeEventListener("mousedown", close);
    }, [open]);

    const chosen = models.find((m) => m.id === value) || null;
    const label = value === "auto" || !value ? "Auto" : `${chosen?.provider || ""}${chosen ? "/" : ""}${chosen?.name || chosen?.model || value}`;
    const ql = q.trim().toLowerCase();
    const pins = (Array.isArray(pinned) ? pinned : []).map((p) => String(p));
    const list = models.slice(0, 400).filter((m) => !ql || `${m.provider} ${m.name} ${m.model}`.toLowerCase().includes(ql));
    const pinnedList = list.filter((m) => pins.includes(m.id));
    const rest = list.filter((m) => !pins.includes(m.id));

    const row = (m) => h("div", {
      key: m.id,
      className: cn("hcd-model-row", m.id === value && "active"),
      role: "button",
      tabIndex: 0,
      "aria-label": `Choose model ${m.name || m.model}`,
      onClick: () => { onChange(m.id); setOpen(false); },
      onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onChange(m.id); setOpen(false); } },
    },
      h("span", { className: "hcd-model-name" }, m.name || m.model),
      h("small", null, m.provider || ""),
      m.context_window ? h("span", { className: "hcd-model-ctx" }, `${fmtTokens(m.context_window)} ctx`) : null,
      h("button", {
        type: "button",
        className: "hcd-model-pin",
        title: pins.includes(m.id) ? "Unpin from favourites" : "Pin to favourites",
        "aria-label": pins.includes(m.id) ? `Unpin model ${m.name}` : `Pin model ${m.name}`,
        onClick: (e) => {
          e.stopPropagation();
          const next = pins.includes(m.id) ? pins.filter((p) => p !== m.id) : [...pins, m.id];
          if (onPinnedChange) onPinnedChange(next);
        },
      }, pins.includes(m.id) ? "★" : "☆"),
    );

    return h(
      "div",
      { className: cn("hcd-model-picker", open && "open"), ref: wrapRef },
      h("button", {
        className: "hcd-ctl-btn hcd-model-btn",
        onClick: () => setOpen((v) => !v),
        disabled,
        title: "Choose model",
        "aria-label": "Choose model",
        "aria-expanded": open,
      },
        "🧠 ", label, " ▾"),
      open
        ? h("div", { className: "hcd-model-pop", role: "listbox" },
            h("input", {
              ref: inputRef,
              className: "hcd-model-search",
              value: q,
              onChange: (e) => setQ(e.target.value),
              placeholder: "Search models…",
              "aria-label": "Search models",
            }),
            h("div", { className: "hcd-model-list" },
              h("div", {
                className: cn("hcd-model-row", value === "auto" && "active"),
                role: "button",
                tabIndex: 0,
                onClick: () => { onChange("auto"); setOpen(false); },
                onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onChange("auto"); setOpen(false); } },
              },
                h("span", { className: "hcd-model-name" }, "Auto"), h("small", null, "let Hermes decide")),
              pinnedList.length
                ? h("div", { className: "hcd-model-group" }, h("h5", null, "★ Favourites"), pinnedList.map(row))
                : null,
              rest.length
                ? h("div", { className: "hcd-model-group" }, pinnedList.length && h("h5", null, "All models"), rest.map(row))
                : h("div", { className: "hcd-empty-small hcd-model-none" }, "No models match"),
            ),
          )
        : null,
    );
  }

  // ── slash-command bridge ────────────────────────────────────────────
  //
  // All dispatching goes through `command.dispatch` (never `slash.exec` — this
  // deployment disables the slash worker, and lazy worker spawns are exactly
  // what the deployment patch exists to prevent). Commands that the pinned
  // bridge does not know resolve to an error, which we surface as a notice.
  //
  // Dispatch result shapes (verified against v2026.5.7):
  //   {type: "send",    message, notice?}  → submit the message as a prompt
  //   {type: "exec",    output}            → show a system line
  //   {type: "alias",   name}              → re-dispatch the aliased command
  //   {type: "skill"}                      → informational (skill took over)

  const FALLBACK_COMMANDS = [
    { name: "retry", description: "Regenerate the last response" },
    { name: "goal", description: "Manage persistent goal mode: status | pause | resume | clear | <text>" },
    { name: "steer", description: "Steer the running turn: /steer <text>" },
    { name: "queue", description: "Queue a prompt behind the running turn: /queue <text>" },
    { name: "compress", description: "Compress conversation context (keep recent turns)" },
    { name: "context", description: "Show context window usage" },
    { name: "undo", description: "Undo the last turn" },
    { name: "model", description: "Show or set the active model: /model <id>" },
    { name: "yolo", description: "Toggle yolo (no approval prompts) for this session" },
    { name: "title", description: "Rename the current conversation" },
    { name: "search", description: "Search the web (may enable tools)" },
  ];

  function CommandBar({ query, catalog, onPick }) {
    const name = String(query || "").replace(/^\//, "").split(/\s+/)[0].toLowerCase();
    if (!name) return null;
    const pool = catalog.length ? catalog : FALLBACK_COMMANDS;
    const hits = pool
      .filter((c) => c.name && c.name.toLowerCase().startsWith(name))
      .slice(0, 8);
    if (!hits.length) return null;
    return h("div", { className: "hcd-cmdbar", role: "listbox", "aria-label": "Slash command suggestions" },
      hits.map((c) =>
        h("div", {
          key: c.name,
          className: "hcd-cmd-row",
          role: "option",
          "aria-selected": "false",
          tabIndex: 0,
          onClick: () => onPick(c),
          onKeyDown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(c); } },
        },
          h("code", null, `/${c.name}`),
          h("small", null, str(c.description || "")),
        )),
    );
  }

  // ── command palette (Cmd/Ctrl+K) ────────────────────────────────────

  function Palette({ actions, sessions, onRun, onClose }) {
    const [q, setQ] = useState("");
    const [cursor, setCursor] = useState(0);
    const inputRef = useRef(null);
    useEffect(() => {
      setTimeout(() => inputRef.current && inputRef.current.focus(), 0);
    }, []);
    useEffect(() => setCursor(0), [q]);

    const ql = String(q || "").toLowerCase().trim();
    const items = useMemo(() => {
      const acts = actions
        .map((a) => ({ kind: "action", ...a, score: ql ? fuzzyScore(ql, `${a.label} ${a.hint || ""}`) : 0 }))
        .filter((a) => a.score !== null && (a.when === undefined || a.when));
      const sess = sessions
        .map((s) => ({ kind: "session", label: s.title || s.id, session: s, score: ql ? fuzzyScore(ql, `${s.title || ""} ${s.id}`) : -1 }))
        .filter((s) => s.score !== null)
        .slice(0, ql ? 8 : 4);
      const all = [...acts, ...sess];
      all.sort((a, b) => b.score - a.score);
      return all.slice(0, 14);
    }, [q, actions, sessions]);

    const run = (item) => {
      if (!item) return;
      onRun(item);
      onClose();
    };

    return h("div", { className: "hcd-modal hcd-palette-modal", onClick: (e) => e.target === e.currentTarget && onClose(), role: "presentation" },
      h("div", { className: "hcd-palette", role: "dialog", "aria-modal": "true", "aria-label": "Command palette" },
        h("input", {
          ref: inputRef,
          value: q,
          onChange: (e) => setQ(e.target.value),
          onKeyDown: (e) => {
            if (e.key === "Escape") { e.preventDefault(); onClose(); }
            else if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, items.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
            else if (e.key === "Enter") { e.preventDefault(); run(items[cursor]); }
          },
          placeholder: "Search commands and conversations…",
          "aria-label": "Search commands and conversations",
        }),
        items.length
          ? h("div", { className: "hcd-palette-list" }, items.map((item, i) =>
              h("div", {
                key: `${item.kind}:${item.label}:${i}`,
                className: cn("hcd-palette-row", i === cursor && "cursor"),
                onMouseEnter: () => setCursor(i),
                onClick: () => run(item),
              },
                h("span", { className: "hcd-palette-icon" }, item.kind === "session" ? "💬" : "⌘"),
                h("span", { className: "hcd-palette-label" }, item.label),
                item.kind === "session"
                  ? h("small", null, relTime(item.session.last_active || item.session.started_at))
                  : h("small", null, item.hint || "")),
            ))
          : h("div", { className: "hcd-palette-empty" }, "No matches"),
      ),
    );
  }

  // ── composer ────────────────────────────────────────────────────────

  function Composer({
    text, setText, onSend, onBackground, onSteer, onDispatchCommand, commandCatalog,
    generating, disabled, readOnly, attachments, onAttach, onRemoveAttachment,
    onOpenTools, toolsSummary, mode, onModeChange, modes, modelPicker,
    enterToSend, setEnterToSend, draftDiscardable, onDiscardDraft,
  }) {
    const [drag, setDrag] = useState(false);
    const taRef = useRef(null);

    useEffect(() => {
      const ta = taRef.current;
      if (!ta) return;
      ta.style.height = "0px";
      ta.style.height = `${Math.min(280, ta.scrollHeight)}px`;
    }, [text]);

    const upload = async (files) => {
      for (const file of Array.from(files || [])) {
        await onAttach(file);
      }
    };

    const submit = (background = false) => {
      const value = text.trim();
      if (!value && !attachments.length) return;
      if (value.startsWith("/")) {
        onDispatchCommand(value);
        setText("");
        return;
      }
      if (generating && onSteer) {
        onSteer(value);
        setText("");
        return;
      }
      if (background) onBackground(value);
      else onSend(value);
      setText("");
    };

    const onKeyDown = (e) => {
      if (e.key === "Enter" && !e.shiftKey && enterToSend) {
        e.preventDefault();
        submit(false);
      } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        submit(false);
      }
    };

    const readyAttachments = attachments.filter((a) => a.status === "ready");
    const pendingAttachments = attachments.filter((a) => a.status === "uploading").length;

    return h(
      "div",
      {
        className: cn("hcd-composer", drag && "drag", readOnly && "readonly"),
        onDragOver: (e) => { e.preventDefault(); setDrag(true); },
        onDragLeave: () => setDrag(false),
        onDrop: (e) => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files); },
      },
      text.startsWith("/")
        ? h(CommandBar, {
            query: text,
            catalog: commandCatalog || [],
            onPick: (cmd) => setText(`/${cmd.name} `),
          })
        : null,
      attachments.length
        ? h(
            "div",
            { className: "hcd-attach-row" },
            attachments.map((att) =>
              h(
                "div",
                { key: att.id, className: `hcd-attach ${att.status}` },
                att.thumb && att.status !== "error"
                  ? h("img", { src: att.thumb, alt: att.name, className: "hcd-attach-thumb" })
                  : h("span", { className: "hcd-attach-icon" }, att.is_image ? "🖼️" : "📄"),
                h(
                  "div",
                  { className: "hcd-attach-meta" },
                  h("span", { className: "hcd-attach-name", title: att.name }, att.name),
                  att.status === "uploading" ? h("small", null, "uploading…")
                    : att.status === "error" ? h("small", { className: "err" }, str(att.error || "upload failed"))
                    : h("small", null, fmtBytes(att.size)),
                ),
                h("button", {
                  onClick: () => onRemoveAttachment(att),
                  "aria-label": `Remove attachment ${att.name}`,
                  title: "Remove attachment",
                }, "×"),
              ),
            ),
          )
        : null,
      h(
        "div",
        { className: "hcd-input-row" },
        h("textarea", {
          ref: taRef,
          rows: 1,
          value: text,
          placeholder: readOnly
            ? "This conversation is read-only"
            : generating
              ? "Steer the running turn… (Enter to steer)"
              : "Message Hermes…  (/ for commands, ⌘K palette)",
          onChange: (e) => setText(e.target.value),
          onKeyDown,
          disabled: disabled && !readOnly,
          "aria-label": "Message input",
        }),
        h("div", { className: "hcd-send-col" },
          pendingAttachments ? h("small", { className: "hcd-upload-note" }, `↑ ${pendingAttachments}`) : null,
          h("div", { className: "hcd-send-row" },
            onBackground && !generating
              ? h("button", { className: "hcd-ctl-btn", onClick: () => submit(true), disabled: !text.trim() || disabled, title: "Run in background" }, "↗")
              : null,
            h("button", {
              className: "hcd-send",
              onClick: () => submit(false),
              disabled: (disabled && !readOnly) || (!text.trim() && !readyAttachments.length),
              "aria-label": generating ? "Steer conversation" : "Send message",
            }, generating ? "Steer" : "Send"),
          ),
        ),
      ),
      h(
        "div",
        { className: "hcd-composer-foot" },
        h("div", { className: "hcd-composer-ctls" },
          h("label", { className: "hcd-file-btn", title: "Attach files" },
            "📎",
            h("input", {
              type: "file",
              multiple: true,
              style: { display: "none" },
              onChange: (e) => { upload(e.target.files); e.target.value = ""; },
              "aria-label": "Attach files",
            }),
          ),
          h("button", { className: "hcd-ctl-btn", onClick: onOpenTools, title: "Tool configuration" }, `🧰 ${toolsSummary || "tools"}`),
          modes && modes.length
            ? h("select", {
                className: "hcd-ctl-btn hcd-mode-select",
                value: mode || "fast",
                onChange: (e) => onModeChange(e.target.value),
                "aria-label": "Chat mode",
              },
              modes.map((m) => h("option", { key: m.id, value: m.id }, `${m.emoji || ""} ${m.label}`.trim())),
              h("option", { value: "custom" }, "⚙️ Custom"))
            : null,
          modelPicker,
        ),
        h("div", { className: "hcd-composer-right" },
          draftDiscardable
            ? h("button", { className: "hcd-ctl-btn hcd-draft-btn", onClick: onDiscardDraft, title: "Discard saved draft" }, "Discard draft")
            : null,
          h("button", {
            className: cn("hcd-ctl-btn", enterToSend && "on"),
            onClick: () => setEnterToSend(!enterToSend),
            title: `Enter sends: ${enterToSend ? "on" : "off (⌘⏎ sends)"}`,
            "aria-pressed": enterToSend,
          }, "⏎ send"),
        ),
      ),
      readOnly ? h("div", { className: "hcd-readonly-note" }, "Read-only conversation") : null,
    );
  }

  // ── session sidebar (folders, tags, pin/archive, bulk actions) ─────

  function SidebarRow({ s, meta, active, onOpen, onMenu, selectable, selected, onSelect }) {
    const entry = meta[s.id] || {};
    return h("div", {
      className: cn("hcd-conv-row", active && "active"),
      draggable: selectable ? "false" : "true",
      onDragStart: (e) => { try { e.dataTransfer.setData("text/hcd-session", s.id); } catch { /* noop */ } },
    },
      selectable
        ? h("input", {
            type: "checkbox",
            checked: !!selected,
            onChange: () => onSelect(s.id),
            "aria-label": `Select conversation ${s.title || s.id}`,
          })
        : null,
      h("button", {
        className: cn("hcd-conv", active && "active"),
        onClick: () => onOpen(s),
        onDragOver: (e) => e.preventDefault(),
        onDrop: (e) => e.preventDefault(),
      },
        h("span", { className: "hcd-conv-title" },
          entry.pinned ? h("span", { className: "hcd-pin-mark", title: "Pinned" }, "📌 ") : null,
          entry.starred ? h("span", { className: "hcd-star-mark", title: "Starred" }, "★ ") : null,
          s.title || "Untitled conversation"),
        h("span", { className: "hcd-conv-sub" },
          s.message_count ? `${num(s.message_count)} msg` : "",
          s.last_active || s.started_at ? ` · ${relTime(s.last_active || s.started_at)}` : "",
          s.model ? ` · ${str(s.model)}` : ""),
        s.snippet
          ? h("span", { className: "hcd-conv-snippet" }, str(s.snippet))
          : null,
        entry.tags && entry.tags.length
          ? h("span", { className: "hcd-conv-tags" }, entry.tags.slice(0, 4).map((t) => h("i", { key: t }, `#${t}`)))
          : null,
      ),
      h("button", {
        className: "hcd-conv-menu",
        onClick: () => onMenu(s),
        "aria-label": `Conversation actions for ${s.title || s.id}`,
        title: "Conversation actions",
      }, "⋯"),
    );
  }

  function SessionSidebar({
    sessions, meta, activeKey, loading, error, onRefresh, onOpen, onNew,
    query, setQuery, folders, allTags, activeTags, toggleTag,
    onRename, onDelete, onSetPinned, onSetStarred, onSetArchived,
    onSetFolder, onNewFolder, onRenameFolder, onDeleteFolder, onSetTags,
    showArchived, setShowArchived,
    bulkMode, setBulkMode, bulkSel, toggleBulk, bulkAll, bulkNone, onBulk,
    onDropToFolder,
  }) {
    const [collapsed, setCollapsed] = useState({});
    const [menuFor, setMenuFor] = useState(null);
    const [folderMenu, setFolderMenu] = useState(null);
    const sidebarRef = useRef(null);

    useEffect(() => {
      if (!menuFor && !folderMenu) return;
      const close = (e) => {
        if (sidebarRef.current && !sidebarRef.current.contains(e.target)) {
          setMenuFor(null);
          setFolderMenu(null);
        }
      };
      document.addEventListener("mousedown", close);
      return () => document.removeEventListener("mousedown", close);
    }, [menuFor, folderMenu]);

    const entryFor = (s) => meta[s.id] || {};
    const visible = sessions.filter((s) => {
      const e = entryFor(s);
      if (!showArchived && e.archived) return false;
      if (showArchived && !e.archived) return false;
      if (activeTags.length && !activeTags.every((t) => (e.tags || []).includes(t))) return false;
      return true;
    });

    // Folders view: pinned first, then folder sections, then unfiled.
    const pinned = visible.filter((s) => entryFor(s).pinned);
    const unfiled = visible.filter((s) => !entryFor(s).pinned && !entryFor(s).folder);
    const byFolder = new Map();
    for (const s of visible) {
      const f = entryFor(s).folder;
      if (!f || entryFor(s).pinned) continue;
      if (!byFolder.has(f)) byFolder.set(f, []);
      byFolder.get(f).push(s);
    }
    for (const f of folders) if (!byFolder.has(f.name)) byFolder.set(f.name, []);

    const renderRows = (rows) =>
      rows.map((s) =>
        h(SidebarRow, {
          key: s.id, s, meta, active: activeKey === s.id || activeKey === s.title,
          onOpen, selectable: bulkMode, selected: bulkSel.includes(s.id), onSelect: toggleBulk,
          onMenu: (sess) => { setMenuFor(sess); setFolderMenu(null); },
        }));

    const folderHeader = (name, count) =>
      h("div", {
        key: `f:${name}`,
        className: cn("hcd-folder", collapsed[name] && "collapsed"),
        onDragOver: (e) => { e.preventDefault(); e.currentTarget.classList.add("over"); },
        onDragLeave: (e) => e.currentTarget.classList.remove("over"),
        onDrop: (e) => {
          e.preventDefault();
          e.currentTarget.classList.remove("over");
          const sid = e.dataTransfer.getData("text/hcd-session");
          if (sid && onDropToFolder) onDropToFolder(sid, name);
        },
      },
        h("button", {
          className: "hcd-folder-head",
          onClick: () => setCollapsed((c) => ({ ...c, [name]: !c[name] })),
          "aria-expanded": !collapsed[name],
        },
          h("span", { className: "hcd-folder-caret" }, collapsed[name] ? "▸" : "▾"),
          h("span", { className: "hcd-folder-name" }, "📁 ", name),
          h("small", null, `${count}`)),
        h("button", {
          className: "hcd-folder-menu",
          onClick: (e) => { e.stopPropagation(); setFolderMenu(name); setMenuFor(null); },
          "aria-label": `Folder actions for ${name}`,
        }, "⋯"),
        folderMenu === name
          ? h("div", { className: "hcd-pop-menu" },
              h("button", { onClick: () => { onRenameFolder(name); setFolderMenu(null); } }, "Rename folder…"),
              h("button", { onClick: () => { onDeleteFolder(name); setFolderMenu(null); } }, "Delete folder"),
            )
          : null);

    const menu = menuFor
      ? h("div", { className: "hcd-pop-menu hcd-conv-actions" },
          h("button", { onClick: () => { onRename(menuFor); setMenuFor(null); } }, "Rename…"),
          h("button", { onClick: () => { onSetPinned(menuFor, !entryFor(menuFor).pinned); setMenuFor(null); } },
            `${entryFor(menuFor).pinned ? "Unpin" : "Pin"} conversation`),
          h("button", { onClick: () => { onSetStarred(menuFor, !entryFor(menuFor).starred); setMenuFor(null); } },
            `${entryFor(menuFor).starred ? "Unstar" : "Star"}`),
          h("button", { onClick: () => { onSetTags(menuFor); setMenuFor(null); } }, "Tags…"),
          h("div", { className: "hcd-menu-sep" }),
          h("div", { className: "hcd-menu-label" }, "Move to folder"),
          h("button", { onClick: () => { onSetFolder(menuFor, ""); setMenuFor(null); } }, "— None —"),
          folders.map((f) =>
            h("button", {
              key: f.name,
              onClick: () => { onSetFolder(menuFor, f.name); setMenuFor(null); },
              className: entryFor(menuFor).folder === f.name ? "check" : "",
            }, `📁 ${f.name}`)),
          h("button", { onClick: () => { setMenuFor(null); onNewFolder(); } }, "＋ New folder…"),
          h("div", { className: "hcd-menu-sep" }),
          h("button", { onClick: () => { onSetArchived(menuFor, !entryFor(menuFor).archived); setMenuFor(null); } },
            entryFor(menuFor).archived ? "Unarchive" : "Archive"),
          h("button", { className: "danger", onClick: () => { onDelete(menuFor); setMenuFor(null); } }, "Delete…"),
        )
      : null;

    return h("aside", { className: "hcd-sidebar", ref: sidebarRef },
      h("div", { className: "hcd-side-top" },
        h("button", { className: "hcd-new", onClick: onNew, "aria-label": "New conversation" }, "＋ New conversation"),
        h("div", { className: "hcd-search" },
          h("input", {
            value: query,
            onChange: (e) => setQuery(e.target.value),
            placeholder: "Search conversations…",
            "aria-label": "Search conversations",
          }),
          h("button", { className: "hcd-refresh", onClick: onRefresh, disabled: loading, title: "Refresh", "aria-label": "Refresh conversation list" }, "⟳"),
        ),
        allTags.length
          ? h("div", { className: "hcd-tag-filter" },
              allTags.slice(0, 12).map((t) =>
                h("button", {
                  key: t,
                  className: cn("hcd-chip", activeTags.includes(t) && "on"),
                  onClick: () => toggleTag(t),
                  "aria-pressed": activeTags.includes(t),
                }, `#${t}`)))
          : null,
        h("div", { className: "hcd-side-tools" },
          h("button", {
            className: cn("hcd-ctl-btn", bulkMode && "on"),
            onClick: () => { setBulkMode(!bulkMode); if (bulkMode) bulkNone(); },
            "aria-pressed": bulkMode,
            title: "Select multiple conversations for bulk actions",
          }, bulkMode ? "✓ Selecting" : "☐ Select"),
          h("button", {
            className: cn("hcd-ctl-btn", showArchived && "on"),
            onClick: () => setShowArchived(!showArchived),
            "aria-pressed": showArchived,
          }, "🗄 Archive"),
          h("button", { className: "hcd-ctl-btn", onClick: onNewFolder, title: "New folder" }, "📁+"),
        ),
        bulkMode
          ? h("div", { className: "hcd-bulk-bar" },
              h("small", null, `${bulkSel.length} selected`),
              h("button", { onClick: bulkAll }, "All"),
              h("button", { onClick: bulkNone }, "None"),
              h("button", { onClick: () => onBulk({ pinned: true }) }, "Pin"),
              h("button", { onClick: () => onBulk({ archived: true }) }, "Archive"),
              h("button", { onClick: () => onBulk({ archived: false }) }, "Unarchive"),
              h("button", { className: "danger", onClick: () => onBulk({ __delete: true }) }, "Delete…"),
            )
          : null,
        error ? h("div", { className: "hcd-side-error" }, str(error)) : null,
      ),
      h(
        "div",
        { className: "hcd-convs" },
        loading && !sessions.length
          ? h("div", { className: "hcd-loading" }, "Loading conversations…")
          : !visible.length && !folders.length
            ? h(Empty, {
                icon: "💬",
                title: query || activeTags.length ? "No conversations match" : showArchived ? "Archive is empty" : "No conversations yet",
                sub: query || activeTags.length || showArchived ? "Try a different search or clear the filters." : "Start a new conversation to see it here.",
              })
            : h(React.Fragment, null,
                pinned.length ? h("div", { className: "hcd-side-group" }, h("h5", null, "📌 Pinned"), renderRows(pinned)) : null,
                Array.from(byFolder.entries()).map(([name, rows]) => h(React.Fragment, { key: name },
                  folderHeader(name, rows.length),
                  !collapsed[name] && rows.length ? h("div", { className: "hcd-folder-body" }, renderRows(rows)) : null)),
                unfiled.length ? h("div", { className: "hcd-side-group" }, h("h5", null, folders.length ? "Unfiled" : "Recent"), renderRows(unfiled)) : null,
              ),
        menu,
      ),
    );
  }

  // ── right panel: activity + info ────────────────────────────────────

  function ActivityPanel({ toolEvents, agents, onInterrupt, generating, delegation, onDelegationPause, trees, onLoadTree, busyTree }) {
    const liveCount = Object.values(agents).filter((a) => a.status !== "complete").length;
    return h("div", { className: "hcd-panel-body" },
      h("div", { className: "hcd-info-block" },
        h("h4", null, "Tools & activity"),
        generating ? h("p", { className: "hcd-hint ok" }, "● Agent working — steer or interrupt any time") : null,
        toolEvents.length
          ? h("div", { className: "hcd-activity-list" }, toolEvents.slice(-60).map((t) => h(ToolCard, { key: t.key, tool: t.tool, defaultOpen: false })))
          : h("p", { className: "hcd-hint" }, "No tool activity in this turn yet."),
      ),
      liveCount || Object.keys(agents).length
        ? h("div", { className: "hcd-info-block" },
            h("h4", null, "🤖 Subagents"),
            h(SubagentTree, { agents, onInterrupt, live: true }),
          )
        : null,
      delegation && (delegation.paused !== undefined || (delegation.agents && delegation.agents.length))
        ? h("div", { className: "hcd-info-block" },
            h("h4", null, "⚡ Delegation"),
            delegation.paused !== undefined
              ? h("p", { className: cn("hcd-hint", delegation.paused ? "warn" : "ok") },
                  delegation.paused ? "Paused — background agents idle" : "Active — background agents may run between turns",
                  " ",
                  h("button", { className: "hcd-ctl-btn", onClick: onDelegationPause }, delegation.paused ? "Resume" : "Pause"))
              : null,
            delegation.agents && delegation.agents.length
              ? h("ul", { className: "hcd-delegation-list" }, delegation.agents.slice(0, 20).map((a, i) =>
                  h("li", { key: i }, `${str(a.status || a.state || "?")} · ${str(a.goal || a.description || a.name || a.id || "agent")}`)))
              : null,
          )
        : null,
      trees && trees.length
        ? h("div", { className: "hcd-info-block" },
            h("h4", null, "🌳 Saved agent trees"),
            h("p", { className: "hcd-hint" }, "Replay a previous run's subagent structure."),
            trees.slice(0, 12).map((t) =>
              h("button", {
                key: t.id || t.key || t.created_at,
                className: "hcd-tree-replay",
                disabled: !!busyTree,
                onClick: () => onLoadTree(t),
              },
                h("span", null, str(t.label || t.title || t.id || "tree")),
                h("small", null, t.created_at ? fmtTime(t.created_at) : ""),
              )),
          )
        : null,
    );
  }

  function InfoPanel({
    session, meta, onSetStarred, onSetPinned, onSetTags, onRename, onShare,
    onExport, onCompact, onUndo, onDeleteSession, readOnly, tree, onOpenSession,
    usageSummary, onRefreshUsage, sessionUsage,
  }) {
    const e = (session && meta[session.id]) || {};
    const sid = session && (session.id || "");
    return h("div", { className: "hcd-panel-body" },
      session
        ? h("div", { className: "hcd-info-block" },
            h("h4", null, "Conversation"),
            h("div", { className: "hcd-kv" },
              h("span", null, "Title"), h("b", null, str(session.title || "Untitled"))),
              h("div", { className: "hcd-kv" },
                h("span", null, "Started"), h("b", null, fmtTime(session.started_at))),
              h("div", { className: "hcd-kv" },
                h("span", null, "Messages"), h("b", null, String(num(session.message_count)))),
              h("div", { className: "hcd-kv" },
                h("span", null, "Source"), h("b", null, str(session.source || ""))),
              h("div", { className: "hcd-kv" },
                h("span", null, "Model"), h("b", null, str(session.model || "—"))),
            readOnly ? h("p", { className: "hcd-hint warn" }, "Read-only — the underlying session is not resumable.") : null,
            tree && (tree.parent || (tree.children && tree.children.length))
              ? h("div", { className: "hcd-lineage" },
                  tree.parent
                    ? h("div", { className: "hcd-kv" },
                        h("span", null, "Branched from"),
                        h("button", { className: "link", onClick: () => onOpenSession(tree.parent.id) }, str(tree.parent.title || tree.parent.id)))
                    : null,
                  tree.children && tree.children.length
                    ? h("div", { className: "hcd-kv hcd-kv-col" },
                        h("span", null, `Branches (${tree.children.length})`),
                        h("div", { className: "hcd-branch-list" }, tree.children.slice(0, 12).map((c) =>
                          h("button", { key: c.id, className: "link", onClick: () => onOpenSession(c.id) },
                            `${str(c.title || c.id)} · ${relTime(c.started_at)}`))))
                    : null)
              : null,
            h("div", { className: "hcd-info-actions" },
              h("button", { onClick: () => onSetStarred(session, !e.starred) }, e.starred ? "★ Unstar" : "☆ Star"),
              h("button", { onClick: () => onSetPinned(session, !e.pinned) }, e.pinned ? "📌 Unpin" : "📌 Pin"),
              h("button", { onClick: () => onSetTags(session) }, "🏷 Tags…"),
              h("button", { onClick: () => onRename(session) }, "Rename…"),
              h("button", { onClick: onShare }, "🔗 Share…"),
              h("button", { onClick: onCompact, disabled: readOnly }, "📦 Compact"),
              h("button", { onClick: onUndo, disabled: readOnly }, "↩ Undo turn"),
              h("button", { onClick: onExport }, "⬇ Export"),
              h("button", { className: "danger", onClick: onDeleteSession }, "🗑 Delete…"),
            ))
        : h("p", { className: "hcd-hint" }, "Open a conversation to see details."),
      h("div", { className: "hcd-info-block" },
        h("h4", null, "Context"),
        sessionUsage && (sessionUsage.context_used || sessionUsage.total)
          ? h("div", { className: "hcd-kv" },
              h("span", null, "Tokens in context"),
              h("b", null, `${fmtTokens(sessionUsage.context_used || sessionUsage.total)}${sessionUsage.context_max ? ` / ${fmtTokens(sessionUsage.context_max)}` : ""}`))
          : h("p", { className: "hcd-hint" }, "Context usage appears after the first turn."),
      ),
      usageSummary
        ? h("div", { className: "hcd-info-block" },
            h("h4", null, "📈 Usage (last 14 days)"),
            h("button", { className: "hcd-ctl-btn", onClick: onRefreshUsage }, "Refresh"),
            usageSummary.per_day && usageSummary.per_day.length
              ? h("table", { className: "hcd-usage-table" },
                  h("thead", null, h("tr", null, h("th", null, "Day"), h("th", null, "In"), h("th", null, "Out"), h("th", null, "Chats"))),
                  h("tbody", null, usageSummary.per_day.slice(-7).map((d) =>
                    h("tr", { key: d.key }, h("td", null, str(d.key)), h("td", null, fmtTokens(d.input)), h("td", null, fmtTokens(d.output)), h("td", null, String(num(d.sessions)))))))
              : h("p", { className: "hcd-hint" }, "No usage recorded yet."),
            usageSummary.per_model && usageSummary.per_model.length
              ? h("ul", { className: "hcd-usage-models" }, usageSummary.per_model.slice(0, 6).map((m) =>
                  h("li", { key: m.key }, `${str(m.key)} — ${fmtTokens(m.input + m.output)} tok (${num(m.sessions)} chats)`)))
              : null,
          )
        : null,
    );
  }

  // ── settings modal ──────────────────────────────────────────────────

  function SettingsModal({ settings, onChange, onClose, onReset }) {
    const set = (patch) => onChange(patch);
    const Toggle = ({ label, k, hint }) =>
      h("label", { className: "hcd-setting-row" },
        h("span", null, h("b", null, label), hint ? h("small", null, hint) : null),
        h("input", { type: "checkbox", checked: !!settings[k], onChange: (e) => set({ [k]: e.target.checked }), "aria-label": label }));
    const Choice = ({ label, k, opts }) =>
      h("label", { className: "hcd-setting-row" },
        h("span", null, h("b", null, label)),
        h("select", { value: settings[k], onChange: (e) => set({ [k]: e.target.value }), "aria-label": label },
          opts.map(([v, l]) => h("option", { key: v, value: v }, l))));
    return h(Modal, { title: "Chat settings", onClose, wide: true },
      h("div", { className: "hcd-settings" },
        h("h5", null, "Interface"),
        h(Toggle, { label: "Enter sends message", k: "enterToSend", hint: "Off = ⌘/Ctrl+Enter sends" }),
        h(Toggle, { label: "Auto-scroll while streaming", k: "autoScroll" }),
        h(Toggle, { label: "Show timestamps", k: "showTimestamps" }),
        h(Toggle, { label: "Show token usage per reply", k: "showUsage" }),
        h(Choice, { label: "Density", k: "density", opts: [["comfortable", "Comfortable"], ["compact", "Compact"]] }),
        h(Choice, { label: "Message width", k: "messageWidth", opts: [["wide", "Wide"], ["narrow", "Narrow"]] }),
        h(Choice, { label: "Font size", k: "fontSize", opts: [["small", "Small"], ["medium", "Medium"], ["large", "Large"]] }),
        h("h5", null, "Behaviour"),
        h(Toggle, { label: "Confirm before deleting", k: "confirmDelete" }),
        h(Toggle, { label: "Auto-title new conversations", k: "autoTitle" }),
        h(Toggle, { label: "Auto-attach recommended tools", k: "autoTools" }),
        h(Toggle, { label: "Save conversation history", k: "saveHistory", hint: "Off = conversations stay out of the sidebar" }),
        h(Toggle, { label: "Memory (@memory)", k: "memoryEnabled" }),
        h(Toggle, { label: "Temporary chats by default", k: "temporaryDefault", hint: "Skip loading saved memories into new chats" }),
        h("div", { className: "hcd-settings-foot" },
          onReset ? h("button", { onClick: onReset }, "Reset to defaults") : null,
          h("button", { className: "primary", onClick: onClose }, "Done"))),
    );
  }

  // ── shares modal ────────────────────────────────────────────────────

  function SharesModal({ shares, onRevoke, onClose, busy }) {
    const [origin] = useState(() => { try { return location.origin; } catch { return ""; } });
    return h(Modal, { title: "Shared links", onClose, wide },
      shares && shares.length
        ? h("table", { className: "hcd-shares-table" },
            h("thead", null, h("tr", null, h("th", null, "Link"), h("th", null, "Conversation"), h("th", null, "Created"), h("th", null, ""))),
            h("tbody", null, shares.map((s) =>
              h("tr", { key: s.token, className: s.revoked ? "revoked" : "" },
                h("td", null,
                  h("input", {
                    readOnly: true,
                    value: `${origin}${BASE}/shared/${s.token}`,
                    onFocus: (e) => e.target.select(),
                    "aria-label": "Share link",
                  })),
                h("td", null, str(s.session_title || s.session_id)),
                h("td", null, fmtTime(s.created_at)),
                h("td", null,
                  s.revoked
                    ? h("span", { className: "hcd-badge error" }, "revoked")
                    : h("button", { disabled: !!busy, onClick: () => onRevoke(s.token) }, "Revoke"))))))
        : h("p", { className: "hcd-hint" }, "No share links yet. Create one from a conversation's Info panel."),
    );
  }

  // ── main component ──────────────────────────────────────────────────

  const PAGE = 200;        // transcript page size (REST)
  const RENDER_WINDOW = 400; // max messages mounted in the DOM at once
  const MAX_TOOLS = 120;   // activity list cap for the live turn

  function ChatDashboard() {
    // sidebar / registry
    const [sessions, setSessions] = useState([]);
    const [sessionsLoading, setSessionsLoading] = useState(false);
    const [sessionsError, setSessionsError] = useState("");
    const [hasMoreSessions, setHasMoreSessions] = useState(false);
    const [query, setQuery] = useState("");
    const [meta, setMeta] = useState({});
    const [folders, setFolders] = useState([]);
    const [allTags, setAllTags] = useState([]);
    const [activeTags, setActiveTags] = useState([]);
    const [showArchived, setShowArchived] = useState(false);
    const [bulkMode, setBulkMode] = useState(false);
    const [bulkSel, setBulkSel] = useState([]);

    // conversation
    const [selected, setSelected] = useState(null); // {id,title,key,readOnly,temporary}
    const [messages, setMessages] = useState([]);
    const [toolRows, setToolRows] = useState([]);   // current turn activity (ordered)
    const [agents, setAgents] = useState({});       // subagent tree (live)
    const [replayTree, setReplayTree] = useState(null);
    const [generating, setGenerating] = useState(false);
    const [prompts, setPrompts] = useState([]);
    const [streamingId, setStreamingId] = useState(null);
    const [olderOffset, setOlderOffset] = useState(0); // 0 = everything loaded
    const [renderLimit, setRenderLimit] = useState(RENDER_WINDOW);

    // composer
    const [text, setText] = useState(() => readDraft("new"));
    const [attachments, setAttachments] = useState([]);

    // infrastructure
    const [gwStatus, setGwStatus] = useState("idle");
    const [toasts, setToasts] = useState([]);
    const [usage, setUsage] = useState(null);        // session.usage for meter
    const [usageSummary, setUsageSummary] = useState(null);
    const [tree, setTree] = useState(null);          // branch lineage
    const [settings, setSettings] = useState(null);
    const [caps, setCaps] = useState(null);
    const [pinnedModels, setPinnedModels] = useState([]);
    const [modelValue, setModelValue] = useState("auto");
    const [commandCatalog, setCommandCatalog] = useState([]);
    const [delegation, setDelegation] = useState(null);
    const [spawnTrees, setSpawnTrees] = useState([]);
    const [busyTree, setBusyTree] = useState(false);
    const [goal, setGoal] = useState(null);          // {text, paused}
    const [voice, setVoice] = useState(null);
    const [toolsState, setToolsState] = useState({ open: false, enabled: [], available: [] });

    // ui
    const [panel, setPanel] = useState("activity");  // right panel tab
    const [sidebarOpen, setSidebarOpen] = useState(true);
    const [modal, setModal] = useState(null);        // {kind, ...}
    const [paletteOpen, setPaletteOpen] = useState(false);
    const [shares, setShares] = useState(null);

    const scrollRef = useRef(null);
    const stickBottom = useRef(true);
    const composerRef = useRef(null);
    const gwRef = useRef(null);
    const selRef = useRef(null);      // selected mirror for event filter
    const gwSidRef = useRef(null);    // gateway-side session id (may differ after resume)
    const pendingModelRef = useRef(null); // model chosen before a gateway session exists (applied on first send/resume)
    const generatingRef = useRef(false);
    const queryTimer = useRef(null);
    const sessionsRef = useRef([]);
    sessionsRef.current = sessions;

    selRef.current = selected;
    generatingRef.current = generating;

    const toast = useCallback((textT, kind = "info") => {
      const id = nowId();
      setToasts((ts) => [...ts.slice(-4), { id, text: textT, kind }]);
      setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), kind === "error" ? 9000 : 4500);
    }, []);

    const openModal = (m) => setModal(m);
    const closeModal = () => setModal(null);

    const confirmDialog = (title, message, confirmLabel, danger) =>
      new Promise((resolve) => {
        openModal({
          kind: "confirm",
          title, message, confirmLabel, danger,
          onConfirm: () => { closeModal(); resolve(true); },
          onCancel: () => { closeModal(); resolve(false); },
        });
      });

    const promptDialog = (title, label, initial, placeholder, confirmLabel) =>
      new Promise((resolve) => {
        openModal({
          kind: "prompt",
          title, label, initial, placeholder, confirmLabel,
          onConfirm: (v) => { closeModal(); resolve(v); },
          onCancel: () => { closeModal(); resolve(null); },
        });
      });

    // ── registry / settings / capabilities ────────────────────────────

    const loadMeta = useCallback(async () => {
      try {
        const m = await fetchJSON(`${BASE}/metadata`);
        setMeta(m || {});
        const tags = new Set();
        Object.values(m || {}).forEach((e) => (e && Array.isArray(e.tags) ? e.tags.forEach((t) => tags.add(t)) : null));
        setAllTags(Array.from(tags).sort());
      } catch { /* metadata is decorative — ignore */ }
    }, []);

    const loadFolders = useCallback(async () => {
      try {
        const f = await fetchJSON(`${BASE}/folders`);
        setFolders(f.folders || []);
      } catch { setFolders([]); }
    }, []);

    const refreshSessions = useCallback(async ({ offset = 0, q = query } = {}) => {
      setSessionsLoading(true);
      setSessionsError("");
      try {
        const res = await fetchJSON(`${BASE}/sessions?limit=120&offset=${offset}&q=${encodeURIComponent(q || "")}`);
        const rows = res.sessions || [];
        setSessions((prev) => (offset ? [...prev, ...rows] : rows));
        setHasMoreSessions(!!res.has_more);
      } catch (e) {
        setSessionsError(`History unavailable (${str(e.message || e)})`);
      } finally {
        setSessionsLoading(false);
      }
    }, [query]);

    useEffect(() => {
      (async () => {
        try {
          const s = await fetchJSON(`${BASE}/settings`);
          setSettings(s);
          setPinnedModels(Array.isArray(s.pinnedModels) ? s.pinnedModels.map(String) : []);
        } catch {
          setSettings({});
        }
        try {
          const c = await fetchJSON(`${BASE}/capabilities`);
          setCaps(c);
        } catch { setCaps({ modes: [], models: [], toolsets: [], agents: [] }); }
        refreshSessions({});
        loadMeta();
        loadFolders();
      })();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // fetch the server's slash-command catalog once the gateway is up
    useEffect(() => {
      if (gwStatus === "open") loadCommandCatalog();
    }, [gwStatus]); // eslint-disable-line react-hooks/exhaustive-deps

    // server-side search, debounced
    useEffect(() => {
      clearTimeout(queryTimer.current);
      queryTimer.current = setTimeout(() => refreshSessions({ q: query }), 250);
      return () => clearTimeout(queryTimer.current);
    }, [query, refreshSessions]);

    const patchSettings = useCallback((patch) => {
      setSettings((s) => ({ ...(s || {}), ...patch }));
      fetchJSON(`${BASE}/settings`, { method: "PUT", body: patch }).catch(() => {});
    }, []);

    // ── gateway boot + event handling ─────────────────────────────────

    useEffect(() => {
      const gw = new Gateway();
      gwRef.current = gw;
      const offStatus = gw.on("status", (ev) => setGwStatus(ev.status));
      const offEvent = gw.on("*", (ev) => handleEventRef.current(ev));
      gw.connect().catch(() => { /* surfaced via status */ });
      const onHide = () => { try { gw.ws && gw.ws.close(); } catch { /* noop */ } };
      window.addEventListener("pagehide", onHide);
      return () => {
        offStatus();
        offEvent();
        window.removeEventListener("pagehide", onHide);
        gw.dispose();
        gwRef.current = null;
      };
    }, []);

    const ownsEvent = (ev) => {
      const sid = ev.session_id || ev.sessionId || "";
      if (!sid) return true; // unscoped status events
      const sel = selRef.current;
      return sid === gwSidRef.current || (sel ? sid === sel.id : false);
    };

    const pushSystem = (line) => {
      setMessages((ms) => [...ms, { id: nowId(), role: "system", content: str(line), timestamp: Date.now() / 1000 }]);
    };

    const handleEvent = (ev) => {
      const type = str(ev.type);
      const p = ev.payload || {};
      if (!ownsEvent(ev)) return;

      switch (type) {
        case "gateway.ready":
          setGwStatus("open");
          break;
        case "session.info": {
          const sid = str(p.session_id || ev.session_id || "");
          if (sid) gwSidRef.current = sid;
          setSelected((sel) => sel && {
            ...sel,
            title: p.title || sel.title,
            model: p.model || sel.model,
          });
          if (p.usage) setUsage(p.usage);
          if (p.yolo !== undefined) setSelected((sel) => sel && { ...sel, yolo: !!p.yolo });
          break;
        }
        case "session.usage":
          if (p.usage) setUsage(p.usage);
          else if (p.total || p.context_used) setUsage(p);
          break;
        case "message.start": {
          const id = str(p.message_id) || nowId();
          setStreamingId(id);
          setMessages((ms) => [...ms, {
            id, role: "assistant", content: "", reasoning: "", streaming: true,
            timestamp: Date.now() / 1000, model: p.model || (selRef.current && selRef.current.model),
          }]);
          break;
        }
        case "message.delta": {
          const t = str(p.text || p.content || "");
          const r = str(p.reasoning || p.thinking || "");
          setMessages((ms) => {
            if (!ms.length) return ms;
            const i = ms.length - 1;
            const last = ms[i];
            if (last.role !== "assistant") return ms;
            const copy = ms.slice();
            copy[i] = { ...last, content: last.content + t, reasoning: last.reasoning + r, streaming: true };
            return copy;
          });
          break;
        }
        case "message.complete": {
          setStreamingId(null);
          setGenerating(false);
          setMessages((ms) => {
            if (!ms.length) return ms;
            const i = ms.length - 1;
            const copy = ms.slice();
            const last = copy[i];
            copy[i] = {
              ...last,
              content: p.text !== undefined ? str(p.text) : last.content,
              reasoning: p.reasoning !== undefined ? str(p.reasoning) : last.reasoning,
              streaming: false,
              status: str(p.status || "complete"),
              usage: p.usage || last.usage,
              warning: p.warning ? str(p.warning) : last.warning,
              model: (p.usage && p.usage.model) || last.model,
            };
            return copy;
          });
          if (p.status === "error") toast("The model reported an error for this reply.", "error");
          // best-effort: persist this turn's subagent tree for replay
          setAgents((current) => {
            if (Object.keys(current).length) {
              const gw = gwRef.current;
              const key = (selRef.current && selRef.current.key) || gwSidRef.current;
              if (gw && key) {
                gw.request("spawn_tree.save", { session_id: key, subagents: Object.values(current) }, 15000).catch(() => {});
              }
            }
            return current;
          });
          refreshUsage();
          refreshSessions({});
          break;
        }
        case "error": {
          const msg = str(p.message || p.error || "gateway error");
          setGenerating(false);
          setStreamingId(null);
          pushSystem(`⚠ ${msg}`);
          break;
        }
        case "tool.start":
        case "tool.begin": {
          const key = nowId();
          setToolRows((rows) => [...rows, {
            key,
            tool: {
              id: key, name: str(p.name || p.tool || "tool"), status: "running",
              context: str(p.context || p.input || ""), title: str(p.title || ""),
            },
          }].slice(-MAX_TOOLS));
          break;
        }
        case "tool.progress":
        case "tool.delta": {
          setToolRows((rows) => {
            if (!rows.length) return rows;
            const i = rows.length - 1;
            const copy = rows.slice();
            copy[i] = {
              ...copy[i],
              tool: { ...copy[i].tool, preview: str(p.preview || p.text || "").slice(0, 2000), status: "running" },
            };
            return copy;
          });
          break;
        }
        case "tool.complete": {
          setToolRows((rows) => {
            if (!rows.length) return rows;
            const i = rows.length - 1;
            const copy = rows.slice();
            copy[i] = {
              ...copy[i],
              tool: {
                ...copy[i].tool,
                status: p.status === "error" || p.error ? "error" : "done",
                summary: str(p.summary || "").slice(0, 400),
                duration_s: num(p.duration_s || p.duration_seconds),
                todos: Array.isArray(p.todos) ? p.todos : null,
                inline_diff: p.inline_diff ? str(p.inline_diff) : "",
                error: p.error ? str(p.error) : null,
              },
            };
            return copy;
          });
          break;
        }
        case "approval.request":
          setPrompts((ps) => [...ps.filter((x) => x.kind !== "approval"), { kind: "approval", ...p, request_id: str(p.request_id || nowId()) }]);
          break;
        case "clarify.request":
          setPrompts((ps) => [...ps, { kind: "clarify", ...p, request_id: str(p.request_id || nowId()) }]);
          break;
        case "sudo.request":
          setPrompts((ps) => [...ps, { kind: "sudo", ...p, request_id: str(p.request_id || nowId()) }]);
          break;
        case "secret.request":
          setPrompts((ps) => [...ps, { kind: "secret", ...p, request_id: str(p.request_id || nowId()) }]);
          break;
        case "approval.resolved":
        case "clarify.resolved":
        case "sudo.expire":
        case "sudo.expired":
        case "secret.expire":
        case "secret.expired": {
          const rid = str(p.request_id || "");
          setPrompts((ps) => ps.filter((x) => !rid || x.request_id !== rid));
          break;
        }
        case "status.update":
          if (str(p.kind) === "goal" || p.goal || p.paused !== undefined) {
            setGoal({ text: str(p.goal || p.text || ""), paused: !!p.paused });
          }
          break;
        case "voice.status":
          setVoice({ state: str(p.status || p.state || "unknown"), detail: str(p.detail || "") });
          break;
        case "voice.transcript":
          setVoice((v) => ({ state: (v && v.state) || "listening", detail: str(p.text || "") }));
          break;
        default:
          if (type.startsWith("subagent.")) {
            setAgents((current) => applySubagentEvent(current, { type, payload: p, session_id: ev.session_id }));
          }
          break;
      }
    };
    const handleEventRef = useRef(handleEvent);
    handleEventRef.current = handleEvent;

    // ── usage / context ──────────────────────────────────────────────

    const refreshUsage = useCallback(() => {
      const gw = gwRef.current;
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!gw || gw.status !== "open" || !sid) return;
      gw.request("session.usage", { session_id: sid }, 20000).then((res) => {
        const u = (res && (res.usage || res)) || null;
        if (u && (u.total || u.context_used)) setUsage(u);
      }).catch(() => {});
    }, []);

    const refreshUsageSummary = useCallback(() => {
      fetchJSON(`${BASE}/usage?days=14`).then(setUsageSummary).catch(() => setUsageSummary(null));
    }, []);

    useEffect(() => {
      if (panel !== "info") return;
      refreshUsageSummary();
    }, [panel, refreshUsageSummary]);

    // periodic context refresh while a conversation is open
    useEffect(() => {
      if (!selected) return;
      const t = setInterval(refreshUsage, 45000);
      return () => clearInterval(t);
    }, [selected, refreshUsage]);

    // ── session lifecycle ────────────────────────────────────────────

    const newChat = useCallback(() => {
      setSelected(null);
      setMessages([]);
      setToolRows([]);
      setAgents({});
      setReplayTree(null);
      setPrompts([]);
      setUsage(null);
      setTree(null);
      setOlderOffset(0);
      setRenderLimit(RENDER_WINDOW);
      setGoal(null);
      gwSidRef.current = null;
      pendingModelRef.current = null;
      setAttachments([]);
      try { composerRef.current && composerRef.current.focus(); } catch { /* noop */ }
    }, []);

    const openSession = useCallback(async (s) => {
      const sid = typeof s === "string" ? s : s.id;
      const row = typeof s === "string" ? sessions.find((x) => x.id === sid) || { id: sid } : s;
      stickBottom.current = true;
      setToolRows([]);
      setAgents({});
      setReplayTree(null);
      setPrompts([]);
      setUsage(null);
      setGoal(null);
      setBulkMode(false);
      setBulkSel([]);
      setAttachments([]);
      setOlderOffset(0);
      setRenderLimit(RENDER_WINDOW);
      setSessionsError("");
      pendingModelRef.current = null;
      // newest page first; older pages load on demand when scrolling up
      try {
        const res = await fetchJSON(`${BASE}/sessions/${encodeURIComponent(sid)}?limit=${PAGE}&offset=-${PAGE}`);
        setSelected({ id: sid, title: res.session && res.session.title, key: sid, readOnly: false, model: res.session && res.session.model });
        setMessages(res.messages || []);
        setOlderOffset(num(res.offset) > 0 ? num(res.offset) : 0);
      } catch (e) {
        setSelected({ id: sid, title: row.title, key: sid, readOnly: false });
        setMessages([]);
        toast(`Could not load transcript (${str(e.message || e)})`, "error");
      }
      // resume through the gateway when it is up so new turns append to the
      // stored history; re-resume every open — session ids are never cached
      // across reconnects (the server finalizes sessions on disconnect).
      const gw = gwRef.current;
      if (gw) {
        gw.connect().then(() =>
          gw.request("session.resume", { session_id: sid }, 60000).then((res) => {
            gwSidRef.current = str((res && res.session_id) || sid);
            if (res && res.usage) setUsage(res.usage);
            if (res && res.title) setSelected((sel) => sel && { ...sel, title: res.title });
          }).catch(() => {
            // not resumable (e.g. finalized child / compressed continuation)
            setSelected((sel) => sel && { ...sel, readOnly: true });
          }),
        ).catch(() => {});
      }
      try {
        fetchJSON(`${BASE}/sessions/${encodeURIComponent(sid)}/tree`).then(setTree).catch(() => setTree(null));
      } catch { /* optional */ }
      try { composerRef.current && composerRef.current.focus(); } catch { /* noop */ }
    }, [sessions, toast]);

    // A model chosen while no live gateway session existed yet (a brand-new
    // chat, or a conversation opened but not yet resumed) is queued in
    // pendingModelRef.  config.set {key:"model", session_id} only switches a
    // live session on the pinned v2026.5.7 gateway, so the pending choice is
    // flushed here the moment the conversation gets its gateway session id —
    // before the first prompt.submit runs, so the first reply already uses it.
    const flushPendingModel = useCallback(async (sid) => {
      const want = pendingModelRef.current;
      if (!want || want === "auto") return;
      const gw = gwRef.current;
      if (!gw || !sid) return;
      try {
        await gw.request("config.set", { key: "model", value: want, session_id: sid }, 30000);
        pendingModelRef.current = null;
        setSelected((s) => s && { ...s, model: want });
      } catch (e) {
        // best-effort: don't block the send; leave the toast + model picker
        // state to surface the failure to the user.
        const m = str((e && e.message) || e);
        if (!/busy|4009/i.test(m)) pendingModelRef.current = null;
      }
    }, []);

    const ensureSession = useCallback(async () => {
      const sel = selRef.current;
      if (sel) {
        if (gwSidRef.current) {
          await flushPendingModel(gwSidRef.current);
          return { session_id: gwSidRef.current, key: sel.key || sel.id };
        }
        const gw = gwRef.current;
        if (!gw) throw new Error("gateway is not connected");
        await gw.connect();
        try {
          const res = await gw.request("session.resume", { session_id: sel.id }, 60000);
          gwSidRef.current = str((res && res.session_id) || sel.id);
          await flushPendingModel(gwSidRef.current);
          return { session_id: gwSidRef.current, key: sel.key || sel.id };
        } catch {
          setSelected((s) => s && { ...s, readOnly: true });
          throw new Error("This conversation cannot be continued (session is closed).");
        }
      }
      const gw = gwRef.current;
      if (!gw) throw new Error("gateway is not connected");
      await gw.connect();
      const params = { background: false };
      if (settings && settings.temporaryDefault) params.temporary = true;
      let res;
      try {
        res = await gw.request("session.create", params, 60000);
      } catch (e) {
        if (params.temporary === undefined) throw e;
        res = await gw.request("session.create", { background: false }, 60000);
      }
      const sid = str((res && (res.session_id || res.id)) || "");
      if (!sid) throw new Error("gateway did not return a session id");
      gwSidRef.current = sid;
      const title = str((res && res.title) || "New conversation");
      setSelected({ id: sid, key: sid, title, readOnly: false, model: (res && res.model) || "" });
      await flushPendingModel(sid);
      return { session_id: sid, key: sid };
    }, [settings, flushPendingModel]);

    // ── attachments ──────────────────────────────────────────────────

    const onAttach = useCallback(async (file) => {
      const local = {
        id: nowId(),
        name: str(file && file.name) || "file",
        size: num(file && file.size),
        status: "uploading",
        is_image: /^image\//.test(str(file && file.type)),
      };
      try {
        local.thumb = URL.createObjectURL(file);
      } catch { /* object URLs are decorative */ }
      setAttachments((a) => [...a, local]);
      const fd = new FormData();
      fd.append("file", file);
      fd.append("conversation_id", (selRef.current && (selRef.current.key || selRef.current.id)) || "");
      try {
        const res = await fetchJSON(`${BASE}/attachments`, { method: "POST", body: fd });
        setAttachments((a) => a.map((x) => (x.id === local.id ? { ...x, ...res, status: "ready" } : x)));
      } catch (e) {
        setAttachments((a) => a.map((x) => (x.id === local.id ? { ...x, status: "error", error: str(e.message || e) } : x)));
        toast(`Upload failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    const removeAttachment = useCallback((att) => {
      setAttachments((a) => a.filter((x) => x.id !== att.id));
      try { att.thumb && URL.revokeObjectURL(att.thumb); } catch { /* noop */ }
    }, []);

    // ── sending / steering ───────────────────────────────────────────

    const send = useCallback(async (value, { background = false } = {}) => {
      const body = String(value || "").trim();
      if (!body) return;
      const ready = attachments.filter((a) => a.status === "ready");
      let textOut = body;
      if (ready.length) {
        textOut = `${body}\n\n${ready.map((a) => str(a.prompt_reference || a.path || a.name)).join("\n")}`.trim();
      }
      let created = null;
      setGenerating(true);
      setToolRows([]);
      setAgents({});
      setReplayTree(null);
      stickBottom.current = true;
      const userMsg = { id: nowId(), role: "user", content: body, timestamp: Date.now() / 1000 };
      if (ready.length) userMsg.attachments = ready.map((a) => ({ name: a.name, size: a.size, is_image: !!a.is_image }));
      setMessages((ms) => [...ms, userMsg]);
      try {
        created = await ensureSession();
        // images ride the gateway's attachment pipeline when it is available;
        // failures degrade to the @path references already in the text
        for (const att of ready.filter((a) => a.is_image && a.path)) {
          try {
            await gwRef.current.request("image.attach", { session_id: created.session_id, path: att.path }, 30000);
          } catch { /* @path reference remains */ }
        }
        await gwRef.current.request(
          background ? "prompt.background" : "prompt.submit",
          { session_id: created.session_id, text: textOut },
          60000,
        );
        setAttachments((cur) => {
          cur.forEach((a) => { try { a.thumb && URL.revokeObjectURL(a.thumb); } catch { /* noop */ } });
          return [];
        });
      } catch (e) {
        // RESTORE the draft — a failed submit must never eat the user's text
        setMessages((ms) => ms.filter((m) => m.id !== userMsg.id));
        setGenerating(false);
        setText((cur) => (cur.trim() ? `${body}\n\n${cur}` : body));
        writeDraft(selRef.current ? selRef.current.key || selRef.current.id : "new", body);
        toast(`Send failed: ${str(e.message || e)}`, "error");
        return;
      }
      if (background) {
        setGenerating(false);
        pushSystem("Prompt queued in the background — it will continue even if you switch tabs.");
      }
    }, [attachments, ensureSession, toast]);

    const steer = useCallback(async (value) => {
      const body = String(value || "").trim();
      if (!body) return;
      const sid = gwSidRef.current;
      if (!sid) { toast("No live turn to steer.", "error"); return; }
      try {
        const res = await gwRef.current.request("session.steer", { session_id: sid, text: body }, 30000);
        if (res && res.status === "rejected") toast("Steer rejected — the turn is already finishing.", "error");
        else pushSystem(`🧭 Steer queued: ${body}`);
      } catch (e) {
        toast(`Steer failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    const interrupt = useCallback(async () => {
      const sid = gwSidRef.current;
      if (!sid) return;
      try {
        await gwRef.current.request("session.interrupt", { session_id: sid }, 30000);
        toast("Stop requested — finishing up…");
      } catch (e) {
        toast(`Stop failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    const choiceOf = (c) => (["once", "session", "always"].includes(c) ? c : "deny");

    const respondPrompt = useCallback(async (kind, promptP, answerValue) => {
      const sid = gwSidRef.current;
      if (!sid) { toast("Gateway session is not active.", "error"); return; }
      const gw = gwRef.current;
      // v2026.5.7 _respond: each prompt flavour takes its own parameter name
      // (clarify→answer, sudo→password, secret→value, approval→choice).
      const methodMap = {
        clarify: "clarify.respond",
        sudo: "sudo.respond",
        secret: "secret.respond",
        approval: "approval.respond",
      };
      const method = methodMap[kind] || "clarify.respond";
      const params = { session_id: sid };
      if (promptP && promptP.request_id) params.request_id = promptP.request_id;
      if (kind === "clarify") params.answer = answerValue;
      else if (kind === "sudo") params.password = answerValue;
      else if (kind === "secret") {
        params.value = answerValue;
        if (answerValue === "__skip__") { params.value = ""; params.skip = true; }
      } else {
        // approval choices: once / session / always map to the gateway's
        // allow scopes; anything unknown resolves to deny (safe default).
        const choice = choiceOf(answerValue);
        params.choice = choice;
        if (promptP && promptP.all) params.all = true;
      }
      setPrompts((ps) => ps.filter((x) => x !== promptP && x.request_id !== (promptP && promptP.request_id)));
      try {
        await gw.request(method, params, 30000);
        if (kind === "approval") pushSystem(params.choice === "deny" ? "🚫 Action denied" : "✅ Action approved");
      } catch (e) {
        toast(`${kind} respond failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    // ── turn actions (retry / regenerate / continue / edit) ───────────

    const submitGatewayText = useCallback(async (textOut, extra = {}) => {
      const created = await ensureSession();
      setGenerating(true);
      stickBottom.current = true;
      try {
        await gwRef.current.request("prompt.submit", {
          session_id: created.session_id,
          text: textOut,
          ...extra,
        }, 60000);
      } catch (e) {
        setGenerating(false);
        toast(`Request failed: ${str(e.message || e)}`, "error");
      }
    }, [ensureSession, toast]);

    const runRetry = useCallback(async () => {
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!sid) { toast("Open a conversation first.", "error"); return; }
      try {
        const res = await gwRef.current.request("command.dispatch", { name: "retry", session_id: sid }, 30000);
        const type = str(res && res.type);
        if (type === "send" && res.message) {
          if (res.notice) pushSystem(res.notice);
          setToolRows([]);
          setAgents({});
          await submitGatewayText(str(res.message));
        } else if (type === "exec") {
          pushSystem(str(res.output || "retry: nothing to redo"));
        } else {
          toast("Nothing to retry yet.", "error");
        }
        refreshUsage();
      } catch (e) {
        toast(`Retry failed: ${str(e.message || e)}`, "error");
      }
    }, [submitGatewayText, refreshUsage, toast]);

    const runUndo = useCallback(async () => {
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!sid) { toast("Open a conversation first.", "error"); return; }
      try {
        await gwRef.current.request("session.undo", { session_id: sid }, 30000);
        pushSystem("↩ Last turn undone.");
        await reloadTranscript();
        refreshUsage();
      } catch (e) {
        toast(`Undo failed: ${str(e.message || e)}`, "error");
      }
    }, [toast, refreshUsage]);

    const reloadTranscript = useCallback(async () => {
      const sel = selRef.current;
      if (!sel) return;
      try {
        const res = await fetchJSON(`${BASE}/sessions/${encodeURIComponent(sel.id)}?limit=${PAGE}&offset=-${PAGE}`);
        setMessages(res.messages || []);
        setOlderOffset(num(res.offset) > 0 ? num(res.offset) : 0);
      } catch { /* keep what we have */ }
    }, []);

    const compact = useCallback(async (focusTopic) => {
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!sid) { toast("Open a conversation first.", "error"); return; }
      try {
        const params = { session_id: sid };
        if (focusTopic) params.focus_topic = focusTopic;
        const res = await gwRef.current.request("session.compress", params, 120000);
        const before = fmtTokens(res && res.before) || "?";
        const after = fmtTokens(res && res.after) || "?";
        pushSystem(`📦 Context compacted (${before} → ${after} tokens)${res && res.summary ? ` — ${str(res.summary)}` : ""}`);
        refreshUsage();
      } catch (e) {
        toast(`Compact failed: ${str(e.message || e)}`, "error");
      }
    }, [toast, refreshUsage]);

    const requestCompact = useCallback(async (initialTopic) => {
      const topic = await promptDialog("Compact context", "Optional focus topic (kept in detail, e.g. “the auth refactor”)", initialTopic || "", "Leave empty to keep recent turns", "Compact");
      if (topic === null) return;
      compact(topic || undefined);
    }, [compact]);

    // ── message actions ──────────────────────────────────────────────

    const onMessageAction = useCallback(async (action, msg, index) => {
      const copyText = () => {
        try {
          navigator.clipboard.writeText(str(msg.content));
          toast("Copied to clipboard");
        } catch { toast("Clipboard unavailable", "error"); }
      };
      if (action === "copy") return copyText();
      if (action === "edit") { setText(str(msg.content)); try { composerRef.current && composerRef.current.focus(); } catch { /* noop */ } return; }
      if (action === "editRetry") {
        const edited = await promptDialog("Edit & retry", "Edit your message — the conversation is rewound to this point and the edit resubmitted", str(msg.content), "Your edited message", "Resend");
        if (!edited) return;
        const created = await ensureSession();
        setGenerating(true);
        stickBottom.current = true;
        try {
          await gwRef.current.request("prompt.submit", {
            session_id: created.session_id,
            text: edited,
            confirm_truncate: true,
            confirm_empty_truncate: true,
            ...(msg.id && /^\d+$/.test(String(msg.id)) ? { truncate_before_row_id: num(msg.id) } : {}),
          }, 60000);
          setToolRows([]);
          setAgents({});
        } catch (e) {
          setGenerating(false);
          toast(`Edit & retry failed: ${str(e.message || e)} — try “Edit”, then resend.`, "error");
        }
        return;
      }
      if (action === "regenerate") return runRetry();
      if (action === "continue") return submitGatewayText("Continue.");
      if (action === "branch") {
        try {
          const res = await fetchJSON(`${BASE}/branch`, {
            method: "POST",
            body: { session_id: selRef.current.id, message_index: index, title: `${(selRef.current.title || "Conversation").slice(0, 60)} (branch)` },
          });
          toast(`Branched → ${res.session_id}`);
          refreshSessions({});
          openSession(res.session_id);
        } catch (e) {
          toast(`Branch failed: ${str(e.message || e)}`, "error");
        }
        return;
      }
      if (action === "delete") {
        const lastUserIdx = (() => {
          let idx = -1;
          messages.forEach((m, i) => { if (m.role === "user") idx = i; });
          return idx;
        })();
        if (index < lastUserIdx) {
          toast("Only the latest turn can be removed (use Undo turn, or Branch to keep a copy).", "error");
          return;
        }
        const ok = settings && settings.confirmDelete ? await confirmDialog("Delete latest turn?", "This removes your last message and Hermes' reply.", "Delete turn", true) : true;
        if (!ok) return;
        runUndo();
      }
    }, [messages, settings, ensureSession, submitGatewayText, runUndo, runRetry, refreshSessions, openSession, toast]);

    // ── slash-command dispatch ───────────────────────────────────────

    const loadCommandCatalog = useCallback(async () => {
      const gw = gwRef.current;
      if (!gw || commandCatalog.length) return;
      try {
        const res = await gw.request("commands.catalog", {}, 20000);
        const flat = [];
        const groups = (res && (res.categories || res.groups)) || res || {};
        if (Array.isArray(groups)) {
          groups.forEach((c) => (c.commands || []).forEach((cmd) => flat.push(cmd)));
        } else if (typeof groups === "object") {
          Object.values(groups).forEach((cmds) => (Array.isArray(cmds) ? cmds : cmds && cmds.commands ? cmds.commands : []).forEach((cmd) => flat.push(cmd)));
        }
        if (flat.length) setCommandCatalog(flat.map((c) => ({ name: str(c.name || c.command || ""), description: str(c.description || c.help || "") })).filter((c) => c.name));
      } catch { /* fallback list already in place */ }
    }, [commandCatalog.length]);

    const dispatchCommand = useCallback(async (raw) => {
      const line = String(raw || "").trim().replace(/^\//, "");
      const [name, ...rest] = line.split(/\s+/);
      const arg = rest.join(" ");
      if (!name) return;
      const sid = () => gwSidRef.current || (selRef.current && selRef.current.key) || "";

      // client-side specials that map onto verified RPCs
      if (name === "new" || name === "chat") { newChat(); return; }
      if (name === "retry") { runRetry(); return; }
      if (name === "undo") { runUndo(); return; }
      if (name === "compress" || name === "compact") { requestCompact(arg); return; }
      if (name === "context") { refreshUsage(); pushSystem(usageMeterText(usage)); return; }
      if (name === "steer") {
        if (!arg) { toast("Usage: /steer <text>", "error"); return; }
        steer(arg);
        return;
      }
      if (name === "export") { doExport(arg || "markdown"); return; }
      if (name === "share") { createShare(); return; }
      if (name === "model") {
        if (!arg) { pushSystem(`Current model: ${str((selRef.current && selRef.current.model) || "auto")}`); return; }
        const gw = gwRef.current;
        const live = gwSidRef.current;
        try {
          if (gw && live) {
            await gw.request("config.set", { key: "model", value: arg, session_id: live }, 30000);
            pushSystem(`🧠 Model set to ${arg}`);
          } else {
            // No live gateway session (new chat / not resumed) — queue it so
            // the conversation starts on it, mirroring the model picker.
            pendingModelRef.current = arg;
            patchSettings({ defaultModel: arg });
            pushSystem(`🧠 Model ${arg} queued — applies to this conversation`);
          }
          setSelected((s) => s && { ...s, model: arg });
        } catch (e) {
          const m = str((e && e.message) || e);
          toast(/busy|4009/i.test(m)
            ? "Stop the current reply first (■ Stop), then switch the model."
            : `Model switch failed: ${m}`, "error");
        }
        return;
      }
      if (name === "yolo") {
        try {
          await gwRef.current.request("config.set", { key: "yolo", value: arg || "toggle", ...(sid() ? { session_id: sid() } : {}) }, 30000);
          setSelected((s) => s && { ...s, yolo: !s.yolo });
          pushSystem(`🔓 Yolo ${arg === "off" ? "disabled" : "toggled"}`);
        } catch (e) {
          toast(`Yolo failed: ${str(e.message || e)}`, "error");
        }
        return;
      }
      if (name === "goal") {
        let goalSid = sid();
        if (!goalSid) {
          try { goalSid = (await ensureSession()).session_id; }
          catch (e) { toast(`Goal mode needs a session: ${str(e.message || e)}`, "error"); return; }
        }
        try {
          const res = await gwRef.current.request("command.dispatch", { name: "goal", arg: arg || "status", session_id: goalSid }, 30000);
          const type = str(res && res.type);
          if (type === "send" && res.message) {
            if (res.notice) pushSystem(res.notice);
            await submitGatewayText(str(res.message));
          } else if (type === "exec") {
            pushSystem(str(res.output || ""));
          } else if (type === "alias" && res.name) {
            dispatchCommand(`/${res.name}${arg ? ` ${arg}` : ""}`);
          } else {
            pushSystem("Goal: no active goal.");
          }
        } catch (e) {
          toast(`/goal failed: ${str(e.message || e)}`, "error");
        }
        return;
      }
      if (name === "queue") {
        if (!arg) { toast("Usage: /queue <text>", "error"); return; }
        try {
          const created = await ensureSession();
          await gwRef.current.request("prompt.background", { session_id: created.session_id, text: arg }, 60000);
          pushSystem("⏳ Queued behind the running turn.");
        } catch (e) {
          toast(`Queue failed: ${str(e.message || e)}`, "error");
        }
        return;
      }

      // everything else goes to the server-side command bridge (creating a
      // session on demand — the user asked for a command, give it somewhere to run)
      let bridgeSid = sid();
      if (!bridgeSid) {
        try { bridgeSid = (await ensureSession()).session_id; }
        catch (e) { toast(`Commands need a session: ${str(e.message || e)}`, "error"); return; }
      }
      try {
        const res = await gwRef.current.request("command.dispatch", { name, arg, session_id: bridgeSid }, 60000);
        const type = str(res && res.type);
        if (type === "send" && res.message) {
          if (res.notice) pushSystem(res.notice);
          setToolRows([]);
          setAgents({});
          await submitGatewayText(str(res.message));
        } else if (type === "exec") {
          pushSystem(str(res.output || `/${name} done`));
        } else if (type === "alias" && res.name) {
          dispatchCommand(`/${res.name}${arg ? ` ${arg}` : ""}`);
        } else {
          pushSystem(`/${name} accepted.`);
        }
      } catch (e) {
        const m = str(e.message || e);
        pushSystem(`⚠ /${name}: ${m}${/unknown|not found|4018/i.test(m) ? " — this deployment disables the slash worker; quick commands only." : ""}`);
      }
    }, [newChat, runRetry, runUndo, requestCompact, refreshUsage, steer, submitGatewayText, ensureSession, toast, usage, patchSettings]);

    const usageMeterText = (u) => {
      if (!u || !(u.context_used || u.total)) return "Context usage unknown — send a message first.";
      const used = num(u.context_used || u.total);
      const max = num(u.context_max);
      return `Context: ${fmtTokens(used)}${max ? ` / ${fmtTokens(max)} (${Math.round((used / max) * 100)}%)` : ""} tokens in play.`;
    };

    // ── share / export ───────────────────────────────────────────────

    const createShare = useCallback(async () => {
      const sel = selRef.current;
      if (!sel) { toast("Open a conversation first.", "error"); return; }
      try {
        const res = await fetchJSON(`${BASE}/share/${encodeURIComponent(sel.id)}`, { method: "POST", body: {} });
        try { navigator.clipboard.writeText(`${location.origin}${res.url}`); } catch { /* optional */ }
        toast("Share link created and copied to clipboard");
        openShares(res.token);
      } catch (e) {
        toast(`Share failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    const openShares = useCallback(async (highlightToken) => {
      try {
        const res = await fetchJSON(`${BASE}/shares`);
        setShares(res.shares || []);
      } catch {
        setShares([]);
      }
      openModal({ kind: "shares", highlight: highlightToken });
    }, []);

    const revokeShare = useCallback(async (token) => {
      try {
        await fetchJSON(`${BASE}/share/${encodeURIComponent(token)}`, { method: "DELETE" });
        const res = await fetchJSON(`${BASE}/shares`);
        setShares(res.shares || []);
      } catch (e) {
        toast(`Revoke failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    const doExport = useCallback(async (fmt = "markdown") => {
      const sel = selRef.current;
      if (!sel) { toast("Open a conversation first.", "error"); return; }
      try {
        const res = await fetch(`${BASE}/export/${encodeURIComponent(sel.id)}?format=${encodeURIComponent(fmt)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `${(sel.title || sel.id).replace(/[^\w.-]+/g, "_").slice(0, 60) || "conversation"}.${fmt === "json" ? "json" : fmt === "txt" ? "txt" : "md"}`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      } catch (e) {
        toast(`Export failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    // ── metadata mutations ───────────────────────────────────────────

    const patchMeta = useCallback(async (session, patch) => {
      const sid = typeof session === "string" ? session : session.id;
      setMeta((m) => ({ ...m, [sid]: { ...(m[sid] || {}), ...patch } }));
      try {
        await fetchJSON(`${BASE}/metadata/${encodeURIComponent(sid)}`, { method: "PUT", body: patch });
        loadMeta();
        loadFolders();
      } catch (e) {
        toast(`Could not save (${str(e.message || e)})`, "error");
      }
    }, [loadMeta, loadFolders, toast]);

    const renameSession = useCallback(async (session) => {
      const sid = typeof session === "string" ? session : session.id;
      const cur = (typeof session === "object" && session.title) || (meta[sid] && meta[sid].renamed) || "";
      const title = await promptDialog("Rename conversation", "New title", cur, "Title", "Rename");
      if (!title) return;
      try {
        await fetchJSON(`${BASE}/sessions/${encodeURIComponent(sid)}/rename`, { method: "POST", body: { title } });
        setSessions((ss) => ss.map((s) => (s.id === sid ? { ...s, title } : s)));
        setSelected((sel) => sel && sel.id === sid ? { ...sel, title } : sel);
        toast("Renamed");
      } catch (e) {
        toast(`Rename failed: ${str(e.message || e)}`, "error");
      }
    }, [meta, toast]);

    const deleteSession = useCallback(async (session) => {
      const sid = typeof session === "string" ? session : session.id;
      const ok = settings && settings.confirmDelete !== false
        ? await confirmDialog("Delete conversation?", "The transcript is removed from disk. This cannot be undone.", "Delete", true)
        : true;
      if (!ok) return;
      // live sessions must go through the gateway (4023 otherwise)
      try {
        await gwRef.current.request("session.delete", { session_id: sid }, 30000);
      } catch { /* not live / gateway down — REST fallback below */ }
      try {
        await fetchJSON(`${BASE}/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" });
        toast("Conversation deleted");
        if (selRef.current && selRef.current.id === sid) newChat();
        refreshSessions({});
        loadMeta();
      } catch (e) {
        const m = str(e.message || e);
        toast(/409|active/.test(m) ? "Session is active in another tab — close it there first." : `Delete failed: ${m}`, "error");
      }
    }, [settings, refreshSessions, loadMeta, newChat, toast]);

    const setTagsFor = useCallback(async (session) => {
      const sid = typeof session === "string" ? session : session.id;
      const cur = ((meta[sid] || {}).tags || []).join(", ");
      const val = await promptDialog("Tags", "Comma-separated tags (autocompletes existing ones)", cur, "e.g. research, rust, invoices", "Save tags");
      if (val === null) return;
      const tags = val.split(",").map((t) => t.trim().replace(/^#/, "")).filter(Boolean);
      patchMeta(sid, { tags });
    }, [meta, patchMeta]);

    const newFolder = useCallback(async () => {
      const name = await promptDialog("New folder", "Folder name", "", "e.g. Work", "Create");
      if (!name) return;
      try {
        await fetchJSON(`${BASE}/folders/${encodeURIComponent(name)}`, { method: "PUT", body: {} });
        loadFolders();
      } catch (e) {
        toast(`Folder failed: ${str(e.message || e)}`, "error");
      }
    }, [loadFolders, toast]);

    const renameFolder = useCallback(async (name) => {
      const newName = await promptDialog("Rename folder", "New name", name, "Folder name", "Rename");
      if (!newName || newName === name) return;
      try {
        await fetchJSON(`${BASE}/folders/${encodeURIComponent(name)}`, { method: "PUT", body: { new_name: newName } });
        loadFolders();
        loadMeta();
      } catch (e) {
        toast(`Rename failed: ${str(e.message || e)}`, "error");
      }
    }, [loadFolders, loadMeta, toast]);

    const deleteFolder = useCallback(async (name) => {
      const ok = await confirmDialog("Delete folder?", `“${name}” is removed; its conversations become unfiled (nothing is deleted).`, "Delete folder", true);
      if (!ok) return;
      try {
        await fetchJSON(`${BASE}/folders/${encodeURIComponent(name)}`, { method: "DELETE" });
        loadFolders();
        loadMeta();
      } catch (e) {
        toast(`Delete failed: ${str(e.message || e)}`, "error");
      }
    }, [loadFolders, loadMeta, toast]);

    const dropToFolder = useCallback((sid, folder) => {
      patchMeta(sid, { folder });
      toast(folder ? `Moved to ${folder}` : "Moved out of folder");
    }, [patchMeta, toast]);

    const runBulk = useCallback(async (patch) => {
      if (!bulkSel.length) { toast("Select conversations first.", "error"); return; }
      const isDelete = patch && patch.__delete;
      const ok = isDelete
        ? await confirmDialog(`Delete ${bulkSel.length} conversations?`, "Transcripts are removed from disk. This cannot be undone.", `Delete ${bulkSel.length}`, true)
        : true;
      if (!ok) return;
      try {
        const body = isDelete ? { ids: bulkSel, delete: true } : { ids: bulkSel, patch };
        const res = await fetchJSON(`${BASE}/metadata/bulk`, { method: "POST", body });
        if (isDelete) {
          const skipped = (res.skipped || []).length;
          toast(`Deleted ${res.deleted.length} conversation${res.deleted.length === 1 ? "" : "s"}${skipped ? ` (${skipped} active — skipped)` : ""}`);
          if (selRef.current && res.deleted.includes(selRef.current.id)) newChat();
          setBulkSel([]);
          setBulkMode(false);
          refreshSessions({});
          loadMeta();
        } else {
          toast(`Updated ${res.updated.length} conversation${res.updated.length === 1 ? "" : "s"}`);
          loadMeta();
        }
      } catch (e) {
        toast(`Bulk action failed: ${str(e.message || e)}`, "error");
      }
    }, [bulkSel, refreshSessions, loadMeta, newChat, toast]);

    // ── delegation / spawn trees ─────────────────────────────────────

    const refreshDelegation = useCallback(() => {
      const gw = gwRef.current;
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!gw || gw.status !== "open" || !sid) return;
      gw.request("delegation.status", { session_id: sid }, 20000)
        .then((res) => setDelegation(res || null))
        .catch(() => setDelegation(null));
    }, []);

    const toggleDelegationPause = useCallback(async () => {
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!sid || !delegation) return;
      try {
        await gwRef.current.request("delegation.pause", { session_id: sid, paused: !delegation.paused }, 30000);
        setDelegation((d) => (d ? { ...d, paused: !d.paused } : d));
      } catch (e) {
        toast(`Delegation control failed: ${str(e.message || e)}`, "error");
      }
    }, [delegation, toast]);

    const interruptSubagent = useCallback(async (subagentId) => {
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!sid || !subagentId) return;
      try {
        await gwRef.current.request("subagent.interrupt", { session_id: sid, subagent_id: subagentId }, 30000);
        setAgents((cur) => (cur[subagentId] ? { ...cur, [subagentId]: { ...cur[subagentId], status: "complete", summary: "interrupted" } } : cur));
      } catch (e) {
        toast(`Interrupt failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    const loadSpawnTrees = useCallback(() => {
      const gw = gwRef.current;
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!gw || gw.status !== "open" || !sid) return;
      gw.request("spawn_tree.list", { session_id: sid }, 20000)
        .then((res) => {
          const list = (res && (res.trees || res.list || res.items)) || (Array.isArray(res) ? res : []);
          setSpawnTrees(list.slice(0, 20));
        })
        .catch(() => setSpawnTrees([]));
    }, []);

    const loadTreeById = useCallback(async (t) => {
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      const id = t && (t.id || t.key || t.tree_id);
      if (!sid || !id) return;
      setBusyTree(true);
      try {
        const res = await gwRef.current.request("spawn_tree.load", { session_id: sid, id }, 20000);
        const subs = (res && (res.subagents || res.tree)) || [];
        const map = {};
        (Array.isArray(subs) ? subs : []).forEach((a) => { if (a && a.id) map[str(a.id)] = { ...a, status: a.status || "complete" }; });
        setReplayTree(Object.keys(map).length ? map : null);
        if (!Object.keys(map).length) toast("That tree had no subagents.", "error");
      } catch (e) {
        toast(`Tree load failed: ${str(e.message || e)}`, "error");
      } finally {
        setBusyTree(false);
      }
    }, [toast]);

    useEffect(() => {
      if (panel !== "activity") return;
      refreshDelegation();
      loadSpawnTrees();
      const t = setInterval(refreshDelegation, 30000);
      return () => clearInterval(t);
    }, [panel, selected, gwStatus]);

    // ── tools popover state ──────────────────────────────────────────

    const openTools = useCallback(() => {
      const gw = gwRef.current;
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      if (!gw || !sid) { setToolsState({ open: true, enabled: [], available: [], detached: true }); return; }
      gw.connect().then(async () => {
        try {
          const res = await gw.request("session.info", { session_id: sid }, 30000);
          const enabled = (res && res.enabled_toolsets) || [];
          setToolsState({ open: true, enabled, available: (caps && caps.toolsets) || [], info: res || null });
        } catch {
          setToolsState({ open: true, enabled: [], available: (caps && caps.toolsets) || [], detached: true });
        }
      }).catch(() => setToolsState({ open: true, enabled: [], available: (caps && caps.toolsets) || [], detached: true }));
    }, [caps]);

    const toggleToolset = useCallback(async (name, enable) => {
      const sid = gwSidRef.current || (selRef.current && selRef.current.key);
      try {
        const res = await gwRef.current.request("tools.configure", {
          action: enable ? "enable" : "disable",
          names: [name],
          ...(sid ? { session_id: sid } : {}),
        }, 30000);
        setToolsState((s) => ({ ...s, enabled: res.enabled_toolsets || s.enabled }));
        toast(`${enable ? "Enabled" : "Disabled"} ${name}`);
      } catch (e) {
        toast(`Toolset change failed: ${str(e.message || e)}`, "error");
      }
    }, [toast]);

    const toolsSummary = useMemo(() => {
      if (toolsState.detached) return "tools (open a chat)";
      if (!toolsState.enabled.length) return "tools";
      return toolsState.enabled.slice(0, 3).join(", ") + (toolsState.enabled.length > 3 ? "…" : "");
    }, [toolsState]);

    // ── model ────────────────────────────────────────────────────────

    const changeModel = useCallback(async (id) => {
      const prev = (selRef.current && selRef.current.model) || modelValue;
      if (id === "auto") {
        setModelValue("auto");
        pendingModelRef.current = null;
        setSelected((s) => s && { ...s, model: "" });
        return;
      }
      setModelValue(id);
      setSelected((s) => s && { ...s, model: id });
      // A real model switch is `config.set {key:"model", value, session_id}`
      // against a LIVE gateway session (this is what the pinned v2026.5.7
      // gateway uses to hot-swap the running agent).  Without a live
      // session_id the write only updates global config for *new* sessions,
      // so when none is active yet we queue the choice and apply it the
      // moment the conversation gets its gateway session (see ensureSession).
      const gw = gwRef.current;
      const live = gwSidRef.current;
      if (gw && live) {
        try {
          await gw.request("config.set", { key: "model", value: id, session_id: live }, 30000);
          pendingModelRef.current = null;
          setSelected((s) => s && { ...s, model: id });
          toast(`Model: ${id}`);
        } catch (e) {
          const m = str((e && e.message) || e);
          // revert the optimistic label so it never lies about the active model
          setSelected((s) => s && { ...s, model: s.model === id ? prev : s.model });
          setModelValue(prev);
          toast(/busy|4009/i.test(m)
            ? "Stop the current reply first (■ Stop), then switch the model."
            : `Model switch failed: ${m}`, "error");
        }
        return;
      }
      // No live gateway session yet (new chat / not resumed): remember it.
      pendingModelRef.current = id;
      patchSettings({ defaultModel: id });
      toast(`Model: ${id} — applies to this conversation`);
    }, [modelValue, patchSettings, toast]);

    const changePinnedModels = useCallback((pins) => {
      setPinnedModels(pins);
      patchSettings({ pinnedModels: pins });
    }, [patchSettings]);

    // ── drafts ───────────────────────────────────────────────────────

    const draftKeyFor = selected ? (selected.key || selected.id) : "new";
    const lastDraftKey = useRef("new");
    useEffect(() => {
      if (lastDraftKey.current !== draftKeyFor) {
        // key just changed (opened another conversation / new chat): swap in
        // that conversation's draft WITHOUT persisting the outgoing text
        // under the new key (that would leak drafts across conversations)
        lastDraftKey.current = draftKeyFor;
        setText(readDraft(draftKeyFor));
        return;
      }
      writeDraft(draftKeyFor, text);
    }, [text, draftKeyFor]);

    // ── scrolling ────────────────────────────────────────────────────

    const onScroll = useCallback(() => {
      const el = scrollRef.current;
      if (!el) return;
      const nearTop = el.scrollTop < 60;
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      stickBottom.current = atBottom || el.scrollHeight <= el.clientHeight;
      if (nearTop && olderOffset > 0) loadOlder();
    }, [olderOffset]);

    const loadOlder = useCallback(async () => {
      const sel = selRef.current;
      if (!sel || olderOffset <= 0) return;
      const nextOffset = Math.max(0, olderOffset - PAGE);
      try {
        const res = await fetchJSON(`${BASE}/sessions/${encodeURIComponent(sel.id)}?limit=${olderOffset - nextOffset}&offset=${nextOffset}`);
        const older = res.messages || [];
        if (older.length) {
          const el = scrollRef.current;
          const prevHeight = el ? el.scrollHeight : 0;
          setMessages((ms) => [...older, ...ms]);
          setOlderOffset(nextOffset);
          setRenderLimit((r) => r + older.length);
          requestAnimationFrame(() => {
            if (el) el.scrollTop = el.scrollHeight - prevHeight + el.scrollTop;
          });
        } else {
          setOlderOffset(0);
        }
      } catch { /* offline — keep current view */ }
    }, [olderOffset]);

    useEffect(() => {
      const el = scrollRef.current;
      if (!el || !settings || settings.autoScroll === false || !stickBottom.current) return;
      el.scrollTop = el.scrollHeight;
    }, [messages, toolRows]);

    // ── keyboard shortcuts ───────────────────────────────────────────

    useEffect(() => {
      const onKey = (e) => {
        const mod = e.metaKey || e.ctrlKey;
        if (mod && e.key.toLowerCase() === "k") {
          e.preventDefault();
          setPaletteOpen((v) => !v);
        } else if (mod && e.shiftKey && e.key.toLowerCase() === "o") {
          e.preventDefault();
          newChat();
        } else if (mod && e.key === "/") {
          e.preventDefault();
          try { composerRef.current && composerRef.current.focus(); } catch { /* noop */ }
        } else if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
          e.preventDefault();
          const ss = sessionsRef.current;
          if (ss.length) {
            const cur = selRef.current ? ss.findIndex((x) => x.id === selRef.current.id) : -1;
            const next = e.key === "ArrowUp" ? Math.max(0, cur - 1) : Math.min(ss.length - 1, cur + 1);
            const target = ss[cur === -1 ? 0 : next];
            if (target) openSession(target);
          }
        } else if (e.key === "Escape") {
          if (paletteOpen) { setPaletteOpen(false); return; }
          if (modal) { closeModal(); return; }
          if (generatingRef.current) { interrupt(); }
        }
      };
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    }, [paletteOpen, modal, generating, newChat, openSession, interrupt]);

    // ── palette actions ──────────────────────────────────────────────

    const paletteActions = useMemo(() => ([
      { label: "New conversation", hint: "⌘⇧O", run: newChat },
      { label: "Toggle sidebar", hint: "", run: () => setSidebarOpen((v) => !v) },
      { label: "Settings", hint: "", run: () => openModal({ kind: "settings" }) },
      { label: "Share this conversation…", hint: "", when: !!selected, run: createShare },
      { label: "Manage share links", hint: "", run: () => openShares() },
      { label: "Export as Markdown", hint: "", when: !!selected, run: () => doExport("markdown") },
      { label: "Export as JSON", hint: "", when: !!selected, run: () => doExport("json") },
      { label: "Export as plain text", hint: "", when: !!selected, run: () => doExport("txt") },
      { label: "Compact context…", hint: "", when: !!selected, run: requestCompact },
      { label: "Undo last turn", hint: "", when: !!selected, run: runUndo },
      { label: "Retry last response", hint: "", when: !!selected, run: runRetry },
      { label: "Stop generating", hint: "Esc", when: generating, run: interrupt },
      { label: "Reconnect gateway", hint: "", run: () => { const gw = gwRef.current; if (gw) { try { gw.ws && gw.ws.close(); } catch { /* noop */ } gw.connect().catch(() => {}); } } },
    ]), [selected, generating, newChat, createShare, openShares, doExport, requestCompact, runUndo, runRetry, interrupt]);

    const onPaletteRun = useCallback((item) => {
      if (item.kind === "session") openSession(item.session);
      else if (item.run) item.run();
    }, [openSession]);

    // ── derived ──────────────────────────────────────────────────────

    const models = (caps && caps.models) || [];
    const modes = (caps && caps.modes) || [];
    const contextWindow = useMemo(() => {
      const m = models.find((x) => x.id === (selRef.current && selRef.current.model)) || models.find((x) => x.id === modelValue);
      return m && m.context_window;
    }, [models, modelValue, selected]);

    const windowedMessages = useMemo(() => {
      if (messages.length <= renderLimit) return messages;
      return messages.slice(messages.length - renderLimit);
    }, [messages, renderLimit]);

    const currentMode = selected && selected.mode ? selected.mode : (settings && settings.defaultMode) || "fast";

    const onModeChange = useCallback((modeId) => {
      if (modeId === "custom") { openModal({ kind: "settings" }); return; }
      const mode = modes.find((m) => m.id === modeId);
      patchSettings({ defaultMode: modeId });
      setSelected((s) => s && { ...s, mode: modeId });
      if (mode) toast(`${mode.emoji || "✨"} ${mode.label} mode`);
      const sid = gwSidRef.current;
      // Only verified RPCs: `fast` and `yolo` are real config.set keys on the
      // pinned gateway. Strategy prompts are seeded into the composer (fully
      // visible and editable) instead of being silently injected.
      if (mode && mode.strategy && sid) {
        if (mode.strategy.fast) {
          gwRef.current.request("config.set", { key: "fast", value: "on", session_id: sid }, 30000).catch(() => {});
        } else {
          gwRef.current.request("config.set", { key: "fast", value: "off", session_id: sid }, 30000).catch(() => {});
        }
        if (mode.strategy.yolo) {
          gwRef.current.request("config.set", { key: "yolo", value: "on", session_id: sid }, 30000).catch(() => {});
        }
      }
      if (mode && mode.strategy && mode.strategy.prompt && !text.trim()) {
        setText(`${str(mode.strategy.prompt)}\n\n`);
      }
    }, [modes, patchSettings, toast, text]);

    if (!settings) {
      return h("div", { className: "hcd hcd-loading-page" }, "Loading chat…");
    }

    const readOnly = !!(selected && selected.readOnly);

    return h(
      "div",
      { className: `hcd density-${settings.density || "comfortable"} width-${settings.messageWidth || "wide"} font-${settings.fontSize || "medium"}` },
      h(ToastStack, { toasts, dismiss: (id) => setToasts((ts) => ts.filter((t) => t.id !== id)) }),

      sidebarOpen
        ? h(SessionSidebar, {
            sessions, meta, activeKey: selected ? selected.id : "", loading: sessionsLoading,
            error: sessionsError, onRefresh: () => refreshSessions({}), onOpen: openSession, onNew: newChat,
            query, setQuery, folders, allTags, activeTags,
            toggleTag: (t) => setActiveTags((ts) => (ts.includes(t) ? ts.filter((x) => x !== t) : [...ts, t])),
            onRename: renameSession, onDelete: deleteSession,
            onSetPinned: (s, v) => patchMeta(s, { pinned: v }),
            onSetStarred: (s, v) => patchMeta(s, { starred: v }),
            onSetArchived: (s, v) => patchMeta(s, { archived: v }),
            onSetFolder: (s, f) => patchMeta(s, { folder: f }),
            onNewFolder: newFolder, onRenameFolder: renameFolder, onDeleteFolder: deleteFolder,
            onSetTags: setTagsFor,
            showArchived, setShowArchived,
            bulkMode, setBulkMode, bulkSel,
            toggleBulk: (sid) => setBulkSel((ss) => (ss.includes(sid) ? ss.filter((x) => x !== sid) : [...ss, sid])),
            bulkAll: () => setBulkSel(sessions.map((s) => s.id)),
            bulkNone: () => setBulkSel([]),
            onBulk: runBulk,
            onDropToFolder: dropToFolder,
          })
        : null,

      h(
        "main",
        { className: "hcd-main" },
        h(
          "div",
          { className: "hcd-topbar" },
          h("div", { className: "hcd-topbar-left" },
            h("button", { className: "hcd-icon-btn", onClick: () => setSidebarOpen((v) => !v), title: "Toggle sidebar", "aria-label": "Toggle sidebar" }, "☰"),
            h(StatusDot, { status: gwStatus }),
            h("h1", { className: "hcd-title" }, selected ? str(selected.title || "Untitled conversation") : "New conversation"),
            goal && goal.text
              ? h("button", {
                  className: cn("hcd-goal-pill", goal.paused && "paused"),
                  title: `Goal mode: ${goal.text}${goal.paused ? " (paused)" : ""} — click for status`,
                  onClick: () => dispatchCommand("/goal status"),
                }, `${goal.paused ? "⏸" : "⊙"} ${str(goal.text).slice(0, 40)}`)
              : null,
            voice
              ? h("span", { className: "hcd-voice-pill", title: `Voice: ${voice.state}${voice.detail ? ` — ${voice.detail}` : ""}` }, `🎙 ${voice.state}`)
              : null,
          ),
          h("div", { className: "hcd-topbar-right" },
            generating
              ? h("button", { className: "hcd-stop", onClick: interrupt, "aria-label": "Stop generating" }, "■ Stop")
              : null,
            h(ContextMeter, { usage, contextWindow, onCompact: requestCompact, disabled: !selected }),
            h("button", { className: "hcd-ctl-btn", onClick: () => setPaletteOpen(true), title: "Command palette (⌘K)" }, "⌘K"),
            h("button", { className: "hcd-ctl-btn", onClick: () => openModal({ kind: "settings" }), title: "Settings", "aria-label": "Settings" }, "⚙"),
            h("button", { className: "hcd-ctl-btn", onClick: () => setPanel((p) => (p ? "" : "activity")), title: "Toggle panel" }, panel ? "▶" : "◀"),
          ),
        ),

        h(
          "div",
          { className: `hcd-chat ${panel ? "with-panel" : ""}` },
          h(
            "div",
            { className: "hcd-scroll", ref: scrollRef, onScroll },
            olderOffset > 0
              ? h("div", { className: "hcd-load-older" },
                  h("button", { onClick: loadOlder }, `Load ${Math.min(PAGE, olderOffset)} earlier messages…`))
              : null,
            messages.length > renderLimit
              ? h("div", { className: "hcd-load-older" },
                  h("button", { onClick: () => setRenderLimit((r) => r + RENDER_WINDOW) }, `Show ${messages.length - renderLimit} earlier messages…`))
              : null,
            !messages.length && !generating
              ? h(Welcome, { modes, onPick: (m) => { onModeChange(m.id); composerRef.current && composerRef.current.focus(); } })
              : windowedMessages.map((m, i) =>
                  h(MessageView, {
                    key: m.id || `i${i}`,
                    msg: m,
                    index: messages.indexOf(m),
                    active: streamingId === m.id,
                    showTime: settings.showTimestamps !== false,
                    showUsage: settings.showUsage,
                    onAction: onMessageAction,
                  })),
            toolRows.length
              ? h("div", { className: "hcd-turn-tools" },
                  toolRows.map(({ key, tool }) => h(ToolCard, { key, tool, defaultOpen: tool.status === "running" })))
              : null,
            Object.keys(agents).length
              ? h(SubagentTree, { agents, onInterrupt: interruptSubagent, live: true })
              : null,
            replayTree
              ? h(SubagentTree, { agents: replayTree, onInterrupt: () => {}, live: false, title: "🌳 Replayed agent tree (read-only)" })
              : null,
            prompts.map((p, i) => h(PromptCard, { key: p.request_id || `p${i}`, prompt: p, onRespond: respondPrompt })),
          ),
          h(Composer, {
            text, setText,
            onSend: (v) => send(v),
            onBackground: (v) => send(v, { background: true }),
            onSteer: steer,
            onDispatchCommand: dispatchCommand,
            commandCatalog,
            generating, disabled: gwStatus !== "open" && !selected, readOnly,
            attachments, onAttach, onRemoveAttachment: removeAttachment,
            onOpenTools: openTools, toolsSummary,
            mode: currentMode, onModeChange, modes,
            modelPicker: h(ModelPicker, {
              models, pinned: pinnedModels, value: (selected && selected.model) || modelValue,
              onChange: changeModel, onPinnedChange: changePinnedModels, disabled: false,
            }),
            enterToSend: settings.enterToSend !== false,
            setEnterToSend: (v) => patchSettings({ enterToSend: v }),
            draftDiscardable: !!text.trim(),
            onDiscardDraft: () => { setText(""); writeDraft(draftKeyFor, ""); },
          }),
        ),
      ),

      panel
        ? h(
            "aside",
            { className: "hcd-panel" },
            h("div", { className: "hcd-panel-tabs" },
              h("button", { className: panel === "activity" ? "on" : "", onClick: () => setPanel("activity") }, "Activity"),
              h("button", { className: panel === "info" ? "on" : "", onClick: () => setPanel("info") }, "Info")),
            panel === "activity"
              ? h(ActivityPanel, {
                  toolEvents: toolRows, agents, onInterrupt: interruptSubagent, generating,
                  delegation, onDelegationPause: toggleDelegationPause,
                  trees: spawnTrees, onLoadTree: loadTreeById, busyTree,
                })
              : h(InfoPanel, {
                  session: selected, meta,
                  onSetStarred: (s, v) => patchMeta(s, { starred: v }),
                  onSetPinned: (s, v) => patchMeta(s, { pinned: v }),
                  onSetTags: setTagsFor, onRename: renameSession,
                  onShare: createShare, onExport: () => doExport("markdown"),
                  onCompact: requestCompact, onUndo: runUndo,
                  onDeleteSession: () => selected && deleteSession(selected),
                  readOnly, tree, onOpenSession: openSession,
                  usageSummary, onRefreshUsage: refreshUsageSummary, sessionUsage: usage,
                }),
          )
        : null,

      toolsState.open
        ? h(Modal, { title: "Tool configuration", onClose: () => setToolsState((s) => ({ ...s, open: false })) },
            toolsState.detached
              ? h("p", { className: "hcd-hint" }, "Open or resume a conversation to configure its toolsets.")
              : h("div", { className: "hcd-tools-list" },
                  (toolsState.available || []).map((t) =>
                    h("label", { key: t.id || t.name, className: "hcd-tool-row" },
                      h("input", {
                        type: "checkbox",
                        checked: toolsState.enabled.includes(t.name || t.id),
                        onChange: (e) => toggleToolset(t.name || t.id, e.target.checked),
                        "aria-label": `Enable toolset ${t.label || t.name}`,
                      }),
                      h("span", null, h("b", null, t.label || t.name), h("small", null, str(t.description || "")))))),
          )
        : null,

      paletteOpen
        ? h(Palette, {
            actions: paletteActions,
            sessions: sessions.filter((s) => !(meta[s.id] || {}).archived),
            onRun: onPaletteRun,
            onClose: () => setPaletteOpen(false),
          })
        : null,

      modal && modal.kind === "settings"
        ? h(SettingsModal, {
            settings, onChange: patchSettings, onClose: closeModal,
            onReset: () => {
              const fresh = {
                density: "comfortable", messageWidth: "wide", fontSize: "medium",
                showTimestamps: true, showUsage: true, confirmDelete: false, enterToSend: true,
                autoScroll: true, autoTitle: true, autoTools: true, saveHistory: true,
                memoryEnabled: true, temporaryDefault: false, defaultMode: "fast",
                defaultModel: "", defaultAgent: "auto", defaultTools: [], pinnedModels: [],
              };
              setSettings(fresh);
              fetchJSON(`${BASE}/settings`, { method: "PUT", body: fresh }).catch(() => {});
            },
          })
        : null,
      modal && modal.kind === "shares"
        ? h(SharesModal, { shares, onRevoke: revokeShare, onClose: closeModal })
        : null,
      modal && modal.kind === "confirm"
        ? h(ConfirmModal, {
            title: modal.title, message: modal.message, confirmLabel: modal.confirmLabel,
            danger: modal.danger, onConfirm: modal.onConfirm, onCancel: modal.onCancel,
          })
        : null,
      modal && modal.kind === "prompt"
        ? h(PromptModal, {
            title: modal.title, label: modal.label, initial: modal.initial,
            placeholder: modal.placeholder, confirmLabel: modal.confirmLabel,
            onConfirm: modal.onConfirm, onCancel: modal.onCancel,
          })
        : null,
    );
  }

  // ── welcome / mode picker ────────────────────────────────────────────

  function Welcome({ modes, onPick }) {
    const examples = [
      "Summarize this repository and its architecture",
      "Research the latest changes in the EU AI Act",
      "Write a Python script that renames files by EXIF date",
      "Explain CRDTs like I'm a senior engineer",
    ];
    return h(
      "div",
      { className: "hcd-welcome" },
      h("div", { className: "hcd-welcome-hero" },
        h("div", { className: "hcd-welcome-logo" }, "H"),
        h("h2", null, "How can I help you today?"),
        h("p", null, "Chat runs on the real Hermes agent — tools, subagents, memory and all. Everything is stored server-side.")),
      modes && modes.length
        ? h("div", { className: "hcd-mode-grid" },
            modes.slice(0, 8).map((m) =>
              h("button", {
                key: m.id,
                className: "hcd-mode-card",
                onClick: () => onPick(m),
                title: str(m.description || ""),
              },
                h("span", { className: "hcd-mode-emoji" }, str(m.emoji || "✨")),
                h("b", null, str(m.label)),
                h("small", null, str(m.description || "")))))
        : null,
      h("div", { className: "hcd-examples" },
        h("h5", null, "Try asking…"),
        examples.map((e) => h("div", { key: e, className: "hcd-example" }, e))),
      h("p", { className: "hcd-shortcut-hint" },
        "⌘K palette · ⌘⇧O new chat · ⌘/ focus input · / slash commands · Esc stops generation"),
    );
  }

  function ChatTab(props) {
    return h(ErrorBoundary, null, h(ChatDashboard, props));
  }

  registry.register("hermes-chat-dashboard", ChatTab);

  // ── "chat disabled" banner (upstream-guard behaviour) ───────────────

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
          ", so the Chat link redirects here. Set ",
          h("code", null, "HERMES_DASHBOARD_TUI=1"),
          " in Render's Environment tab (it may be overridden by a stale copy in ",
          h("code", null, "/opt/data/.env"),
          " on images older than this fix — redeploy on the current image and it is cleaned up automatically), then restart the service."),
        h("button", { type: "button", onClick: () => { setDismissed(true); try { sessionStorage.setItem("hcd-disabled-banner", "1"); } catch { /* noop */ } } }, "Dismiss"),
      );
    }
    registry.registerSlot("hermes-chat-dashboard", "header-banner", ChatDisabledBanner);
  }
})();
