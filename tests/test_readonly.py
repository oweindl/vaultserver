"""Nur-Lese-Betrieb ([readonly]): Schreiben wird abgewiesen, Lesen geht, die Antwort nennt das neue Ziel."""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vaultserver.config import Config
from vaultserver.store import ReadOnly
from vaultserver.web import create_app, hash_password

H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer tok"}
TARGET = "https://mcp.example.com/mcp/neu"


def make(tmp_path: Path, readonly: bool):
    v = tmp_path / "vault"
    (v / "Moon").mkdir(parents=True)
    (v / "Moon" / "A.md").write_text("# A\n\nText.\n", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite", users={"oliver": hash_password("pw-1234")},
                 tokens={"tok": "ubuntu1"}, git_commit=False, git_pull_seconds=0, watch_seconds=0,
                 readonly=readonly, readonly_target=TARGET)
    c = TestClient(create_app(cfg, start_background=False))
    c.__enter__()
    assert c.post("/api/login", json={"user": "oliver", "password": "pw-1234"}).status_code == 200
    return c, v


@pytest.fixture
def ro(tmp_path):
    c, v = make(tmp_path, True)
    yield c, v
    c.__exit__(None, None, None)


def call(c, tool, args):
    r = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": tool, "arguments": args}}).json()["result"]
    return r.get("structuredContent") or r


WRITES = [
    ("write", {"path": "Moon/B.md", "content": "# B\n"}),
    ("patch_section", {"path": "Moon/A.md", "content": "x", "base_version": "v"}),
    ("set_property", {"path": "Moon/A.md", "key": "Status", "value": "offen"}),
    ("move", {"source": "Moon/A.md", "target": "Moon/C.md"}),
    ("delete", {"path": "Moon/A.md"}),
    ("create_folder", {"path": "Neu"}),
    ("claim", {"path": "Moon/A.md"}),
    ("release", {"path": "Moon/A.md"}),
    ("upload_link", {"path": "Moon/x.png"}),
    ("upload", {"path": "Moon/x.png", "data_base64": "AAAA"}),
    ("upload_url", {"path": "Moon/x.png", "url": "https://example.com/x.png"}),
]


@pytest.mark.parametrize("tool,args", WRITES)
def test_mcp_write_refused(ro, tool, args):
    c, v = ro
    res = call(c, tool, args)
    assert res["error"] == "read_only" and res["migrated"] is True and res["migrated_to"] == TARGET
    assert TARGET in res["message"] and res["separat_einbinden"] is True and "separat" in res["message"]
    assert (v / "Moon" / "A.md").read_text(encoding="utf-8") == "# A\n\nText.\n"
    assert not (v / "Moon" / "B.md").exists() and not (v / "Neu").exists()


def test_mcp_read_still_works_with_notice(ro):
    c, _ = ro
    res = call(c, "read", {"path": "Moon/A.md"})
    assert "Text." in str(res) and TARGET in res["read_only_hinweis"]
    g = call(c, "guide", {})
    assert g["read_only"]["migrated_to"] == TARGET


def test_web_write_refused(ro):
    c, v = ro
    r = c.put("/api/note", json={"path": "Moon/B.md", "content": "# B\n"})
    assert r.status_code in (404, 405, 422) or r.json().get("error") == "read_only"
    up = c.post("/api/upload", params={"folder": "Moon"}, files={"file": ("x.png", b"x")})
    assert up.status_code == 422 and up.json()["error"] == "read_only" and TARGET in up.json()["message"]
    assert not (v / "Moon" / "x.png").exists()


def test_start_does_not_create_bin(ro):
    _, v = ro
    assert not (v / "RecycleBin").exists()


def test_store_level_gate(ro):
    c, _ = ro
    st = c.app.state.svc.store
    for call_ in (lambda: st.link_commits("t"), lambda: st.refresh_status("t"), lambda: st.purge(None, "t"),
                  lambda: st.restore("x", "t"), lambda: st._write_file("Moon/Z.md", "x")):
        with pytest.raises(ReadOnly):
            call_()
    assert st.ensure_bin() is None


def test_normal_mode_writes(tmp_path):
    c, v = make(tmp_path, False)
    try:
        res = call(c, "write", {"path": "Moon/B.md", "content": "# B\n"})
        assert res.get("created") is True and (v / "Moon" / "B.md").exists()
        assert "read_only_hinweis" not in res
    finally:
        c.__exit__(None, None, None)


def test_config_load(tmp_path):
    (tmp_path / "vault").mkdir()
    toml = tmp_path / "v.toml"
    toml.write_text(f'vault_path = "vault"\n[readonly]\nenabled = true\ntarget = "{TARGET}"\n', encoding="utf-8")
    cfg = Config.load(toml)
    assert cfg.readonly and TARGET in cfg.readonly_text()
    toml.write_text('vault_path = "vault"\n[readonly]\nenabled = true\nmessage = "Eigener Text"\n', encoding="utf-8")
    assert Config.load(toml).readonly_text() == "Eigener Text"
