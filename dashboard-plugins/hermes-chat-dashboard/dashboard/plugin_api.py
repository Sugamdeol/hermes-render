#!/usr/bin/env python3
"""Polished web-chat dashboard plugin backend for Hermes.

The UI talks to the real Hermes ``tui_gateway`` WebSocket for conversation
creation, resume, streaming, tool events, cancellation, model switching, tool
configuration, steering, compaction, slash commands, subagent observability,
background jobs, and voice controls.  This plugin adds the small
web-dashboard-specific layer that upstream does not own yet: capability
presentation, durable UI metadata (pins/stars/tags/folders/archive), secure
share/export helpers, folder + tag registries, bulk actions, branch lineage,
usage rollups, and browser file uploads that are handed back to the existing
Hermes attachment/context-reference pipeline.

Every helper degrades gracefully on the pinned runtime: ``SessionDB`` method
surfaces differ between releases (``search_sessions`` vs ``search_messages``,
``get_messages`` with/without pagination), so calls are feature-detected with
``getattr`` and wrapped defensively.  History listing NEVER requires the
gateway — it keeps working read-only while ``tui_gateway`` restarts.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse

router = APIRouter()

MAX_UPLOAD_BYTES = int(os.environ.get("HERMES_CHAT_UPLOAD_MAX_BYTES", str(25 * 1024 * 1024)))
META_FILE = "chat_dashboard_meta.json"
SETTINGS_FILE = "chat_dashboard_settings.json"
SHARES_FILE = "chat_dashboard_shares.json"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Tool/activity payloads are kept bounded so a long agent turn can never pin
# hundreds of MB of transcript text in the dashboard process's memory.
MAX_TOOL_ROWS = 400
MAX_SESSION_ROWS = 500


def _require_session(request: Request) -> None:
    try:
        from hermes_cli.web_server import _has_valid_session_token
    except Exception:
        raise HTTPException(status_code=503, detail="dashboard auth helper unavailable")
    if not _has_valid_session_token(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _state_dir() -> Path:
    p = _home() / "dashboard_chat"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _json_path(name: str) -> Path:
    return _state_dir() / name


def _read_json(name: str, default: Any) -> Any:
    path = _json_path(name)
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(name: str, value: Any) -> None:
    path = _json_path(name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _session_db():
    try:
        from hermes_state import SessionDB
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"session database unavailable: {exc}")
    return SessionDB()


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _configured_modes(cfg: dict) -> list[dict]:
    raw = (((cfg.get("dashboard") or {}).get("chat") or {}).get("modes")
           or (cfg.get("chat_dashboard") or {}).get("modes"))
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict) and item.get("id") and item.get("label"):
                out.append(item)
        if out:
            return out
    return []


def _default_modes(toolsets: list[dict]) -> list[dict]:
    names = {str(t.get("name", "")).lower() for t in toolsets}
    has_web = any(n in names for n in ("web", "browser", "search"))
    has_code = any(n in names for n in ("code", "terminal", "shell", "python"))
    modes = [
        {"id": "fast", "emoji": "⚡", "label": "Fast", "description": "Quick answers with concise tool use.", "strategy": {"fast": True, "prompt": "Answer quickly and directly. Use tools only when they materially improve the answer."}},
        {"id": "reasoning", "emoji": "🧠", "label": "Reasoning", "description": "Deeper reasoning and verification.", "strategy": {"fast": False, "prompt": "Think carefully, verify assumptions, and present only a concise safe summary of your reasoning."}},
        {"id": "research", "emoji": "🔎", "label": "Research", "description": "Gather sources and cite evidence.", "recommended_tools": ["web", "browser"] if has_web else [], "strategy": {"prompt": "Use Hermes research capabilities and cite sources when available. Prefer current evidence over memory for factual claims."}},
        {"id": "coding", "emoji": "💻", "label": "Coding", "description": "Programming, debugging and repository work.", "recommended_tools": ["terminal", "code", "files"] if has_code else [], "strategy": {"prompt": "Act as a careful coding agent. Inspect before editing, run focused checks, and explain the changed files."}},
        {"id": "agent", "emoji": "🤖", "label": "Agent", "description": "Plan and execute multi-step tasks.", "strategy": {"prompt": "Plan the task, execute step by step using Hermes agents/tools, and report progress at high level."}},
        {"id": "autonomous", "emoji": "🛠️", "label": "Autonomous", "description": "Let Hermes use tools, plugins and agents with minimal interaction.", "strategy": {"yolo": True, "prompt": "Proceed autonomously when safe. Ask only for genuinely risky, destructive, or credential-sensitive actions."}},
        {"id": "study", "emoji": "📚", "label": "Study", "description": "Explanations, revision and learning.", "strategy": {"prompt": "Teach clearly with examples, checks for understanding, and a structured learning path."}},
        {"id": "writing", "emoji": "✍️", "label": "Writing", "description": "Drafting, editing and style work.", "strategy": {"prompt": "Help write and edit with attention to audience, structure, tone, and concrete revisions."}},
    ]
    return modes


@router.get("/capabilities")
async def capabilities(request: Request):
    _require_session(request)
    cfg = _load_config()

    toolsets: list[dict] = []
    try:
        from toolsets import get_all_toolsets, get_toolset_info
        enabled = set((((cfg.get("tools") or {}).get("enabled_toolsets")) or []))
        for name in sorted(get_all_toolsets().keys()):
            info = get_toolset_info(name) or {}
            toolsets.append({
                "id": name,
                "name": name,
                "label": name.replace("_", " ").title(),
                "description": info.get("description", ""),
                "tool_count": info.get("tool_count", 0),
                "enabled": (name in enabled) if enabled else True,
            })
    except Exception:
        pass

    models: list[dict] = []
    try:
        from hermes_cli.model_switch import list_authenticated_providers
        user_providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
        custom_providers = cfg.get("custom_providers") if isinstance(cfg.get("custom_providers"), list) else []
        for provider in list_authenticated_providers(user_providers=user_providers, custom_providers=custom_providers, max_models=100):
            for model in provider.get("models") or []:
                mid = model.get("id") if isinstance(model, dict) else str(model)
                if mid:
                    models.append({
                        "id": f"{provider.get('slug')}:{mid}",
                        "model": mid,
                        "provider": provider.get("slug") or provider.get("name") or "",
                        "name": mid,
                        "speed": model.get("speed") if isinstance(model, dict) else None,
                        "context_window": model.get("context_window") if isinstance(model, dict) else None,
                        "cost": model.get("cost") if isinstance(model, dict) else None,
                    })
    except Exception:
        pass

    current_model = ""
    try:
        current_model = str((cfg.get("model") or {}).get("default") or "")
    except Exception:
        pass

    agents = [
        {"id": "auto", "label": "Auto", "description": "Hermes chooses the best agent or agent team."},
        {"id": "research", "label": "Research Agent", "description": "Uses web/search/source gathering when available."},
        {"id": "coding", "label": "Coding Agent", "description": "Repository, debugging, terminal and code-review work."},
        {"id": "data", "label": "Data Agent", "description": "Analysis, extraction, transformation and reporting."},
        {"id": "browser", "label": "Browser Agent", "description": "Browser/web automation when configured."},
        {"id": "planning", "label": "Planning Agent", "description": "Breaks large work into trackable steps."},
        {"id": "reviewer", "label": "Reviewer Agent", "description": "Checks work and finds gaps."},
    ]

    modes = _configured_modes(cfg) or _default_modes(toolsets)
    return {
        "modes": modes,
        "models": models,
        "current_model": current_model,
        "toolsets": toolsets,
        "agents": agents,
        "features": {
            "streaming": True,
            "files": True,
            "images": True,
            "voice": True,
            "background_tasks": True,
            "branching": True,
            "sharing": True,
            "exports": ["markdown", "json", "txt"],
        },
    }


# ── metadata (pins / stars / tags / folders / archive) ───────────────


def _read_meta() -> dict:
    meta = _read_json(META_FILE, {})
    return meta if isinstance(meta, dict) else {}


@router.get("/metadata")
async def metadata(request: Request):
    _require_session(request)
    return _read_meta()


META_FIELDS = {"pinned", "starred", "archived", "tags", "folder"}


def _sanitize_tags(tags: Any) -> list[str]:
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for t in tags:
        name = str(t or "").strip().lstrip("#")[:40]
        if name and name not in out:
            out.append(name)
    return out[:24]


@router.put("/metadata/{session_id}")
async def put_metadata(request: Request, session_id: str, body: dict):
    _require_session(request)
    meta = _read_meta()
    entry = meta.get(session_id, {}) if isinstance(meta.get(session_id), dict) else {}
    for key in META_FIELDS:
        if key in body:
            value = body[key]
            if key == "tags":
                value = _sanitize_tags(value)
            elif key == "folder":
                value = str(value or "").strip()[:60]
            elif key in {"pinned", "starred", "archived"}:
                value = bool(value)
            entry[key] = value
    entry["updated_at"] = time.time()
    meta[session_id] = entry
    _write_json(META_FILE, meta)
    return {"ok": True, "metadata": entry}


def _apply_meta_patch(meta: dict, session_id: str, patch: dict) -> None:
    entry = meta.get(session_id, {}) if isinstance(meta.get(session_id), dict) else {}
    for key in META_FIELDS:
        if key not in patch:
            continue
        value = patch[key]
        if key == "tags":
            value = _sanitize_tags(value)
        elif key == "folder":
            value = str(value or "").strip()[:60]
        elif key in {"pinned", "starred", "archived"}:
            value = bool(value)
        entry[key] = value
    entry["updated_at"] = time.time()
    meta[session_id] = entry


@router.post("/metadata/bulk")
async def bulk_metadata(request: Request, body: dict):
    """Apply one metadata patch (or a delete) to many sessions at once.

    Body: ``{"ids": [...], "patch": {...}}`` or ``{"ids": [...], "delete": true}``.
    Deletes go through SessionDB (the gateway refuses live sessions; those are
    skipped and reported) and also drop the session's metadata row.
    """
    _require_session(request)
    ids = [str(i) for i in (body or {}).get("ids") or [] if str(i or "").strip()][:200]
    if not ids:
        raise HTTPException(status_code=400, detail="ids required")
    do_delete = bool((body or {}).get("delete"))
    patch = (body or {}).get("patch")
    if not do_delete and not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch or delete required")

    meta = _read_meta()
    updated: list[str] = []
    deleted: list[str] = []
    skipped: list[str] = []
    if do_delete:
        db = None
        try:
            db = _session_db()
        except HTTPException:
            db = None
        for sid in ids:
            ok = False
            if db is not None:
                try:
                    ok = bool(db.delete_session(sid, sessions_dir=_home() / "sessions"))
                except Exception:
                    ok = False
            if ok:
                deleted.append(sid)
                meta.pop(sid, None)
            else:
                skipped.append(sid)
        try:
            if db is not None:
                db.close()
        except Exception:
            pass
    else:
        for sid in ids:
            _apply_meta_patch(meta, sid, patch)
            updated.append(sid)
    _write_json(META_FILE, meta)
    return {"ok": True, "updated": updated, "deleted": deleted, "skipped": skipped}


# ── folders registry ─────────────────────────────────────────────────


def _folder_names(meta: dict) -> list[str]:
    names = set()
    reg = meta.get("folders") if isinstance(meta.get("folders"), list) else []
    for name in reg:
        if isinstance(name, str) and name.strip():
            names.add(name.strip()[:60])
    for entry in meta.values():
        if isinstance(entry, dict) and isinstance(entry.get("folder"), str) and entry["folder"].strip():
            names.add(entry["folder"].strip()[:60])
    return sorted(names)


@router.get("/folders")
async def list_folders(request: Request):
    _require_session(request)
    meta = _read_meta()
    counts: dict[str, int] = {}
    for entry in meta.values():
        if isinstance(entry, dict) and isinstance(entry.get("folder"), str) and entry["folder"].strip():
            counts[entry["folder"]] = counts.get(entry["folder"], 0) + 1
    return {"folders": [{"name": n, "count": counts.get(n, 0)} for n in _folder_names(meta)]}


@router.put("/folders/{name}")
async def put_folder(request: Request, name: str, body: dict | None = None):
    """Create a folder, or rename it when ``{"new_name": "..."}`` is given."""
    _require_session(request)
    name = str(name or "").strip()[:60]
    new_name = str(((body or {}).get("new_name")) or "").strip()[:60]
    if not name:
        raise HTTPException(status_code=400, detail="folder name required")
    meta = _read_meta()
    reg = [n for n in (meta.get("folders") if isinstance(meta.get("folders"), list) else []) if isinstance(n, str)]
    if new_name:
        if not new_name:
            raise HTTPException(status_code=400, detail="new_name required")
        if new_name in _folder_names(meta) and new_name != name:
            raise HTTPException(status_code=409, detail="folder already exists")
        for entry in meta.values():
            if isinstance(entry, dict) and entry.get("folder") == name:
                entry["folder"] = new_name
        reg = [new_name if n == name else n for n in reg]
        if new_name not in reg:
            reg.append(new_name)
    else:
        if name not in reg:
            reg.append(name)
    meta["folders"] = reg
    _write_json(META_FILE, meta)
    return {"ok": True, "folders": _folder_names(meta)}


@router.delete("/folders/{name}")
async def delete_folder(request: Request, name: str):
    """Remove a folder; sessions keep their transcripts and simply become unfiled."""
    _require_session(request)
    name = str(name or "").strip()
    meta = _read_meta()
    for entry in meta.values():
        if isinstance(entry, dict) and entry.get("folder") == name:
            entry["folder"] = ""
    reg = [n for n in (meta.get("folders") if isinstance(meta.get("folders"), list) else [])
           if isinstance(n, str) and n != name]
    meta["folders"] = reg
    _write_json(META_FILE, meta)
    return {"ok": True, "folders": _folder_names(meta)}


def _content_text(content: Any) -> str:
    """Flatten OpenAI-style content parts (text/image/audio) to display text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, list):
        parts = []
        for part in content:
            text = _content_text(part).strip()
            if text:
                parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in {"text", "input_text", "output_text"}:
            return str(content.get("text") or content.get("content") or "")
        if kind in {"image_url", "input_image", "image"}:
            return "[image]"
        if kind in {"input_audio", "audio"}:
            return "[audio]"
        if kind:
            return f"[{kind}]"
        if "text" in content:
            return str(content.get("text") or "")
        return "[structured content]"
    return str(content)


