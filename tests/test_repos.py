"""Mehrere eingebundene Repos: über die Web-Einrichtung anlegen, als Projekt über MCP anbieten, Commits,
Papierkorb, Pull/Push und changes_since je Repo, Token verschlüsselt und nie sichtbar."""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vaultserver.config import Config
from vaultserver.repos import Repos, default_name
from vaultserver.secretbox import open_, seal
from vaultserver.web import create_app, hash_password

from test_scope import call

TOKEN = "kunden-TOKEN-abcdef123456"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=repo,
                          capture_output=True, text=True, check=True).stdout


def bare_repo(tmp: Path, name: str, notes: dict[str, str]) -> Path:
    work = tmp / f"{name}-work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    for p, text in notes.items():
        (work / p).parent.mkdir(parents=True, exist_ok=True)
        (work / p).write_text(text, encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "Start")
    bare = tmp / f"{name}.git"
    git(tmp, "clone", "-q", "--bare", str(work), str(bare))
    return bare


@pytest.fixture
def env(tmp_path: Path):
    v = tmp_path / "vault"
    v.mkdir()
    git(v, "init", "-q", "-b", "main")
    (v / "Eigenes").mkdir()
    (v / "Eigenes" / "Notiz.md").write_text("# Notiz\n\nVom Stamm.\n", encoding="utf-8")
    git(v, "add", "-A")
    git(v, "commit", "-q", "-m", "Stamm")
    acme = bare_repo(tmp_path, "acme", {"Start.md": "# Acme\n\nKundenwissen Acme\n",
                                         "Doku/Anleitung.md": "# Anleitung\n\nSchritt eins\n"})
    beta = bare_repo(tmp_path, "beta", {"README.md": "# Beta\n\nKundenwissen Beta\n"})
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite",
                 users={"oliver": hash_password("altes-passwort")}, tokens={"tok": "ubuntu1"},
                 git_commit=True, git_pull_seconds=0, watch_seconds=0,
                 repo_schemes=["https", "file"])
    return {"cfg": cfg, "vault": v, "acme": acme, "beta": beta, "tmp": tmp_path}


def login(c):
    assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200


def test_default_name():
    assert default_name("https://github.com/acme/Kunden_Vault.git") == "kunden-vault"
    assert default_name("git@gitlab.com:x/y.git") == "y"


def test_secretbox_roundtrip():
    box = seal(b"geheim", TOKEN)
    assert TOKEN not in box and open_(b"geheim", box) == TOKEN
    with pytest.raises(ValueError):
        open_(b"anderes", box)


