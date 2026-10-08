"""Dünne Hülle um das git-Kommando für den Vault und die Code-Repos."""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

PASSWORD_ENV = "VAULTSERVER_GIT_PASSWORD"   # nur in der Umgebung des git-Prozesses, nie auf der Kommandozeile


class GitError(RuntimeError):
    pass


def strip_credentials(url: str) -> str:
    """Zugangsdaten aus einer URL entfernen (https://user:token@host/... -> https://host/...)."""
    return re.sub(r"(?<=://)[^/@\s]+@", "", url or "")


def repo_key(url: str) -> str:
    """Vergleichsschlüssel host/pfad: gleich für https- und ssh-Schreibweise, ohne Zugangsdaten und .git."""
    url = strip_credentials(url.strip())
    m = re.match(r"^(?:ssh://)?[^/@\s]+@([^:/\s]+)[:/](.+)$", url)       # git@host:pfad, ssh://git@host/pfad
    if m:
        host, path = m.group(1), m.group(2)
    else:
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path}"


def parse_committer(text: str) -> tuple[str, str]:
    m = re.match(r"^\s*(.+?)\s*<([^<>\s]+)>\s*$", text or "")
    if not m:
        raise ValueError(f"[git] committer muss „Name <adresse>“ sein, nicht „{text}“")
    return m.group(1), m.group(2)


class Git:
    def __init__(self, repo: Path, committer: str = "", url: str = "", token: str = "",
                 username: str = "x-access-token"):
        self.repo = Path(repo)
        name, email = parse_committer(committer) if committer else ("VaultServer", f"vaultserver@{socket.gethostname()}")
        self.ident = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
        self._token = token
        self._env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        self._auth: list[str] = []
        parts = urlsplit(url) if url else None
        if token and parts and parts.scheme in ("http", "https") and parts.hostname:
            # Token nur für den Host des Vault-Repos; vorhandene Helfer (z. B. gh) für diesen Host abschalten
            ctx = f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"
            helper = (f"!f() {{ test \"$1\" = get || exit 0; echo username={username}; "
                      f"echo \"password=${PASSWORD_ENV}\"; }}; f")
            self._auth = ["-c", "credential.helper=", "-c", f"credential.{ctx}.helper=",
                          "-c", f"credential.{ctx}.helper={helper}"]
            self._env[PASSWORD_ENV] = token

    def redact(self, text: str) -> str:
        text = strip_credentials(text)
        return text.replace(self._token, "***") if self._token else text

    @property
    def enabled(self) -> bool:
        return (self.repo / ".git").exists()

    def run(self, *args: str, check: bool = True) -> str:
        res = subprocess.run(["git", *self.ident, *self._auth, *args], cwd=self.repo, capture_output=True,
                             text=True, env=self._env)
        if check and res.returncode != 0:
            raise GitError(self.redact(f"git {' '.join(args)}: {res.stderr.strip() or res.stdout.strip()}"))
        return res.stdout

    def clone(self, url: str, branch: str = "") -> None:
        """url nach self.repo klonen (Ordner fehlt oder ist leer)."""
        self.repo.mkdir(parents=True, exist_ok=True)
        self.run("clone", "-q", *(["--branch", branch] if branch else []), "--", strip_credentials(url), ".")

    def remote_url(self, remote: str = "origin") -> str:
        return strip_credentials(self.run("remote", "get-url", remote, check=False).strip())

    def current_branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD", check=False).strip()

    def commit(self, paths: list[str], message: str, agent: str) -> str | None:
        """Gegebene Pfade (auch gelöschte) committen. Gibt den Commit-Hash zurück oder None."""
        if not self.enabled:
            return None
        self.run("add", "-A", "--", *paths)
        if not self.run("diff", "--cached", "--name-only", "--", *paths).strip():
            return None
        safe = "".join(c for c in agent if c.isalnum() or c in "-_./") or "unbekannt"
        self.run("commit", "-q", "-m", message, f"--author={agent} <{safe}@vaultserver>", "--", *paths)
        return self.head()

    def pull(self, remote: str, branch: str = "") -> str:
        args = ["pull", "--rebase", "--autostash", "-q", remote]
        if branch:
            args.append(branch)
        return self.run(*args)

    def push(self, remote: str, branch: str = "") -> None:
        self.run("push", "-q", remote, *([f"HEAD:{branch}"] if branch else []))

    def ahead(self, remote: str) -> int:
        out = self.run("rev-list", "--count", "@{u}..HEAD", check=False).strip()
        return int(out) if out.isdigit() else 0

    def log(self, limit: int = 20, path: str | None = None, since: str | None = None,
            rev_range: str | None = None) -> list[dict]:
        """Commits mit geänderten Dateien, neueste zuerst."""
        args = ["log", f"-n{limit}", "--name-only", "--format=\x1e%H\x1f%an\x1f%aI\x1f%s"]
        if rev_range:
            args.append(rev_range)
        if since:
            args.append(f"--since={since}")
        if path:
            args += ["--", path]
        out = []
        for block in self.run(*args, check=False).split("\x1e")[1:]:
            head, _, files = block.partition("\n")
            h, author, date, subject = head.split("\x1f", 3)
            out.append({"commit": h, "author": author, "date": date, "message": subject,
                        "files": [f for f in files.strip().splitlines() if f]})
        return out

    def last_commit_per_file(self, limit: int = 300) -> dict[str, dict]:
        seen: dict[str, dict] = {}
        for c in self.log(limit):
            for f in c["files"]:
                seen.setdefault(f, {k: c[k] for k in ("commit", "author", "date", "message")})
        return seen

    def resolve_since(self, since: str) -> str:
        """Commit-Hash oder Zeitpunkt -> Commit-Hash (letzter Commit vor dem Zeitpunkt)."""
        if self.run("cat-file", "-t", since, check=False).strip() == "commit":
            return since
        out = self.run("rev-list", "-n1", f"--before={since}", "HEAD", check=False).strip()
        if not out:  # Zeitpunkt vor dem ersten Commit: alles ist neu
            out = self.run("hash-object", "-t", "tree", "/dev/null").strip()
        return out

    def changed_files(self, since: str) -> list[tuple[str, str]]:
        """(Status, Pfad) seit einem Commit, inkl. nicht committeter Änderungen."""
        out = self.run("diff", "--name-status", "-M", since, "--", ".", check=False)
        rows = []
        for line in out.splitlines():
            parts = line.split("\t")
            rows.append((parts[0][0], parts[-1]))
        for line in self.run("ls-files", "--others", "--exclude-standard", check=False).splitlines():
            rows.append(("A", line))
        return rows

    def changed_lines(self, since: str, path: str) -> list[tuple[int, int]]:
        """Geänderte Zeilenbereiche (neue Datei) seit einem Commit."""
        out = self.run("diff", "-U0", since, "--", path, check=False)
        ranges = []
        for line in out.splitlines():
            if line.startswith("@@"):
                new = line.split("+", 1)[1].split(" ", 1)[0]
                start, _, count = new.partition(",")
                n = int(count) if count else 1
                s = int(start)
                ranges.append((s, s + max(n, 1) - 1))
        return ranges

    def show(self, rev: str, path: str) -> str | None:
        res = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=self.repo, capture_output=True, text=True)
        return res.stdout if res.returncode == 0 else None


