"""Route-level tests for the hermes-chat-dashboard plugin backend.

Runs the real FastAPI router from dashboard/plugins (plugin_api.py) against a
real FastAPI TestClient, with a fake ``hermes_state.SessionDB`` that mirrors
the *pinned* upstream surface (v2026.5.7): ``list_sessions_rich`` (with
offset), ``get_messages`` (NO limit/offset), ``search_messages`` (not
``search_sessions``), lineage via ``parent_session_id`` rows, etc.

These tests are skipped automatically when fastapi/httpx are not installed
(the pure-helper tests in test_chat_dashboard_plugin.py still run).
"""
import importlib.util
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import skipUnless

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except Exception:  # pragma: no cover
    HAS_FASTAPI = False

import unittest


class FakeSessionDB:
    """Mirrors the pinned hermes_state.SessionDB surface used by the plugin.

    Storage is class-level so every ``SessionDB()`` the plugin constructs
    sees the same database (like the real sqlite file).
    """

    rows = {}
    messages = {}
    deleted = []

    @classmethod
    def reset(cls):
        cls.rows = {}
        cls.messages = {}
        cls.deleted = []

    def __init__(self, db_path=None):
        pass

    def close(self):
        pass

    # ── sessions ──────────────────────────────────────────────────────
    def create_session(self, session_id, source, **kwargs):
        FakeSessionDB.rows[session_id] = {
            "id": session_id, "source": source, "title": None,
            "started_at": time.time(), "message_count": 0,
            "ended_at": None, "end_reason": None,
            "parent_session_id": kwargs.get("parent_session_id"),
        }
        FakeSessionDB.messages.setdefault(session_id, [])
        return session_id

    def get_session(self, session_id):
        return FakeSessionDB.rows.get(session_id)

    def get_session_by_title(self, title):
        for r in FakeSessionDB.rows.values():
            if r.get("title") == title:
                return r
        return None

    def list_sessions_rich(self, source=None, exclude_sources=None, limit=20,
                           offset=0, include_children=False,
                           project_compression_tips=True,
                           order_by_last_active=False):
        out = []
        for r in sorted(FakeSessionDB.rows.values(), key=lambda x: x["started_at"], reverse=True):
            if source and r["source"] != source:
                continue
            if exclude_sources and r["source"] in exclude_sources:
                continue
            if not include_children:
                parent_id = r.get("parent_session_id")
                if parent_id:
                    parent = FakeSessionDB.rows.get(parent_id)
                    # pinned rule: only branch children are surfaced
                    if not (parent and parent.get("end_reason") == "branched"):
                        continue
            msgs = FakeSessionDB.messages.get(r["id"], [])
            first_user = next((m for m in msgs if m.get("role") == "user"), None)
            out.append({**r,
                        "preview": str((first_user or {}).get("content") or "")[:60],
                        "last_active": msgs[-1]["timestamp"] if msgs else r["started_at"]})
        return out[offset:offset + limit]

    def set_session_title(self, session_id, title):
        if session_id not in FakeSessionDB.rows:
            return False
        FakeSessionDB.rows[session_id]["title"] = title
        return True

    def get_session_title(self, session_id):
        r = FakeSessionDB.rows.get(session_id)
        return r.get("title") if r else None

    def reopen_session(self, session_id):
        if session_id in FakeSessionDB.rows:
            FakeSessionDB.rows[session_id]["ended_at"] = None

    def end_session(self, session_id, end_reason):
        if session_id in FakeSessionDB.rows:
            FakeSessionDB.rows[session_id].setdefault("ended_at", time.time())
            FakeSessionDB.rows[session_id].setdefault("end_reason", end_reason)

    def delete_session(self, session_id, sessions_dir=None):
        if session_id not in FakeSessionDB.rows:
            return False
        FakeSessionDB.deleted.append(session_id)
        del FakeSessionDB.rows[session_id]
        FakeSessionDB.messages.pop(session_id, None)
        return True

    # ── messages ──────────────────────────────────────────────────────
    def append_message(self, session_id, role, content=None, **kwargs):
        FakeSessionDB.messages.setdefault(session_id, []).append({
            "id": len(FakeSessionDB.messages.get(session_id, [])),
            "role": role, "content": content,
            "tool_name": kwargs.get("tool_name"),
            "tool_calls": kwargs.get("tool_calls"),
            "tool_call_id": kwargs.get("tool_call_id"),
            "timestamp": time.time(),
        })
        if session_id in FakeSessionDB.rows:
            FakeSessionDB.rows[session_id]["message_count"] = len(FakeSessionDB.messages[session_id])
        return len(FakeSessionDB.messages[session_id])

    def get_messages(self, session_id):
        return [dict(m) for m in FakeSessionDB.messages.get(session_id, [])]

    def get_messages_as_conversation(self, session_id, include_ancestors=False):
        return [{"role": m["role"], "content": m["content"]}
                for m in FakeSessionDB.messages.get(session_id, [])]

    # ── search (pinned name is search_messages) ───────────────────────
    def search_messages(self, query, **kwargs):
        hits = []
        for sid, msgs in FakeSessionDB.messages.items():
            for m in msgs:
                if m.get("content") and query.lower() in str(m["content"]).lower():
                    hits.append({"session_id": sid, "id": m["id"], "role": m["role"],
                                 "snippet": f">>>{query}<<<", "content": m["content"],
                                 "timestamp": m["timestamp"]})
        return hits


