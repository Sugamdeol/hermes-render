/* hermes-chat-dashboard — web chat workspace for the dashboard.
 *
 * A ChatGPT-style chat UI that drives the real Hermes ``tui_gateway`` over a
 * pure-Python WebSocket (``/api/ws``) and talks to this plugin's backend
 * routes (``/api/plugins/hermes-chat-dashboard/...``) for durable session
 * metadata, transcripts, uploads, shares and exports.
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
    if (n.includes("agent") || n.includes("delegate")) return "🤖";
    return "🛠️";
  };

  // ── markdown → html (escape-first, safe) ────────────────────────────

  function inlineMd(text) {
    let s = esc(text);
    // bare https?:// URLs
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)"']+)/g, '$1<a href="$2" target="_blank" rel="noreferrer noopener">$2</a>');
    // [text](https://url)
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
    // inline code
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    // bold, then italic/underline-ish
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    s = s.replace(/(^|[^~])~~([^~]+)~~/g, "$1<del>$2</del>");
    return s;
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
            .join("")}</tbody></table></div>`,
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

      // list group (loose: blank lines inside are kept)
      const isList = (l) => /^\s*([-*+]|\d+[.)])\s+/.test(l);
      if (isList(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const items = [];
        while (i < lines.length) {
          const l = lines[i];
          if (isList(l)) {
            const m = l.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
            items.push({ done: null, text: m[2] });
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
            .map((it) => `<li>${inlineMd(it.text)}</li>`)
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
      html = html.replace(
        `\u0000CODE${idx}\u0000`,
        `<div class="hcd-code"><div class="hcd-code-head"><span>${safeLang}</span><button type="button" class="hcd-copy-code" data-copy='${safeCode.replace(/'/g, "&#39;")}'>Copy</button></div><pre><code>${safeCode}</code></pre></div>`,
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

  // ── gateway client ─────────────────────────────────────────────────

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
          ws.removeEventListener("open", onOpen);
          ws.removeEventListener("error", onError);
          if (ok) resolve();
          else reject(err || new Error("Gateway WebSocket failed"));
        };
        const onOpen = () => {
          this.retries = 0;
          this.setStatus("open");
          settle(true);
        };
        const onError = () => settle(false, new Error("Gateway WebSocket failed"));

        ws.addEventListener("message", (ev) => {
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

    dispose() {
      this.stop = true;
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

  // ── shared small components ─────────────────────────────────────────

  function StatusDot({ status }) {
    const cls = status === "open" ? "ok" : status === "connecting" ? "busy" : "warn";
    return h("span", { className: `hcd-dot ${cls}`, title: `Gateway: ${status}` });
  }

  function ToastStack({ toasts, dismiss }) {
    return h(
      "div",
      { className: "hcd-toasts" },
      toasts.map((t) =>
        h(
          "div",
          { key: t.id, className: `hcd-toast ${t.kind || "info"}` },
          h("span", null, t.text),
          h("button", { onClick: () => dismiss(t.id), title: "Dismiss" }, "×"),
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

  // ── tool activity ───────────────────────────────────────────────────

  function ToolCard({ tool, defaultOpen }) {
    const status = tool.status || "running";
    const isRunning = status === "running" || status === "pending";
    const title = tool.title || tool.goal || tool.name || "Tool";
    const sub =
      tool.summary ||
      tool.context ||
      tool.preview ||
      (tool.error ? tool.error : isRunning ? "Working…" : "Done");
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
          tool.duration_s ? h("em", null, fmtDuration(tool.duration_s)) : null,
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
        sub && !tool.inline_diff ? h("p", { className: "hcd-tool-sub" }, sub) : null,
        tool.context && tool.inline_diff ? h("p", { className: "hcd-tool-sub" }, tool.context) : null,
        tool.todos && tool.todos.length
          ? h(
              "ul",
              { className: "hcd-todos" },
              tool.todos.map((t, i) =>
                h(
                  "li",
                  { key: i, className: (t && t.status) || "" },
                  `${t && t.status === "done" ? "✓" : "○"} ${t && (t.content || t.text) ? t.content || t.text : t}`,
                ),
              ),
            )
          : null,
        tool.preview && tool.inline_diff ? h("pre", null, tool.preview) : null,
        tool.inline_diff
          ? h("div", { className: "hcd-diff" }, tool.inline_diff.split("\n").map((l, i) =>
              h(
                "div",
                { key: i, className: cn("hcd-diff-line", l.startsWith("+") && "add", l.startsWith("-") && "del") },
                l || " ",
              ),
            ))
          : null,
        tool.error ? h("p", { className: "hcd-tool-error" }, tool.error) : null,
      ),
    );
  }

  // ── prompts (approval / clarify / sudo / secret) ────────────────────

  function PromptCard({ prompt, onRespond }) {
    const kind = prompt.kind || "clarify";
    const [value, setValue] = useState("");
    const question = prompt.question || prompt.message || prompt.prompt || prompt.description || prompt.command || "Hermes is waiting for input";
    const choices =
      Array.isArray(prompt.choices) && prompt.choices.length
        ? prompt.choices
        : Array.isArray(prompt.options)
          ? prompt.options
          : null;

    return h(
      "div",
      { className: `hcd-prompt ${kind}` },
      h("div", { className: "hcd-prompt-head" },
        h("strong", null, kind === "approval" ? "⚠️ Action approval" : kind === "clarify" ? "❓ Clarification" : kind === "sudo" ? "🔐 sudo password" : "🔑 Secret needed"),
        h("button", { className: "hcd-prompt-dismiss", onClick: () => onRespond(kind, prompt, "deny") }, "×"),
      ),
      h("p", null, question),
      choices
        ? h(
            "div",
            { className: "hcd-prompt-choices" },
            choices.map((c) =>
              h(
                "button",
                { key: String(c), onClick: () => onRespond(kind, prompt, String(c)) },
                String(c),
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
            h("input", { type: kind === "sudo" ? "password" : "text", value, onChange: (e) => setValue(e.target.value), placeholder: kind === "sudo" ? "sudo password" : "value" }),
            h("button", { className: "primary", onClick: () => value && onRespond(kind, prompt, value) }, "Submit"),
          )
        : null,
      kind === "approval"
        ? h(
            "div",
            { className: "hcd-prompt-choices" },
            h("button", { className: "approve", onClick: () => onRespond(kind, prompt, "once") }, "Allow once"),
            h("button", { className: "approve", onClick: () => onRespond(kind, prompt, "session") }, "Allow this chat"),
            h("button", { className: "deny", onClick: () => onRespond(kind, prompt, "deny") }, "Deny"),
          )
        : null,
    );
  }

  // ── message ─────────────────────────────────────────────────────────

  function MessageView({ msg, index, active, onAction, showTime, showUsage }) {
    const bodyRef = useRef(null);
    useEffect(() => {
      bindCopyButtons(bodyRef.current);
    }, [msg.content, msg.streaming]);
    const isHermes = msg.role === "assistant";
    const isUser = msg.role === "user";

    return h(
      "article",
      { className: cn("hcd-message", `hcd-${msg.role || "assistant"}`), id: msg.id || undefined },
      h("div", { className: "hcd-avatar", title: roleLabel(msg.role) }, isHermes ? "H" : isUser ? "🧑" : msg.role === "tool" ? "⚙️" : "ℹ️"),
      h(
        "div",
        { className: "hcd-bubble" },
        h(
          "div",
          { className: "hcd-meta" },
          h("strong", null, roleLabel(msg.role)),
          showTime && msg.timestamp ? h("span", null, fmtTime(msg.timestamp)) : null,
          msg.model ? h("span", { className: "hcd-model-pill" }, msg.model) : null,
          msg.status === "interrupted" ? h("span", { className: "hcd-status-pill interrupted" }, "stopped") : null,
          msg.status === "error" ? h("span", { className: "hcd-status-pill error" }, "error") : null,
        ),
        msg.error
          ? h(
              "div",
              { className: "hcd-message-error" },
              h("strong", null, "Something went wrong"),
              h("details", null, h("summary", null, "View details"), h("pre", null, msg.error)),
            )
          : h("div", { ref: bodyRef, className: "hcd-markdown", dangerouslySetInnerHTML: { __html: renderMarkdown(msg.content || (active ? "▌" : "")) + (active && msg.content ? '<span class="hcd-cursor">▌</span>' : "") } }),
        msg.reasoning && msg.reasoning.trim()
          ? h(
              "details",
              { className: "hcd-reasoning", open: false },
              h("summary", null, "Reasoning"),
              h("pre", null, msg.reasoning),
            )
          : null,
        msg.usage && msg.usage.total && showUsage !== false
          ? h("div", { className: "hcd-usage" },
              `${fmtTokens(msg.usage.input || 0)} in · ${fmtTokens(msg.usage.output || 0)} out · ${msg.usage.calls || 1} call${(msg.usage.calls || 1) === 1 ? "" : "s"} ${msg.usage.model ? `· ${msg.usage.model}` : ""}`)
          : null,
        msg.warning ? h("div", { className: "hcd-warning" }, msg.warning) : null,
        h(
          "div",
          { className: "hcd-actions" },
          h("button", { onClick: () => onAction("copy", msg, index) }, "Copy"),
          isUser && h("button", { onClick: () => onAction("edit", msg, index) }, "Edit"),
          isHermes && h("button", { onClick: () => onAction("regenerate", msg, index) }, "Regenerate"),
          isHermes && !msg.streaming && h("button", { onClick: () => onAction("continue", msg, index) }, "Continue"),
          h("button", { onClick: () => onAction("branch", msg, index) }, "Branch"),
          h("button", { className: "danger", onClick: () => onAction("delete", msg, index) }, "Delete"),
        ),
      ),
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
      },
        "🧠 ", label, " ▾"),
      open
        ? h("div", { className: "hcd-model-pop" },
            h("input", {
              ref: inputRef,
              className: "hcd-model-search",
              value: q,
              onChange: (e) => setQ(e.target.value),
              placeholder: "Search models…",
            }),
            h("div", { className: "hcd-model-list" },
              h("div", {
                className: cn("hcd-model-row", value === "auto" && "active"),
                role: "button",
                tabIndex: 0,
                onClick: () => { onChange("auto"); setOpen(false); },
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

  // ── composer ────────────────────────────────────────────────────────

  function Composer({ disabled, readOnly, modes, models, agents, toolsets, selected, setSelected, attachments, setAttachments, onSend, onStop, generating, onBackground, draftSeed, clearDraft, gatewayStatus, onRetryConnect, onUndo, canUndo, pinnedModels, onSetPinnedModels, onModelChange }) {
    const [text, setText] = useState("");
    const [toolsOpen, setToolsOpen] = useState(false);
    const [drag, setDrag] = useState(false);
    const ta = useRef(null);

    useEffect(() => {
      if (draftSeed) {
        setText(draftSeed);
        clearDraft();
      }
    }, [draftSeed]);

    useEffect(() => {
      if (ta.current) {
        ta.current.style.height = "auto";
        ta.current.style.height = Math.min(240, ta.current.scrollHeight) + "px";
      }
    }, [text]);

    const upload = async (files) => {
      for (const file of Array.from(files || [])) {
        const local = { id: nowId(), name: file.name, size: file.size, status: "uploading" };
        setAttachments((a) => [...a, local]);
        const fd = new FormData();
        fd.append("file", file);
        fd.append("conversation_id", selected.sessionKey || selected.sessionId || "");
        try {
          const res = await fetchJSON(`${BASE}/attachments`, { method: "POST", body: fd });
          setAttachments((a) => a.map((x) => (x.id === local.id ? { ...res, status: "ready" } : x)));
        } catch (e) {
          setAttachments((a) => a.map((x) => (x.id === local.id ? { ...local, status: "error", error: e.message } : x)));
        }
      }
    };

    const submit = (background = false) => {
      const value = text.trim();
      if (!value && !attachments.length) return;
      if (background) onBackground(value);
      else onSend(value);
      setText("");
    };

    const onKeyDown = (e) => {
      if (e.key === "Enter" && !e.shiftKey && selected.enterToSend) {
        e.preventDefault();
        submit(false);
      } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        submit(false);
      }
    };

    const toolCount = selected.autoTools ? "auto" : (selected.tools || []).length;

    return h(
      "div",
      {
        className: cn("hcd-composer", drag && "drag", readOnly && "readonly"),
        onDragOver: (e) => { e.preventDefault(); setDrag(true); },
        onDragLeave: () => setDrag(false),
        onDrop: (e) => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files); },
      },
      attachments.length
        ? h(
            "div",
            { className: "hcd-attach-row" },
            attachments.map((a) =>
              h(
                "span",
                { key: a.id, className: cn("hcd-attach", a.status) },
                a.is_image ? "🖼️" : "📎",
                " ",
                a.name,
                a.status === "uploading" ? " …" : "",
                a.status === "error" ? " — upload failed" : "",
                h("button", { onClick: () => setAttachments((xs) => xs.filter((x) => x.id !== a.id)) }, "×"),
              ),
            ),
          )
        : null,
      h("div", { className: "hcd-composer-row" },
        h("textarea", {
          ref: ta,
          value: text,
          placeholder: readOnly
            ? "Session is read-only — reconnect to the gateway to continue"
            : selected.temporary
              ? "Temporary chat — nothing is saved to memory"
              : "Message Hermes…  (Drag files here or paste to attach)",
          disabled: disabled || readOnly,
          onPaste: (e) => {
            const fs = [...(e.clipboardData?.files || [])];
            if (fs.length) upload(fs);
          },
          onChange: (e) => setText(e.target.value),
          onKeyDown: onKeyDown,
        }),
      ),
      h(
        "div",
        { className: "hcd-controls" },
        h("label", { className: "hcd-ctl" }, "Mode",
          h("select", {
            value: selected.mode,
            onChange: (e) => setSelected((s) => ({ ...s, mode: e.target.value })),
          }, modes.map((m) => h("option", { value: m.id, key: m.id }, `${m.emoji || ""} ${m.label}`))),
        ),
        h(ModelPicker, {
          models,
          pinned: pinnedModels,
          value: selected.model,
          disabled,
          onChange: (v) => (onModelChange ? onModelChange(v) : setSelected((s) => ({ ...s, model: v, model_label: undefined }))),
          onPinnedChange: onSetPinnedModels,
        }),
        h("label", { className: "hcd-ctl" }, "Agent",
          h("select", {
            value: selected.agent,
            onChange: (e) => setSelected((s) => ({ ...s, agent: e.target.value })),
          }, agents.map((a) => h("option", { value: a.id, key: a.id }, a.label))),
        ),
        h("button", { className: cn("hcd-ctl-btn", toolsOpen && "on"), onClick: () => setToolsOpen((v) => !v), title: "Choose tools" },
          `🛠️ Tools${toolCount === "auto" ? " · auto" : toolCount ? ` · ${toolCount}` : ""}`),
        h("button", { className: cn("hcd-ctl-btn", selected.autoTools && "on"), onClick: () => setSelected((s) => ({ ...s, autoTools: !s.autoTools })), title: "Auto-select tools" }, "Auto"),
        h("button", { className: cn("hcd-ctl-btn", selected.temporary && "on"), onClick: () => setSelected((s) => ({ ...s, temporary: !s.temporary })), title: "Ephemeral chat" }, "Temp"),
        h("label", { className: "hcd-ctl-btn hcd-upload", title: "Attach files" }, "📎",
          h("input", { type: "file", multiple: true, onChange: (e) => upload(e.target.files), hidden: true }),
        ),
        h("span", { className: "hcd-spacer" }),
        canUndo && !readOnly ? h("button", { className: "secondary", onClick: onUndo, disabled: disabled || generating, title: "Remove the last turn" }, "↩ Undo") : null,
        readOnly
          ? h("button", { className: "secondary", onClick: onRetryConnect, disabled: disabled }, "Reconnect")
          : generating
            ? h("button", { className: "danger", onClick: onStop }, "■ Stop")
            : h("button", { className: "secondary", onClick: () => submit(true), disabled: disabled }, "Background"),
        h("button", { className: "hcd-send", onClick: () => submit(false), disabled: disabled || readOnly, title: "Send (Enter)" }, "Send ↵"),
      ),
      toolsOpen
        ? h(
            "div",
            { className: "hcd-tools-pop" },
            h("div", { className: "hcd-tools-pop-head" },
              h("strong", null, "Toolsets"),
              h("button", { onClick: () => setSelected((s) => ({ ...s, autoTools: !s.autoTools, tools: [] })) }, selected.autoTools ? "Use Auto" : "Use manual"),
            ),
            h("div", { className: "hcd-tools-grid" },
              toolsets.map((t) => {
                const id = t.id || t.name;
                return h("label", { key: id, className: cn("hcd-tool-toggle", selected.autoTools && "disabled") },
                  h("input", {
                    type: "checkbox",
                    disabled: selected.autoTools,
                    checked: (selected.tools || []).includes(id),
                    onChange: (e) => setSelected((s) => ({
                      ...s,
                      autoTools: false,
                      tools: e.target.checked ? [...new Set([...(s.tools || []), id])] : (s.tools || []).filter((x) => x !== id),
                    })),
                  }),
                  h("span", null, toolIcon(id), " ", t.label || t.name),
                  h("small", null, `${t.tool_count || 0} tools`),
                  t.description ? h("small", { className: "desc" }, t.description) : null,
                );
              }),
            ),
          )
        : null,
    );
  }

  // ── session sidebar ─────────────────────────────────────────────────

  const sorter = (a, b) => (num(b.started_at) - num(a.started_at)) || String(a.id).localeCompare(String(b.id));

  function SessionSidebar({ sessions, meta, currentKey, query, setQuery, filter, setFilter, onNew, onOpen, onMeta, onRename, onDelete, loading, hasMore, onLoadMore }) {
    const [menuFor, setMenuFor] = useState(null);
    const [confirmDelete, setConfirmDelete] = useState(null);

    useEffect(() => {
      if (!menuFor) return;
      const close = () => setMenuFor(null);
      window.addEventListener("click", close);
      return () => window.removeEventListener("click", close);
    }, [menuFor]);

    const q = (query || "").trim().toLowerCase();
    const visible = useMemo(
      () =>
        sessions
          .filter((s) => !q || `${s.title || ""} ${s.preview || ""} ${s.id}`.toLowerCase().includes(q))
          .filter((s) => (filter === "pinned" ? meta[s.id]?.pinned || meta[s.id]?.starred : true))
          .filter((s) => (filter === "archived" ? !!meta[s.id]?.archived : !meta[s.id]?.archived))
          .sort(sorter),
      [sessions, meta, q, filter],
    );

    const grouped = useMemo(() => {
      const groups = [];
      let today = null, yesterday = null, week = null, older = null;
      const now = new Date();
      const dayStart = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
      const todayStart = dayStart(now);
      const yStart = todayStart - 86400;
      const wStart = todayStart - 6 * 86400;
      for (const s of visible) {
        const t = num(s.started_at);
        const g = !t ? "older" : t >= todayStart ? "today" : t >= yStart ? "yesterday" : t >= wStart ? "week" : "older";
        if (g === "today") (today = today || []).push(s);
        else if (g === "yesterday") (yesterday = yesterday || []).push(s);
        else if (g === "week") (week = week || []).push(s);
        else (older = older || []).push(s);
      }
      if (today) groups.push(["Today", today]);
      if (yesterday) groups.push(["Yesterday", yesterday]);
      if (week) groups.push(["Previous 7 days", week]);
      if (older) groups.push(["Earlier", older]);
      return groups;
    }, [visible]);

    const menu = (s) =>
      h("div", { className: "hcd-conv-menu", onClick: (e) => e.stopPropagation() },
        h("button", { onClick: () => { onMeta(s.id, { pinned: !meta[s.id]?.pinned }); setMenuFor(null); } }, meta[s.id]?.pinned ? "📌 Unpin" : "📌 Pin"),
        h("button", { onClick: () => { onMeta(s.id, { starred: !meta[s.id]?.starred }); setMenuFor(null); } }, meta[s.id]?.starred ? "★ Unstar" : "☆ Star"),
        h("button", { onClick: () => { const title = window.prompt("Rename conversation", s.title || ""); if (title && title.trim()) onRename(s.id, title.trim()); setMenuFor(null); } }, "✏️ Rename"),
        h("button", { onClick: () => { const folder = window.prompt("Folder name (empty to remove)", meta[s.id]?.folder || ""); if (folder !== null) onMeta(s.id, { folder: folder.trim() }); setMenuFor(null); } }, meta[s.id]?.folder ? `📁 ${meta[s.id].folder}` : "📁 Folder…"),
        h("button", { onClick: () => { const tags = window.prompt("Tags (comma separated)", (meta[s.id]?.tags || []).join(", ")); if (tags !== null) onMeta(s.id, { tags: tags.split(",").map((t) => t.trim()).filter(Boolean) }); setMenuFor(null); } }, meta[s.id]?.tags?.length ? `🏷️ ${meta[s.id].tags.length} tag${meta[s.id].tags.length === 1 ? "" : "s"}` : "🏷️ Tags…"),
        h("button", { onClick: () => { onMeta(s.id, { archived: !meta[s.id]?.archived }); setMenuFor(null); } }, meta[s.id]?.archived ? "↩️ Unarchive" : "🗄️ Archive"),
        confirmDelete === s.id
          ? h("button", { className: "danger", onClick: () => { onDelete(s.id); setConfirmDelete(null); setMenuFor(null); } }, "⚠️ Confirm delete")
          : h("button", { className: "danger", onClick: () => setConfirmDelete(s.id) }, "🗑️ Delete"),
      );

    return h("aside", { className: "hcd-left" },
      h("div", { className: "hcd-side-head" },
        h("div", null, h("strong", null, "💬 Chats"), h("small", null, `${sessions.length} conversations`)),
        h("button", { className: "hcd-new-btn", onClick: onNew, title: "New chat (⌘⇧O)" }, "+ New"),
      ),
      h("div", { className: "hcd-search-row" },
        h("input", { className: "hcd-search", value: query, onChange: (e) => setQuery(e.target.value), placeholder: "Search conversations…", onKeyDown: (e) => { if (e.key === "Escape") setQuery(""); } }),
        query ? h("button", { className: "hcd-search-clear", onClick: () => setQuery("") }, "×") : null,
      ),
      h("div", { className: "hcd-filters" },
        ["all", "pinned", "archived"].map((f) =>
          h("button", { key: f, className: cn("hcd-filter", filter === f && "active"), onClick: () => setFilter(f) },
            f === "all" ? "All" : f === "pinned" ? "Pinned" : "Archived"),
        ),
      ),
      loading
        ? h("div", { className: "hcd-empty-small" }, "Loading conversations…")
        : visible.length === 0
          ? h(Empty, { icon: "💬", title: "No conversations", sub: q ? "No results for your search." : "Start a new chat to see history here." })
          : h("div", { className: "hcd-groups" },
              grouped.map(([label, arr]) =>
                h("section", { key: label },
                  h("h4", null, label),
                  arr.map((s) => {
                    const active = currentKey === s.id;
                    const m_ = meta[s.id] || {};
                    return h("div", { key: s.id, className: cn("hcd-conv-wrap", active && "active") },
                      h("button", { className: cn("hcd-conv", active && "active"), onClick: () => onOpen(s.id) },
                        h("span", { className: "hcd-conv-title" },
                          m_.pinned ? "📌 " : m_.starred ? "★ " : "",
                          m_.folder ? `📁 ` : "",
                          s.title || s.preview || "Untitled chat"),
                        h("span", { className: "hcd-conv-preview" },
                          s.snippet ? `“${s.snippet}”` : s.preview || s.id),
                        h("span", { className: "hcd-conv-meta" },
                          relTime(s.started_at),
                          num(s.message_count) ? ` · ${s.message_count} msg` : "",
                          m_.tags && m_.tags.length ? ` · 🏷️ ${m_.tags.join(", ")}` : "",
                        ),
                      ),
                      h("button", { className: "hcd-conv-more", onClick: (e) => { e.stopPropagation(); setMenuFor(menuFor === s.id ? null : s.id); }, title: "More actions" }, "⋯"),
                      menuFor === s.id ? menu(s) : null,
                    );
                  }),
                ),
              ),
              hasMore
                ? h("button", { className: "hcd-load-more", onClick: onLoadMore }, "Load more")
                : null,
            ),
    );
  }

  // ── right panel ─────────────────────────────────────────────────────

  function RightPanel({ tab, setTab, tools, prompts, tasks, files, sessionInfo, onClearTools, collapsed, setCollapsed, onExport, onShare, onDeleteCurrent, onRenameCurrent, onRespondPrompt }) {
    if (collapsed)
      return h("aside", { className: "hcd-context collapsed" },
        h("button", { className: "hcd-collapse", onClick: () => setCollapsed(false), title: "Expand panel" }, "◀"),
      );

    const running = tools.filter((t) => t.status === "running" || t.status === "pending").length;

    return h("aside", { className: "hcd-context" },
      h("div", { className: "hcd-context-head" },
        h("button", { className: "hcd-collapse", onClick: () => setCollapsed(true), title: "Collapse panel" }, "▶"),
        h("div", { className: "hcd-context-tabs" },
          h("button", { className: cn("hcd-tab", tab === "activity" && "active"), onClick: () => setTab("activity") }, "Activity"),
          h("button", { className: cn("hcd-tab", tab === "info" && "active"), onClick: () => setTab("info") }, "Info"),
        ),
      ),
      tab === "activity"
        ? h("div", null,
            h("div", { className: "hcd-panel-tools" },
              h("h4", null, "Tool activity"),
              running ? h("span", { className: "hcd-badge running" }, `${running} running`) : null,
              tools.length ? h("button", { className: "hcd-clear", onClick: onClearTools }, "Clear") : null,
            ),
            tools.length === 0 && prompts.length === 0
              ? h(Empty, { icon: "🛠️", title: "No tool activity", sub: "Tool calls appear here as they run, in order." })
              : h("div", { className: "hcd-tool-list" },
                  prompts.map((p) => h(PromptCard, { key: p.id, prompt: p, onRespond: onRespondPrompt })),
                  tools.map((t, i) => h(ToolCard, { key: t.key || t.tool_id || i, tool: t })),
                ),
          )
        : h("div", null,
            h("h4", null, "Conversation"),
            sessionInfo
              ? h("div", { className: "hcd-session-info" },
                  h("div", { className: "hcd-session-row" }, h("span", null, "Title"), h("strong", null, sessionInfo.title || "Untitled")),
                  h("div", { className: "hcd-session-row" }, h("span", null, "Created"), h("strong", null, fmtTime(sessionInfo.started_at) || "—")),
                  h("div", { className: "hcd-session-row" }, h("span", null, "Messages"), h("strong", null, String(sessionInfo.message_count || 0))),
                  h("div", { className: "hcd-session-row" }, h("span", null, "Model"), h("strong", null, sessionInfo.model || "—")),
                  h("div", { className: "hcd-session-row" }, h("span", null, "Source"), h("strong", null, sessionInfo.source || "—")),
                  h("div", { className: "hcd-session-row" }, h("span", null, "ID"), h("code", null, sessionInfo.id || "")),
                )
              : h("p", { className: "hcd-empty-small" }, "Open a conversation to see its details."),
            h("div", { className: "hcd-session-actions" },
              h("button", { onClick: () => onRenameCurrent() }, "✏️ Rename"),
              h("button", { onClick: () => onExport("markdown") }, "⬇️ Export"),
              h("button", { onClick: () => onShare() }, "🔗 Share"),
              h("button", { className: "danger", onClick: () => onDeleteCurrent() }, "🗑️ Delete"),
            ),
            h("h4", null, "Background tasks"),
            tasks.length
              ? tasks.map((t) =>
                  h("div", { className: cn("hcd-task", t.done && "done"), key: t.id },
                    h("strong", null, t.done ? "✓ " : "🤖 ", t.done ? "Task finished" : "Task running"),
                    h("p", null, t.text),
                  ))
              : h("p", { className: "hcd-empty-small" }, "No background tasks."),
            h("h4", null, "Attachments"),
            files && files.length
              ? h("div", { className: "hcd-file-list" },
                  files.map((f) => h("div", { className: "hcd-file", key: f.id || f.path }, "📎 ", f.name || f.path)))
              : h("p", { className: "hcd-empty-small" }, "Files attached to the current message appear here."),
          ),
    );
  }

  // ── settings modal ──────────────────────────────────────────────────

  function SettingsModal({ settings, setSettings, modes, models, agents, toolsets, onClose }) {
    const update = (patch) => {
      const next = { ...settings, ...patch };
      setSettings(next);
      fetchJSON(`${BASE}/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) }).catch(() => {});
    };
    const Toggle = ({ label, checked, onChange, hint }) =>
      h("label", { className: "hcd-setting-row" },
        h("input", { type: "checkbox", checked: !!checked, onChange: (e) => onChange(e.target.checked) }),
        h("span", null, label, hint && h("small", null, hint)),
      );
    const Select = ({ label, value, onChange, children }) =>
      h("label", { className: "hcd-setting-row" },
        h("span", null, label),
        h("select", { value, onChange: (e) => onChange(e.target.value) }, children),
      );

    return h("div", { className: "hcd-modal", onClick: (e) => e.target === e.currentTarget && onClose() },
      h("div", { className: "hcd-settings" },
        h("button", { className: "hcd-x", onClick: onClose }, "×"),
        h("h2", null, "Chat settings"),
        h("h3", null, "Defaults for new chats"),
        Select({ label: "Default mode", value: settings.defaultMode || "fast", onChange: (v) => update({ defaultMode: v }) },
          modes.map((m) => h("option", { value: m.id, key: m.id }, `${m.emoji || ""} ${m.label}`))),
        Select({ label: "Default model", value: settings.defaultModel || "auto", onChange: (v) => update({ defaultModel: v }) },
          h("option", { value: "auto" }, "Auto"),
          models.slice(0, 250).map((m) => h("option", { value: m.id, key: m.id }, `${m.provider || ""}/${m.name || m.model}`))),
        Select({ label: "Default agent", value: settings.defaultAgent || "auto", onChange: (v) => update({ defaultAgent: v }) },
          agents.map((a) => h("option", { value: a.id, key: a.id }, a.label))),
        h("label", { className: "hcd-setting-row hcd-setting-tools" },
          h("span", null, "Default toolsets"),
          h("div", { className: "hcd-setting-chips" },
            toolsets.map((t) => {
              const id = t.id || t.name;
              const on = (settings.defaultTools || []).includes(id);
              return h("button", {
                key: id,
                className: cn("hcd-chip", on && "on"),
                onClick: () => update({ defaultTools: on ? (settings.defaultTools || []).filter((x) => x !== id) : [...new Set([...(settings.defaultTools || []), id])] }),
              }, toolIcon(id), " ", t.label || t.name);
            }),
          ),
        ),
        h("h3", null, "Appearance"),
        Select({ label: "Density", value: settings.density, onChange: (v) => update({ density: v }) },
          h("option", { value: "comfortable" }, "Comfortable"),
          h("option", { value: "compact" }, "Compact")),
        Select({ label: "Message width", value: settings.messageWidth || "wide", onChange: (v) => update({ messageWidth: v }) },
          h("option", { value: "narrow" }, "Narrow"),
          h("option", { value: "wide" }, "Wide"),
          h("option", { value: "full" }, "Full")),
        Select({ label: "Font size", value: settings.fontSize || "medium", onChange: (v) => update({ fontSize: v }) },
          h("option", { value: "small" }, "Small"),
          h("option", { value: "medium" }, "Medium"),
          h("option", { value: "large" }, "Large")),
        Toggle({ label: "Show timestamps", checked: settings.showTimestamps, onChange: (v) => update({ showTimestamps: v }) }),
        Toggle({ label: "Show token usage", checked: settings.showUsage !== false, onChange: (v) => update({ showUsage: v }) }),
        Toggle({ label: "Show reasoning blocks", checked: settings.showReasoning !== false, onChange: (v) => update({ showReasoning: v }) }),
        Toggle({ label: "Auto-scroll to newest message", checked: settings.autoScroll !== false, onChange: (v) => update({ autoScroll: v }) }),
        h("h3", null, "Behaviour"),
        Toggle({ label: "Enter sends (Shift+Enter = newline)", checked: settings.enterToSend !== false, onChange: (v) => update({ enterToSend: v }) }),
        Toggle({ label: "Auto-select tools", checked: settings.autoTools !== false, onChange: (v) => update({ autoTools: v }) }),
        Toggle({ label: "Auto-title conversations", hint: "Hermes names chats after the first message.", checked: settings.autoTitle !== false, onChange: (v) => update({ autoTitle: v }) }),
        Toggle({ label: "Confirm before deleting a session", checked: !!settings.confirmDelete, onChange: (v) => update({ confirmDelete: v }) }),
        h("h3", null, "Privacy"),
        Toggle({ label: "Save history", checked: settings.saveHistory !== false, onChange: (v) => update({ saveHistory: v }) }),
        Toggle({ label: "Use long-term memory", checked: settings.memoryEnabled !== false, onChange: (v) => update({ memoryEnabled: v }) }),
        Toggle({ label: "New chats are temporary by default", checked: !!settings.temporaryDefault, onChange: (v) => update({ temporaryDefault: v }) }),
        h("p", { className: "hcd-settings-note" }, "Preferences are stored per browser on this device and synced to the server for this plugin."),
      ),
    );
  }

  // ── top bar ─────────────────────────────────────────────────────────

  function TopBar({ title, subtitle, status, onMenu, onExport, onShare, onSettings, onNew, readOnly, extra }) {
    return h("header", { className: "hcd-topbar" },
      h("button", { className: "hcd-mobile", onClick: onMenu }, "☰"),
      h("div", { className: "hcd-topbar-title" },
        h("strong", null, title || "Hermes Chat"),
        h("small", null,
          h(StatusDot, { status }),
          " ",
          subtitle || (status === "open" ? "Connected to Hermes gateway" : status === "connecting" ? "Connecting to gateway…" : "Gateway offline — history is read-only")),
      ),
      h("div", { className: "hcd-topbar-actions" },
        extra || null,
        readOnly ? null : h("button", { onClick: onNew, title: "New chat (⌘⇧O)" }, "+ New"),
        h("button", { onClick: () => onExport("markdown"), title: "Export conversation" }, "Export"),
        h("button", { onClick: onShare, title: "Create read-only share link" }, "Share"),
        h("button", { onClick: onSettings, title: "Settings" }, "⚙️"),
      ),
    );
  }

  // ── welcome screen ──────────────────────────────────────────────────

  const QUICK_PROMPTS = [
    ["🧠 Ask Hermes", "Answer this question with the right level of detail:\n"],
    ["💻 Write code", "Help me implement or debug this code task. First inspect context, then propose and apply a safe plan:\n"],
    ["🔎 Research", "Research this thoroughly, gather sources, compare findings, and summarize with citations:\n"],
    ["📄 Analyse a file", "I will attach a file. Read it, identify the important structure and findings, then suggest next actions."],
    ["🧪 Run an experiment", "Design and run a small experiment to test this hypothesis:\n"],
    ["🤖 Spawn an agent", "Use Hermes agents to plan and execute this multi-step objective:\n"],
    ["⚙️ Manage the system", "Inspect and help manage this Hermes/Render system task safely:\n"],
    ["📊 Analyse data", "Analyse this dataset or data question and produce clear conclusions:\n"],
  ];

  function Welcome({ onQuick }) {
    return h("div", { className: "hcd-welcome" },
      h("h1", null, "What can Hermes do for you?"),
      h("p", null, "Chat with the same Hermes brain that powers tools, memory, plugins, agents and background tasks."),
      h("div", { className: "hcd-quick" },
        QUICK_PROMPTS.map(([label, prompt]) =>
          h("button", { key: label, onClick: () => onQuick(prompt) }, h("span", null, label), h("small", null, "Click to start")),
        ),
      ),
      h("div", { className: "hcd-shortcuts" },
        h("kbd", null, "⌘K"), " search conversations · ",
        h("kbd", null, "⌘⇧O"), " new chat · ",
        h("kbd", null, "Esc"), " stop generation",
      ),
    );
  }

  // ── main app ────────────────────────────────────────────────────────

  function ChatDashboard() {
    const gwRef = useRef(null);
    const activeSidRef = useRef("");
    const listEndRef = useRef(null);
    const messagesRef = useRef([]);
    const toolsRef = useRef([]);
    const selectedRef = useRef({});
    const settingsRef = useRef({});
    const turnRef = useRef({ token: 0, assistantId: null });
    const releaseSessionRef = useRef(null);

    const [ready, setReady] = useState(false);
    const [conn, setConn] = useState("idle");
    const [error, setError] = useState("");
    const [toasts, setToasts] = useState([]);
    const [cap, setCap] = useState({ modes: [], models: [], agents: [], toolsets: [], features: {} });
    const [settings, setSettings] = useState({ enterToSend: true, autoTools: true, memoryEnabled: true, showTimestamps: true, autoScroll: true, showReasoning: true, messageWidth: "wide", density: "comfortable", fontSize: "medium", showUsage: true, confirmDelete: false, defaultMode: "fast", defaultModel: "", defaultAgent: "auto", defaultTools: [], temporaryDefault: false, pinnedModels: [] });
    const [selected, setSelected] = useState({ sessionId: "", sessionKey: "", mode: "fast", model: "auto", agent: "auto", tools: [], autoTools: true, temporary: false, memoryEnabled: true, enterToSend: true });
    const [readOnly, setReadOnly] = useState(false);
    const [sessions, setSessions] = useState([]);
    const [meta, setMeta] = useState({});
    const [query, setQuery] = useState("");
    const [filter, setFilter] = useState("all");
    const [loadingSessions, setLoadingSessions] = useState(false);
    const [hasMoreSessions, setHasMoreSessions] = useState(false);
    const [messages, setMessages] = useState([]);
    const [tools, setTools] = useState([]);
    const [prompts, setPrompts] = useState([]);
    const [generating, setGenerating] = useState(false);
    const [activity, setActivity] = useState("");
    const [attachments, setAttachments] = useState([]);
    const [tasks, setTasks] = useState([]);
    const [contextCollapsed, setContextCollapsed] = useState(false);
    const [rightTab, setRightTab] = useState("activity");
    const [showSettings, setShowSettings] = useState(false);
    const [mobileSide, setMobileSide] = useState(false);
    const [draftSeed, setDraftSeed] = useState("");
    const [share, setShare] = useState("");
    const [titleInput, setTitleInput] = useState(null);

    // keep refs in sync for event handlers registered once
    selectedRef.current = selected;
    settingsRef.current = settings;
    useEffect(() => { messagesRef.current = messages; }, [messages]);
    useEffect(() => { toolsRef.current = tools; }, [tools]);

    const toast = useCallback((text, kind = "info") => {
      const id = nowId();
      setToasts((ts) => [...ts, { id, text, kind }]);
      setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), 4500);
    }, []);

    // ── sessions (REST-backed; independent of the gateway) ────────────

    const refreshSessions = useCallback(async (opts = {}) => {
      const { offset = 0, quiet = false, search = "" } = opts;
      setLoadingSessions((v) => (offset === 0 ? true : v));
      try {
        const qs = new URLSearchParams({ limit: "200", offset: String(offset) });
        const q = String(search || "").trim();
        if (q.length >= 2) qs.set("q", q);
        const [listRes, metaRes] = await Promise.all([
          fetchJSON(`${BASE}/sessions?${qs}`),
          fetchJSON(`${BASE}/metadata`),
        ]);
        const rows = (listRes.sessions || []);
        setSessions((prev) => {
          if (offset === 0) return rows;
          const seen = new Set(prev.map((s) => s.id));
          return [...prev, ...rows.filter((s) => !seen.has(s.id))];
        });
        setHasMoreSessions(!!listRes.has_more);
        setMeta(metaRes || {});
      } catch (e) {
        if (!quiet) {
          setError(`Could not load conversations: ${e.message}`);
          toast("Failed to load conversation list", "error");
        }
      } finally {
        setLoadingSessions(false);
      }
    }, []);

    useEffect(() => {
      refreshSessions();
      const poll = setInterval(() => refreshSessions({ quiet: true }), 30000);
      const onFocus = () => refreshSessions({ quiet: true });
      window.addEventListener("focus", onFocus);
      return () => { clearInterval(poll); window.removeEventListener("focus", onFocus); };
    }, [refreshSessions]);

    // Debounced server-side full-text search across message content.
    useEffect(() => {
      const q = query.trim();
      const timer = setTimeout(() => {
        refreshSessions({ quiet: true, search: q });
      }, 300);
      return () => clearTimeout(timer);
    }, [query, refreshSessions]);

    // ── gateway boot ──────────────────────────────────────────────────

    useEffect(() => {
      let disposed = false;
      async function boot() {
        try {
          const [c, st] = await Promise.all([
            fetchJSON(`${BASE}/capabilities`),
            fetchJSON(`${BASE}/settings`),
          ]);
          if (disposed) return;
          setCap(c);
          setSettings(st);
          setSelected((s) => ({
            ...s,
            mode: st.defaultMode || "fast",
            model: st.defaultModel || "auto",
            agent: st.defaultAgent || "auto",
            tools: Array.isArray(st.defaultTools) ? st.defaultTools : [],
            autoTools: st.autoTools !== false,
            temporary: !!st.temporaryDefault,
            memoryEnabled: st.memoryEnabled !== false,
            enterToSend: st.enterToSend !== false,
          }));
        } catch (e) {
          if (!disposed) { setError(e.message); toast("Plugin API unavailable", "error"); }
        }
        const gw = new Gateway();
        gwRef.current = gw;
        const off = gw.on("status", (ev) => setConn(ev.status));
        try {
          await gw.connect();
          if (!disposed) setReady(true);
        } catch (e) {
          if (!disposed) {
            setError("");
            setReadOnly(true);
            toast(`Gateway offline: ${e.message}`, "error");
          }
        }
        releaseSessionRef.current = () => {
          const sid = activeSidRef.current;
          if (!sid) return;
          activeSidRef.current = "";
          try { gw.request("session.close", { session_id: sid }, 10000).catch(() => {}); } catch { /* noop */ }
        };
      }
      boot();
      return () => {
        disposed = true;
        try { releaseSessionRef.current && releaseSessionRef.current(); } catch { /* noop */ }
        gwRef.current && gwRef.current.dispose();
      };
    }, [toast]);

    // ── gateway events ────────────────────────────────────────────────

    useEffect(() => {
      const gw = gwRef.current;
      if (!gw || !ready) return;

      const ownsEvent = (ev) => {
        const sid = ev.session_id || "";
        return !sid || sid === activeSidRef.current;
      };

      const applyText = (text) => {
        const id = turnRef.current.assistantId;
        if (!id) return;
        setMessages((ms) => {
          const copy = [...ms];
          const i = copy.findIndex((m) => m.id === id);
          if (i >= 0) {
            copy[i] = { ...copy[i], content: (copy[i].content || "") + text, streaming: true };
          }
          return copy;
        });
      };

      const offs = [
        gw.on("message.start", (ev) => {
          if (!ownsEvent(ev)) return;
          setGenerating(true);
          setActivity("🧠 Thinking…");
          const id = nowId();
          turnRef.current.assistantId = id;
          setMessages((ms) => [...ms, { id, role: "assistant", content: "", streaming: true, timestamp: Date.now() / 1000 }]);
        }),
        gw.on("message.delta", (ev) => {
          if (!ownsEvent(ev)) return;
          setActivity("✍️ Responding…");
          applyText(ev.payload?.text || "");
        }),
        gw.on("reasoning.delta", (ev) => {
          if (!ownsEvent(ev) || settingsRef.current.showReasoning === false) return;
          setActivity("🧠 Reasoning…");
          const id = turnRef.current.assistantId;
          if (!id) return;
          setMessages((ms) => ms.map((m) => m.id === id ? { ...m, reasoning: (m.reasoning || "") + (ev.payload?.text || "") } : m));
        }),
        gw.on("thinking.delta", (ev) => {
          if (!ownsEvent(ev)) return;
          const id = turnRef.current.assistantId;
          if (!id) return;
          setMessages((ms) => ms.map((m) => m.id === id ? { ...m, thinking: (m.thinking || "") + (ev.payload?.text || "") } : m));
        }),
        gw.on("message.complete", async (ev) => {
          if (!ownsEvent(ev)) return;
          setGenerating(false);
          setActivity("");
          const text = ev.payload?.text || "";
          const id = turnRef.current.assistantId;
          turnRef.current.assistantId = null;
          if (id) {
            setMessages((ms) => ms.map((m) => m.id === id
              ? {
                  ...m,
                  content: text || m.content,
                  streaming: false,
                  status: ev.payload?.status || "complete",
                  usage: ev.payload?.usage || null,
                  warning: ev.payload?.warning || null,
                  reasoning: m.reasoning || ev.payload?.reasoning || null,
                }
              : m));
          }
          setPrompts([]);
          await refreshSessions({ quiet: true });
          // discover the durable key once the DB row exists
          if (selectedRef.current.sessionId && !selectedRef.current.sessionKey) {
            try {
              const r = await gw.request("session.title", { session_id: selectedRef.current.sessionId });
              if (r && r.session_key) {
                setSelected((s) => ({ ...s, sessionKey: r.session_key }));
              }
            } catch { /* title may not exist yet */ }
          }
        }),
        gw.on("tool.start", (ev) => {
          if (!ownsEvent(ev)) return;
          const p = ev.payload || {};
          const key = p.tool_id || nowId();
          setTools((ts) => [...ts, { key, tool_id: p.tool_id || key, name: p.name || "tool", context: p.context || "", status: "running", started_at: Date.now() / 1000, turn: turnRef.current.token }]);
          setActivity(`🛠️ Running ${p.name || "tool"}…`);
        }),
        gw.on("tool.progress", (ev) => {
          if (!ownsEvent(ev)) return;
          const p = ev.payload || {};
          setTools((ts) => {
            const copy = [...ts];
            const i = [...copy].reverse().findIndex((t) => t.status === "running" || t.status === "pending");
            if (i >= 0) {
              const idx = copy.length - 1 - i;
              copy[idx] = { ...copy[idx], preview: p.preview || copy[idx].preview, name: p.name || copy[idx].name };
            }
            return copy;
          });
          setActivity(`🛠️ ${p.preview || p.name || "Tool running"}`);
        }),
        gw.on("tool.generating", (ev) => {
          if (!ownsEvent(ev)) return;
          setActivity(`🛠️ Preparing ${ev.payload?.name || "tool"}…`);
        }),
        gw.on("tool.complete", (ev) => {
          if (!ownsEvent(ev)) return;
          const p = ev.payload || {};
          setTools((ts) => {
            if (!p.tool_id) return ts;
            const i = ts.findIndex((t) => String(t.tool_id) === String(p.tool_id));
            if (i < 0) return [...ts, { key: p.tool_id, tool_id: p.tool_id, name: p.name || "tool", status: "complete", ...p }];
            const copy = [...ts];
            copy[i] = { ...copy[i], ...p, status: p.status || "complete" };
            return copy;
          });
        }),
        ...["subagent.started", "subagent.progress", "subagent.complete", "subagent.summary", "subagent.tool"].map((type) =>
          gw.on(type, (ev) => {
            if (!ownsEvent(ev)) return;
            const p = ev.payload || {};
            const key = `${type}-${p.subagent_id || p.goal || nowId()}-${p.task_index || 0}`;
            setTools((ts) => {
              const i = ts.findIndex((t) => t.key === key);
              if (i >= 0) {
                const copy = [...ts];
                copy[i] = { ...copy[i], ...p, key, kind: "subagent", status: type === "subagent.complete" ? "complete" : "running" };
                return copy;
              }
              return [...ts, { key, kind: "subagent", title: p.goal || "Subagent", status: "running", ...p }];
            });
          }),
        ),
        gw.on("approval.request", (ev) => {
          if (!ownsEvent(ev)) return;
          setPrompts((ps) => [...ps, { ...(ev.payload || {}), id: ev.payload?.request_id || nowId(), kind: "approval" }]);
          setActivity("⏸ Awaiting approval…");
        }),
        gw.on("clarify.request", (ev) => {
          if (!ownsEvent(ev)) return;
          setPrompts((ps) => [...ps, { ...(ev.payload || {}), id: ev.payload?.request_id || nowId(), kind: "clarify" }]);
          setActivity("❓ Needs clarification…");
        }),
        gw.on("sudo.request", (ev) => {
          if (!ownsEvent(ev)) return;
          setPrompts((ps) => [...ps, { ...(ev.payload || {}), id: ev.payload?.request_id || nowId(), kind: "sudo" }]);
          setActivity("🔐 sudo password required…");
        }),
        gw.on("secret.request", (ev) => {
          if (!ownsEvent(ev)) return;
          setPrompts((ps) => [...ps, { ...(ev.payload || {}), id: ev.payload?.request_id || nowId(), kind: "secret" }]);
          setActivity("🔑 secret value required…");
        }),
        gw.on("status.update", (ev) => setActivity(ev.payload?.text || ev.payload?.kind || "Working…")),
        gw.on("error", (ev) => {
          if (!ownsEvent(ev)) return;
          setGenerating(false);
          setActivity("");
          const id = turnRef.current.assistantId || nowId();
          turnRef.current.assistantId = null;
          setMessages((ms) => [...ms, { id, role: "assistant", content: "", error: ev.payload?.message || "Unknown error", timestamp: Date.now() / 1000 }]);
        }),
        gw.on("background.complete", (ev) => {
          if (!ownsEvent(ev)) return;
          setTasks((ts) => ts.map((t) => t.id === ev.payload?.task_id ? { ...t, done: true } : t));
          setMessages((ms) => [...ms, { id: nowId(), role: "system", content: `Background task ${ev.payload?.task_id || ""} completed:\n\n${ev.payload?.text || ""}`, timestamp: Date.now() / 1000 }]);
          refreshSessions({ quiet: true });
        }),
        gw.on("session.info", (ev) => {
          if (!ownsEvent(ev)) return;
          if (ev.payload?.model) setSelected((s) => ({ ...s, model_label: ev.payload.model }));
        }),
      ];
      return () => offs.forEach((off) => off());
    }, [refreshSessions, ready]);

    // auto-scroll
    useEffect(() => {
      if (settings.autoScroll !== false) {
        const el = listEndRef.current;
        if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "end" });
      }
    }, [messages, tools, activity, prompts]);

    // keyboard shortcuts
    useEffect(() => {
      const onKey = (e) => {
        const mod = e.ctrlKey || e.metaKey;
        if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); document.querySelector(".hcd-search")?.focus(); }
        if (mod && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); newChat(); }
        if (mod && e.key === "/") { e.preventDefault(); document.querySelector(".hcd-composer textarea")?.focus(); }
        if (e.key === "Escape") {
          if (prompts.length) return;
          if (generating) stop();
          setMobileSide(false);
          setTitleInput(null);
        }
        if (mod && e.shiftKey && e.key.toLowerCase() === "b") { e.preventDefault(); setMobileSide((s) => !s); }
      };
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    });

    // ── session helpers ───────────────────────────────────────────────

    const releaseSession = (sid) => {
      if (!sid) return;
      if (activeSidRef.current === sid) activeSidRef.current = "";
      try { gwRef.current && gwRef.current.request("session.close", { session_id: sid }, 10000).catch(() => {}); } catch { /* noop */ }
    };

    const ensureSession = async () => {
      if (selectedRef.current.sessionId) {
        activeSidRef.current = selectedRef.current.sessionId;
        return selectedRef.current.sessionId;
      }
      const res = await gwRef.current.request("session.create", { cols: 100 });
      activeSidRef.current = res.session_id;
      setSelected((s) => ({ ...s, sessionId: res.session_id }));
      setMessages([]);
      setTools([]);
      setPrompts([]);
      setReadOnly(false);
      return res.session_id;
    };

    const applySelectors = async (sid) => {
      const mode = cap.modes.find((m) => m.id === selectedRef.current.mode);
      const strategy = (mode && mode.strategy) || {};
      try { if (strategy.fast !== undefined) await gwRef.current.request("config.set", { session_id: sid, key: "fast", value: strategy.fast ? "fast" : "normal" }, 30000); } catch { /* non-fatal */ }
      try { if (strategy.yolo) await gwRef.current.request("config.set", { session_id: sid, key: "yolo", value: "on" }, 30000); } catch { /* non-fatal */ }
      const sel = selectedRef.current;
      if (sel.model && sel.model !== "auto" && !sel.model_label) {
        const m = cap.models.find((x) => x.id === sel.model);
        if (m) {
          try { await gwRef.current.request("config.set", { session_id: sid, key: "model", value: `${m.provider}:${m.model || m.name}` }, 60000); }
          catch (e) { toast(`Model switch failed: ${e.message}`, "error"); }
        }
      }
      if (!sel.autoTools && sel.tools && sel.tools.length) {
        try { await gwRef.current.request("tools.configure", { session_id: sid, action: "enable", names: sel.tools }, 60000); } catch { /* non-fatal */ }
      }
      return mode;
    };

    // Live model switching: applies to the active gateway session immediately;
    // for a fresh chat it is persisted when the first message is sent.
    const changeModel = async (mid) => {
      setSelected((s) => ({ ...s, model: mid }));
      const sid = selectedRef.current.sessionId;
      if (!sid || mid === "auto") return;
      const m = cap.models.find((x) => x.id === mid);
      if (!m) return;
      if (generating) {
        toast("Model change will apply to the next message", "info");
        return;
      }
      try {
        await gwRef.current.request("config.set", { session_id: sid, key: "model", value: `${m.provider}:${m.model || m.name}` }, 60000);
        setSelected((s) => ({ ...s, model_label: `${m.provider}/${m.model || m.name}` }));
        toast(`Model switched to ${m.name || m.model}`, "info");
      } catch (e) {
        toast(`Model switch failed: ${e.message}`, "error");
      }
    };

    const setPinnedModels = (pins) => {
      const next = { ...settingsRef.current, pinnedModels: Array.isArray(pins) ? pins : [] };
      setSettings(next);
      fetchJSON(`${BASE}/settings`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) }).catch(() => {});
    };

    const composePrompt = (text) => {
      const mode = cap.modes.find((m) => m.id === selectedRef.current.mode);
      const prefixes = [];
      if (mode && mode.strategy && mode.strategy.prompt) prefixes.push(`Mode instruction (${mode.label}): ${mode.strategy.prompt}`);
      if (selectedRef.current.agent && selectedRef.current.agent !== "auto")
        prefixes.push(`Agent preference: use the ${selectedRef.current.agent} specialist or Hermes multi-agent equivalent when helpful.`);
      if (selectedRef.current.temporary)
        prefixes.push("Privacy instruction: this is a temporary chat. Do not intentionally save facts from this turn to long-term memory or the user profile.");
      if (selectedRef.current.memoryEnabled === false)
        prefixes.push("Memory instruction: avoid using or updating long-term memory unless explicitly requested.");
      const refs = attachments.filter((a) => a.status === "ready").map((a) => a.prompt_reference || (a.path ? `@${a.path}` : "")).filter(Boolean);
      return `${prefixes.length ? prefixes.join("\n") + "\n\n" : ""}${refs.length ? `Attachments:\n${refs.join("\n")}\n\n` : ""}${text || "Please analyse the attached file(s)."}`;
    };

    const send = async (text) => {
      setError("");
      setReadOnly(false);
      try {
        const sid = await ensureSession();
        await applySelectors(sid);
        const readyAttachments = attachments.filter((a) => a.status === "ready");
        const value = text || "Please analyse the attached file(s).";
        turnRef.current.token += 1;
        setMessages((ms) => [...ms, { id: nowId(), role: "user", content: value, attachments: readyAttachments, timestamp: Date.now() / 1000 }]);
        const prompt = composePrompt(text);
        setAttachments([]);
        await gwRef.current.request("prompt.submit", { session_id: sid, text: prompt });
        setTimeout(async () => {
          try {
            const r = await gwRef.current.request("session.title", { session_id: sid });
            if (r && r.session_key) {
              setSelected((s) => ({ ...s, sessionKey: r.session_key }));
              refreshSessions({ quiet: true });
            }
          } catch { /* not yet saved */ }
        }, 1800);
      } catch (e) {
        setError(e.message);
        toast(`Could not send message: ${e.message}`, "error");
        setGenerating(false);
      }
    };

    const background = async (text) => {
      try {
        const sid = await ensureSession();
        const res = await gwRef.current.request("prompt.background", { session_id: sid, text: composePrompt(text) });
        setTasks((ts) => [{ id: res.task_id, text: text || "Background task", done: false }, ...ts]);
        setMessages((ms) => [...ms, { id: nowId(), role: "system", content: `Task created successfully: ${res.task_id}`, timestamp: Date.now() / 1000 }]);
      } catch (e) {
        toast(`Could not start background task: ${e.message}`, "error");
      }
    };

    const stop = () => {
      const sid = selectedRef.current.sessionId;
      if (sid) gwRef.current?.request("session.interrupt", { session_id: sid }, 10000).catch(() => {});
      setGenerating(false);
      setActivity("Stopped by user");
      toast("Generation stopped");
    };

    // ── open / switch sessions ────────────────────────────────────────

    const openSession = async (id) => {
      if (!id) return;
      setError("");
      setDraftSeed("");
      setShare("");
      setPrompts([]);
      setActivity("");
      setMobileSide(false);
      const prevSid = selectedRef.current.sessionId;
      if (prevSid) releaseSession(prevSid);
      setSelected((s) => ({ ...s, sessionId: "", sessionKey: id }));
      setMessages([]);
      setTools([]);
      try {
        // 1. Always try the durable REST transcript first — it works even
        // while the gateway is down and shows tool calls that the gateway
        // summary omits.
        try {
          const detail = await fetchJSON(`${BASE}/sessions/${encodeURIComponent(id)}`);
          const rows = detail.messages || [];
          const msgs = [];
          const toolCards = [];
          rows.forEach((m) => {
            if (m.role === "tool") {
              toolCards.push({ key: m.id, tool_id: m.id, name: m.tool_name || "tool", context: m.content || "", status: "complete", started_at: m.timestamp });
              return;
            }
            if (m.role === "assistant" && !m.content && !m.error) return;
            msgs.push({
              id: m.id || nowId(),
              role: m.role,
              content: m.content || "",
              timestamp: m.timestamp,
              status: m.role === "assistant" ? "complete" : undefined,
              usage: null,
            });
          });
          setMessages(msgs);
          setTools(toolCards);
        } catch (e) {
          // REST unavailable — the gateway resume below still supplies a
          // display transcript, so this is not fatal.
        }
        // 2. Attach to the gateway so the user can keep chatting.
        try {
          await gwRef.current.connect();
          const res = await gwRef.current.request("session.resume", { session_id: id, cols: 100 }, 180000);
          setReadOnly(false);
          setSelected((s) => ({ ...s, sessionId: res.session_id, sessionKey: res.resumed || id }));
          activeSidRef.current = res.session_id;
          // If the REST transcript failed, fall back to the gateway summary.
          if (!messagesRef.current.length && (res.messages || []).length) {
            setMessages((res.messages || []).map((m, i) => ({
              id: `${id}-${i}`,
              role: m.role,
              content: m.text || m.content || "",
              timestamp: m.timestamp,
            })));
          }
        } catch (e) {
          setReadOnly(true);
          setError("");
          toast(`Gateway could not open this session (still viewable): ${e.message}`, "warn");
        }
      } catch (e) {
        setError(e.message);
        toast(`Could not load conversation: ${e.message}`, "error");
      }
    };

    const newChat = () => {
      const sid = selectedRef.current.sessionId;
      releaseSession(sid);
      setSelected((s) => ({ ...s, sessionId: "", sessionKey: "" }));
      setMessages([]);
      setTools([]);
      setPrompts([]);
      setAttachments([]);
      setShare("");
      setReadOnly(false);
      setActivity("");
      setDraftSeed("");
      setRightTab("activity");
      setTimeout(() => document.querySelector(".hcd-composer textarea")?.focus(), 50);
    };

    const onMeta = async (sid, patch) => {
      setMeta((m) => ({ ...m, [sid]: { ...(m[sid] || {}), ...patch } }));
      try {
        await fetchJSON(`${BASE}/metadata/${encodeURIComponent(sid)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        });
      } catch (e) {
        toast(`Could not update conversation: ${e.message}`, "error");
      }
    };

    const onRename = async (sid, title) => {
      try {
        await fetchJSON(`${BASE}/sessions/${encodeURIComponent(sid)}/rename`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title }),
        });
        setSessions((ss) => ss.map((s) => (s.id === sid ? { ...s, title } : s)));
        toast("Conversation renamed");
      } catch (e) {
        toast(`Rename failed: ${e.message}`, "error");
      }
    };

    const onDelete = async (sid) => {
      if (settingsRef.current.confirmDelete && !window.confirm("Delete this conversation permanently?")) return;
      const active = selectedRef.current.sessionKey === sid && selectedRef.current.sessionId;
      try {
        if (active) {
          releaseSession(selectedRef.current.sessionId);
          await gwRef.current.request("session.delete", { session_id: sid }, 30000);
        } else {
          await fetchJSON(`${BASE}/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" });
        }
        setSessions((ss) => ss.filter((s) => s.id !== sid));
        if (selectedRef.current.sessionKey === sid) newChat();
        toast("Conversation deleted");
        refreshSessions({ quiet: true });
      } catch (e) {
        // gateway may refuse active deletes — try the backend as a fallback
        try {
          await fetchJSON(`${BASE}/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" });
          setSessions((ss) => ss.filter((s) => s.id !== sid));
          if (selectedRef.current.sessionKey === sid) newChat();
          toast("Conversation deleted");
          refreshSessions({ quiet: true });
        } catch (e2) {
          toast(`Delete failed: ${e2.message}`, "error");
        }
      }
    };

    // undo last turn
    const undo = async () => {
      if (!selectedRef.current.sessionId || generating) return;
      try {
        await gwRef.current.request("session.undo", { session_id: selectedRef.current.sessionId }, 30000);
        // refetch the durable transcript so the UI matches server state
        const key = selectedRef.current.sessionKey;
        if (key) {
          const detail = await fetchJSON(`${BASE}/sessions/${encodeURIComponent(key)}`);
          const rows = detail.messages || [];
          const msgs = [];
          const toolCards = [];
          rows.forEach((m) => {
            if (m.role === "tool") {
              toolCards.push({ key: m.id, tool_id: m.id, name: m.tool_name || "tool", context: m.content || "", status: "complete", started_at: m.timestamp });
              return;
            }
            if (m.role === "assistant" && !m.content && !m.error) return;
            msgs.push({ id: m.id || nowId(), role: m.role, content: m.content || "", timestamp: m.timestamp });
          });
          setMessages(msgs);
          setTools(toolCards);
        }
        toast("Last turn undone");
        refreshSessions({ quiet: true });
      } catch (e) {
        toast(`Undo failed: ${e.message}`, "error");
      }
    };

    // ── prompt responses (approval/clarify/sudo/secret) ───────────────

    const respondPrompt = async (kind, prompt, value) => {
      const sid = selectedRef.current.sessionId || prompt.session_id || "";
      const method = kind === "clarify" ? "clarify.respond" : kind === "sudo" ? "sudo.respond" : kind === "secret" ? "secret.respond" : "approval.respond";
      const params = {
        session_id: sid,
        request_id: prompt.request_id || prompt.id,
        response: value,
      };
      if (kind === "approval") {
        params.choice = value;
        delete params.response;
      }
      try {
        await gwRef.current.request(method, params, 30000);
        setPrompts((ps) => ps.filter((p) => p.id !== prompt.id && p.request_id !== prompt.request_id));
        setActivity("");
      } catch (e) {
        toast(`Response failed: ${e.message}`, "error");
      }
    };

    // ── message actions ───────────────────────────────────────────────

    const msgAction = async (action, msg, idx) => {
      if (action === "copy") return navigator.clipboard?.writeText(msg.content || "").then(() => toast("Copied to clipboard"));
      if (action === "delete") return setMessages((ms) => ms.filter((_, i) => i !== idx));
      if (action === "continue") return send("Please continue from where you left off.");
      if (action === "regenerate") {
        const prev = [...messages].slice(0, idx).reverse().find((m) => m.role === "user");
        return send(prev ? prev.content : "Please regenerate your previous response.");
      }
      if (action === "edit") {
        const next = window.prompt("Edit message", msg.content || "");
        if (next != null && next.trim()) {
          setMessages((ms) => ms.map((m, i) => (i === idx ? { ...m, content: next } : m)));
          return send(next);
        }
      }
      if (action === "branch") {
        const key = selectedRef.current.sessionKey || selectedRef.current.sessionId;
        if (!key || key.startsWith("local-")) return toast("Branch is available after the conversation is saved.", "warn");
        try {
          const r = await fetchJSON(`${BASE}/branch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: key, message_index: idx, title: "Branch" }),
          });
          await refreshSessions({ quiet: true });
          await openSession(r.session_id);
          toast("Branch created");
        } catch (e) {
          toast(`Branch failed: ${e.message}`, "error");
        }
      }
    };

    // ── export / share ────────────────────────────────────────────────

    const currentKey = selected.sessionKey || (selected.sessionId && !selected.sessionId.startsWith("local-") ? selected.sessionId : "");

    const exportChat = async (fmt) => {
      if (!currentKey) return toast("Export is available once the conversation is saved.", "warn");
      try {
        const headers = new Headers();
        if (window.__HERMES_SESSION_TOKEN__) headers.set("X-Hermes-Session-Token", window.__HERMES_SESSION_TOKEN__);
        const res = await fetch(`${BASE}/export/${encodeURIComponent(currentKey)}?format=${encodeURIComponent(fmt)}`, { headers });
        if (!res.ok) throw new Error(await res.text());
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `hermes-chat-${currentKey}.${fmt === "json" ? "json" : fmt === "txt" ? "txt" : "md"}`;
        a.click();
        URL.revokeObjectURL(url);
        toast("Conversation exported");
      } catch (e) {
        toast(`Export failed: ${e.message}`, "error");
      }
    };

    const shareChat = async () => {
      if (!currentKey) return toast("Share is available once the conversation is saved.", "warn");
      try {
        const r = await fetchJSON(`${BASE}/share/${encodeURIComponent(currentKey)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        const url = `${location.origin}${r.url}`;
        setShare(`${url} — copied to clipboard`);
        await navigator.clipboard?.writeText(url).catch(() => {});
        toast("Share link copied");
      } catch (e) {
        toast(`Share failed: ${e.message}`, "error");
      }
    };

    const setQuick = (promptText) => {
      newChat();
      setDraftSeed(promptText);
    };

    const retryConnect = async () => {
      setReadOnly(false);
      try {
        await gwRef.current.connect();
        setReady(true);
        toast("Gateway connected");
      } catch (e) {
        setReadOnly(true);
        toast(`Still offline: ${e.message}`, "error");
      }
    };

    const renameCurrent = async () => {
      if (!currentKey) return toast("Open a saved conversation first.", "warn");
      const next = window.prompt("Rename conversation", (sessions.find((s) => s.id === currentKey) || {}).title || "");
      if (next && next.trim()) {
        await onRename(currentKey, next.trim());
        setSelected((s) => ({ ...s, sessionKey: currentKey }));
      }
    };

    // ── render ────────────────────────────────────────────────────────

    const currentSession = sessions.find((s) => s.id === currentKey) || null;
    const title = currentSession?.title || (selected.sessionKey ? selected.sessionKey : "Hermes Chat");
    const files = attachments.filter((a) => a.status === "ready");
    const titleEl = titleInput !== null
      ? h("input", {
          className: "hcd-title-edit",
          defaultValue: title,
          autoFocus: true,
          onKeyDown: (e) => {
            if (e.key === "Enter" && e.target.value.trim()) {
              onRename(currentKey, e.target.value.trim()).then(() => setTitleInput(null));
            }
            if (e.key === "Escape") setTitleInput(null);
          },
          onBlur: (e) => {
            if (e.target.value.trim() && e.target.value.trim() !== title) onRename(currentKey, e.target.value.trim());
            setTitleInput(null);
          },
        })
      : h("strong", { title: "Double-click to rename", onDoubleClick: () => currentKey && setTitleInput(title) }, title);

    return h("div", { className: cn("hcd-root", settings.density === "compact" && "compact", settings.messageWidth === "narrow" && "narrow", settings.messageWidth === "full" && "full", settings.fontSize === "small" && "font-small", settings.fontSize === "large" && "font-large") },
      h(SessionSidebar, {
        sessions, meta, currentKey, query, setQuery, filter, setFilter,
        onNew: newChat, onOpen: openSession, onMeta, onRename, onDelete,
        loading: loadingSessions, hasMore: hasMoreSessions,
        onLoadMore: () => refreshSessions({ offset: sessions.length }),
      }),
      h("main", { className: "hcd-main" },
        h(TopBar, {
          title: titleEl, subtitle: activity || (selected.model_label ? `Model: ${selected.model_label}` : undefined),
          status: conn, onMenu: () => setMobileSide(true), onExport: exportChat, onShare: shareChat,
          onSettings: () => setShowSettings(true), onNew: newChat, readOnly,
          extra: generating && h("button", { className: "danger", onClick: stop }, "■ Stop"),
        }),
        error
          ? h("div", { className: "hcd-errorbar" },
              "Something went wrong. ",
              h("details", null, h("summary", null, "View details"), h("pre", null, error)),
              h("button", { onClick: () => setError("") }, "Dismiss"),
            )
          : null,
        share ? h("div", { className: "hcd-sharebar" }, "🔗 ", share, h("button", { onClick: () => setShare("") }, "×")) : null,
        h("div", { className: "hcd-scroll" },
          messages.length === 0 && tools.length === 0
            ? h(Welcome, { onQuick: setQuick })
            : h(React.Fragment, null,
                messages.map((m, i) => h(MessageView, {
                  key: m.id || i, msg: m, index: i, active: m.streaming,
                  onAction: msgAction, showTime: settings.showTimestamps !== false,
                  showUsage: settings.showUsage !== false,
                })),
                tools.length
                  ? h("div", { className: "hcd-tools-inline" },
                      h("div", { className: "hcd-tools-inline-head" },
                        h("span", null, `🛠️ Tools · ${tools.length}`),
                        h("button", { onClick: () => setTools([]) }, "Clear"),
                      ),
                      tools.map((t, i) => h(ToolCard, { key: t.key || t.tool_id || i, tool: t })),
                    )
                  : null,
                prompts.map((p) => h(PromptCard, { key: p.id, prompt: p, onRespond: respondPrompt })),
                activity && generating
                  ? h("div", { className: "hcd-activity" }, h("span", { className: "hcd-activity-dot" }), " ", activity)
                  : null,
                h("div", { ref: listEndRef }),
              ),
        ),
        h(Composer, {
          disabled: !ready, readOnly,
          modes: cap.modes, models: cap.models, agents: cap.agents, toolsets: cap.toolsets,
          selected, setSelected, attachments, setAttachments,
          onSend: send, onStop: stop, generating, onBackground: background,
          draftSeed, clearDraft: () => setDraftSeed(""),
          gatewayStatus: conn, onRetryConnect: retryConnect,
          onUndo: undo,
          canUndo: messages.length > 0 && !!selected.sessionId,
          pinnedModels: settings.pinnedModels || [],
          onSetPinnedModels: setPinnedModels,
          onModelChange: changeModel,
        }),
      ),
      h(RightPanel, {
        tab: rightTab, setTab: setRightTab,
        tools, prompts, tasks, files,
        sessionInfo: currentSession ? { ...currentSession, model: selected.model_label } : selected.sessionId ? { id: selected.sessionId, title, started_at: 0 } : null,
        onClearTools: () => setTools([]),
        collapsed: contextCollapsed, setCollapsed: setContextCollapsed,
        onExport: exportChat, onShare: shareChat, onDeleteCurrent: () => currentKey && onDelete(currentKey),
        onRenameCurrent: renameCurrent, onRespondPrompt: respondPrompt,
      }),
      showSettings && h(SettingsModal, {
        settings, setSettings,
        modes: cap.modes, models: cap.models, agents: cap.agents, toolsets: cap.toolsets,
        onClose: () => setShowSettings(false),
      }),
      h(ToastStack, { toasts, dismiss: (id) => setToasts((ts) => ts.filter((t) => t.id !== id)) }),
      h("div", { className: cn("hcd-mobile-drawer", mobileSide && "open") },
        h(SessionSidebar, {
          sessions, meta, currentKey, query, setQuery, filter, setFilter,
          onNew: newChat, onOpen: openSession, onMeta, onRename, onDelete,
          loading: loadingSessions, hasMore: hasMoreSessions,
          onLoadMore: () => refreshSessions({ offset: sessions.length }),
        }),
      ),
    );
  }

  registry.register("hermes-chat-dashboard", ChatDashboard);

  // ── "chat disabled" banner (unchanged upstream-guard behaviour) ─────

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
          " on images older than this fix — redeploy on the current image and it is cleaned up automatically), then restart the service."),
        h("button", { type: "button", onClick: () => { setDismissed(true); try { sessionStorage.setItem("hcd-disabled-banner", "1"); } catch { /* noop */ } } }, "Dismiss"),
      );
    }
    registry.registerSlot("hermes-chat-dashboard", "header-banner", ChatDisabledBanner);
  }
})();
