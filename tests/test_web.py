from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vaultserver.config import Config
from vaultserver.web import COOKIE, create_app, hash_password


@pytest.fixture
def setup(tmp_path: Path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "A.md").write_text("# A\n", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite",
                 users={"oliver": hash_password("altes-passwort")}, tokens={"tok": "ubuntu1"},
                 git_commit=False, git_pull_seconds=0, watch_seconds=0)
    return cfg


def login(c: TestClient, pw: str) -> int:
    return c.post("/api/login", json={"user": "oliver", "password": pw}).status_code


def test_change_password(setup: Config):
    app = create_app(setup, start_background=False)
    with TestClient(app) as a:
        b = TestClient(app)  # zweiter Browser mit eigenen Cookies, gleiche laufende App
        assert login(a, "altes-passwort") == 200 and login(b, "altes-passwort") == 200
        assert a.post("/api/password", json={"old": "falsch", "new": "neues-passwort-1"}).status_code == 400
        assert a.post("/api/password", json={"old": "altes-passwort", "new": "kurz"}).status_code == 400
        r = a.post("/api/password", json={"old": "altes-passwort", "new": "neues-passwort-1"})
        assert r.status_code == 200
        assert a.get("/api/me").json()["user"] == "oliver"      # eigene Sitzung bleibt
        assert b.get("/api/me").status_code == 401               # andere Sitzung endet
        assert login(b, "altes-passwort") == 401
        assert login(b, "neues-passwort-1") == 200
        assert (setup.db_path.parent / "users.json").stat().st_mode & 0o777 == 0o600
    # Neustart: Passwort aus users.json gilt weiter
    with TestClient(create_app(setup, start_background=False)) as c:
        assert login(c, "neues-passwort-1") == 200


def test_token_cannot_change_password(setup: Config):
    with TestClient(create_app(setup, start_background=False)) as c:
        r = c.post("/api/password", json={"old": "altes-passwort", "new": "neues-passwort-1"},
                   headers={"Authorization": "Bearer tok"})
        assert r.status_code == 403
        assert c.get("/api/me", headers={"Authorization": "Bearer tok"}).status_code == 200
        c.cookies.set(COOKIE, "kaputt")
        assert c.get("/api/me").status_code == 401


def test_clients_create_use_revoke(setup: Config):
    app = create_app(setup, start_background=False)
    with TestClient(app) as c:
        t = TestClient(app)  # zweiter Client ohne Anmelde-Cookie: nur Token
        tok = {"Authorization": "Bearer tok"}
        # nur mit Web-Anmeldung
        assert t.get("/api/setup", headers=tok).status_code == 403
        assert t.post("/api/clients", json={"name": "x"}, headers=tok).status_code == 403
        assert login(c, "altes-passwort") == 200
        d = c.get("/api/setup").json()
        assert [x["name"] for x in d["clients"]] == ["ubuntu1"] and d["clients"][0]["source"] == "config"
        assert any(t["name"] == "guide" for t in d["tools"]) and "tok" not in str(d)
        # anlegen: Token einmal im Klartext, gespeichert nur als Hash
        assert c.post("/api/clients", json={"name": "laptop oliver"}).status_code == 400
        assert c.post("/api/clients", json={"name": "UBUNTU1"}).status_code == 400   # Name schon vergeben
        out = c.post("/api/clients", json={"name": "laptop-oliver"}).json()
        new = {"Authorization": f"Bearer {out['token']}"}
        stored = (setup.db_path.parent / "tokens.json")
        assert out["token"] not in stored.read_text() and stored.stat().st_mode & 0o777 == 0o600
        assert t.get("/api/me", headers=new).json()["agent"] == "laptop-oliver/api"
        r = t.post("/mcp", headers={**new, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 200 and "guide" in r.text
        # erneuern: alter Token sofort ungültig, neuer geht
        ren = c.post(f"/api/clients/{out['id']}/renew").json()
        assert ren["name"] == "laptop-oliver" and ren["token"] != out["token"]
        assert t.get("/api/me", headers=new).status_code == 401
        assert t.get("/api/me", headers={"Authorization": f"Bearer {ren['token']}"}).status_code == 200
        # Token aus der Konfiguration sperren
        cfg_id = next(x["id"] for x in c.get("/api/setup").json()["clients"] if x["name"] == "ubuntu1")
        assert c.delete(f"/api/clients/{cfg_id}").json()["name"] == "ubuntu1"
        assert t.get("/api/me", headers=tok).status_code == 401
        assert t.post("/mcp", headers={**tok, "Content-Type": "application/json"}, json={}).status_code == 401
        assert c.delete("/api/clients/gibtsnicht").status_code == 404
    # Neustart: Sperre und neuer Zugang bleiben
    with TestClient(create_app(setup, start_background=False)) as c:
        assert c.get("/api/me", headers={"Authorization": "Bearer tok"}).status_code == 401
        assert c.get("/api/me", headers={"Authorization": f"Bearer {ren['token']}"}).status_code == 200
