"""MCP-Zugänge (Bearer-Tokens je Rechner).

Zwei Quellen: Tokens aus vaultserver.toml ([server.tokens], Klartext, wie bisher) und Tokens, die in der
Web-Oberfläche angelegt wurden (data/tokens.json, nur als SHA-256 gespeichert, Klartext wird genau einmal
angezeigt). Tokens aus der Konfiguration lassen sich in der Oberfläche sperren; die Sperre steht ebenfalls in
data/tokens.json. Jeder Zugang hat eine id (erste 12 Stellen des SHA-256), damit nie der Token selbst durch die
Oberfläche wandert.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time

from .config import Config

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
SEEN_WRITE_SECONDS = 60


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Clients:
    def __init__(self, config: Config):
        self.config = config
        self.file = config.db_path.parent / "tokens.json"
        self._lock = threading.Lock()
        self._data = self._load()
        self._seen_written = 0.0

    # ------------------------------------------------------------ Datei

    def _load(self) -> dict:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        data.setdefault("tokens", [])      # [{name, hash, created}]
        data.setdefault("revoked", [])     # SHA-256 gesperrter Tokens aus der Konfiguration
        data.setdefault("last_seen", {})   # id -> Zeitpunkt
        data.setdefault("projects", {})    # id -> Projektname (Zugang nur für dieses Projekt)
        return data

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.file)

    # ------------------------------------------------------------ Prüfen

    def lookup(self, token: str) -> str | None:
        """Rechnername zum Token oder None (unbekannt oder gesperrt)."""
        found = self.lookup_full(token)
        return found[0] if found else None

    def lookup_full(self, token: str) -> tuple[str, str | None] | None:
        """(Rechnername, Projekt oder None) zum Token; None, wenn unbekannt oder gesperrt."""
        if not token:
            return None
        h = _sha(token)
        with self._lock:
            if h in self._data["revoked"]:
                return None
            name = self.config.tokens.get(token)
            if name is None:
                name = next((t["name"] for t in self._data["tokens"] if secrets.compare_digest(t["hash"], h)), None)
            if name is None:
                return None
            self._touch(h[:12])
            return name, self._data["projects"].get(h[:12])

    def _touch(self, cid: str) -> None:
        now = time.time()
        self._data["last_seen"][cid] = now
        if now - self._seen_written > SEEN_WRITE_SECONDS:
            self._seen_written = now
            try:
                self._save()
            except OSError:
                pass

    # ------------------------------------------------------------ Verwalten

    def list(self) -> list[dict]:
        with self._lock:
            seen, projects = self._data["last_seen"], self._data["projects"]
            out = []
            for token, name in self.config.tokens.items():
                h = _sha(token)
                if h in self._data["revoked"]:
                    continue
                out.append({"id": h[:12], "name": name, "source": "config", "created": None, "last_seen": seen.get(h[:12]),
                            "project": projects.get(h[:12])})
            for t in self._data["tokens"]:
                out.append({"id": t["hash"][:12], "name": t["name"], "source": "web", "created": t["created"],
                            "last_seen": seen.get(t["hash"][:12]), "project": projects.get(t["hash"][:12])})
            return sorted(out, key=lambda c: c["name"].lower())

    def create(self, name: str, project: str | None = None) -> dict:
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("Name: 1–40 Zeichen, Buchstaben, Ziffern, Punkt, Minus, Unterstrich (z. B. laptop-oliver)")
        if any(c["name"].lower() == name.lower() for c in self.list()):
            raise ValueError(f"Einen Zugang „{name}“ gibt es schon – erst sperren oder „Neuer Token“ verwenden")
        token = secrets.token_urlsafe(32)
        cid = _sha(token)[:12]
        with self._lock:
            self._data["tokens"].append({"name": name, "hash": _sha(token), "created": time.time()})
            if project:
                self._data["projects"][cid] = project
            self._save()
        return {"id": cid, "name": name, "token": token, "project": project or None}

    def set_project(self, cid: str, project: str | None) -> dict:
        """Zugang auf ein Projekt beschränken (oder mit None wieder für den ganzen Vault freigeben)."""
        entry = next((c for c in self.list() if c["id"] == cid), None)
        if not entry:
            raise KeyError(f"Zugang {cid} nicht gefunden")
        with self._lock:
            if project:
                self._data["projects"][cid] = project
            else:
                self._data["projects"].pop(cid, None)
            self._save()
        return {**entry, "project": project or None}

    def rename_project(self, old: str, new: str) -> int:
        """Bindungen an ein umbenanntes Projekt (auch Unter-Vaults old/…) nachziehen. Gibt die Anzahl zurück."""
        n = 0
        with self._lock:
            for cid, p in list(self._data["projects"].items()):
                if p == old or p.startswith(old + "/"):
                    self._data["projects"][cid] = new + p[len(old):]
                    n += 1
            if n:
                self._save()
        return n

    def export(self) -> list[dict]:
        """Im Web angelegte Zugänge mit Hash (nie Klartext) und Projektbindung."""
        with self._lock:
            return [{"name": t["name"], "hash": t["hash"], "created": t["created"],
                     "project": self._data["projects"].get(t["hash"][:12])} for t in self._data["tokens"]]

    def import_entry(self, name: str, hash_: str, created: float | None = None, project: str | None = None) -> str:
        """Zugang aus einem Export übernehmen; der Rechner behält seinen Token. Gibt das Ergebnis als Text."""
        if not NAME_RE.match(name or "") or not re.fullmatch(r"[0-9a-f]{64}", hash_ or ""):
            raise ValueError("ungültiger Eintrag")
        with self._lock:
            if any(t["hash"] == hash_ for t in self._data["tokens"]) or any(
                    _sha(tok) == hash_ for tok in self.config.tokens):
                return "gibt es schon"
            if any(t["name"].lower() == name.lower() for t in self._data["tokens"]) or any(
                    n.lower() == name.lower() for n in self.config.tokens.values()):
                return "Name gibt es schon"
            self._data["tokens"].append({"name": name, "hash": hash_, "created": created or time.time()})
            if project:
                self._data["projects"][hash_[:12]] = project
            self._save()
        return "übernommen"

    def revoke(self, cid: str) -> str:
        """Sperrt einen Zugang; liefert den Namen."""
        with self._lock:
            for t in self._data["tokens"]:
                if t["hash"][:12] == cid:
                    self._data["tokens"].remove(t)
                    self._data["last_seen"].pop(cid, None)
                    self._data["projects"].pop(cid, None)
                    self._save()
                    return t["name"]
            for token, name in self.config.tokens.items():
                h = _sha(token)
                if h[:12] == cid and h not in self._data["revoked"]:
                    self._data["revoked"].append(h)
                    self._data["projects"].pop(cid, None)
                    self._save()
                    return name
        raise KeyError(f"Zugang {cid} nicht gefunden")

    def renew(self, cid: str) -> dict:
        """Neuer Token mit gleichem Namen und gleichem Projekt, der alte ist sofort ungültig."""
        with self._lock:
            project = self._data["projects"].get(cid)
        name = self.revoke(cid)
        return self.create(name, project)
