"""Projekt-Kontexte: /mcp/<projekt>, relative Pfade, projektgebundene Tokens."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vaultserver.config import Area, Config
from vaultserver.web import create_app, hash_password

H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture
def app(tmp_path: Path):
    v = tmp_path / "vault"
    (v / "Finance App" / "Konten").mkdir(parents=True)
    (v / "Finance App" / "Projektbeschreibung.md").write_text("# Finance App\n\nBudget und Konten.\n", encoding="utf-8")
    (v / "Finance App" / "Konten" / "Giro.md").write_text("# Giro\n\nKontostand Sparschwein\n", encoding="utf-8")
    (v / "Moon").mkdir()
    (v / "Moon" / "Rakete.md").write_text("# Rakete\n\nSparschwein auf dem Mond\n", encoding="utf-8")
    (v / "Willkommen.md").write_text("# Willkommen\n", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite",
                 users={"oliver": hash_password("altes-passwort")}, tokens={"tok": "ubuntu1"},
                 git_commit=False, git_pull_seconds=0, watch_seconds=0,
                 areas=[Area(name="Fixliste", folder="Finance App/Fixliste", overview="Finance App/Fixliste.md"),
                        Area(name="Missionen", folder="Moon/Missionen", overview="Moon/Missionen.md")])
    return create_app(cfg, start_background=False)


def call(c, tool, args=None, url="/mcp", token="tok"):
    r = c.post(url, headers={**H, "Authorization": f"Bearer {token}"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}})
    if r.status_code != 200:
        return r.status_code, r.json()
    res = r.json()["result"]
    if res.get("isError"):
        return "error", res["content"][0]["text"]
    data = res.get("structuredContent")
    if data is None:
        data = json.loads(res["content"][0]["text"])
    return 200, data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data


def test_projects_relative_paths(app):
    with TestClient(app) as c:
        # ohne Projekt: alles sichtbar, volle Pfade
        _, hits = call(c, "search", {"text": "Sparschwein"})
        assert {h["path"] for h in hits} == {"Finance App/Konten/Giro.md", "Moon/Rakete.md"}
        # im Projekt: nur Finance App, relative Pfade
        u = "/mcp/finance-app"
        _, hits = call(c, "search", {"text": "Sparschwein"}, url=u)
        assert [h["path"] for h in hits] == ["Konten/Giro.md"]
        _, g = call(c, "guide", url=u)
        assert g["project"]["name"] == "finance-app" and g["project"]["start"] == "Projektbeschreibung.md"
        assert g["project"]["folder"] == "Finance App"
        assert [(a["name"], a["folder"], a["overview"]) for a in g["areas"]] == [("Fixliste", "Fixliste", "Fixliste.md")]
        assert call(c, "guide", {"area": "Missionen"}, url=u)[0] == "error"       # Bereich eines anderen Projekts
        assert call(c, "create_from_template", {"area": "Missionen", "title": "x", "fields": {}}, url=u)[0] == "error"
        assert len(call(c, "guide")[1]["areas"]) == 2
        _, ls = call(c, "list", {}, url=u)
        assert "Moon" not in json.dumps(ls) and "Projektbeschreibung.md" in json.dumps(ls)
        _, n = call(c, "read", {"path": "Konten/Giro"}, url=u)
        assert n["path"] == "Konten/Giro.md" and "Sparschwein" in n["text"]
        assert call(c, "read", {"path": "Finance App/Konten/Giro.md"}, url=u)[1]["path"] == "Konten/Giro.md"  # voller Pfad geht auch
        # außerhalb: nicht lesbar, nicht schreibbar
        assert call(c, "read", {"path": "../Moon/Rakete.md"}, url=u)[0] == "error"
        assert call(c, "read", {"path": "Moon/Rakete.md"}, url=u)[0] == "error"      # wäre Finance App/Moon/Rakete.md
        assert call(c, "write", {"path": "../Moon/neu.md", "content": "# x\n"}, url=u)[0] == "error"
        assert call(c, "move", {"source": "Konten/Giro.md", "target": "../Moon/Giro.md"}, url=u)[0] == "error"
        # schreiben relativ landet im Projektordner
        st, w = call(c, "write", {"path": "Konten/Tagesgeld.md", "content": "# Tagesgeld\n"}, url=u)
        assert st == 200 and w["path"] == "Konten/Tagesgeld.md"
        assert (Path(app.state.svc.config.vault_path) / "Finance App" / "Konten" / "Tagesgeld.md").is_file()
        _, rec = call(c, "recent", {"limit": 50}, url=u)
        assert all(not r["path"].startswith(("Moon", "Willkommen")) for r in rec)
        assert call(c, "link_commits", url=u)[0] == "error"
        # unbekanntes Projekt, gefälschte Kopfzeile
        assert call(c, "guide", url="/mcp/gibtsnicht")[0] == 404
        r = c.post("/mcp", headers={**H, "Authorization": "Bearer tok", "x-vaultserver-scope": "moon"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search", "arguments": {"text": "Sparschwein"}}})
        assert "Finance App/Konten/Giro.md" in r.text   # Kopfzeile vom Client zählt nicht


def test_token_bound_to_project(app):
    with TestClient(app) as c:
        assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
        projects = {p["name"]: p["folder"] for p in c.get("/api/setup").json()["projects"]}
        assert projects == {"finance-app": "Finance App", "moon": "Moon"}
        assert c.post("/api/clients", json={"name": "fin", "project": "gibtsnicht"}).status_code == 400
        out = c.post("/api/clients", json={"name": "fin", "project": "finance-app"}).json()
        tok = out["token"]
        t = TestClient(app)
        # /mcp mit gebundenem Token: automatisch nur das Projekt
        _, hits = call(t, "search", {"text": "Sparschwein"}, token=tok)
        assert [h["path"] for h in hits] == ["Konten/Giro.md"]
        assert call(t, "guide", url="/mcp/finance-app", token=tok)[0] == 200
        assert call(t, "guide", url="/mcp/moon", token=tok)[0] == 403
        assert t.get("/api/tree", headers={"Authorization": f"Bearer {tok}"}).status_code == 403
        assert t.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
        # erneuern behält das Projekt; Freigabe für alles wieder möglich
        ren = c.post(f"/api/clients/{out['id']}/renew").json()
        assert ren["project"] == "finance-app"
        assert c.put(f"/api/clients/{ren['id']}", json={"project": None}).json()["project"] is None
        _, hits = call(t, "search", {"text": "Sparschwein"}, token=ren["token"])
        assert len(hits) == 2
        # Token aus der Konfiguration lässt sich ebenfalls binden
        cid = next(x["id"] for x in c.get("/api/setup").json()["clients"] if x["name"] == "ubuntu1")
        assert c.put(f"/api/clients/{cid}", json={"project": "moon"}).json()["project"] == "moon"
        _, hits = call(t, "search", {"text": "Sparschwein"})
        assert [h["path"] for h in hits] == ["Rakete.md"]