def _search_hits(db: Any, query: str) -> list[dict]:
    """Full-text search across message content, tolerant of the pinned surface.

    v2026.5.7 exposes ``search_messages``; some other releases call it
    ``search_sessions``.  Feature-detect both and never raise.
    """
    query = str(query or "").strip()
    if not query:
        return []
    for name in ("search_messages", "search_sessions"):
        fn = getattr(db, name, None)
        if not callable(fn):
            continue
        try:
            hits = fn(query, limit=50) or []
        except TypeError:
            try:
                hits = fn(query) or []
            except Exception:
                continue
        except Exception:
            continue
        out = []
        for hit in hits:
            if isinstance(hit, dict):
                out.append(hit)
        if out:
            return out[:50]
    return []


def _list_rows(db: Any, limit: int, offset: int) -> tuple[list[dict], bool]:
    """One page of session rows plus a has_more hint, native-offset first."""
    rich = getattr(db, "list_sessions_rich", None)
    rows: list = []
    if callable(rich):
        try:
            rows = list(rich(source=None, limit=limit + 1, offset=offset)) or []
        except TypeError:
            # Older pins without offset: fetch a superset and slice here,
            # the same strategy the gateway's session.list uses.
            try:
                rows = list(rich(source=None, limit=limit + offset + 1)) or []
                rows = rows[offset:offset + limit + 1]
            except Exception:
                rows = []
        except Exception:
            rows = []
    if not rows:
        plain = getattr(db, "list_sessions", None)
        if callable(plain):
            try:
                rows = list(plain(limit=limit + offset + 1)) or []
                rows = rows[offset:offset + limit + 1]
            except Exception:
                rows = []
    has_more = len(rows) > limit
    return [r for r in rows if isinstance(r, dict)][:limit], has_more


