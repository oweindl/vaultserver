"""Projekt-Kontexte: ein Unterordner des Vaults als eigener Einstiegspunkt.

Über /mcp/<projekt> (oder mit einem auf ein Projekt beschränkten Token) sehen die MCP-Werkzeuge nur diesen
Ordner. Pfade gehen relativ zum Projektordner hinein und heraus („Fixliste/FIX-001.md“ statt
„Finance App/Fixliste/FIX-001.md“); volle Pfade innerhalb des Projekts werden ebenfalls angenommen. Alles
außerhalb ist unsichtbar bzw. wird abgelehnt.

Projekte kommen aus [projects] in vaultserver.toml (name = "Ordner" oder name = {folder, start}); ohne
Angabe ist jeder Ordner der obersten Ebene ein Projekt (Name in Kleinbuchstaben mit Bindestrichen).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import Config

SCOPE_HEADER = "x-vaultserver-scope"
START_CANDIDATES = ("Projektbeschreibung.md", "README.md", "Übersicht.md", "Uebersicht.md", "Index.md")
# Felder mit freiem Text: dort keine Pfade umschreiben
TEXT_KEYS = {"content", "text", "snippet", "snip", "message", "body", "summary", "area_rules", "rules"}


def slug(name: str) -> str:
    s = name.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


RULES_NOTE = "_Regeln.md"   # optionale Regel-Notiz je Projekt, Inhalt kommt mit guide


@dataclass(frozen=True)
class Project:
    name: str      # in der URL, z. B. finance-app
    folder: str    # Ordner im Vault, z. B. Finance App
    start: str     # Einstiegsnotiz (voller Pfad) oder ""
    soft_kb: int = 20
    hard_kb: int = 50
    folder_notes: int = 25
    rules: str = ""  # Regel-Notiz (voller Pfad) oder ""


@dataclass(frozen=True)
class Limits:
    soft_kb: int
    hard_kb: int
    folder_notes: int
    project: str = ""


def limits_for(config: Config, path: str, projects: dict | None = None) -> Limits:
    """Größengrenzen für einen Pfad: die seines Projekts, sonst die allgemeinen."""
    for p in (projects if projects is not None else load_projects(config)).values():
        if path == p.folder or path.startswith(p.folder + "/"):
            return Limits(p.soft_kb, p.hard_kb, p.folder_notes, p.name)
    return Limits(config.soft_kb, config.hard_kb, config.folder_notes)


def load_projects(config: Config) -> dict[str, Project]:
    vault: Path = config.vault_path
    raw = config.projects
    if not raw:  # automatisch: Ordner der obersten Ebene
        hidden = set(config.exclude) | set(config.archive_folders)
        raw = {slug(p.name): p.name for p in sorted(vault.iterdir())
               if p.is_dir() and not p.name.startswith(".") and p.name not in hidden and slug(p.name)}
    raw = dict(raw)
    covered = {(v if isinstance(v, str) else v.get("folder", "")).strip("/") for v in raw.values()}
    for folder in config.repo_folders:     # eingebundene Repos sind immer Projekte
        if folder not in covered and (vault / folder).is_dir():
            raw[folder] = folder
    out: dict[str, Project] = {}
    for name, spec in raw.items():
        spec = {"folder": spec} if isinstance(spec, str) else dict(spec)
        folder, start = spec.get("folder", ""), spec.get("start", "")
        folder = folder.strip("/")
        if not folder:
            continue
        if not start:
            for cand in (*(f"{folder}/{c}" for c in START_CANDIDATES), f"{folder}.md", f"{folder}/{folder}.md"):
                if (vault / cand).is_file():
                    start = cand
                    break
        elif not start.startswith(folder + "/"):
            start = f"{folder}/{start}"
        rules = spec.get("rules", RULES_NOTE)
        rules = rules if rules.startswith(folder + "/") else f"{folder}/{rules}"
        out[slug(name) or name] = Project(
            slug(name) or name, folder, start,
            soft_kb=int(spec.get("soft_kb", config.soft_kb)), hard_kb=int(spec.get("hard_kb", config.hard_kb)),
            folder_notes=int(spec.get("folder_notes", config.folder_notes)),
            rules=rules if (vault / rules).is_file() else "")
    return out


class Scope:
    """Übersetzt Pfade zwischen Projekt (relativ) und Vault (voll). Ohne Projekt: alles unverändert."""

    def __init__(self, project: Project | None):
        self.project = project
        self.prefix = project.folder + "/" if project else ""

    def __bool__(self) -> bool:
        return self.project is not None

    # ------------------------------------------------------------ hinein

    def full(self, path: str | None, *, allow_root: bool = False) -> str:
        """Relativer (oder voller) Pfad im Projekt -> voller Vault-Pfad. Wirft ValueError außerhalb."""
        p = (path or "").strip().lstrip("/")
        if not self.project:
            return p
        parts = []
        for seg in PurePosixPath(p).parts if p else ():
            if seg == "..":
                raise ValueError(f"Pfad „{path}“ verlässt das Projekt {self.project.name}")
            if seg != ".":
                parts.append(seg)
        p = "/".join(parts)
        if p == self.project.folder or p.startswith(self.prefix):
            return p
        if not p:
            if allow_root:
                return self.project.folder
            raise ValueError("Pfad fehlt")
        return self.prefix + p

    def folder(self, folder: str | None) -> str | None:
        """Ordner-Filter für Suche/Abfragen: im Projekt immer mindestens der Projektordner."""
        if not self.project:
            return folder
        return self.full(folder, allow_root=True)

    def inside(self, path: str | None) -> bool:
        if not self.project:
            return True
        return bool(path) and (path == self.project.folder or path.startswith(self.prefix))

    def check(self, *paths: str) -> None:
        for p in paths:
            if not self.inside(p):
                raise ValueError(f"„{p}“ liegt nicht im Projekt {self.project.name}")

    # ------------------------------------------------------------ heraus

    def rel(self, obj: Any, key: str | None = None) -> Any:
        """Pfade in Ergebnissen relativ machen (rekursiv, Freitext-Felder bleiben)."""
        if not self.project:
            return obj
        if isinstance(obj, dict):
            return {k: (v if k in TEXT_KEYS else self.rel(v, k)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.rel(v, key) for v in obj]
        if isinstance(obj, str):
            if obj.startswith(self.prefix):
                return obj[len(self.prefix):]
            if obj == self.project.folder:
                return ""
        return obj

    def only(self, rows: list, key: str = "path") -> list:
        """Listeneinträge außerhalb des Projekts weglassen."""
        if not self.project:
            return rows
        return [r for r in rows if not isinstance(r, dict) or key not in r or self.inside(r[key])]