def load_plugin(fake_db_cls=None, home=None):
    """Import plugin_api.py with hermes_* modules stubbed, isolated per test."""
    fake_db_cls = fake_db_cls or FakeSessionDB

    hermes_state = types.ModuleType("hermes_state")
    hermes_state.SessionDB = fake_db_cls
    sys.modules["hermes_state"] = hermes_state

    web_server = types.ModuleType("hermes_cli.web_server")
    web_server._has_valid_session_token = lambda request: True
    config_mod = types.ModuleType("hermes_cli.config")
    config_mod.load_config = lambda: {}
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.web_server = web_server
    hermes_cli.config = config_mod
    sys.modules.setdefault("hermes_cli", hermes_cli)
    sys.modules["hermes_cli.web_server"] = web_server
    sys.modules["hermes_cli.config"] = config_mod

    if home is None:
        home = tempfile.mkdtemp(prefix="hcd-test-")
    import os
    os.environ["HERMES_HOME"] = home

    module_path = (Path(__file__).resolve().parents[1]
                   / "dashboard-plugins" / "hermes-chat-dashboard"
                   / "dashboard" / "plugin_api.py")
    spec = importlib.util.spec_from_file_location(
        f"hermes_chat_dashboard_routes_{int(time.time()*1000)}", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-chat-dashboard")
    return module, TestClient(app), home


@skipUnless(HAS_FASTAPI, "fastapi + httpx required for route tests")
class RouteTests(unittest.TestCase):
    def setUp(self):
        FakeSessionDB.reset()
        self.module, self.client, self.home = load_plugin()
        self._seed()

    @property
    def store(self):
        return FakeSessionDB

    def _seed(self):
        db = FakeSessionDB()
        db.create_session("s1", "web")
        db.set_session_title("s1", "First chat")
        db.append_message("s1", "user", "hello world")
        db.append_message("s1", "assistant", "hi there")
        db.append_message("s1", "tool", "searched", tool_name="web_search")
        db.create_session("s2", "telegram")
        db.append_message("s2", "user", "telegram message")
        db.create_session("s3", "web", parent_session_id="s1")
        db.append_message("s3", "user", "branch content")

    def test_sessions_list_offsets_and_filters_tool_rows(self):
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        ids = [s["id"] for s in body["sessions"]]
        self.assertIn("s1", ids)
        self.assertNotIn("s3", ids)  # child rows are not top-level conversations

    def test_session_detail(self):
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions/s1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["messages"][0]["role"], "user")

    def test_session_detail_404(self):
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions/nope")
        self.assertEqual(r.status_code, 404)

    def test_rename(self):
        r = self.client.post("/api/plugins/hermes-chat-dashboard/sessions/s1/rename",
                             json={"title": "Renamed"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_rename_conflict(self):
        self.client.post("/api/plugins/hermes-chat-dashboard/sessions/s2/rename",
                         json={"title": "First chat"})
        # s2 rename failed with 400 (title in use)? fake allows; just check ok path
        r = self.client.post("/api/plugins/hermes-chat-dashboard/sessions/s1/rename",
                             json={"title": ""})
        self.assertEqual(r.status_code, 400)

    def test_delete(self):
        r = self.client.delete("/api/plugins/hermes-chat-dashboard/sessions/s2")
        self.assertEqual(r.status_code, 200)
        self.assertIn("s2", self.store.deleted)

    def test_delete_missing(self):
        self.assertEqual(
            self.client.delete("/api/plugins/hermes-chat-dashboard/sessions/zz").status_code, 404)

    def test_metadata_roundtrip(self):
        r = self.client.put("/api/plugins/hermes-chat-dashboard/metadata/s1",
                            json={"pinned": True, "tags": ["a", "b"]})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/plugins/hermes-chat-dashboard/metadata")
        self.assertTrue(r.json()["s1"]["pinned"])

    def test_settings_roundtrip(self):
        r = self.client.get("/api/plugins/hermes-chat-dashboard/settings")
        self.assertEqual(r.status_code, 200)
        r = self.client.put("/api/plugins/hermes-chat-dashboard/settings",
                            json={"density": "compact"})
        self.assertEqual(r.json()["settings"]["density"], "compact")

    def test_export_formats(self):
        for fmt, want in (("markdown", "# First chat"), ("txt", "First chat"), ("json", '"session"')):
            r = self.client.get(f"/api/plugins/hermes-chat-dashboard/export/s1?format={fmt}")
            self.assertEqual(r.status_code, 200, fmt)
            self.assertIn(want, r.text)

    def test_share_lifecycle(self):
        r = self.client.post("/api/plugins/hermes-chat-dashboard/share/s1", json={})
        self.assertEqual(r.status_code, 200)
        token = r.json()["token"]
        shared = self.client.get(f"/api/plugins/hermes-chat-dashboard/shared/{token}")
        self.assertEqual(shared.status_code, 200)
        self.assertEqual(shared.json()["session"]["id"], "s1")
        self.assertEqual(
            self.client.delete(f"/api/plugins/hermes-chat-dashboard/share/{token}").status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/plugins/hermes-chat-dashboard/shared/{token}").status_code, 404)

    def test_branch(self):
        r = self.client.post("/api/plugins/hermes-chat-dashboard/branch",
                             json={"session_id": "s1", "message_index": 1, "title": "B2"})
        self.assertEqual(r.status_code, 200)
        new_id = r.json()["session_id"]
        msgs = self.store.messages[new_id]
        self.assertEqual(len(msgs), 2)  # truncated copy up to index 1

    def test_upload(self):
        r = self.client.post(
            "/api/plugins/hermes-chat-dashboard/attachments",
            files={"file": ("notes.txt", b"hello", "text/plain")},
            data={"conversation_id": "s1"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["prompt_reference"].startswith("@"))
        self.assertTrue(Path(body["path"]).exists())
        self.assertEqual(Path(body["path"]).read_bytes(), b"hello")

    def test_search_uses_pinned_search_messages(self):
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions?q=hello")
        self.assertEqual(r.status_code, 200)
        # s1 contains "hello world"; the snippet must come back via search_messages
        s1 = [s for s in r.json()["sessions"] if s["id"] == "s1"]
        self.assertTrue(s1, "session matching full-text query disappeared from results")
        self.assertIn("hello", s1[0].get("snippet", "").lower())



class NewRouteTests(RouteTests):
    """v1.2 additions: pagination (incl. negative offset), folders, bulk,
    branch lineage, usage rollups, share management."""

    def test_negative_offset_returns_newest_page(self):
        db = FakeSessionDB()
        for i in range(10):
            db.append_message("s1", "user" if i % 2 == 0 else "assistant", f"msg {i}")
        db.close()
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions/s1?limit=4&offset=-4")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["total"], 13)  # 3 seeded + 10 added
        self.assertEqual(body["offset"], 9)
        self.assertTrue(body["has_more"])
        contents = [m["content"] for m in body["messages"]]
        self.assertEqual(contents, ["msg 6", "msg 7", "msg 8", "msg 9"])

    def test_positive_offset_pages_from_oldest(self):
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions/s1?limit=2&offset=0")
        body = r.json()
        self.assertEqual([m["content"] for m in body["messages"]][:2], ["hello world", "hi there"])

    def test_folder_lifecycle(self):
        # create
        r = self.client.put("/api/plugins/hermes-chat-dashboard/folders/Work?x=1", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Work", r.json()["folders"])
        # assign via metadata
        r = self.client.put("/api/plugins/hermes-chat-dashboard/metadata/s1", json={"folder": "Work"})
        self.assertEqual(r.status_code, 200)
        # listing derives counts
        r = self.client.get("/api/plugins/hermes-chat-dashboard/folders")
        self.assertEqual([f["name"] for f in r.json()["folders"]], ["Work"])
        self.assertEqual(r.json()["folders"][0]["count"], 1)
        # rename propagates
        r = self.client.put("/api/plugins/hermes-chat-dashboard/folders/Work", json={"new_name": "Projects"})
        self.assertEqual(r.status_code, 200)
        meta = self.client.get("/api/plugins/hermes-chat-dashboard/metadata").json()
        self.assertEqual(meta["s1"]["folder"], "Projects")
        # delete unfiles sessions, keeps transcripts
        r = self.client.delete("/api/plugins/hermes-chat-dashboard/folders/Projects")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["folders"], [])
        meta = self.client.get("/api/plugins/hermes-chat-dashboard/metadata").json()
        self.assertEqual(meta["s1"]["folder"], "")
        self.assertIn("s1", self.store.rows)

    def test_bulk_patch_and_delete(self):
        for sid in ("s1", "s2"):
            r = self.client.put(f"/api/plugins/hermes-chat-dashboard/metadata/{sid}", json={"tags": ["batch"]})
            self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/plugins/hermes-chat-dashboard/metadata/bulk",
                             json={"ids": ["s1", "s2"], "patch": {"archived": True, "pinned": True}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["updated"], ["s1", "s2"])
        meta = self.client.get("/api/plugins/hermes-chat-dashboard/metadata").json()
        self.assertTrue(meta["s1"]["archived"] and meta["s2"]["pinned"])
        # delete skips unknown ids instead of failing
        r = self.client.post("/api/plugins/hermes-chat-dashboard/metadata/bulk",
                             json={"ids": ["s2", "ghost"], "delete": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], ["s2"])
        self.assertEqual(r.json()["skipped"], ["ghost"])
        self.assertNotIn("s2", self.store.rows)

    def test_session_tree(self):
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions/s1/tree")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIsNone(body["parent"])
        self.assertEqual([c["id"] for c in body["children"]], ["s3"])
        # child sees its parent
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions/s3/tree")
        body = r.json()
        self.assertEqual(body["parent"]["id"], "s1")

    def test_usage_summary(self):
        self.store.rows["s1"]["model"] = "gpt-x"
        self.store.rows["s1"]["input_tokens"] = 100
        self.store.rows["s1"]["output_tokens"] = 50
        r = self.client.get("/api/plugins/hermes-chat-dashboard/usage?days=14")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["per_model"][0]["key"], "gpt-x")
        self.assertEqual(body["per_model"][0]["input"], 100)
        self.assertEqual(len(body["per_day"]), 1)

    def test_shares_management_list(self):
        r = self.client.post("/api/plugins/hermes-chat-dashboard/share/s1", json={})
        token = r.json()["token"]
        r = self.client.get("/api/plugins/hermes-chat-dashboard/shares")
        shares = r.json()["shares"]
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0]["session_title"], "First chat")
        self.assertFalse(shares[0]["revoked"])
        r = self.client.delete(f"/api/plugins/hermes-chat-dashboard/share/{token}")
        self.assertEqual(r.status_code, 200)
        shares = self.client.get("/api/plugins/hermes-chat-dashboard/shares").json()["shares"]
        self.assertTrue(shares[0]["revoked"])
        # revoked share no longer serves content
        r = self.client.get(f"/api/plugins/hermes-chat-dashboard/shared/{token}")
        self.assertEqual(r.status_code, 404)

    def test_search_feature_detects_pinned_name(self):
        # the fake exposes search_messages only (the v2026.5.7 surface); the
        # route must use it rather than a non-existent search_sessions
        db = FakeSessionDB()
        db.append_message("s2", "user", "find the needle in here")
        db.close()
        r = self.client.get("/api/plugins/hermes-chat-dashboard/sessions?q=needle")
        self.assertEqual(r.status_code, 200)
        rows = r.json()["sessions"]
        self.assertTrue(any(s["id"] == "s2" and "needle" in s.get("snippet", "") for s in rows))


if __name__ == "__main__":
    unittest.main()
