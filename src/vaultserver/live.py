"""Live-Anzeige: Änderungen am Vault als fortlaufend nummerierte Ereignisse.

Quellen: Schreiben über Store (Web, MCP), Datei-Wächter (direkte Dateiänderungen), git pull. Die Web-Oberfläche
holt sie über Server-Sent Events (/api/events) und aktualisiert Baum, offene Notiz und Listen.

Lesezugriffe der Agenten (touch) laufen getrennt davon in einem kleinen Puffer: nur für die Aktivitätsanzeige im Baum,
ohne Nummer im Feed (kein Nachholen nach einer Unterbrechung) und ohne Baum-Neuladen.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

READ_KEEP = 200          # Lese-Ereignisse im Puffer
READ_SECONDS = 10.0      # ... und höchstens so alt
READ_DEBOUNCE = 1.0      # dieselbe Datei vom selben Agenten höchstens einmal je Sekunde


class Feed:
    def __init__(self, vault: Path, bin_folder: str, keep: int = 2000):
        self.vault = vault
        self.bin = bin_folder
        self.rev = 0
        self.events: deque[dict] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._folders: frozenset[str] | None = None   # erst beim ersten Abgleich (nach dem Start)
        self.reads: deque[dict] = deque(maxlen=READ_KEEP)
        self.read_seq = 0
        self._last_read: dict[tuple[str, str], float] = {}
        self._waiters: set = set()                    # (loop, asyncio.Event) der offenen SSE-Verbindungen

    # ---------------------------------------------------------------- wecken
    def subscribe(self, loop, event) -> None:
        with self._lock:
            self._waiters.add((loop, event))

    def unsubscribe(self, loop, event) -> None:
        with self._lock:
            self._waiters.discard((loop, event))

    def _wake(self) -> None:
        """Wartende SSE-Verbindungen sofort wecken (Aufrufer kann ein beliebiger Thread sein)."""
        with self._lock:
            waiters = list(self._waiters)
        for loop, ev in waiters:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:          # Schleife schon geschlossen
                self.unsubscribe(loop, ev)

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
            if self.bin and self.bin in p.split("/")[:2]:    # Papierkorb im Stamm oder in einem Repo
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
            self.events.append({"rev": self.rev, "time": time.time(), "kind": "write", "agent": agent, "paths": changed,
                                "removed": removed, "tree": tree or bool(removed)})
        self._wake()

    def touch(self, paths: list[str], agent: str = "") -> None:
        """Lesezugriff (Datei oder Ordner, vault-absolut) für die Aktivitätsanzeige."""
        now = time.time()
        with self._lock:
            fresh = []
            for p in dict.fromkeys(paths):
                p = (p or "").strip("/")
                if not p or (self.bin and self.bin in p.split("/")[:2]):
                    continue
                if now - self._last_read.get((p, agent), 0.0) < READ_DEBOUNCE:
                    continue
                self._last_read[(p, agent)] = now
                fresh.append(p)
            if not fresh:
                return
            if len(self._last_read) > 4 * READ_KEEP:
                self._last_read = {k: t for k, t in self._last_read.items() if now - t < READ_DEBOUNCE}
            self.read_seq += 1
            self.reads.append({"seq": self.read_seq, "time": now, "kind": "read", "agent": agent, "paths": fresh})
        self._wake()

    def reads_since(self, seq: int) -> list[dict]:
        """Lese-Ereignisse nach seq, die höchstens READ_SECONDS alt sind."""
        limit = time.time() - READ_SECONDS
        with self._lock:
            return [e for e in self.reads if e["seq"] > seq and e["time"] >= limit]

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
