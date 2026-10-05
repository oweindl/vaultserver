"""Dünne Hülle um das git-Kommando für den Vault und die Code-Repos."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo: Path):
        self.repo = Path(repo)
        self.ident = ["-c", "user.name=VaultServer", "-c", f"user.email=vaultserver@{socket.gethostname()}"]

    @property
    def enabled(self) -> bool:
        return (self.repo / ".git").exists()

    def run(self, *args: str, check: bool = True) -> str:
        res = subprocess.run(["git", *self.ident, *args], cwd=self.repo, capture_output=True, text=True)
        if check and res.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {res.stderr.strip() or res.stdout.strip()}")
        return res.stdout

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
