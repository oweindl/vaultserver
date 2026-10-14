"""Erweiterungspunkte vor und nach dem Schreiben (für Plug-ins).

Jede Schreiboperation des Stores erzeugt ein `WriteEvent`. `before_write`-Hooks laufen nach den eigenen
Prüfungen (Version, Claim, Bereichsregeln) und vor der ersten Änderung an Dateien; der erste Hook, der
`Rejected` wirft, verhindert die Änderung. `after_write`-Hooks laufen nach dem Commit, erst wenn der äußerste
Store-Aufruf fertig und die Sperre wieder frei ist; ihre Fehler werden nur protokolliert.

Ohne registrierte Hooks wird nichts davon berechnet oder aufgerufen.
"""
from __future__ import annotations

import contextvars
import functools
import logging
from dataclasses import dataclass, field, replace
from typing import Callable

log = logging.getLogger("vaultserver.hooks")

KINDS = ("before_write", "after_write")


@dataclass(frozen=True)
class WriteEvent:
    """Eine Schreiboperation. Pfade relativ zum Vault."""

    operation: str                      # write, patch_section, set_property, create_from_template, move, delete,
                                        # trash, upload, mkdir, restore, purge
    path: str                           # betroffene Notiz/Datei/Ordner (bei move: Quelle)
    agent: str                          # Autor des Commits, z. B. "ubuntu1/claude-code", "web/oliver"
    target: str | None = None           # nur move: Ziel
    paths: tuple[str, ...] = ()         # alle geänderten Dateien (im after-Ereignis vollständig, z. B. nachgezogene Links)
    section: str | None = None          # nur patch_section
    properties_before: dict[str, str] = field(default_factory=dict)   # Schlüssel (klein) -> Wert, nur Notizen
    properties_after: dict[str, str] = field(default_factory=dict)
    content_after: str | None = None    # neuer Text der Notiz (Text-Operationen), sonst None
    version_before: str | None = None
    version_after: str | None = None
    commit: str | None = None           # nur after_write (None, wenn Git-Commits ausgeschaltet sind)


Hook = Callable[[WriteEvent], None]

# Nach dem Commit aufzurufende Ereignisse des laufenden (äußersten) Store-Aufrufs
_pending: contextvars.ContextVar[list | None] = contextvars.ContextVar("vaultserver_after_write", default=None)


class Hooks:
    def __init__(self):
        self._hooks: dict[str, list[tuple[str, Hook]]] = {k: [] for k in KINDS}

    def __bool__(self) -> bool:
        return any(self._hooks.values())

    def add(self, kind: str, fn: Hook, owner: str = "") -> None:
        if kind not in KINDS:
            raise ValueError(f"Unbekannter Hook „{kind}“ (möglich: {', '.join(KINDS)})")
        if not callable(fn):
            raise ValueError(f"Hook für {kind} ist nicht aufrufbar")
        self._hooks[kind].append((owner, fn))

    def before(self, event: WriteEvent) -> None:
        """Alle before_write-Hooks der Reihe nach; der erste Rejected gewinnt. Andere Fehler blockieren ebenfalls."""
        from .store import Rejected
        for owner, fn in self._hooks["before_write"]:
            try:
                fn(event)
            except Rejected:
                raise
            except Exception as e:  # noqa: BLE001 – im Zweifel nicht schreiben
                log.exception("before_write-Hook von %s: %s", owner or "?", e)
                raise Rejected(f"Prüfung durch Plug-in „{owner or '?'}“ fehlgeschlagen: {e}") from e

    def after(self, event: WriteEvent, **changes) -> None:
        """after_write vormerken; ausgeführt am Ende des äußersten Store-Aufrufs (siehe `deferred`)."""
        if not self._hooks["after_write"]:
            return
        event = replace(event, **changes)
        queue = _pending.get()
        if queue is None:
            self._run_after(event)
        else:
            queue.append(event)

    def _run_after(self, event: WriteEvent) -> None:
        for owner, fn in self._hooks["after_write"]:
            try:
                fn(event)
            except Exception as e:  # noqa: BLE001 – Schreiben ist schon passiert
                log.exception("after_write-Hook von %s (%s %s): %s", owner or "?", event.operation, event.path, e)

    def flush(self, events: list[WriteEvent]) -> None:
        for ev in events:
            self._run_after(ev)


def deferred(method):
    """Store-Methode: after_write-Ereignisse erst nach dem äußersten Aufruf (Sperre frei) auslösen."""
    @functools.wraps(method)
    def run(self, *a, **kw):
        if _pending.get() is not None or not self.hooks:
            return method(self, *a, **kw)
        token = _pending.set([])
        try:
            return method(self, *a, **kw)
        finally:
            events = _pending.get()
            _pending.reset(token)
            self.hooks.flush(events)
    return run
