/* Minimal React-compatible renderer + browser shims for headless plugin tests.
 *
 * Not a real React — just enough to exercise every render path of the
 * hermes-chat-dashboard bundle: hooks (useState/useEffect/useRef/useMemo/
 * useCallback), function components, fragments, refs, event-handler capture
 * and a synthetic DOM. Re-renders the whole tree from the root on any state
 * change (fine for crash-hunting; reconciliation is not modelled).
 */
"use strict";

// ── synthetic DOM ────────────────────────────────────────────────────

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.textContent = "";
    this.value = "";
    this.disabled = false;
    this._listeners = {};
    this.parentNode = null;
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  addEventListener(t, fn) { (this._listeners[t] || (this._listeners[t] = [])).push(fn); }
  removeEventListener(t, fn) {
    const ls = this._listeners[t]; if (!ls) return;
    const i = ls.indexOf(fn); if (i >= 0) ls.splice(i, 1);
  }
  dispatch(t, ev) {
    ev = ev || {}; ev.type = t; ev.target = ev.target || this; ev.currentTarget = this;
    ev.preventDefault = ev.preventDefault || (() => {});
    ev.stopPropagation = ev.stopPropagation || (() => {});
    for (const fn of (this._listeners[t] || []).slice()) fn(ev);
  }
  focus() {}
  select() {}
  click() { this.dispatch("click", {}); }
  setAttribute() {}
  getBoundingClientRect() { return { top: 0, left: 0, bottom: 0, right: 0, width: 400, height: 300 }; }
  scrollIntoView() {}
  contains(n) {
    let cur = n;
    while (cur) { if (cur === this) return true; cur = cur.parentNode; }
    return false;
  }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  get innerText() { return this.textContent; }
}

const documentShim = {
  _root: new FakeElement("div"),
  createElement: (t) => new FakeElement(t),
  createTextNode: (t) => ({ textContent: t }),
  addEventListener(t, fn) { this._root.addEventListener(t, fn); },
  removeEventListener(t, fn) { this._root.removeEventListener(t, fn); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  getElementById() { return null; },
  body: new FakeElement("body"),
};
documentShim._root.appendChild(documentShim.body);

const locationShim = { protocol: "https:", host: "dashboard.test", origin: "https://dashboard.test", pathname: "/chat", search: "" };

const storageShim = () => {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
  };
};

// ── hooks + element factory ─────────────────────────────────────────

let instanceCursor = null;   // { path: [], slots: [], effects: [] } for the component being rendered
let renderScheduled = false;
let rootComponent = null;
let rootProps = null;
let renderCount = 0;
let lastTree = null;
const MAX_RENDERS_PER_FLUSH = 4000;

function resetRenderer(component, props) {
  rootComponent = component;
  rootProps = props || {};
  instances.clear();
  renderCount = 0;
}

const instances = new Map(); // path string → { slots: [], effects: [] }

function scheduleRender() {
  if (renderScheduled) return;
  renderScheduled = true;
  queueMicrotask(() => {
    renderScheduled = false;
    flushEffects(invokeRender());
  });
}

function invokeRender() {
  if (renderCount > MAX_RENDERS_PER_FLUSH) {
    throw new Error(`harness: exceeded ${MAX_RENDERS_PER_FLUSH} renders in one flush — runaway re-render loop`);
  }
  renderCount++;
  const pendingEffects = [];
  instanceCursor = { path: [], effects: pendingEffects };
  try {
    lastTree = renderNode(rootComponent, rootProps, instanceCursor.path);
  } finally {
    instanceCursor = null;
  }
  return pendingEffects;
}

function runEffects(pending) {
  for (const entry of pending) {
    if (entry.deps === undefined || entry.deps === null || !sameDeps(entry.deps, entry.prevDeps)) {
      if (typeof entry.cleanup === "function") { try { entry.cleanup(); } catch (e) { /* surface later */ throw e; } }
      entry.cleanup = entry.fn() || null;
      entry.prevDeps = entry.deps ? entry.deps.slice() : entry.deps;
    }
  }
}

function sameDeps(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  return a.every((v, i) => Object.is(v, b[i]));
}

function flushEffects(pending) { runEffects(pending); }

function useState(initial) {
  const inst = instanceCursor.inst;
  const idx = instanceCursor.hookIndex++;
  if (inst.slots.length === idx) {
    inst.slots.push({
      kind: "state",
      value: typeof initial === "function" ? initial() : initial,
    });
  }
  const slot = inst.slots[idx];
  const setter = (v) => {
    const next = typeof v === "function" ? v(slot.value) : v;
    if (Object.is(next, slot.value)) return;
    slot.value = next;
    scheduleRender();
  };
  return [slot.value, setter];
}

function useEffect(fn, deps) {
  const inst = instanceCursor.inst;
  const idx = instanceCursor.hookIndex++;
  if (inst.slots.length === idx) {
    const entry = { kind: "effect", fn, deps, cleanup: null, prevDeps: undefined };
    inst.slots.push(entry);
    instanceCursor.effects.push(entry);
    return;
  }
  const entry = inst.slots[idx];
  entry.fn = fn; entry.deps = deps;
  instanceCursor.effects.push(entry);
}

