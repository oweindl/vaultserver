"""Konfiguration aus vaultserver.toml."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        base = path.parent
        normalize = {
            k.lower(): {rk.lower(): rv for rk, rv in v.items()}
            for k, v in data.get("normalize", {}).items()
        }
        return cls(
            vault_path=(base / data["vault_path"]).resolve(),
            db_path=(base / data.get("db_path", "data/index.sqlite")).resolve(),
            archive_folders=data.get("archive_folders", ["Archiv"]),
            exclude=data.get("exclude", [".git", ".obsidian", ".trash"]),
            normalize=normalize,
            canonical={
                k.lower(): [(re.compile(r["match"], re.I), r["value"]) for r in rules]
                for k, rules in data.get("canonical", {}).items()
            },
        )

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
