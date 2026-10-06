"""Live-Anzeige: Änderungen am Vault als fortlaufend nummerierte Ereignisse.

Quellen: Schreiben über Store (Web, MCP), Datei-Wächter (direkte Dateiänderungen), git pull. Die Web-Oberfläche
holt sie über Server-Sent Events (/api/events) und aktualisiert Baum, offene Notiz und Listen.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path


class Feed:
    def __init__(self, vault: Path, bin_folder: str, keep: int = 2000):
        self.vault = vault
        self.bin = bin_folder
        self.rev = 0
        self.events: deque[dict] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._folders: frozenset[str] | None = None   # erst beim ersten Abgleich (nach dem Start)

    def _folder_sig(self) -> frozenset[str]:
        out = set()
        for dirpath, dirs, _files in os.walk(self.vault):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            out.add(Path(dirpath).relative_to(self.vault).as_posix())
        return frozenset(out)

    def publish(self, paths: list[str], agent: str = "", tree: bool = False) -> None:
        paths = [p for p in dict.fromkeys(paths) if p]
        changed, removed = [], []
        for p in paths:
            if self.bin and (p == self.bin or p.startswith(self.bin + "/")):
                tree = True              # Papierkorb: nur Zähler im Baum
                continue
            name = p.rsplit("/", 1)[-1]
            if name.startswith(".") or p == ".":
                tree = True              # .gitkeep = Ordner angelegt/entfernt; "." = unbekannt (pull)
                continue
            (changed if (self.vault / p).exists() else removed).append(p)
        if not (changed or removed or tree):
            return
        with self._lock:
            self.rev += 1
            self.events.append({"rev": self.rev, "time": time.time(), "agent": agent, "paths": changed,
                                "removed": removed, "tree": tree or bool(removed)})

    def check_folders(self) -> None:
        """Neue/gelöschte (auch leere) Ordner, die keinen Datei-Eintrag erzeugen."""
        sig = self._folder_sig()
        if self._folders is None:
            self._folders = sig
        elif sig != self._folders:
            self._folders = sig
            self.publish([], "Datei-Wächter", tree=True)

    def since(self, rev: int) -> tuple[list[dict], bool]:
        """Ereignisse nach rev; zweiter Wert True, wenn rev zu alt ist (Client lädt alles neu)."""
        with self._lock:
            if not self.events or rev >= self.rev:
                return [], False
            oldest = self.events[0]["rev"]
            return [e for e in self.events if e["rev"] > rev], rev < oldest - 1