def _row_summary(s: dict, snippet: str = "") -> dict:
    src = str(s.get("source") or "").strip().lower()
    return {
        "id": s.get("id") or "",
        "title": s.get("title") or "",
        "preview": s.get("preview") or s.get("summary") or "",
        "started_at": s.get("started_at") or s.get("created_at") or 0,
        "last_active": s.get("last_active") or s.get("started_at") or 0,
        "message_count": s.get("message_count") or s.get("msg_count") or 0,
        "source": s.get("source") or "",
        "model": s.get("model") or "",
        "parent_session_id": s.get("parent_session_id") or "",
        "input_tokens": s.get("input_tokens") or 0,
        "output_tokens": s.get("output_tokens") or 0,
        # child rows (sub-agent runs, compression continuations) are noise in
        # a chat picker — same deny rule as the gateway's session.list.
        "_child": src == "tool" or bool(s.get("parent_session_id")),
        "snippet": str(snippet or ""),
    }


@router.get("/sessions")
async def list_chat_sessions(request: Request, limit: int = 120, offset: int = 0, q: str = ""):
    """List stored conversations (works even when the gateway WebSocket is down).

    Mirrors the gateway's ``session.list`` shape (id/title/preview/started_at/
    message_count/source) so the UI can browse and open history without a live
    TUI session.  ``q`` additionally runs a full-text search across message
    content (via ``SessionDB.search_messages`` on the pinned runtime) and merges
    the matching sessions into the result, so stale titles never hide an old chat.
    """
    _require_session(request)
    limit = max(1, min(int(limit or 120), MAX_SESSION_ROWS))
    offset = max(0, int(offset or 0))
    query = str(q or "").strip()
    db = _session_db()
    try:
        rows, has_more = _list_rows(db, limit, offset)
        out = []
        for s in rows:
            entry = _row_summary(s)
            if not entry["id"] or entry["_child"]:
                continue
            out.append(entry)

        if query:
            hits = _search_hits(db, query)
            if hits:
                by_id = {s["id"]: s for s in out}
                merged = list(out)
                for hit in hits:
                    sid = hit.get("session_id") or hit.get("id")
                    if not sid:
                        continue
                    snippet = str(hit.get("snippet") or "")[:200]
                    if sid in by_id:
                        if snippet:
                            by_id[sid]["snippet"] = snippet
                        continue
                    row = None
                    try:
                        row = db.get_session(sid)
                    except Exception:
                        row = None
                    if not isinstance(row, dict):
                        continue
                    entry = _row_summary(row, snippet)
                    if entry["_child"]:
                        continue
                    merged.append(entry)
                out = sorted(merged, key=lambda s: s.get("started_at") or 0, reverse=True)[:limit]
        return {"sessions": out, "has_more": has_more and not query}
    finally:
        try:
            db.close()
        except Exception:
            pass