def vault_git(config) -> Git:
    """Git für den Vault mit Kennung und Zugang aus der Konfiguration (Token aus der Umgebung)."""
    return Git(config.vault_path, committer=config.git_committer, url=config.git_url,
               token=os.environ.get(config.git_token_env, "") if config.git_url else "",
               username=config.git_username)


def ensure_vault(config) -> str:
    """Vault-Repo nach [git] url bereitstellen. Gibt zurück, was passiert ist ("" = nichts zu tun).

    Ordner fehlt oder ist leer -> klonen. Repo mit anderem Remote oder Ordner mit fremden Dateien -> GitError,
    damit nie still ein falsches Repo benutzt oder ein fremder Ordner überschrieben wird."""
    if not config.git_url:
        return ""
    path = Path(config.vault_path)
    git = vault_git(config)
    if not path.exists() or not any(path.iterdir()):
        git.clone(config.git_url, config.git_branch)
        return f"geklont: {strip_credentials(config.git_url)}"
    if not git.enabled:
        raise GitError(f"{path} ist nicht leer und kein Git-Repo – [git] url wird nicht hineingeklont")
    have = git.remote_url(config.git_remote)
    if repo_key(have) != repo_key(config.git_url):
        raise GitError(f"{path} gehört zu „{have or 'ohne Remote ' + config.git_remote}“, "
                       f"konfiguriert ist „{strip_credentials(config.git_url)}“ – Start abgebrochen")
    if config.git_branch and git.current_branch() != config.git_branch:
        raise GitError(f"{path} steht auf Branch „{git.current_branch()}“, konfiguriert ist „{config.git_branch}“")
    return ""
