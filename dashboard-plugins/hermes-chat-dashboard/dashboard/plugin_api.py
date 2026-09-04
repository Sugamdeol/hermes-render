#!/usr/bin/env python3
"""Polished web-chat dashboard plugin backend for Hermes.

The UI talks to the real Hermes ``tui_gateway`` WebSocket for conversation
creation, resume, streaming, tool events, cancellation, model switching, tool
configuration, background jobs, and voice controls.  This plugin adds the small
web-dashboard-specific layer that upstream does not own yet: capability
presentation, durable UI metadata (pins/stars/tags/folders/archive), secure
share/export helpers, and browser file uploads that are handed back to the
existing Hermes attachment/context-reference pipeline.
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


def _content_text(content: Any) -> str:
    """Flatten an OpenAI-style content value (str | list of parts) to text."""
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
        if "text" in content:
            return str(content.get("text") or "")
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    return str(content)


def _args_preview(name: str, args: dict, max_len: int = 160) -> str:
    """Short human-readable preview of tool-call arguments.

    Prefers upstream's ``build_tool_preview`` (same text the TUI shows) and
    falls back to a compact key=value rendering when it is unavailable.
    """
    try:
        from agent.display import build_tool_preview  # type: ignore
        text = build_tool_preview(name, args, max_len=max_len) or ""
        if text:
            return text
    except Exception:
        pass
    if not isinstance(args, dict) or not args:
        return ""
    for key in ("command", "cmd", "query", "url", "path", "file_path", "pattern", "prompt", "task", "content"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            val = " ".join(val.split())
            return val if len(val) <= max_len else val[: max_len - 1] + "…"
    try:
        text = json.dumps(args, ensure_ascii=False)
    except Exception:
        text = str(args)
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more chars)"


def _build_transcript(rows: list[dict], result_limit: int = 4000) -> list[dict]:
    """Turn raw session DB rows into display turns for the web UI.

    Output is chronological.  Assistant tool calls are attached to the
    assistant turn that issued them (``tools`` list, in call order) and the
    matching ``tool`` rows fill in the result preview, so the UI can render
    each turn as *steps → answer* instead of a flat, unordered pile.  Empty
    assistant rows that only carried tool calls are folded into the tools of
    the following visible assistant message when there is one.
    """
    turns: list[dict] = []
    tools_by_call_id: dict[str, dict] = {}
    pending_tools: list[dict] = []
    counter = 0

    def _new_turn(role: str, row: dict, text: str) -> dict:
        nonlocal counter
        counter += 1
        turn = {
            "id": f"m{counter}",
            "role": role,
            "content": text,
            "timestamp": row.get("timestamp"),
        }
        return turn

    for row in rows:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        if role not in ("user", "assistant", "tool", "system"):
            continue
        text = _content_text(row.get("content"))

        if role == "tool":
            call_id = row.get("tool_call_id") or ""
            entry = tools_by_call_id.get(call_id)
            if entry is None:
                entry = {
                    "tool_id": call_id or f"tool-{len(tools_by_call_id) + 1}",
                    "name": row.get("tool_name") or "tool",
                    "context": "",
                    "args": {},
                    "status": "complete",
                    "started_at": row.get("timestamp"),
                }
                tools_by_call_id[entry["tool_id"]] = entry
                pending_tools.append(entry)
            entry["status"] = "complete"
            entry["result"] = _clip(text, result_limit)
            entry["completed_at"] = row.get("timestamp")
            if entry.get("started_at") and row.get("timestamp"):
                try:
                    entry["duration_s"] = max(0.0, float(row["timestamp"]) - float(entry["started_at"]))
                except Exception:
                    pass
            continue

        if role == "assistant":
            calls = row.get("tool_calls") or []
            new_tools = []
            for tc in calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or tc.get("name") or "tool"
                raw_args = fn.get("arguments", tc.get("arguments", "{}"))
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args or "{}")
                    except Exception:
                        args = {"_raw": raw_args}
                else:
                    args = raw_args if isinstance(raw_args, dict) else {}
                call_id = tc.get("id") or f"call-{len(tools_by_call_id) + 1}"
                entry = {
                    "tool_id": call_id,
                    "name": name,
                    "context": _args_preview(name, args),
                    "args": args,
                    "status": "running",
                    "started_at": row.get("timestamp"),
                }
                tools_by_call_id[call_id] = entry
                new_tools.append(entry)
            reasoning = row.get("reasoning") or row.get("reasoning_content") or ""
            if not text.strip():
                # Tool-only assistant step: keep its tools for the next visible turn.
                pending_tools.extend(new_tools)
                if reasoning and pending_tools:
                    pending_tools[0].setdefault("reasoning", str(reasoning))
                continue
            turn = _new_turn("assistant", row, text)
            tools = pending_tools + new_tools
            pending_tools = []
            if tools:
                turn["tools"] = tools
            if reasoning:
                turn["reasoning"] = str(reasoning)
            turns.append(turn)
            continue

        if not text.strip():
            continue
        if pending_tools and role == "user":
            # Tools ran but the assistant produced no visible text (interrupted
            # or errored turn).  Surface them as their own step block so the
            # history still shows what happened.
            counter += 1
            turns.append({"id": f"m{counter}", "role": "assistant", "content": "", "timestamp": row.get("timestamp"), "tools": pending_tools, "incomplete": True})
            pending_tools = []
        turns.append(_new_turn(role, row, text))

    if pending_tools:
        counter += 1
        turns.append({"id": f"m{counter}", "role": "assistant", "content": "", "timestamp": pending_tools[-1].get("completed_at") or pending_tools[-1].get("started_at"), "tools": pending_tools, "incomplete": True})
    for turn in turns:
        for tool in turn.get("tools") or []:
            tool.pop("args", None)
    return turns


def _lineage_ids(db, session_id: str) -> list[str]:
    """Root→tip chain of sessions (compression/branch continuations)."""
    try:
        chain = db._session_lineage_root_to_tip(session_id)  # noqa: SLF001 - upstream helper
        if chain:
            return [c for c in chain if c]
    except Exception:
        pass
    return [session_id]


@router.get("/sessions")
async def list_sessions(request: Request, limit: int = 100, offset: int = 0):
    """Session list for the chat sidebar.

    Wraps ``list_sessions_rich`` (ordered by most recent activity, hiding
    ``tool`` sub-agent runs) and merges the plugin's UI metadata so the
    sidebar needs one round-trip.
    """
    _require_session(request)
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    db = _session_db()
    try:
        try:
            rows = db.list_sessions_rich(limit=limit * 2, offset=offset, order_by_last_active=True)
        except TypeError:
            rows = db.list_sessions_rich(limit=limit * 2, offset=offset)
        try:
            total = db.session_count()
        except Exception:
            total = None
    finally:
        db.close()
    meta = _read_json(META_FILE, {})
    now = time.time()
    out = []
    for s in rows:
        if (s.get("source") or "").strip().lower() == "tool":
            continue
        sid = s.get("id")
        m = meta.get(sid) if isinstance(meta.get(sid), dict) else {}
        out.append({
            "id": sid,
            "title": s.get("title") or "",
            "preview": s.get("preview") or "",
            "source": s.get("source") or "",
            "model": s.get("model") or "",
            "started_at": s.get("started_at") or 0,
            "last_active": s.get("last_active") or s.get("started_at") or 0,
            "ended_at": s.get("ended_at"),
            "end_reason": s.get("end_reason"),
            "message_count": s.get("message_count") or 0,
            "tool_call_count": s.get("tool_call_count") or 0,
            "parent_session_id": s.get("parent_session_id"),
            "is_active": s.get("ended_at") is None and (now - (s.get("last_active") or s.get("started_at") or 0)) < 300,
            "pinned": bool(m.get("pinned")) or bool(m.get("starred")),
            "archived": bool(m.get("archived")),
            "tags": m.get("tags") or [],
        })
        if len(out) >= limit:
            break
    return {"sessions": out, "total": total, "limit": limit, "offset": offset}


@router.get("/transcript/{session_id}")
async def get_transcript(request: Request, session_id: str):
    """Read-only transcript for viewing a session without resuming it.

    Resuming through ``session.resume`` builds a full agent (slow, and it
    pins tens of MB per session); browsing history should not pay that.
    """
    _require_session(request)
    db = _session_db()
    try:
        sid = None
        try:
            sid = db.resolve_session_id(session_id)
        except Exception:
            sid = None
        session = db.get_session(sid or session_id)
        if not session:
            session = db.get_session_by_title(session_id) if hasattr(db, "get_session_by_title") else None
            if not session:
                raise HTTPException(status_code=404, detail="session not found")
        sid = session.get("id") or sid or session_id
        rows: list[dict] = []
        for chain_id in _lineage_ids(db, sid):
            try:
                rows.extend(db.get_messages(chain_id))
            except Exception:
                continue
    finally:
        db.close()
    rows.sort(key=lambda r: ((r.get("timestamp") or 0), (r.get("id") or 0)))
    return {
        "session": {k: session.get(k) for k in ("id", "title", "source", "model", "started_at", "ended_at", "end_reason", "message_count", "tool_call_count", "parent_session_id", "input_tokens", "output_tokens") if k in session},
        "messages": _build_transcript(rows),
    }


@router.put("/sessions/{session_id}/title")
async def rename_session(request: Request, session_id: str, body: dict):
    _require_session(request)
    title = str((body or {}).get("title") or "").strip()
    db = _session_db()
    try:
        try:
            if not db.set_session_title(session_id, title):
                raise HTTPException(status_code=404, detail="session not found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        db.close()
    return {"ok": True, "title": title}


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    _require_session(request)
    db = _session_db()
    try:
        try:
            deleted = db.delete_session(session_id, sessions_dir=_home() / "sessions")
        except TypeError:
            deleted = db.delete_session(session_id)
    finally:
        db.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="session not found")
    meta = _read_json(META_FILE, {})
    if session_id in meta:
        meta.pop(session_id, None)
        _write_json(META_FILE, meta)
    return {"ok": True}


@router.get("/metadata")
async def metadata(request: Request):
    _require_session(request)
    return _read_json(META_FILE, {})


@router.put("/metadata/{session_id}")
async def put_metadata(request: Request, session_id: str, body: dict):
    _require_session(request)
    meta = _read_json(META_FILE, {})
    allowed = {"pinned", "starred", "archived", "tags", "folder"}
    entry = meta.get(session_id, {}) if isinstance(meta.get(session_id), dict) else {}
    for key in allowed:
        if key in body:
            entry[key] = body[key]
    entry["updated_at"] = time.time()
    meta[session_id] = entry
    _write_json(META_FILE, meta)
    return {"ok": True, "metadata": entry}


@router.get("/settings")
async def get_settings(request: Request):
    _require_session(request)
    defaults = {
        "density": "comfortable",
        "messageWidth": "wide",
        "showTimestamps": True,
        "enterToSend": True,
        "autoScroll": True,
        "autoTitle": True,
        "autoTools": True,
        "saveHistory": True,
        "memoryEnabled": True,
        "temporaryDefault": False,
        "defaultMode": "fast",
        "defaultAgent": "auto",
        "defaultTools": [],
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
        messages = db.get_messages(source)[: upto + 1]
        new_id = secrets.token_hex(4)
        db.create_session(new_id, source="web", parent_session_id=source)
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
        db.close()


@router.get("/export/{session_id}")
async def export_session(request: Request, session_id: str, format: str = "markdown"):
    _require_session(request)
    fmt = (format or "markdown").lower()
    db = _session_db()
    try:
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        messages = db.get_messages(session_id)
    finally:
        db.close()
    title = session.get("title") or session_id
    if fmt == "json":
        return {"session": session, "messages": messages}
    if fmt not in {"markdown", "md", "txt", "text"}:
        raise HTTPException(status_code=400, detail="supported formats: markdown, json, txt")
    md = fmt in {"markdown", "md"}
    parts = [f"# {title}", ""] if md else [f"{title}", ""]
    for turn in _build_transcript(messages, result_limit=1200):
        role = "Hermes" if turn["role"] == "assistant" else turn["role"].title()
        parts.append(f"## {role}" if md else f"[{role}]")
        for tool in turn.get("tools") or []:
            label = f"{tool.get('name')}" + (f" — {tool.get('context')}" if tool.get("context") else "")
            parts.append(f"- 🛠 `{label}`" if md else f"  * tool: {label}")
            if tool.get("result"):
                body = str(tool["result"]).rstrip()
                parts.append(f"\n  ```\n  {body.replace(chr(10), chr(10) + '  ')}\n  ```" if md else "    " + body.replace("\n", "\n    "))
        if turn.get("content"):
            parts.append(str(turn["content"]))
        parts.append("")
    media = "text/markdown" if fmt in {"markdown", "md"} else "text/plain"
    return PlainTextResponse("\n".join(parts), media_type=f"{media}; charset=utf-8")


@router.post("/share/{session_id}")
async def create_share(request: Request, session_id: str, body: dict | None = None):
    _require_session(request)
    db = _session_db()
    try:
        if not db.get_session(session_id):
            raise HTTPException(status_code=404, detail="session not found")
    finally:
        db.close()
    shares = _read_json(SHARES_FILE, {})
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
        return {"share": {k: v for k, v in share.items() if k != "session_id"}, "session": session, "messages": db.get_messages(sid)}
    finally:
        db.close()