def _get_messages_page(db: Any, session_id: str, limit: int, offset: int) -> tuple[list, int]:
    """One chronological page of messages plus the total row count.

    The pinned ``SessionDB.get_messages(session_id)`` takes no limit/offset, so
    pagination is applied here (load once per request, return only the page —
    transcripts are never retained between requests).  Newer surfaces that do
    accept ``limit``/``offset`` are used directly.
    """
    fn = getattr(db, "get_messages", None)
    if not callable(fn):
        return [], 0
    try:
        rows = fn(session_id, limit=limit, offset=offset) or []
        # With native pagination we cannot know the total cheaply; the
        # session row's message_count is the best estimate.
        return list(rows), max(len(rows) + offset, 0)
    except TypeError:
        pass  # pinned surface: get_messages(session_id) only
    except Exception:
        return [], 0
    try:
        rows = fn(session_id) or []
    except Exception:
        return [], 0
    total = len(rows)
    return list(rows[offset:offset + limit]), total


def _message_rows(records: list, session_id: str, offset: int) -> list[dict]:
    messages = []
    tool_names: dict[str, str] = {}
    for i, m in enumerate(records):
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "message"
        content = _content_text(m.get("content"))
        tool_call_id = m.get("tool_call_id") or ""
        tool_name = m.get("tool_name") or tool_names.get(tool_call_id) or ""
        if role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"] or []:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and fn.get("name"):
                    tool_names[tc.get("id") or ""] = fn["name"]
        row_id = m.get("id")
        if row_id is None:
            row_id = f"{session_id}-{offset + i}"
        messages.append({
            "id": str(row_id),
            "role": role,
            "content": content,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "tool_calls": m.get("tool_calls"),
            "reasoning": m.get("reasoning") or "",
            "timestamp": m.get("timestamp") or m.get("created_at") or 0,
        })
    return messages


