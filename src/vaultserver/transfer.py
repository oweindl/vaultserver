"""Konfiguration exportieren und importieren (Web-Einrichtung): eingebundene Repos, MCP-Zugänge, Web-Benutzer.

Was in vaultserver.toml steht, bleibt draußen (die Datei selbst ist die Sicherung). Zugänge und Benutzer
wandern nur als Hash (Rechner behalten ihre Tokens, Passwörter bleiben gleich). Repo-Tokens nur mit
Passphrase: verschlüsselt mit einem aus der Passphrase abgeleiteten Schlüssel (scrypt), nie im Klartext.
Import fügt nur hinzu, was es noch nicht gibt; vorhandene Einträge bleiben unverändert.
"""
from __future__ import annotations

import hashlib
import secrets
import socket
import time

from .secretbox import open_, seal

FORMAT = "vaultserver-config"
VERSION = 1
PARTS = ("repos", "clients", "users")
MIN_PASSPHRASE = 10


def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def export_config(svc, users, parts: list[str], passphrase: str = "") -> dict:
    parts = [p for p in PARTS if p in (parts or PARTS)]
    if passphrase and len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"Passphrase: mindestens {MIN_PASSPHRASE} Zeichen")
    out: dict = {"format": FORMAT, "version": VERSION, "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "server": socket.gethostname(), "parts": parts}
    key = None
    if passphrase:
        salt = secrets.token_bytes(16)
        key = _key(passphrase, salt)
        out["kdf"] = {"name": "scrypt", "salt": salt.hex(), "check": seal(key, "vaultserver")}
    if "repos" in parts:
        rows = []
        for r in svc.repos.list():
            if r.source != "web":
                continue
            row = {k: v for k, v in r.public().items() if k not in ("source", "token_env", "added_by", "added_at")}
            tok = svc.repos.token(r)
            if key and tok:
                row["token_sealed"] = seal(key, tok)
            rows.append(row)
        out["repos"] = rows
        out["repos_in_toml"] = [r.name for r in svc.repos.list() if r.source == "config"]
    if "clients" in parts:
        out["clients"] = svc.clients.export()
    if "users" in parts:
        out["users"] = users.all()
    return out


def import_config(svc, users, data: dict, parts: list[str], passphrase: str = "", dry_run: bool = True,
                  agent: str = "") -> dict:
    """Bericht je Eintrag: {"part", "name", "result"}. dry_run=True ändert nichts."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("Keine VaultServer-Konfiguration (Feld format fehlt oder passt nicht)")
    if data.get("version") != VERSION:
        raise ValueError(f"Version {data.get('version')} wird nicht unterstützt")
    parts = [p for p in PARTS if p in (parts or PARTS) and p in data]
    key = None
    kdf = data.get("kdf")
    if kdf and passphrase:
        key = _key(passphrase, bytes.fromhex(kdf["salt"]))
        try:
            open_(key, kdf["check"])
        except ValueError:
            raise ValueError("Passphrase passt nicht") from None
    report = []

    def note(part: str, name: str, result: str, ok: bool = True) -> None:
        report.append({"part": part, "name": name, "result": result, "ok": ok})

    if "repos" in parts:
        have = {r.name for r in svc.repos.list()}
        for row in data.get("repos", []):
            name = row.get("name", "")
            sealed = row.get("token_sealed", "")
            token = ""
            if sealed and key:
                token = open_(key, sealed)
            hint = "" if not sealed else (" mit Token" if key else " ohne Token (Passphrase fehlt)")
            if name in have:
                note("repos", name, "gibt es schon – unverändert")
                continue
            if dry_run:
                note("repos", name, f"wird geklont{hint}: {row.get('url', '')}")
                continue
            try:
                svc.repos.add(name, row.get("url", ""), branch=row.get("branch", ""), token=token,
                              username=row.get("username") or "x-access-token", committer=row.get("committer", ""),
                              push=row.get("push", True), pull_seconds=row.get("pull_seconds"), agent=agent,
                              split=bool(row.get("split", False)))
                note("repos", name, f"geklont{hint}")
            except Exception as e:  # noqa: BLE001 – ein Repo darf den Rest nicht aufhalten
                note("repos", name, svc.store.git.redact(str(e)), ok=False)
    if "clients" in parts:
        existing = {c["name"].lower() for c in svc.clients.list()}
        for row in data.get("clients", []):
            name = row.get("name", "")
            if dry_run:
                note("clients", name, "gibt es schon – unverändert" if name.lower() in existing
                     else f"wird übernommen{' (nur ' + row['project'] + ')' if row.get('project') else ''}")
                continue
            try:
                note("clients", name, svc.clients.import_entry(name, row.get("hash", ""), row.get("created"),
                                                               row.get("project")))
            except ValueError as e:
                note("clients", name, str(e), ok=False)
    if "users" in parts:
        current = users.all()
        for user, stored in (data.get("users") or {}).items():
            if user in current:
                note("users", user, "gibt es schon – unverändert")
                continue
            if dry_run:
                note("users", user, "wird übernommen (Passwort wie auf dem alten Server)")
                continue
            try:
                users.set_hash(user, stored)
                note("users", user, "übernommen")
            except ValueError as e:
                note("users", user, str(e), ok=False)
    return {"dry_run": dry_run, "parts": parts, "has_tokens": bool(kdf), "passphrase_ok": bool(key),
            "report": report}
