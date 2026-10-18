"""Tests for the Files-tab storage analysis / search / zip routes."""

import io
import time
import zipfile

import pytest

from hermes_cli import web_server
from hermes_cli.web_routers import fs_analysis as fs_analysis_routes

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DASHBOARD_FILES_ROOT", str(tmp_path))
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous_auth_required is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous_auth_required


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "proj" / "sub").mkdir(parents=True)
    (tmp_path / "media").mkdir()
    (tmp_path / "proj" / "a.py").write_text("x" * 5000)
    (tmp_path / "proj" / "sub" / "b.md").write_text("y" * 1200)
    (tmp_path / "media" / "vid.mp4").write_bytes(b"z" * 80000)
    (tmp_path / "notes.txt").write_text("n" * 300)
    (tmp_path / "config.yaml").write_text("secret")  # credential denylist
    return tmp_path


def test_analysis_aggregates_tree_and_hides_sensitive(client, tree):
    response = client.get("/api/fs/analysis", params={"path": str(tree)})

    assert response.status_code == 200
    data = response.json()
    types = {entry["ext"]: entry for entry in data["types"]}
    assert types[".py"]["count"] == 1
    assert types[".mp4"] == {"ext": ".mp4", "count": 1, "bytes": 80000}
    assert types[".md"]["bytes"] == 1200
    assert data["totals"]["bytes"] >= 5000 + 1200 + 80000 + 300
    assert data["totals"]["files"] >= 4
    assert data["disk"]["total"] > 0
    by_name = {child["name"]: child for child in data["children"]}
    assert by_name["proj"]["bytes"] == 6200
    assert by_name["proj"]["files"] == 2
    assert by_name["media"]["bytes"] == 80000
    assert by_name["notes.txt"]["is_directory"] is False
    assert by_name["proj"]["is_directory"] is True
    assert all(entry["ext"] != ".yaml" for entry in data["types"])
    assert [entry["name"] for entry in data["largest"]][:2] == ["vid.mp4", "a.py"]
    assert all(entry["name"] != "config.yaml" for entry in data["largest"])
    assert data["truncated"] is False


def test_analysis_caches_until_fresh(client, tree):
    first = client.get("/api/fs/analysis", params={"path": str(tree)}).json()
    assert first["cached"] is False
    second = client.get("/api/fs/analysis", params={"path": str(tree)}).json()
    assert second["cached"] is True
    assert second["generated_at"] == first["generated_at"]

    (tree / "proj" / "new.py").write_text("q" * 700)
    refreshed = client.get("/api/fs/analysis", params={"path": str(tree), "fresh": "true"}).json()
    assert refreshed["cached"] is False
    assert refreshed["generated_at"] != first["generated_at"]
    proj = next(child for child in refreshed["children"] if child["name"] == "proj")
    assert proj["files"] == 3


def test_analysis_rejects_path_outside_root(client, tmp_path):
    outside = tmp_path.parent
    response = client.get("/api/fs/analysis", params={"path": str(outside), "fresh": "true"})
    assert response.status_code == 403


def test_search_matches_substring_glob_and_hides_sensitive(client, tree):
    response = client.get("/api/fs/search", params={"path": str(tree), "query": ".md"})
    assert response.status_code == 200
    matches = response.json()["matches"]
    assert [(m["name"], m["is_directory"]) for m in matches] == [("b.md", False)]

    globbed = client.get("/api/fs/search", params={"path": str(tree), "query": "pr*"}).json()
    assert [m["name"] for m in globbed["matches"]] == ["proj"]
    assert globbed["matches"][0]["is_directory"] is True

    # Sensitive files never appear even when named directly.
    secret = client.get("/api/fs/search", params={"path": str(tree), "query": "config.yaml"}).json()
    assert secret["matches"] == []


def test_search_requires_meaningful_query(client, tree):
    response = client.get("/api/fs/search", params={"path": str(tree), "query": "x"})
    assert response.status_code == 400


def test_zip_streams_directory_and_skips_sensitive(client, tree):
    response = client.get("/api/fs/zip", params={"path": str(tree / "proj")})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["a.py", "sub/b.md"]
    assert archive.read("a.py") == b"x" * 5000


def test_zip_rejects_over_cap(client, tree, monkeypatch):
    monkeypatch.setattr(fs_analysis_routes, "_ZIP_MAX_BYTES", 1024)
    response = client.get("/api/fs/zip", params={"path": str(tree / "proj")})
    assert response.status_code == 413


def test_zip_rejects_files(client, tree):
    response = client.get("/api/fs/zip", params={"path": str(tree / "notes.txt")})
    assert response.status_code == 400
