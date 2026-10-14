"""Interne Python-Schnittstelle zum Vault für Aufrufer im selben Prozess (z. B. einen Agenten in einem Plug-in).

Dünne Schicht über `Store` und `Index`: dieselbe Versionsprüfung (Conflict), dieselben Regeln und Claims
(Rejected) und dieselben Hooks wie Web und MCP, aber ohne HTTP. Der Autor ist frei wählbar und erscheint als
Commit-Autor (z. B. "agent:x"). Mit `scope` sieht die Schnittstelle nur einen Ordner – wie ein
projektgebundener Token: Pfade gehen relativ hinein und heraus, alles außerhalb wird abgelehnt (ValueError).
"""
from __future__ import annotations

from typing import Any

from . import sizes
from .scope import Project, Scope


class InternalVault:
    def __init__(self, service, author: str, scope: str | None = None):
        if not (author or "").strip():
            raise ValueError("Autor fehlt (erscheint als Commit-Autor, z. B. „agent:x“)")
        self.svc = service
        self.store = service.store
        self.author = author.strip()
        self.scope = Scope(self._project(scope)) if scope else Scope(None)

    def _project(self, scope: str) -> Project:
        """Projektname (wie in /mcp/<projekt>) oder Ordner im Vault."""
        projects = self.svc.projects()
        if scope in projects:
            return projects[scope]
        folder = scope.strip().strip("/")
        if not folder or not (self.svc.config.vault_path / folder).is_dir():
            raise ValueError(f"Scope „{scope}“ ist weder ein Projekt noch ein Ordner im Vault")
        return Project(name=folder, folder=folder, start="")

    def _full(self, path: str) -> str:
        return self.store._norm_path(self.scope.full(path))

    # ------------------------------------------------------------ lesen

    def read(self, path: str, section: str | None = None, lines: tuple[int, int] | None = None) -> dict[str, Any]:
        return self.scope.rel(self.store.read_tracked(self._full(path), section=section, lines=lines))

    def outline(self, path: str) -> list[dict[str, Any]]:
        with self.store.lock:
            return self.scope.rel(self.svc.index.outline(self._full(path)))

    def search(self, text: str, folder: str | None = None, tag: str | None = None,
               properties: dict[str, str] | None = None, include_archive: bool = False,
               limit: int = 20, mode: str = "text") -> list[dict[str, Any]]:
        return self.scope.rel(self.scope.only(self.svc.search(
            text, mode=mode, folder=self.scope.folder(folder), tag=tag, properties=properties,
            include_archive=include_archive, limit=limit)))

    def query(self, filters: list[str], folder: str | None = None, fields: list[str] | None = None,
              include_archive: bool = False) -> list[dict[str, Any]]:
        from .cli import parse_filter
        with self.store.lock:
            return self.scope.rel(self.scope.only(self.svc.index.query(
                [parse_filter(f) for f in filters], folder=self.scope.folder(folder), fields=fields,
                include_archive=include_archive)))

    # ------------------------------------------------------------ schreiben

    def write(self, path: str, content: str, base_version: str | None = None, message: str | None = None) -> dict[str, Any]:
        full = self._full(path)
        if not (self.svc.config.vault_path / full).exists():
            sizes.check_new_note(self.store, full, content)
        return self.scope.rel(self.store.write(full, content, self.author, base_version=base_version, message=message))

    def patch_section(self, path: str, section: str | None, content: str, base_version: str,
                      mode: str = "replace", message: str | None = None) -> dict[str, Any]:
        return self.scope.rel(self.store.patch_section(self._full(path), section, content, self.author,
                                                       base_version=base_version, mode=mode, message=message))

    def set_property(self, path: str, key: str, value: str, base_version: str | None = None,
                     message: str | None = None) -> dict[str, Any]:
        return self.scope.rel(self.store.set_property(self._full(path), key, value, self.author,
                                                      base_version=base_version, message=message))

    def create_from_template(self, area: str, title: str, fields: dict[str, Any], summary: str = "",
                             body: str = "", message: str | None = None) -> dict[str, Any]:
        if self.scope and not self.scope.inside(self.svc.config.area_by_name(area).folder):
            raise ValueError(f"Bereich „{area}“ liegt nicht im Scope {self.scope.project.folder}")
        return self.scope.rel(self.store.create_from_template(area, title, fields, self.author, summary=summary,
                                                              body=body, message=message))

    def claim(self, path: str, note: str = "", minutes: int | None = None) -> dict[str, Any]:
        return self.scope.rel(self.store.claim(self._full(path), self.author, note=note, minutes=minutes))

    def release(self, path: str) -> dict[str, Any]:
        return self.scope.rel(self.store.release(self._full(path), self.author))
