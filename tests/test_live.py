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


def mcp(c, tool, args=None, url="/mcp", agent=None):
    h = {**H, **({"X-Vaultserver-Agent": agent} if agent else {})}
    r = c.post(url, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}})
    return r.json()["result"]


def test_touch_feed(tmp_path: Path, monkeypatch):
    """Lese-Ereignisse: eigener Puffer, entprellt, verdrängen keine Schreib-Ereignisse, verfallen nach 10 s."""
    from vaultserver import live
    v = tmp_path / "vault"
    (v / "Moon").mkdir(parents=True)
    (v / "Moon" / "A.md").write_text("# A\n", encoding="utf-8")
    f = live.Feed(v, "RecycleBin", keep=5)
    f.publish(["Moon/A.md"], "ubuntu1/claude-code")
    assert f.events[-1]["kind"] == "write" and f.rev == 1
    clock = [1000.0]
    monkeypatch.setattr(live.time, "time", lambda: clock[0])
    f.touch(["Moon/A.md"], "linuxdev/claude-code")
    assert f.reads[-1] == {"seq": 1, "time": 1000.0, "kind": "read", "agent": "linuxdev/claude-code", "paths": ["Moon/A.md"]}
    f.touch(["Moon/A.md"], "linuxdev/claude-code")           # entprellt: gleiche Datei, gleicher Agent < 1 s
    assert f.read_seq == 1
    f.touch(["Moon/A.md"], "ubuntu1/claude-code")            # anderer Agent zählt
    f.touch(["RecycleBin/x.md", ""], "ubuntu1/claude-code")  # Papierkorb und leere Pfade nicht
    assert f.read_seq == 2
    clock[0] += 1.1
    f.touch(["Moon/A.md"], "linuxdev/claude-code")
    assert f.read_seq == 3
    # viele Lesezugriffe: Feed (rev, Schreib-Ereignisse) bleibt unberührt, Puffer begrenzt
    for i in range(500):
        f.touch([f"Moon/N{i}.md"], "x")
    assert f.rev == 1 and len(f.events) == 1 and len(f.reads) == live.READ_KEEP
    assert f.since(0)[0][0]["paths"] == ["Moon/A.md"]
    assert len(f.reads_since(0)) == live.READ_KEEP
    assert f.reads_since(f.read_seq - 3) == list(f.reads)[-3:]
    clock[0] += live.READ_SECONDS + 1                         # älter als 10 s: nicht mehr ausliefern
    assert f.reads_since(0) == []


def test_touch_wakes_waiters(tmp_path: Path):
    """publish/touch wecken wartende SSE-Verbindungen sofort (aus einem anderen Thread)."""
    import asyncio
    import threading
    from vaultserver.live import Feed
    f = Feed(tmp_path, "RecycleBin")

    async def run():
        loop, ev = asyncio.get_running_loop(), asyncio.Event()
        f.subscribe(loop, ev)
        t0 = loop.time()
        threading.Timer(0.05, lambda: f.touch(["A.md"], "x")).start()
        await asyncio.wait_for(ev.wait(), 2)
        f.unsubscribe(loop, ev)
        return loop.time() - t0
    assert asyncio.run(run()) < 0.5


def test_mcp_read_touches(tmp_path: Path):
    v = tmp_path / "vault"
    (v / "Moon" / "Missionen").mkdir(parents=True)
    (v / "Moon" / "A.md").write_text("# A\n\nSiehe [[B]]\n", encoding="utf-8")
    (v / "Moon" / "Missionen" / "B.md").write_text("# B\n\ntext\n", encoding="utf-8")
    (v / "Moon" / "Missionen" / "plan.pdf").write_bytes(b"%PDF-1.4 x")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite", users={}, tokens={"tok": "linuxdev"},
                 git_commit=False, git_pull_seconds=0, watch_seconds=0)
    app = create_app(cfg, start_background=False)
    feed = app.state.svc.feed
    with TestClient(app) as c:
        rev = feed.rev
        assert not mcp(c, "read", {"path": "Moon/A.md"}).get("isError")
        e = feed.reads[-1]
        assert e["kind"] == "read" and e["paths"] == ["Moon/A.md"] and e["agent"] == "linuxdev/claude-code"
        mcp(c, "outline", {"path": "Moon/Missionen/B"}, agent="nachtlauf")
        assert feed.reads[-1]["paths"] == ["Moon/Missionen/B.md"] and feed.reads[-1]["agent"] == "linuxdev/nachtlauf"
        mcp(c, "backlinks", {"path": "Moon/Missionen/B.md"}, agent="b")
        assert feed.reads[-1]["paths"] == ["Moon/Missionen/B.md"]
        mcp(c, "read_file", {"path": "Moon/Missionen/plan.pdf"})
        assert feed.reads[-1]["paths"] == ["Moon/Missionen/plan.pdf"]
        mcp(c, "list", {"folder": "Moon/Missionen"}, agent="l")
        assert feed.reads[-1]["paths"] == ["Moon/Missionen"]
        # Projekt-Kontext: Pfade vault-absolut
        mcp(c, "read", {"path": "Missionen/B.md"}, url="/mcp/moon", agent="p")
        assert feed.reads[-1]["paths"] == ["Moon/Missionen/B.md"] and feed.reads[-1]["agent"] == "linuxdev/p"
        # search/query melden nichts, Fehler (unbekannte Datei) auch nicht
        seq = feed.read_seq
        mcp(c, "search", {"text": "text"}, agent="s")
        mcp(c, "query", {"filters": ["Status=offen"]}, agent="s")
        assert mcp(c, "read", {"path": "Moon/Gibtsnicht.md"}, agent="s").get("isError")
        assert feed.read_seq == seq
        assert feed.rev == rev                       # Lesen erzeugt keine Feed-Ereignisse (kein Baum-Neuladen)