def test_repos_ueber_web_einbinden_und_per_mcp_anbieten(env):
    app = create_app(env["cfg"], start_background=False)
    with TestClient(app) as c:
        # nur mit Web-Anmeldung, nicht mit MCP-Token
        assert c.get("/api/repos", headers={"Authorization": "Bearer tok"}).status_code == 403
        assert c.post("/api/vault/sync", headers={"Authorization": "Bearer tok"}).status_code == 403
        login(c)
        root = c.get("/api/repos").json()["root"]
        assert root["name"] == "vault" and root["enabled"] and root["branch"] == "main" and root["head"]
        assert c.post("/api/vault/sync").status_code == 200
        url = f"file://{env['acme']}"
        t = c.post("/api/repos/test", json={"url": url}).json()
        assert t["ok"] and t["branches"] == ["main"] and t["default"] == "main"
        r = c.post("/api/repos", json={"url": url, "token": TOKEN, "committer": "Acme Vault <vault@acme.example>"})
        assert r.status_code == 200, r.text
        info = r.json()
        assert info["name"] == "acme" and info["cloned"] and info["has_token"] and info["branch"] == "main"
        assert "token_box" not in info and TOKEN not in r.text
        r = c.post("/api/repos", json={"url": f"file://{env['beta']}", "name": "beta-kunde"})
        assert r.status_code == 200, r.text
        # doppelt / ungültig
        assert c.post("/api/repos", json={"url": url, "name": "acme2"}).status_code == 400
        assert c.post("/api/repos", json={"url": "http://example.com/x.git"}).status_code == 400
        assert c.post("/api/repos", json={"url": url, "name": "Eigenes"}).status_code == 400
        assert c.post("/api/repos", json={"url": "https://u:pw@example.com/x.git"}).status_code == 400

        # Token liegt nur verschlüsselt in repos.json
        stored = (env["cfg"].db_path.parent / "repos.json").read_text()
        assert TOKEN not in stored and json.loads(stored)["repos"][0]["token_box"].startswith("v1:")
        assert (env["cfg"].db_path.parent / "repos.json").stat().st_mode & 0o777 == 0o600

        # Stamm-Repo ignoriert die eingebundenen Ordner
        assert "acme" not in git(env["vault"], "status", "--porcelain")

        # als Projekte angeboten
        projects = {p["name"]: p for p in c.get("/api/setup").json()["projects"]}
        assert projects["acme"]["repo"] and projects["beta-kunde"]["repo"] and not projects["eigenes"]["repo"]
        _, hits = call(c, "search", {"text": "Kundenwissen"}, url="/mcp/acme")
        assert [h["path"] for h in hits] == ["Start.md"]
        _, hits = call(c, "search", {"text": "Kundenwissen"})
        assert {h["path"] for h in hits} == {"acme/Start.md", "beta-kunde/README.md"}

        # Schreiben über MCP im Projekt -> Commit im Kunden-Repo mit dessen Kennung, nicht im Stamm
        root_head = git(env["vault"], "rev-parse", "HEAD")
        code, res = call(c, "write", {"path": "Neu.md", "content": "# Neu\n\nvon MCP\n"}, url="/mcp/acme")
        assert code == 200, res
        acme_dir = env["vault"] / "acme"
        assert git(acme_dir, "log", "-1", "--format=%s|%cn <%ce>").strip().endswith("|Acme Vault <vault@acme.example>")
        assert git(env["vault"], "rev-parse", "HEAD") == root_head

        # Push ins Kunden-Repo
        c.post("/api/repos/acme/sync")
        assert "Neu.md" in git(env["acme"], "ls-tree", "--name-only", "main")
        st = c.get("/api/repos").json()["repos"][0]
        assert st["push"]["ok"] and st["pull"]["ok"]

        # Papierkorb bleibt im Repo
        code, res = call(c, "delete", {"path": "Doku/Anleitung.md"}, url="/mcp/acme")
        assert code == 200, res
        assert list((acme_dir / "RecycleBin").glob("*/acme/Doku/Anleitung.md"))
        assert not (env["vault"] / "RecycleBin").exists() or not list((env["vault"] / "RecycleBin").glob("*/acme"))
        code, binlist = call(c, "recycle_bin", url="/mcp/acme")
        assert code == 200 and len(binlist) == 1
        code, res = call(c, "restore", {"id": binlist[0]["id"]}, url="/mcp/acme")
        assert code == 200 and (acme_dir / "Doku" / "Anleitung.md").exists()

        # changes_since: Commit-Hash eines Repos gilt nur dort
        start = git(acme_dir, "rev-list", "--max-parents=0", "HEAD").strip()
        code, ch = call(c, "changes_since", {"since": start}, url="/mcp/acme")
        assert code == 200 and "Neu.md" in [f["path"] for f in ch["files"]]
        assert ch["head"] == git(acme_dir, "rev-parse", "HEAD").strip()

        # Repo-Ordner lässt sich nicht löschen oder verschieben
        code, res = call(c, "delete", {"path": "acme"})
        assert "eingebundenes Repo" in json.dumps(res, ensure_ascii=False) and (env["vault"] / "acme" / ".git").is_dir()

        # Entfernen: Ordner wandert nach data/removed-repos, Projekt verschwindet
        r = c.delete("/api/repos/beta-kunde")
        assert r.status_code == 200 and Path(r.json()["moved_to"]).is_dir()
        assert not (env["vault"] / "beta-kunde").exists()
        assert "beta-kunde" not in {p["name"] for p in c.get("/api/setup").json()["projects"]}
        assert "/beta-kunde/" not in (env["vault"] / ".git" / "info" / "exclude").read_text()

    # Neustart: Repos und Token bleiben
    with TestClient(create_app(env["cfg"], start_background=False)) as c:
        login(c)
        rows = c.get("/api/repos").json()["repos"]
        assert [r["name"] for r in rows] == ["acme"] and rows[0]["has_token"]


