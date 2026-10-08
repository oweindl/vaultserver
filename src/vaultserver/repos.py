"""Eingebundene Git-Repos: jedes Repo liegt als Ordner der obersten Ebene im Vault und ist damit ein Projekt
(eigener MCP-Endpunkt /mcp/<name>, Auswahl in der Oberfläche, auf das Projekt beschränkte Zugänge).

Repos kommen aus [[repos]] in vaultserver.toml (nur lesbar, Token aus der Umgebung) oder werden in der
Web-Einrichtung angelegt (data/repos.json, Token verschlüsselt mit dem Server-Geheimnis). Das Repo im
Vault-Stamm (vault_path selbst) bleibt davon unberührt; eingebundene Ordner stehen in dessen .git/info/exclude.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config
from .gitops import Git, GitError, repo_key, strip_credentials
from .secretbox import open_, seal, server_secret

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
EXCLUDE_MARK = "# vaultserver: eingebundene Repos"


@dataclass
class Repo:
    name: str                       # Ordner im Vault und Projektname (/mcp/<name>)
    url: str
    branch: str = ""
    username: str = "x-access-token"
    committer: str = ""             # "Name <adresse>", leer = wie [git] committer
    push: bool = True
    pull_seconds: int = 60          # 0 = nie
    split: bool = False             # True: jeder Ordner der obersten Ebene im Repo ist ein eigener Vault
    source: str = "web"             # "web" (data/repos.json) oder "config" (vaultserver.toml)
    token_env: str = ""             # nur config: Umgebungsvariable mit dem Token
    token_box: str = field(default="", repr=False)   # nur web: verschlüsseltes Token
    added_by: str = ""
    added_at: str = ""

    def public(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("token_box",)}
        d["url"] = strip_credentials(self.url)
        return d


def contains(g: Git, ancestor: str, rev: str) -> bool:
    """True, wenn ancestor in rev enthalten ist."""
    try:
        g.run("merge-base", "--is-ancestor", ancestor, rev)
        return True
    except GitError:
        return False


def default_name(url: str) -> str:
    """Ordnername aus der Repo-Adresse: https://github.com/acme/Kunden-Vault.git -> kunden-vault."""
    last = repo_key(url).rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "-", last.lower()).strip("-")[:63]


