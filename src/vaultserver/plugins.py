"""Plug-ins: Erweiterungen, die sich in einen laufenden VaultServer einhängen.

Ein Plug-in ist ein installiertes Paket mit einem Entry Point der Gruppe `vaultserver.plugins`, der auf ein
Modul oder Objekt mit `register(ctx)` zeigt. Geladen wird nur, was in der Konfiguration ausdrücklich
freigeschaltet ist (`[plugins] enabled = [...]`); andere installierte Plug-ins werden nicht einmal importiert.
Scheitert ein freigeschaltetes Plug-in beim Laden, startet der Server nicht (PluginError).

`register(ctx)` bekommt einen `PluginContext`: Routen einhängen (mit der Anmeldung des Servers oder
ausdrücklich mit eigener), Start/Stopp-Funktionen, Schreib-Hooks, die interne Schnittstelle, einen Logger.
Details: docs/PLUGINS.md.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable

GROUP = "vaultserver.plugins"
AUTH_MODES = ("vaultserver", "own")

log = logging.getLogger("vaultserver.plugins")


class PluginError(RuntimeError):
    """Ein freigeschaltetes Plug-in fehlt oder lässt sich nicht laden – der Server startet nicht."""


@dataclass
class PluginContext:
    name: str
    settings: dict                       # [plugins.<name>] aus der Konfiguration
    app: Any                             # FastAPI-Anwendung
    service: Any                         # vaultserver.service.Service
    log: logging.Logger
    startup: list[Callable] = field(default_factory=list)
    shutdown: list[Callable] = field(default_factory=list)
    routes: list[tuple[str, str]] = field(default_factory=list)   # (Präfix, Anmeldung)

    @property
    def config(self):
        return self.service.config

    @property
    def store(self):
        return self.service.store

    def on_startup(self, fn: Callable) -> None:
        """Beim Start des Servers aufrufen (nach dem Hintergrund-Dienst); sync oder async. Fehler beenden den Start."""
        self.startup.append(fn)

    def on_shutdown(self, fn: Callable) -> None:
        """Beim Beenden aufrufen (vor dem Hintergrund-Dienst, umgekehrte Reihenfolge); Fehler werden protokolliert."""
        self.shutdown.append(fn)

    def add_hook(self, kind: str, fn: Callable) -> None:
        """kind: "before_write" (darf mit store.Rejected ablehnen) oder "after_write" (nach dem Commit)."""
        self.store.hooks.add(kind, fn, owner=self.name)

    def include_router(self, router, prefix: str, auth: str = "vaultserver") -> None:
        """Router unter `prefix` einhängen. auth="vaultserver": Anmeldung wie /api (Web-Sitzung oder Token);
        auth="own": ohne Anmeldung durch den Server – das Plug-in prüft selbst (ausdrücklich, je Präfix)."""
        prefix = "/" + (prefix or "").strip("/")
        if auth not in AUTH_MODES:
            raise ValueError(f"auth muss {' oder '.join(AUTH_MODES)} sein, nicht „{auth}“")
        if prefix == "/" or prefix in ("/api", "/static") or prefix.startswith(("/mcp", "/api/drop", "/static/")) \
                or prefix in ("/healthz", "/favicon.ico"):
            raise ValueError(f"Präfix {prefix} gehört dem Server (/, /mcp, /api, /static, /healthz) – eigenen Pfad wählen")
        self.app.include_router(router, prefix=prefix)
        self.routes.append((prefix, auth))
        if auth == "own":
            self.log.warning("Plug-in %s: %s ohne Anmeldung durch den Server (eigene Prüfung)", self.name, prefix)

    def vault(self, author: str, scope: str | None = None):
        """Interne Schnittstelle (lesen, suchen, schreiben) mit `author` als Commit-Autor, optional nur ein Ordner."""
        from .internal import InternalVault
        return InternalVault(self.service, author, scope=scope)


def load_plugins(app, service) -> list[PluginContext]:
    """Freigeschaltete Plug-ins laden und registrieren (in der Reihenfolge der Konfiguration)."""
    enabled = list(dict.fromkeys(service.config.plugins_enabled))
    if not enabled:
        return []
    available = {ep.name: ep for ep in entry_points(group=GROUP)}
    loaded: list[PluginContext] = []
    for name in enabled:
        ep = available.get(name)
        if ep is None:
            known = ", ".join(sorted(available)) or "keine"
            raise PluginError(f"Plug-in „{name}“ ist freigeschaltet, aber nicht installiert (installiert: {known})")
        try:
            obj = ep.load()
        except BaseException as e:  # noqa: BLE001 – auch Fehler beim Import klar melden
            raise PluginError(f"Plug-in „{name}“ lässt sich nicht importieren ({ep.value}): {e}") from e
        register = getattr(obj, "register", None)
        if not callable(register):
            raise PluginError(f"Plug-in „{name}“ ({ep.value}) hat keine Funktion register(ctx)")
        ctx = PluginContext(name=name, settings=dict(service.config.plugin_settings.get(name, {})), app=app,
                            service=service, log=logging.getLogger(f"vaultserver.plugin.{name}"))
        try:
            register(ctx)
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"Plug-in „{name}“: register fehlgeschlagen: {e}") from e
        log.info("Plug-in %s geladen (%s)", name, ep.value)
        loaded.append(ctx)
    return loaded


async def run_startup(plugins: list[PluginContext]) -> None:
    for p in plugins:
        for fn in p.startup:
            try:
                res = fn()
                if inspect.isawaitable(res):
                    await res
            except Exception as e:  # noqa: BLE001
                raise PluginError(f"Plug-in „{p.name}“: Start fehlgeschlagen: {e}") from e


async def run_shutdown(plugins: list[PluginContext]) -> None:
    for p in reversed(plugins):
        for fn in reversed(p.shutdown):
            try:
                res = fn()
                if inspect.isawaitable(res):
                    await res
            except Exception:  # noqa: BLE001 – beim Beenden weitermachen
                log.exception("Plug-in %s: Fehler beim Beenden", p.name)


def plugin_route(plugins: list[PluginContext], path: str) -> str | None:
    """Anmeldung ("vaultserver"/"own") für einen Pfad unter einem Plug-in-Präfix, sonst None (längstes Präfix gewinnt)."""
    best = None
    for p in plugins:
        for prefix, auth in p.routes:
            if (path == prefix or path.startswith(prefix + "/")) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, auth)
    return best[1] if best else None
