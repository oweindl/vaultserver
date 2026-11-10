"""Lese-Token: Zugang, der nur lesen darf (MCP und REST), Verwaltung, Erneuern, Export."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_scope import H, call

from vaultserver.config import Area, Config
from vaultserver.web import create_app, hash_password


@pytest.fixture
def app(tmp_path: Path):
    v = tmp_path / "vault"
    (v / "Finance App" / "Konten").mkdir(parents=True)
    (v / "Finance App" / "Konten" / "Giro.md").write_text("# Giro\n\nKontostand Sparschwein\n", encoding="utf-8")
    (v / "Moon").mkdir()
    (v / "Moon" / "Rakete.md").write_text("# Rakete\n\nSparschwein auf dem Mond\n", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite",
                 users={"oliver": hash_password("altes-passwort")}, tokens={"tok": "ubuntu1"},
                 git_commit=False, git_pull_seconds=0, watch_seconds=0,
                 areas=[Area(name="Fixliste", folder="Finance App/Fixliste", overview="Finance App/Fixliste.md")])
    return create_app(cfg, start_background=False)


def tree(app) -> dict[str, str]:
    root = Path(app.state.svc.config.vault_path)
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*.md"))}


def dummy(schema: dict):
    """Gültiger Beispielwert zu einem JSON-Schema (der Schutz greift vor der Auswertung der Werte)."""
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            opts = [o for o in schema[key] if o.get("type") != "null"]
            return dummy(opts[0]) if opts else None
    return {"string": "x", "integer": 1, "number": 1, "boolean": False, "array": [], "object": {}}.get(schema.get("type"), "x")


def tools(c, token):
    r = c.post("/mcp", headers={**H, "Authorization": f"Bearer {token}"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return r.json()["result"]["tools"]


def make(c, **body):
    assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
    out = c.post("/api/clients", json={"name": "pulse", **body}).json()
    c.cookies.clear()
    return out


def test_readonly_mcp_reads_but_never_writes(app):
    with TestClient(app) as c:
        out = make(TestClient(app), readonly=True)
        assert out["readonly"] is True
        tok = out["token"]
        # lesen geht
        _, hits = call(c, "search", {"text": "Sparschwein"}, token=tok)
        assert len(hits) == 2
        assert call(c, "read", {"path": "Moon/Rakete.md"}, token=tok)[0] == 200
        assert call(c, "recent", {"limit": 5}, token=tok)[0] == 200
        before = tree(app)
        # jedes Werkzeug, das nicht als „nur lesend“ angemeldet ist, lehnt den Lese-Token ab
        writers = [t for t in tools(c, tok) if not (t.get("annotations") or {}).get("readOnlyHint")]
        assert len(writers) >= 10, [t["name"] for t in writers]
        for t in writers:
            props = t["inputSchema"].get("properties", {})
            args = {k: dummy(props[k]) for k in t["inputSchema"].get("required", [])}
            status, text = call(c, t["name"], args, token=tok)
            assert status == "error" and "nur lesen" in text, (t["name"], status, text)
        assert tree(app) == before
        # derselbe Aufruf mit dem normalen Zugang schreibt
        st, _ = call(c, "write", {"path": "Moon/Neu.md", "content": "# Neu\n"})
        assert st == 200 and (Path(app.state.svc.config.vault_path) / "Moon" / "Neu.md").is_file()


def test_readonly_rest_only_read_addresses(app):
    with TestClient(app) as admin:
        tok = make(admin, readonly=True)["token"]
        t = TestClient(app)
        h = {"Authorization": f"Bearer {tok}"}
        assert t.get("/api/recent", headers=h).status_code == 200
        assert t.get("/api/tree", headers=h).status_code == 200
        assert t.get("/api/search", params={"q": "Sparschwein"}, headers=h).status_code == 200
        assert t.get("/api/me", headers=h).status_code == 200
        # nichts Schreibendes, nichts zum Einrichten
        before = tree(app)
        assert t.put("/api/note", headers=h, json={"path": "Moon/x", "text": "# x\n"}).status_code == 403
        assert t.delete("/api/note", headers=h, params={"path": "Moon/Rakete.md"}).status_code == 403
        assert t.post("/api/folder", headers=h, json={"path": "Moon/neu"}).status_code == 403
        assert t.post("/api/clients", headers=h, json={"name": "x"}).status_code == 403
        for path in ("/api/setup", "/api/repos", "/api/trash"):
            assert t.get(path, headers=h).status_code == 403, path
        assert tree(app) == before
        # der normale Zugang aus der Konfiguration darf weiter schreiben
        r = t.put("/api/note", headers={"Authorization": "Bearer tok"}, json={"path": "Moon/x", "text": "# x\n"})
        assert r.status_code == 200, r.text


def test_manage_readonly(app, tmp_path):
    clients = app.state.svc.clients
    a = clients.create("lesend", readonly=True)
    b = clients.create("normal")
    by = {c["name"]: c for c in clients.list()}
    assert by["lesend"]["readonly"] is True and by["normal"]["readonly"] is False and by["ubuntu1"]["readonly"] is False
    assert clients.lookup_access(a["token"])[2] is True and clients.lookup_access(b["token"])[2] is False
    assert clients.lookup_full(a["token"])[0] == "lesend"            # bisherige Schnittstelle unverändert
    # erneuern behält „nur lesen“; der alte Token ist ungültig
    ren = clients.renew(a["id"])
    assert ren["readonly"] is True and clients.lookup_access(a["token"]) is None and clients.lookup_access(ren["token"])[2] is True
    # umschalten, nicht für Zugänge aus der Konfiguration
    assert clients.set_readonly(b["id"], True)["readonly"] is True and clients.lookup_access(b["token"])[2] is True
    assert clients.set_readonly(b["id"], False)["readonly"] is False and clients.lookup_access(b["token"])[2] is False
    cfg_id = next(c["id"] for c in clients.list() if c["source"] == "config")
    with pytest.raises(ValueError):
        clients.set_readonly(cfg_id, True)
    with pytest.raises(KeyError):
        clients.set_readonly("gibtsnicht", True)
    # bleibt über einen Neustart und im Export erhalten, Sperren räumt auf
    from vaultserver.clients import Clients
    again = Clients(app.state.svc.config)
    assert next(c for c in again.list() if c["name"] == "lesend")["readonly"] is True
    exp = {e["name"]: e for e in clients.export()}
    assert exp["lesend"]["readonly"] is True and exp["normal"]["readonly"] is False
    clients.revoke(ren["id"])
    assert ren["id"] not in clients._data["readonly"]


def test_import_keeps_readonly(app, tmp_path):
    src = app.state.svc.clients
    c = src.create("lesend", readonly=True)
    row = next(e for e in src.export() if e["name"] == "lesend")
    src.revoke(c["id"])
    assert src.import_entry(row["name"], row["hash"], row["created"], row["project"], row["readonly"]) == "übernommen"
    assert src.lookup_access(c["token"])[2] is True


def test_web_update_readonly(app):
    with TestClient(app) as c:
        out = make(c, project="moon")
        assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
        r = c.put(f"/api/clients/{out['id']}", json={"readonly": True, "project": "moon"}).json()
        assert r["readonly"] is True
        assert next(x for x in c.get("/api/setup").json()["clients"] if x["id"] == out["id"])["readonly"] is True
        r = c.put(f"/api/clients/{out['id']}", json={"readonly": False, "project": "moon"}).json()
        assert r["readonly"] is False