class Repos:
    def __init__(self, config: Config, lock: threading.RLock | None = None):
        self.config = config
        self.lock = lock or threading.RLock()
        self.file = config.db_path.parent / "repos.json"
        self.status: dict[str, dict] = {}          # name -> {"pull": {...}, "push": {...}}
        self._gits: dict[str, Git] = {}
        self._repos: dict[str, Repo] = {}
        self.load()

    # ------------------------------------------------------------ Laden und Speichern

    def load(self) -> None:
        repos: dict[str, Repo] = {}
        for spec in self.config.repos:
            spec = dict(spec)
            name = spec.get("name") or default_name(spec.get("url", ""))
            spec.setdefault("token_env", f"VS_GIT_TOKEN_{name.upper().replace('-', '_')}")
            spec.setdefault("pull_seconds", self.config.git_pull_seconds or 60)
            repos[name] = Repo(**{**spec, "name": name, "source": "config"})
        if self.file.exists():
            for spec in json.loads(self.file.read_text(encoding="utf-8")).get("repos", []):
                if spec.get("name") not in repos:
                    repos[spec["name"]] = Repo(**{**spec, "source": "web"})
        with self.lock:
            self._repos = repos
            self._gits = {}
            self._publish()

    def _publish(self) -> None:
        """Ordner und Aufteilung für Projekte, Papierkörbe und Git sichtbar machen."""
        self.config.repo_folders = sorted(self._repos)
        self.config.repo_split = sorted(n for n, r in self._repos.items() if r.split)

    def _save(self) -> None:
        rows = [asdict(r) for r in self._repos.values() if r.source == "web"]
        for r in rows:
            r.pop("source", None)
            r.pop("token_env", None)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"repos": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self.file)

    # ------------------------------------------------------------ Zugriff

    def list(self) -> list[Repo]:
        return list(self._repos.values())

    def get(self, name: str) -> Repo:
        try:
            return self._repos[name]
        except KeyError:
            raise KeyError(f"Repo „{name}“ gibt es nicht") from None

    def folder(self, name: str) -> Path:
        return self.config.vault_path / name

    def token(self, repo: Repo) -> str:
        if repo.source == "config":
            return os.environ.get(repo.token_env, "")
        return open_(server_secret(self.config), repo.token_box) if repo.token_box else ""

    def _git(self, repo: Repo, path: Path | None = None) -> Git:
        return Git(path or self.folder(repo.name), committer=repo.committer or self.config.git_committer,
                   url=repo.url, token=self.token(repo), username=repo.username)

    def git(self, name: str) -> Git:
        g = self._gits.get(name)
        if g is None:
            g = self._gits[name] = self._git(self.get(name))
        return g

    def mounted(self) -> dict[str, Git]:
        """Ordner -> Git für alle Repos, die schon geklont sind."""
        return {n: self.git(n) for n in list(self._repos) if (self.folder(n) / ".git").exists()}

    def info(self, name: str) -> dict:
        r = self.get(name)
        g = self.git(name)
        out = {**r.public(), "cloned": g.enabled, "has_token": bool(self.token(r)),
               **self.status.get(name, {"pull": None, "push": None})}
        if g.enabled:
            out["current_branch"] = g.current_branch()
            out["head"] = g.head()[:10]
        return out

    # ------------------------------------------------------------ Prüfen

    def _check_url(self, url: str) -> str:
        url = (url or "").strip()
        parts = urlsplit(url)
        scheme = parts.scheme or ("file" if url.startswith("/") else "")
        if scheme not in self.config.repo_schemes:
            raise ValueError(f"Nur {', '.join(s + '://' for s in self.config.repo_schemes)}-Adressen erlaubt")
        if parts.username or parts.password:
            raise ValueError("Zugangsdaten nicht in die Adresse schreiben, sondern ins Feld Token")
        return url

    def _check_name(self, name: str) -> str:
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("Name: Kleinbuchstaben, Ziffern und Bindestriche (höchstens 63 Zeichen)")
        reserved = {x.lower() for x in (*self.config.exclude, *self.config.archive_folders,
                                         self.config.recycle_folder)}
        if name in reserved:
            raise ValueError(f"„{name}“ ist reserviert")
        if name in self._repos:
            raise ValueError(f"Repo „{name}“ gibt es schon")
        if self.folder(name).exists():
            raise ValueError(f"Im Vault gibt es schon einen Ordner „{name}“")
        return name

    def test(self, url: str, token: str = "", username: str = "x-access-token", name: str = "") -> dict:
        """Verbindung prüfen ohne zu klonen. Ohne neues Token gilt das gespeicherte des Repos name."""
        url = self._check_url(url)
        if not token and name in self._repos:
            token = self.token(self._repos[name])
        work = self.config.db_path.parent
        work.mkdir(parents=True, exist_ok=True)
        g = Git(work, url=url, token=token, username=username)
        return {"ok": True, **g.ls_remote(url)}

    # ------------------------------------------------------------ Ändern

    def add(self, name: str, url: str, branch: str = "", token: str = "", username: str = "x-access-token",
            committer: str = "", push: bool = True, pull_seconds: int | None = None, agent: str = "",
            split: bool = False) -> Repo:
        """Repo klonen und einbinden. Klont in einen versteckten Ordner und benennt erst am Ende um."""
        url = self._check_url(url)
        name = self._check_name(name or default_name(url))
        if committer:
            from .gitops import parse_committer
            parse_committer(committer)
        for r in self._repos.values():
            if repo_key(r.url) == repo_key(url) and (not branch or not r.branch or r.branch == branch):
                raise ValueError(f"Dieses Repo ist schon als „{r.name}“ eingebunden")
        repo = Repo(name=name, url=url, branch=branch, username=username or "x-access-token",
                    committer=committer, push=bool(push), split=bool(split),
                    pull_seconds=(self.config.git_pull_seconds or 60) if pull_seconds is None else max(0, int(pull_seconds)),
                    token_box=seal(server_secret(self.config), token) if token else "",
                    added_by=agent, added_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        tmp = self.config.vault_path / f".vs-clone-{name}-{secrets.token_hex(3)}"
        try:
            self._git(repo, tmp).clone(url, branch)
            if not repo.branch:
                repo.branch = Git(tmp).current_branch()
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        with self.lock:
            if self.folder(name).exists():
                shutil.rmtree(tmp, ignore_errors=True)
                raise ValueError(f"Im Vault gibt es schon einen Ordner „{name}“")
            self._root_exclude(add=name)
            os.replace(tmp, self.folder(name))
            self._repos[name] = repo
            self._gits.pop(name, None)
            self._publish()
            self._save()
        return repo

    def update(self, name: str, *, token: str | None = None, branch: str | None = None,
               username: str | None = None, committer: str | None = None, push: bool | None = None,
               pull_seconds: int | None = None, split: bool | None = None) -> Repo:
        with self.lock:
            repo = self.get(name)
            if repo.source != "web":
                raise ValueError("Dieses Repo steht in vaultserver.toml und lässt sich nur dort ändern")
            if committer is not None:
                if committer:
                    from .gitops import parse_committer
                    parse_committer(committer)
                repo.committer = committer
            if token is not None and token != "":
                repo.token_box = seal(server_secret(self.config), token)
            if username is not None and username:
                repo.username = username
            if push is not None:
                repo.push = bool(push)
            if pull_seconds is not None:
                repo.pull_seconds = max(0, int(pull_seconds))
            if split is not None:
                repo.split = bool(split)
                self._publish()
            self._gits.pop(name, None)
            if branch and branch != repo.branch:
                g = self.git(name)
                dirty = g.dirty_files()
                if dirty:
                    raise ValueError(f"Branch wechseln geht nicht: {len(dirty)} nicht committete Änderungen")
                if g.ahead(self.config.git_remote):
                    raise ValueError("Branch wechseln geht nicht: es gibt noch nicht gepushte Commits")
                g.switch_branch(self.config.git_remote, branch)
                repo.branch = branch
            self._save()
            return repo

    def set_url(self, name: str, url: str) -> Repo:
        """Neue Adresse (z. B. Repo auf GitHub umbenannt oder umgezogen). Wird vorher geprüft; das Repo unter der
        neuen Adresse muss den aktuellen Stand kennen, sonst wäre es ein anderes Repo."""
        with self.lock:
            repo = self.get(name)
            if repo.source != "web":
                raise ValueError("Dieses Repo steht in vaultserver.toml und lässt sich nur dort ändern")
            url = self._check_url(url)
            if repo_key(url) == repo_key(repo.url):
                repo.url = url
                self._save()
                return repo
            for r in self._repos.values():
                if r.name != name and repo_key(r.url) == repo_key(url):
                    raise ValueError(f"Diese Adresse ist schon als „{r.name}“ eingebunden")
            g = self.git(name)
            remote = self.config.git_remote
            old = g.remote_url(remote)
            ref = f"refs/remotes/{remote}/{repo.branch or g.current_branch()}"
            known = g.run("rev-parse", "--verify", "-q", ref, check=False).strip()   # letzter bekannter Remote-Stand
            g.run("remote", "set-url", remote, strip_credentials(url))
            try:
                probe = Git(self.folder(name), committer=repo.committer, url=url, token=self.token(repo),
                            username=repo.username)
                probe.run("fetch", "-q", remote)
                now = g.run("rev-parse", "--verify", "-q", ref, check=False).strip()
                same = bool(now) and (not known or contains(g, known, now))
                if not same:
                    raise ValueError("Unter der neuen Adresse fehlt der bisherige Stand – ist das wirklich dasselbe Repo?")
            except Exception:
                g.run("remote", "set-url", remote, old or strip_credentials(repo.url), check=False)
                if known:
                    g.run("update-ref", ref, known, check=False)   # Remote-Stand wie vorher
                raise
            repo.url = url
            self._gits.pop(name, None)
            self._save()
            return repo

    def rename(self, name: str, new: str) -> Repo:
        """Ordner und Projektnamen ändern. Zugänge und offene Pushes stellt der Aufrufer um."""
        with self.lock:
            repo = self.get(name)
            if repo.source != "web":
                raise ValueError("Dieses Repo steht in vaultserver.toml und lässt sich nur dort ändern")
            new = self._check_name(new)
            src, dst = self.folder(name), self.folder(new)
            self._root_exclude(add=new)
            if src.exists():
                os.replace(src, dst)
            self._root_exclude(remove=name)
            del self._repos[name]
            repo.name = new
            self._repos[new] = repo
            self._gits.pop(name, None)
            if name in self.status:
                self.status[new] = self.status.pop(name)
            self._publish()
            self._save()
            return repo

    def remove(self, name: str, force: bool = False) -> dict:
        """Repo aus dem Vault nehmen. Der Ordner wandert nach data/removed-repos (nichts geht verloren)."""
        with self.lock:
            repo = self.get(name)
            if repo.source != "web":
                raise ValueError("Dieses Repo steht in vaultserver.toml und lässt sich nur dort entfernen")
            folder = self.folder(name)
            moved_to = None
            if folder.exists():
                g = self.git(name)
                if not force and g.enabled:
                    dirty = g.dirty_files()
                    if dirty:
                        raise ValueError(f"{len(dirty)} nicht committete Änderungen – erst prüfen oder erzwingen")
                    if g.ahead(self.config.git_remote):
                        raise ValueError("Es gibt noch nicht gepushte Commits – erst pushen oder erzwingen")
                dest = self.config.db_path.parent / "removed-repos" / f"{name}-{time.strftime('%Y%m%dT%H%M%S')}"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder), str(dest))
                moved_to = str(dest)
            del self._repos[name]
            self._gits.pop(name, None)
            self.status.pop(name, None)
            self._publish()
            self._root_exclude(remove=name)
            self._save()
            return {"name": name, "removed": True, "moved_to": moved_to}

    def ensure_cloned(self) -> list[str]:
        """Repos aus der Konfiguration klonen, deren Ordner fehlt oder leer ist; Remote der vorhandenen prüfen."""
        done = []
        for repo in self.list():
            folder = self.folder(repo.name)
            if folder.exists() and any(folder.iterdir()):
                g = self.git(repo.name)
                if not g.enabled:
                    raise GitError(f"Ordner „{repo.name}“ ist nicht leer und kein Git-Repo")
                if repo_key(g.remote_url(self.config.git_remote)) != repo_key(repo.url):
                    raise GitError(f"Ordner „{repo.name}“ gehört zu einem anderen Repo als {strip_credentials(repo.url)}")
                self._root_exclude(add=repo.name)
                continue
            self._root_exclude(add=repo.name)
            self.git(repo.name).clone(repo.url, repo.branch)
            done.append(repo.name)
        return done

    # ------------------------------------------------------------ Repo im Stamm: eingebundene Ordner ignorieren

    def _root_exclude(self, add: str | None = None, remove: str | None = None) -> None:
        gitdir = self.config.vault_path / ".git"
        if not gitdir.is_dir():
            return
        f = gitdir / "info" / "exclude"
        f.parent.mkdir(parents=True, exist_ok=True)
        lines = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
        entry = lambda n: f"/{n}/"   # noqa: E731
        if add and entry(add) not in lines:
            if EXCLUDE_MARK not in lines:
                lines.append(EXCLUDE_MARK)
            lines.append(entry(add))
        if remove:
            lines = [x for x in lines if x != entry(remove)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------ Status

    def track(self, name: str, what: str, ok: bool, error: str = "") -> None:
        st = self.status.setdefault(name, {"pull": None, "push": None})
        at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        st[what] = {"ok": True, "at": at} if ok else {"ok": False, "at": at, "error": error[:500]}
