"""Vault aus einem konfigurierten Repo: klonen, fremdes Repo ablehnen, Token nie in Meldungen."""

import base64
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from vaultserver.config import Config
from vaultserver.gitops import GitError, ensure_vault, repo_key, strip_credentials, vault_git
from vaultserver.store import Store

TOKEN = "geheim-TOKEN-1234567890"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=repo,
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """Bare-Repo mit einer Notiz auf main."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "Start.md").write_text("# Start\n\nHallo.\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "erste Notiz")
    bare = tmp_path / "kunde.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def cfg(tmp_path: Path, url: str, **kw) -> Config:
    return Config(vault_path=tmp_path / "vault", db_path=tmp_path / "data" / "idx.sqlite", git_url=url, **kw)


def test_repo_key_https_ssh_gleich():
    assert repo_key("https://github.com/Acme/Vault.git") == repo_key("git@github.com:Acme/Vault")
    assert repo_key("https://x:tok@GitHub.com/Acme/Vault/") == "github.com/Acme/Vault"
    assert repo_key("https://github.com/acme/a") != repo_key("https://github.com/acme/b")
    assert strip_credentials("https://u:p@h.example/x.git") == "https://h.example/x.git"


def test_ohne_url_nichts_tun(tmp_path: Path):
    c = Config(vault_path=tmp_path / "vault", db_path=tmp_path / "idx.sqlite")
    assert ensure_vault(c) == ""
    assert not (tmp_path / "vault").exists()


def test_klont_in_leeren_ordner(tmp_path: Path, remote: Path):
    (tmp_path / "vault").mkdir()          # wie ein leeres Docker-Volume
    c = cfg(tmp_path, str(remote), git_branch="main", git_committer="Acme Vault <vault@acme.example>")
    assert ensure_vault(c).startswith("geklont")
    assert (tmp_path / "vault" / "Start.md").exists()
    assert ensure_vault(c) == ""          # zweiter Start: nichts zu tun
    store = Store(c)
    store.index.sync()
    store.write("Neu.md", "# Neu\n", agent="test")
    log = git(tmp_path / "vault", "log", "-1", "--format=%cn <%ce>")
    assert log.strip() == "Acme Vault <vault@acme.example>"


def test_fremdes_repo_wird_abgelehnt(tmp_path: Path, remote: Path):
    ensure_vault(cfg(tmp_path, str(remote)))
    with pytest.raises(GitError, match="gehört zu"):
        ensure_vault(cfg(tmp_path, "https://github.com/andere/firma.git"))


def test_falscher_branch_wird_abgelehnt(tmp_path: Path, remote: Path):
    ensure_vault(cfg(tmp_path, str(remote)))
    with pytest.raises(GitError, match="Branch"):
        ensure_vault(cfg(tmp_path, str(remote), git_branch="kunde"))


def test_fremder_ordner_wird_nicht_ueberschrieben(tmp_path: Path, remote: Path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "privat.md").write_text("x", encoding="utf-8")
    with pytest.raises(GitError, match="kein Git-Repo"):
        ensure_vault(cfg(tmp_path, str(remote)))
    assert (tmp_path / "vault" / "privat.md").read_text() == "x"


def test_committer_muss_name_und_adresse_haben(tmp_path: Path, remote: Path):
    with pytest.raises(ValueError):
        vault_git(cfg(tmp_path, str(remote), git_committer="nur-ein-name"))


def test_status_von_pull_und_push(tmp_path: Path, remote: Path):
    c = cfg(tmp_path, str(remote), git_push=True)
    ensure_vault(c)
    store = Store(c)
    store.index.sync()
    store.pull()
    assert store.git_status["pull"]["ok"] is True
    store.write("Neu.md", "# Neu\n", agent="test")
    store.push()
    assert store.git_status["push"]["ok"] is True
    assert "Neu.md" in git(remote, "ls-tree", "--name-only", "main")


class _AuthServer(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self):
        auth = self.headers.get("Authorization")
        _AuthServer.seen.append(auth or "")
        if not auth:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_token_wird_gesendet_aber_nie_gemeldet(tmp_path: Path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _AuthServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _AuthServer.seen = []
        monkeypatch.setenv("VS_GIT_TOKEN", TOKEN)
        url = f"http://127.0.0.1:{srv.server_port}/acme/vault.git"
        with pytest.raises(GitError) as e:
            ensure_vault(cfg(tmp_path, url))
        sent = [base64.b64decode(a.split()[1]).decode() for a in _AuthServer.seen if a]
        assert f"x-access-token:{TOKEN}" in sent
        assert TOKEN not in str(e.value)
    finally:
        srv.shutdown()


def test_zugangsdaten_in_der_url_landen_nie_in_meldungen(tmp_path: Path):
    url = f"http://nutzer:{TOKEN}@127.0.0.1:9/acme/vault.git"
    with pytest.raises(GitError) as e:
        ensure_vault(cfg(tmp_path, url))
    assert TOKEN not in str(e.value)