def test_entfernen_mit_ungepushten_commits_braucht_force(env):
    app = create_app(env["cfg"], start_background=False)
    with TestClient(app) as c:
        login(c)
        assert c.post("/api/repos", json={"url": f"file://{env['acme']}", "push": False}).status_code == 200
        assert call(c, "write", {"path": "X.md", "content": "# X\n"}, url="/mcp/acme")[0] == 200
        r = c.delete("/api/repos/acme")
        assert r.status_code == 400 and "gepushte" in r.json()["error"]
        assert c.delete("/api/repos/acme?force=true").status_code == 200


def test_pull_holt_aenderungen_und_aktualisiert_index(env):
    app = create_app(env["cfg"], start_background=False)
    with TestClient(app) as c:
        login(c)
        assert c.post("/api/repos", json={"url": f"file://{env['beta']}"}).status_code == 200
        other = env["tmp"] / "anderer-rechner"
        git(env["tmp"], "clone", "-q", str(env["beta"]), str(other))
        (other / "Neu.md").write_text("# Neu\n\nFernaenderung\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "fern")
        git(other, "push", "-q", "origin", "main")
        c.post("/api/repos/beta/sync")
        _, hits = call(c, "search", {"text": "Fernaenderung"}, url="/mcp/beta")
        assert [h["path"] for h in hits] == ["Neu.md"]


def test_fehlerhaftes_token_wird_nie_angezeigt(env):
    app = create_app(env["cfg"], start_background=False)
    with TestClient(app) as c:
        login(c)
        r = c.post("/api/repos/test", json={"url": "https://127.0.0.1:9/acme/vault.git", "token": TOKEN})
        assert r.status_code == 400 and TOKEN not in r.text


def test_repos_aus_der_toml(env, monkeypatch):
    cfg = env["cfg"]
    cfg.repos = [{"name": "acme", "url": f"file://{env['acme']}", "branch": "main"}]
    monkeypatch.setenv("VS_GIT_TOKEN_ACME", TOKEN)
    app = create_app(cfg, start_background=False)
    with TestClient(app) as c:
        login(c)
        row = c.get("/api/repos").json()["repos"][0]
        assert row["source"] == "config" and row["cloned"] and row["has_token"]
        # nur in der toml änderbar
        assert c.put("/api/repos/acme", json={"push": False}).status_code == 400
        assert c.delete("/api/repos/acme").status_code == 400
    assert Repos(cfg).get("acme").token_env == "VS_GIT_TOKEN_ACME"


def test_branch_wechseln_und_token_aendern(env):
    work = env["tmp"] / "acme-work"
    git(work, "checkout", "-q", "-b", "entwurf")
    (work / "Entwurf.md").write_text("# Entwurf\n\nnur im Entwurf\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "entwurf")
    git(work, "push", "-q", str(env["acme"]), "entwurf")
    app = create_app(env["cfg"], start_background=False)
    with TestClient(app) as c:
        login(c)
        assert c.post("/api/repos", json={"url": f"file://{env['acme']}"}).status_code == 200
        assert c.post("/api/repos/test", json={"url": f"file://{env['acme']}", "name": "acme"}).json()["branches"] == ["entwurf", "main"]
        r = c.put("/api/repos/acme", json={"branch": "entwurf", "token": TOKEN})
        assert r.status_code == 200, r.text
        assert r.json()["current_branch"] == "entwurf" and r.json()["has_token"]
        _, hits = call(c, "search", {"text": "Entwurf"}, url="/mcp/acme")
        assert [h["path"] for h in hits] == ["Entwurf.md"]
        # mit nicht committeter Änderung kein Wechsel
        (env["vault"] / "acme" / "Lose.md").write_text("x", encoding="utf-8")
        r = c.put("/api/repos/acme", json={"branch": "main"})
        assert r.status_code == 400 and "nicht committete" in r.json()["error"]