@router.get("/sessions/{session_id}")
async def get_chat_session(request: Request, session_id: str, limit: int = 0, offset: int = 0):
    """Transcript for one conversation (read-only; no gateway needed).

    ``limit``/``offset`` page through the stored rows oldest-first.  ``limit=0``
    (or absent) returns everything — the lazy-load path used when a saved chat
    is first opened asks for the newest page only.
    """
    _require_session(request)
    limit = int(limit or 0)
    offset = int(offset or 0)
    db = _session_db()
    try:
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        fn = getattr(db, "get_messages", None)
        if limit <= 0 or offset < 0:
            # full fetch path: no pagination, or a negative offset which asks
            # for the NEWEST page ("offset=-N&limit=N" → last N messages)
            records = []
            if callable(fn):
                try:
                    records = fn(session_id) or []
                except Exception:
                    records = []
            total = len(records)
            if limit <= 0:
                messages = _message_rows(list(records), session_id, 0)
                offset_out = 0
            else:
                limit = min(limit, MAX_SESSION_ROWS)
                real = max(0, total + offset)
                messages = _message_rows(records[real:real + limit], session_id, real)
                offset_out = real
        else:
            limit = min(limit, MAX_SESSION_ROWS)
            records, total = _get_messages_page(db, session_id, limit, offset)
            messages = _message_rows(records, session_id, offset)
            offset_out = offset
        return {
            "session": session,
            "messages": messages,
            "count": len(messages),
            "total": total,
            # has_more = more rows exist outside this page. For the UI's
            # newest-page requests that means "older history can be loaded".
            "offset": offset_out,
            "has_more": offset_out > 0 or (offset_out + len(messages)) < total,
        }
    finally:
        try:
            db.close()
        except Exception:
            pass