function useRef(initial) {
  const inst = instanceCursor.inst;
  const idx = instanceCursor.hookIndex++;
  if (inst.slots.length === idx) inst.slots.push({ kind: "ref", value: { current: initial } });
  return inst.slots[idx].value;
}

function useMemo(fn, deps) {
  const inst = instanceCursor.inst;
  const idx = instanceCursor.hookIndex++;
  if (inst.slots.length === idx) { inst.slots.push({ kind: "memo", value: fn(), deps }); return inst.slots[idx].value; }
  const slot = inst.slots[idx];
  if (!sameDeps(deps, slot.deps)) { slot.value = fn(); slot.deps = deps; }
  return slot.value;
}

function useCallback(fn, deps) { return useMemo(() => fn, deps); }

function createContext(defaultValue) {
  const ctx = { _current: defaultValue, Provider: null, Consumer: null };
  ctx.Provider = function Provider({ value, children }) { ctx._current = value; return children; };
  return ctx;
}
function useContext(ctx) { return ctx._current; }

const Fragment = Symbol("harness.fragment");

// minimal React.Component stand-in (enough for ErrorBoundary-style classes)
class Component {
  constructor(props, updater) { this.props = props || {}; this.state = null; this._updater = updater || null; }
  setState(partial) {
    if (!this._updater) return;
    this._updater(typeof partial === "function" ? partial(this.state) : partial);
  }
}
Component.prototype.isReactComponent = {};

function createElement(type, props, ...children) {
  const kids = children.flat(8);
  // real React semantics: children are also readable from props
  const p = props || {};
  if (kids.length && p.children === undefined) p.children = kids;
  return { type, props: p, children: kids };
}

function renderNode(node, path, instPath) {
  if (typeof node === "function") return renderNode({ type: node, props: {}, children: [] }, path, instPath);
  if (node == null || node === false || node === true) return null;
  if (typeof node === "string" || typeof node === "number") return { text: String(node) };
  if (Array.isArray(node)) return node.map((n, i) => renderNode(n, path, instPath ? `${instPath}/${i}` : null));
  if (typeof node.type === "function") {
    // class component (e.g. an ErrorBoundary): instantiate once per key,
    // wire state via getDerivedStateFromError/setState, render via .render()
    const isClass = node.type.prototype && typeof node.type.prototype.render === "function";
    const key = `${instPath || ""}::${node.type.name || "anon"}:${node.props && node.props.key != null ? node.props.key : "?"}`;
    let inst = instances.get(key);
    if (!inst) { inst = { slots: [] }; instances.set(key, inst); }
    const prevCursor = instanceCursor;
    instanceCursor = { path, inst, hookIndex: 0, effects: prevCursor.effects };
    let out;
    try {
      if (isClass) {
        if (!inst.classInst) {
          const updater = (partial) => {
            const self = inst.classInst;
            self.state = { ...(self.state || {}), ...(typeof partial === "function" ? partial(self.state) : partial) };
            scheduleRender();
          };
          inst.classInst = new node.type(node.props || {}, updater);
          // real React attaches the updater externally after construction
          // (class ctors call super(props) only) — mirror that
          inst.classInst._updater = updater;
        } else {
          inst.classInst.props = node.props || {};
        }
        out = inst.classInst.render();
      } else {
        out = node.type(node.props || {});
      }
    } finally {
      inst.slots.length = instanceCursor.hookIndex;
      instanceCursor = prevCursor;
    }
    return renderNode(out, path, key);
  }
  if (node.type === Fragment) {
    return { type: Fragment, props: node.props, children: (node.children || []).map((c, i) => renderNode(c, path, instPath ? `${instPath}/f${i}` : null)) };
  }
  // DOM-ish element: attach ref
  const { ref, ...rest } = node.props || {};
  if (ref) {
    if (typeof ref === "function") ref(new FakeElement(node.type));
    else {
      ref.current = ref.current && ref.current.tagName ? ref.current : Object.assign(new FakeElement(node.type), { tagName: String(node.type).toUpperCase() });
    }
  }
  return { type: node.type, props: rest, children: (node.children || []).map((c, i) => renderNode(c, path, instPath ? `${instPath}/d${i}` : null)) };
}

// ── tree walking utilities (find nodes / handlers in the last render) ──

function walkTree(node, fn, parents = []) {
  if (node == null) return;
  if (Array.isArray(node)) { for (const n of node) walkTree(n, fn, parents); return; }
  if (typeof node === "object") {
    fn(node, parents);
    if (node.children) for (const c of node.children) walkTree(c, fn, parents.concat([node]));
  }
}

function findAll(pred) {
  const out = [];
  walkTree(lastTree, (n, parents) => { if (n && n.type && pred(n, parents)) out.push({ node: n, parents }); });
  return out;
}

function textOf(node) {
  let s = "";
  walkTree(node, (n) => { if (n && n.text != null) s += n.text; if (n && n.type === Fragment) return; });
  return s;
}

function instancesIter() { return instances.entries(); }

module.exports = {
  _instances: instancesIter,
  FakeElement,
  documentShim,
  locationShim,
  storageShim,
  createElement,
  Fragment,
  hooks: { useState, useEffect, useRef, useMemo, useCallback, useContext, createContext },
  Component,
  resetRenderer,
  invokeRender,
  flushEffects,
  scheduleRender,
  findAll,
  textOf,
  walkTree,
  get lastTree() { return lastTree; },
};
