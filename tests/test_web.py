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