@router.get("/sessions/{session_id}/tree")
async def get_session_tree(request: Request, session_id: str):
    """Branch lineage: the parent session plus every direct child (branches,
    compression continuations).  Children are discovered through
    ``list_sessions_rich(include_children=True)`` when the pinned runtime
    exposes it, and degrade to an empty list otherwise."""
    _require_session(request)
    db = _session_db()
    try:
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        parent = None
        parent_id = session.get("parent_session_id") or ""
        if parent_id:
            try:
                parent = db.get_session(parent_id)
            except Exception:
                parent = None
        children: list[dict] = []
        rich = getattr(db, "list_sessions_rich", None)
        if callable(rich):
            try:
                rows = list(rich(source=None, limit=MAX_SESSION_ROWS, include_children=True,
                                 project_compression_tips=False)) or []
                children = [
                    {
                        "id": r.get("id") or "",
                        "title": r.get("title") or "",
                        "started_at": r.get("started_at") or 0,
                        "message_count": r.get("message_count") or 0,
                        "end_reason": r.get("end_reason") or "",
                    }
                    for r in rows
                    if isinstance(r, dict) and r.get("parent_session_id") == session_id
                ]
            except TypeError:
                try:
                    rows = list(rich(limit=MAX_SESSION_ROWS, include_children=True)) or []
                    children = [
                        {"id": r.get("id") or "", "title": r.get("title") or "",
                         "started_at": r.get("started_at") or 0,
                         "message_count": r.get("message_count") or 0,
                         "end_reason": r.get("end_reason") or ""}
                        for r in rows
                        if isinstance(r, dict) and r.get("parent_session_id") == session_id
                    ]
                except Exception:
                    children = []
            except Exception:
                children = []
        children.sort(key=lambda c: c.get("started_at") or 0, reverse=True)
        return {
            "session": {"id": session.get("id"), "title": session.get("title") or ""},
            "parent": ({"id": parent.get("id"), "title": parent.get("title") or "",
                        "started_at": parent.get("started_at") or 0}
                       if isinstance(parent, dict) else None),
            "children": children[:50],
        }
    finally:
        try:
            db.close()
        except Exception:
            pass


