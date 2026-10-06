"""Konfiguration aus vaultserver.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Area:
    """Ein Bereich des Vaults mit eigenen Regeln (Ideen 1, 2, 5, 6)."""

    name: str
    folder: str                         # Ordner der Einzeldateien, z. B. ContentManagementTool/Fixliste
    overview: str = ""                  # Übersichtsdatei, z. B. ContentManagementTool/Fixliste.md
    rules_section: str = ""             # Abschnitt der Übersicht mit den Arbeitsregeln
    id_regex: str = ""                  # Nummer aus dem Dateinamen, z. B. ^FIX-(\d+)
    filename: str = ""                  # Muster für neue Dateien, z. B. FIX-{n:03d}.md
    heading: str = ""                   # erste Überschrift, z. B. # FIX-{n:03d} · {title}
    template: str = ""                  # Text unter der Überschrift; {fields} wird ersetzt
    overview_entry: str = ""            # Eintrag in der Übersicht
    overview_section: str = ""          # neue Einträge ans Ende dieses Abschnitts der Übersicht
    overview_before: str = ""           # … oder vor diese Überschrift; sonst ans Dateiende
    commit_regex: str = ""              # Idee 8: Nummer im Commit-Text, z. B. \bFIX-(\d{3})
    required: list[str] = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)        # Vorbelegung für create_from_template
    allowed: dict[str, list[str]] = field(default_factory=dict)  # Schlüssel (klein) -> kanonische Werte
    overview_columns: list[str] = field(default_factory=list)


@dataclass
class Config:
    vault_path: Path
    db_path: Path
    archive_folders: list[str] = field(default_factory=lambda: ["Archiv"])
    exclude: list[str] = field(default_factory=lambda: [".git", ".obsidian", ".trash"])
    # Eigenschaft (klein) -> {Rohwert (klein) -> kanonischer Wert}
    normalize: dict[str, dict[str, str]] = field(default_factory=dict)
    # Eigenschaft (klein) -> geordnete Regeln (Regex, Zielwert); erste passende gewinnt
    canonical: dict[str, list[tuple[re.Pattern, str]]] = field(default_factory=dict)
    # Eigenschaft (klein) -> Gruppe -> kanonische Werte; in Abfragen als "@gruppe"
    groups: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    areas: list[Area] = field(default_factory=list)
    status_note: str = ""               # Idee 5: Notiz mit automatischem Statusblock
    # Projekt-Kontexte: Name -> Ordner oder {folder, start}; leer = Ordner der obersten Ebene
    projects: dict = field(default_factory=dict)
    # Größe: weiche Grenze (Hinweis + Aufteilungsvorschlag), harte Grenze (neue Notizen abgelehnt),
    # Notizen je Ordner (Hinweis auf Unterordner); je Projekt in [projects] überschreibbar
    soft_kb: int = 20
    hard_kb: int = 50
    folder_notes: int = 25
    max_upload_mb: int = 25             # größte Datei beim Hochladen (Web und MCP)
    # Papierkorb im Vault-Stamm: Gelöschtes landet hier (wiederherstellbar), nicht im Suchindex
    recycle_folder: str = "RecycleBin"

    host: str = "0.0.0.0"
    port: int = 8100
    tokens: dict[str, str] = field(default_factory=dict)      # Token -> Rechnername
    users: dict[str, str] = field(default_factory=dict)       # Benutzer -> scrypt-Hash
    secret: str = ""                                          # Signatur der Sitzungs-Cookies

    git_commit: bool = True
    git_pull_seconds: int = 60          # 0 = nie
    git_push: bool = False
    git_remote: str = "origin"
    git_branch: str = ""                # leer = aktueller Branch
    watch_seconds: float = 3.0          # Datei-Wächter, 0 = aus
    claim_minutes: int = 120

    code_repos: list[dict] = field(default_factory=list)  # Idee 8: {path, name, url}
    commit_link_minutes: int = 0        # 0 = nur auf Aufruf

    semantic_enabled: bool = False
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "nomic-embed-text"

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        base = path.parent
        normalize = {
            k.lower(): {rk.lower(): rv for rk, rv in v.items()}
            for k, v in data.get("normalize", {}).items()
        }
        server = data.get("server", {})
        git = data.get("git", {})
        sem = data.get("semantic", {})
        areas = []
        for a in data.get("areas", []):
            a = dict(a)
            a["allowed"] = {k.lower(): [str(x).lower() for x in v] for k, v in a.get("allowed", {}).items()}
            areas.append(Area(**a))
        return cls(
            vault_path=(base / data["vault_path"]).expanduser().resolve(),
            db_path=(base / data.get("db_path", "data/index.sqlite")).expanduser().resolve(),
            archive_folders=data.get("archive_folders", ["Archiv"]),
            exclude=data.get("exclude", [".git", ".obsidian", ".trash"]),
            normalize=normalize,
            canonical={
                k.lower(): [(re.compile(r["match"], re.I), r["value"]) for r in rules]
                for k, rules in data.get("canonical", {}).items()
            },
            groups={k.lower(): {g.lower(): [x.lower() for x in vs] for g, vs in v.items()}
                    for k, v in data.get("groups", {}).items()},
            areas=areas,
            status_note=data.get("status_note", ""),
            projects=data.get("projects", {}),
            recycle_folder=data.get("recycle_folder", "RecycleBin"),
            soft_kb=data.get("limits", {}).get("soft_kb", 20),
            hard_kb=data.get("limits", {}).get("hard_kb", 50),
            folder_notes=data.get("limits", {}).get("folder_notes", 25),
            max_upload_mb=data.get("limits", {}).get("max_upload_mb", 25),
            host=server.get("host", "0.0.0.0"),
            port=server.get("port", 8100),
            tokens={v: k for k, v in server.get("tokens", {}).items()},
            users=server.get("users", {}),
            secret=server.get("secret", ""),
            git_commit=git.get("commit", True),
            git_pull_seconds=git.get("pull_seconds", 60),
            git_push=git.get("push", False),
            git_remote=git.get("remote", "origin"),
            git_branch=git.get("branch", ""),
            watch_seconds=server.get("watch_seconds", 3.0),
            claim_minutes=server.get("claim_minutes", 120),
            code_repos=[{**r, "path": str((base / r["path"]).expanduser().resolve())}
                        for r in data.get("code_repos", [])],
            commit_link_minutes=data.get("commit_link_minutes", 0),
            semantic_enabled=sem.get("enabled", False),
            ollama_url=sem.get("ollama_url", "http://localhost:11434"),
            ollama_model=sem.get("model", "nomic-embed-text"),
        )

    def __post_init__(self):
        if self.recycle_folder and self.recycle_folder not in self.exclude:
            self.exclude = [*self.exclude, self.recycle_folder]

    def normalize_value(self, key_norm: str, value: str) -> str:
        v = " ".join(value.strip().lower().split())
        exact = self.normalize.get(key_norm, {})
        if v in exact:
            return exact[v]
        for pattern, target in self.canonical.get(key_norm, []):
            m = pattern.search(v)
            if m:
                return m.expand(target)
        return v

    def expand_group(self, key_norm: str, value: str) -> list[str] | None:
        """"@erledigt" -> Liste der kanonischen Werte der Gruppe, sonst None."""
        if not value.startswith("@"):
            return None
        group = self.groups.get(key_norm, {}).get(value[1:].lower())
        if group is None:
            raise ValueError(f"Unbekannte Gruppe {value} für {key_norm}")
        return group

    def area_for(self, path: str) -> Area | None:
        for a in self.areas:
            if path.startswith(a.folder.rstrip("/") + "/") or path == a.overview:
                return a
        return None

    def area_by_name(self, name: str) -> Area:
        for a in self.areas:
            if a.name.lower() == name.lower():
                return a
        raise KeyError(f"Unbekannter Bereich: {name} (bekannt: {', '.join(a.name for a in self.areas)})")
