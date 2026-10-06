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
        if not token:
            return None
        h = _sha(token)
        with self._lock:
            if h in self._data["revoked"]:
                return None
            name = self.config.tokens.get(token)
            if name is None:
                name = next((t["name"] for t in self._data["tokens"] if secrets.compare_digest(t["hash"], h)), None)
            if name is not None:
                self._touch(h[:12])
            return name

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
            seen = self._data["last_seen"]
            out = []
            for token, name in self.config.tokens.items():
                h = _sha(token)
                if h in self._data["revoked"]:
                    continue
                out.append({"id": h[:12], "name": name, "source": "config", "created": None, "last_seen": seen.get(h[:12])})
            for t in self._data["tokens"]:
                out.append({"id": t["hash"][:12], "name": t["name"], "source": "web", "created": t["created"],
                            "last_seen": seen.get(t["hash"][:12])})
            return sorted(out, key=lambda c: c["name"].lower())

    def create(self, name: str) -> dict:
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("Name: 1–40 Zeichen, Buchstaben, Ziffern, Punkt, Minus, Unterstrich (z. B. laptop-oliver)")
        if any(c["name"].lower() == name.lower() for c in self.list()):
            raise ValueError(f"Einen Zugang „{name}“ gibt es schon – erst sperren oder „Neuer Token“ verwenden")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._data["tokens"].append({"name": name, "hash": _sha(token), "created": time.time()})
            self._save()
        return {"id": _sha(token)[:12], "name": name, "token": token}

    def revoke(self, cid: str) -> str:
        """Sperrt einen Zugang; liefert den Namen."""
        with self._lock:
            for t in self._data["tokens"]:
                if t["hash"][:12] == cid:
                    self._data["tokens"].remove(t)
                    self._data["last_seen"].pop(cid, None)
                    self._save()
                    return t["name"]
            for token, name in self.config.tokens.items():
                h = _sha(token)
                if h[:12] == cid and h not in self._data["revoked"]:
                    self._data["revoked"].append(h)
                    self._save()
                    return name
        raise KeyError(f"Zugang {cid} nicht gefunden")

    def renew(self, cid: str) -> dict:
        """Neuer Token mit gleichem Namen, der alte ist sofort ungültig."""
        name = self.revoke(cid)
        return self.create(name)