@router.post("/sessions/{session_id}/rename")
async def rename_chat_session(request: Request, session_id: str, body: dict):
    _require_session(request)
    title = str((body or {}).get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    db = _session_db()
    try:
        if not db.get_session(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        try:
            ok = db.set_session_title(session_id, title)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": bool(ok), "session_id": session_id, "title": title}
    finally:
        try:
            db.close()
        except Exception:
            pass


@router.delete("/sessions/{session_id}")
async def delete_chat_session(request: Request, session_id: str):
    """Delete a stored conversation.

    The UI first asks the gateway (``session.delete``) so sessions that are
    live in this process are refused safely; this endpoint is the fallback for
    history that is not bound to an active gateway session.
    """
    _require_session(request)
    db = _session_db()
    try:
        if not db.get_session(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        try:
            deleted = db.delete_session(
                session_id, sessions_dir=_home() / "sessions"
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"delete failed: {exc}")
    finally:
        try:
            db.close()
        except Exception:
            pass
    if not deleted:
        raise HTTPException(status_code=409, detail="session could not be deleted (may be active)")
    meta = _read_meta()
    if isinstance(meta, dict) and session_id in meta:
        meta.pop(session_id, None)
        _write_json(META_FILE, meta)
    return {"ok": True, "deleted": session_id}


@router.get("/usage")
async def usage_summary(request: Request, days: int = 14):
    """Token usage rollups (per day, per model) derived from session rows.

    Cheap: one ``list_sessions_rich`` pass over the window — no message bodies
    are read.  Degrades to empty when the helper is unavailable.
    """
    _require_session(request)
    days = max(1, min(int(days or 14), 90))
    cutoff = time.time() - days * 86400
    db = _session_db()
    try:
        rich = getattr(db, "list_sessions_rich", None)
        rows: list = []
        if callable(rich):
            try:
                rows = list(rich(source=None, limit=MAX_SESSION_ROWS, include_children=True,
                                 project_compression_tips=False)) or []
            except TypeError:
                try:
                    rows = list(rich(limit=MAX_SESSION_ROWS, include_children=True)) or []
                except Exception:
                    rows = []
            except Exception:
                rows = []
        per_day: dict[str, dict] = {}
        per_model: dict[str, dict] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            started = r.get("started_at") or 0
            if not started or started < cutoff:
                continue
            day = time.strftime("%Y-%m-%d", time.localtime(started))
            model = str(r.get("model") or "unknown")
            inp = int(r.get("input_tokens") or 0)
            outp = int(r.get("output_tokens") or 0)
            for key, bucket_map in ((day, per_day), (model, per_model)):
                b = bucket_map.setdefault(key, {"input": 0, "output": 0, "sessions": 0})
                b["input"] += inp
                b["output"] += outp
                b["sessions"] += 1
        return {
            "days": days,
            "per_day": [{"key": k, **v} for k, v in sorted(per_day.items())],
            "per_model": [{"key": k, **v} for k, v in
                          sorted(per_model.items(), key=lambda kv: -(kv[1]["input"] + kv[1]["output"]))[:20]],
        }
    finally:
        try:
            db.close()
        except Exception:
            pass


@router.get("/settings")
async def get_settings(request: Request):
    _require_session(request)
    defaults = {
        "density": "comfortable",
        "messageWidth": "wide",
        "fontSize": "medium",
        "showTimestamps": True,
        "showUsage": True,
        "confirmDelete": False,
        "enterToSend": True,
        "autoScroll": True,
        "autoTitle": True,
        "autoTools": True,
        "saveHistory": True,
        "memoryEnabled": True,
        "temporaryDefault": False,
        "defaultMode": "fast",
        "defaultModel": "",
        "defaultAgent": "auto",
        "defaultTools": [],
        "pinnedModels": [],
    }
    saved = _read_json(SETTINGS_FILE, {})
    if isinstance(saved, dict):
        defaults.update(saved)
    return defaults


@router.put("/settings")
async def put_settings(request: Request, body: dict):
    _require_session(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="settings body must be an object")
    current = await get_settings(request)
    current.update(body)
    _write_json(SETTINGS_FILE, current)
    return {"ok": True, "settings": current}


def _safe_filename(name: str) -> str:
    base = SAFE_NAME_RE.sub("_", Path(name or "upload").name).strip("._")
    return base[:120] or "upload"


@router.post("/attachments")
async def upload_attachment(request: Request, file: UploadFile = File(...), conversation_id: str = Form("")):
    _require_session(request)
    filename = _safe_filename(file.filename or "upload")
    ext = Path(filename).suffix.lower()
    content_type = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    upload_dir = _home() / "uploads" / "web-chat" / time.strftime("%Y%m%d")
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{int(time.time())}_{secrets.token_hex(4)}_{filename}"
    total = 0
    with target.open("wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                try:
                    target.unlink(missing_ok=True)
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail="file exceeds upload limit")
            fh.write(chunk)
    is_image = content_type.startswith("image/") or ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    return {
        "ok": True,
        "id": secrets.token_hex(8),
        "name": filename,
        "path": str(target),
        "size": total,
        "content_type": content_type,
        "is_image": is_image,
        "prompt_reference": f"@{target}",
        "conversation_id": conversation_id,
    }


@router.post("/branch")
async def branch_from_message(request: Request, body: dict):
    _require_session(request)
    source = str(body.get("session_id") or "").strip()
    upto = int(body.get("message_index", -1))
    title = str(body.get("title") or "").strip() or "Branch"
    if not source or upto < 0:
        raise HTTPException(status_code=400, detail="session_id and message_index required")
    db = _session_db()
    try:
        if not db.get_session(source):
            raise HTTPException(status_code=404, detail="session not found")
        try:
            messages = (db.get_messages(source) or [])[: upto + 1]
        except Exception:
            messages = []
        new_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        try:
            db.create_session(new_id, source="web", parent_session_id=source)
        except TypeError:
            db.create_session(new_id, source="web")
        for msg in messages:
            db.append_message(new_id, msg.get("role", "user"), msg.get("content"), tool_name=msg.get("tool_name"), tool_calls=msg.get("tool_calls"), tool_call_id=msg.get("tool_call_id"))
        try:
            db.set_session_title(new_id, title)
        except Exception:
            pass
        try:
            db.end_session(source, "branched")
        except Exception:
            pass
        return {"ok": True, "session_id": new_id, "parent": source, "title": title}
    finally:
        try:
            db.close()
        except Exception:
            pass


@router.get("/export/{session_id}")
async def export_session(request: Request, session_id: str, format: str = "markdown"):
    _require_session(request)
    fmt = (format or "markdown").lower()
    db = _session_db()
    try:
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            messages = db.get_messages(session_id) or []
        except Exception:
            messages = []
    finally:
        try:
            db.close()
        except Exception:
            pass
    title = session.get("title") or session_id
    if fmt == "json":
        return {"session": session, "messages": messages}
    if fmt not in {"markdown", "md", "txt", "text"}:
        raise HTTPException(status_code=400, detail="supported formats: markdown, json, txt")
    parts = [f"# {title}", ""] if fmt in {"markdown", "md"} else [f"{title}", ""]
    for msg in messages:
        role = (msg.get("role") or "message").title()
        content = _content_text(msg.get("content"))
        parts.extend([f"## {role}" if fmt in {"markdown", "md"} else f"[{role}]", str(content), ""])
    media = "text/markdown" if fmt in {"markdown", "md"} else "text/plain"
    return PlainTextResponse("\n".join(parts), media_type=f"{media}; charset=utf-8")


# ── shares ───────────────────────────────────────────────────────────


@router.post("/share/{session_id}")
async def create_share(request: Request, session_id: str, body: dict | None = None):
    _require_session(request)
    db = _session_db()
    try:
        if not db.get_session(session_id):
            raise HTTPException(status_code=404, detail="session not found")
    finally:
        try:
            db.close()
        except Exception:
            pass
    shares = _read_json(SHARES_FILE, {})
    if not isinstance(shares, dict):
        shares = {}
    token = secrets.token_urlsafe(24)
    shares[token] = {
        "session_id": session_id,
        "created_at": time.time(),
        "revoked": False,
        "read_only": True,
        "label": (body or {}).get("label", "") if isinstance(body, dict) else "",
    }
    _write_json(SHARES_FILE, shares)
    return {"ok": True, "token": token, "url": f"/api/plugins/hermes-chat-dashboard/shared/{token}"}


@router.get("/shares")
async def list_shares(request: Request):
    """Every share (active + revoked) with its session title, for management."""
    _require_session(request)
    shares = _read_json(SHARES_FILE, {})
    if not isinstance(shares, dict):
        return {"shares": []}
    titles: dict[str, str] = {}
    db = None
    try:
        db = _session_db()
    except HTTPException:
        db = None
    out = []
    for token, share in shares.items():
        if not isinstance(share, dict):
            continue
        sid = share.get("session_id") or ""
        if sid and sid not in titles:
            titles[sid] = ""
            if db is not None:
                try:
                    row = db.get_session(sid)
                    titles[sid] = (row or {}).get("title") or ""
                except Exception:
                    pass
        out.append({
            "token": token,
            "session_id": sid,
            "session_title": titles.get(sid, ""),
            "created_at": share.get("created_at") or 0,
            "revoked": bool(share.get("revoked")),
            "revoked_at": share.get("revoked_at") or 0,
            "label": share.get("label") or "",
        })
    if db is not None:
        try:
            db.close()
        except Exception:
            pass
    out.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    return {"shares": out}


@router.delete("/share/{token}")
async def revoke_share(request: Request, token: str):
    _require_session(request)
    shares = _read_json(SHARES_FILE, {})
    if token not in shares:
        raise HTTPException(status_code=404, detail="share not found")
    shares[token]["revoked"] = True
    shares[token]["revoked_at"] = time.time()
    _write_json(SHARES_FILE, shares)
    return {"ok": True}


@router.get("/shared/{token}")
async def get_shared(token: str):
    shares = _read_json(SHARES_FILE, {})
    share = shares.get(token)
    if not isinstance(share, dict) or share.get("revoked"):
        raise HTTPException(status_code=404, detail="share not found")
    sid = share.get("session_id")
    db = _session_db()
    try:
        session = db.get_session(sid)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        try:
            messages = db.get_messages(sid) or []
        except Exception:
            messages = []
        return {"share": {k: v for k, v in share.items() if k != "session_id"}, "session": session, "messages": messages}
    finally:
        try:
            db.close()
        except Exception:
            pass
