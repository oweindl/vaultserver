"""Live-Anzeige: Änderungen aus Web, MCP, Datei-Wächter landen als Ereignisse im Feed."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from vaultserver.config import Config
from vaultserver.web import create_app, hash_password

H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer tok"}


def test_feed(tmp_path: Path):
    v = tmp_path / "vault"
    (v / "Moon").mkdir(parents=True)
    (v / "Moon" / "A.md").write_text("# A\n\nalt\n", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite", users={"oliver": hash_password("altes-passwort")},
                 tokens={"tok": "ubuntu1"}, git_commit=False, git_pull_seconds=0, watch_seconds=0)
    app = create_app(cfg, start_background=False)
    svc = app.state.svc
    with TestClient(app) as c:
        r0 = svc.feed.rev
        # MCP schreibt -> Ereignis mit Pfad und Agent
        n = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": "read", "arguments": {"path": "Moon/A.md"}}}).json()["result"]["structuredContent"]
        c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "patch_section",
               "arguments": {"path": "Moon/A.md", "section": "A", "content": "neu\n", "base_version": n["version"]}}})
        evs, reset = svc.feed.since(r0)
        assert not reset and evs[-1]["paths"] == ["Moon/A.md"] and evs[-1]["agent"] == "ubuntu1/claude-code" and not evs[-1]["tree"]
        # Datei direkt geändert, neue Datei, gelöscht -> Datei-Wächter
        r1 = svc.feed.rev
        (v / "Moon" / "A.md").write_text("# A\n\nvon außen\n", encoding="utf-8")
        (v / "Moon" / "B.md").write_text("# B\n", encoding="utf-8")
        svc.scan()
        e = svc.feed.since(r1)[0][-1]
        assert sorted(e["paths"]) == ["Moon/A.md", "Moon/B.md"] and e["agent"] == "Datei-Wächter"
        (v / "Moon" / "B.md").unlink()
        svc.scan()
        e = svc.feed.since(r1)[0][-1]
        assert e["removed"] == ["Moon/B.md"] and e["tree"]
        # leerer Ordner von außen -> Baum neu
        r2 = svc.feed.rev
        (v / "Moon" / "Leer").mkdir()
        svc.scan()
        assert svc.feed.since(r2)[0][-1]["tree"]
        # Papierkorb über das Web -> removed + tree
        assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
        r3 = svc.feed.rev
        c.delete("/api/note", params={"path": "Moon/A.md"})
        e = svc.feed.since(r3)[0][-1]
        assert e["removed"] == ["Moon/A.md"] and e["tree"] and e["agent"] == "web/oliver"
        # zu alter Stand -> reset
        for i in range(2100):
            svc.feed.publish([], "x", tree=True)
        assert svc.feed.since(r3)[1] is True
