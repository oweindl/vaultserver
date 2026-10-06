"""Gemeinsame Kernlogik für MCP und Web: guide, recent, Hintergrundabgleich."""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from pathlib import PurePosixPath

from .config import Config
from .index import Index
from .semantic import Semantic
from .store import Store, find_section
from .clients import Clients
from .scope import Project, load_projects

log = logging.getLogger("vaultserver")

# Wer gerade aufruft (Rechner aus dem Token, Werkzeug aus dem Aufruf) – für Commits und Claims
current_agent: contextvars.ContextVar[str] = contextvars.ContextVar("current_agent", default="unbekannt")

GENERAL_RULES = """\
# VaultServer – Arbeitsregeln für Agenten

1. **Erst finden, dann gezielt lesen.** `search` (Volltext) oder `query` (Eigenschaften) liefern Pfad und
   Abschnitt. Dann `outline` und `read` mit `section` – keine großen Dateien komplett lesen.
2. **Version mitgeben.** Jeder Lesezugriff liefert `version`. Schreibende Aufrufe (`write`, `patch_section`,
   `delete`) brauchen sie als `base_version`. Bei Konflikt kommt der aktuelle Stand zurück: neu lesen,
   Änderung neu anwenden, nicht blind überschreiben.
3. **Kleinste passende Änderung.** Statuswechsel mit `set_property`, Abschnitte mit `patch_section`;
   `write` nur für neue Notizen oder echte Neufassungen.
4. **Eigenschaften konsequent setzen.** In Bereichen mit Regeln (siehe unten) sind Pflichtfelder und erlaubte
   Werte verbindlich; der Server lehnt Änderungen ab, die neue Verstöße erzeugen. Der Wert einer Eigenschaft
   ist nur der kanonische Wert (z. B. `Status: erledigt (Test)`); Details wie Commits, Branches und Datum
   gehören in `Umsetzung`.
5. **Neue Einträge** (FIX, Feature) nur mit `create_from_template`: Nummer, Datei und Eintrag in der
   Übersicht entstehen automatisch.
6. **Reservieren.** Vor längerer Arbeit an einem Eintrag `claim` aufrufen (setzt „in Arbeit“), am Ende
   `release`. Reservierungen anderer Rechner respektieren.
7. **Nachtläufe** beginnen mit `changes_since` (letzter bekannter Commit oder Zeitpunkt) statt alles zu lesen.
8. **Archiv** ist in Suche und Abfragen ausgeblendet; nur bei Bedarf `include_archive=true`.
   **Löschen** (`delete`) schiebt Notizen, Anhänge und Ordner in den Papierkorb `RecycleBin`; `recycle_bin` zeigt ihn,
   `restore` holt einen Eintrag an die alte Stelle zurück. Neue leere Ordner mit `create_folder`.
9. Jede Änderung wird ein Git-Commit mit deinem Rechnernamen; `message` kurz und mit Nummer (z. B.
   `FIX-054 Status erledigt (Test)`).
"""


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.index = Index(config)
        self.store = Store(config, self.index)
        self.clients = Clients(config)
        self.lock = self.store.lock
        self.semantic = (Semantic(self.index, config.ollama_url, config.ollama_model)
                         if config.semantic_enabled else None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str = ""
        with self.lock:
            self.index.sync()
        try:
            self.store.ensure_bin()
        except Exception as e:  # noqa: BLE001 – Start nicht blockieren
            log.warning("Papierkorb anlegen: %s", e)

    # ------------------------------------------------------------ Projekte

    def projects(self) -> dict[str, Project]:
        """Projekt-Kontexte (bei automatischer Erkennung jedes Mal frisch, neue Ordner gelten sofort)."""
        return load_projects(self.config)

    # ------------------------------------------------------------ guide (Idee 1)

    def guide(self, area: str | None = None) -> dict:
        with self.lock:
            areas = []
            for a in self.config.areas:
                entry = {"name": a.name, "folder": a.folder, "overview": a.overview,
                         "required": a.required, "allowed": a.allowed}
                if a.filename:
                    entry["create_with"] = f'create_from_template(area="{a.name}", title=..., fields={{...}})'
                areas.append(entry)
            out: dict = {"rules": GENERAL_RULES, "areas": areas,
                         "groups": self.config.groups,
                         "archive_folders": self.config.archive_folders}
            if area:
                a = self.config.area_by_name(area)
                detail = {"name": a.name, "folder": a.folder, "overview": a.overview, "required": a.required,
                          "allowed": a.allowed, "filename": a.filename, "heading": a.heading}
                if a.overview and a.rules_section:
                    try:
                        text = (self.config.vault_path / a.overview).read_text(encoding="utf-8")
                        sp = find_section(text, a.rules_section)
                        detail["area_rules"] = "\n".join(text.splitlines()[sp.start:sp.end]).strip()
                    except (KeyError, FileNotFoundError):
                        detail["area_rules"] = ""
                out["area"] = detail
            return out

    # ------------------------------------------------------------ recent

    def recent(self, limit: int = 20, include_archive: bool = False) -> list[dict]:
        with self.lock:
            if self.store.git.enabled:
                out, seen = [], set()
                for c in self.store.git.log(limit * 5):
                    for f in c["files"]:
                        if f in seen or not f.endswith(".md"):
                            continue
                        if not include_archive and any(f.startswith(a + "/") or f"/{a}/" in f
                                                       for a in self.config.archive_folders):
                            continue
                        seen.add(f)
                        out.append({"path": f, "date": c["date"], "author": c["author"],
                                    "message": c["message"], "commit": c["commit"][:10],
                                    "exists": (self.config.vault_path / f).exists()})
                        if len(out) >= limit:
                            return out
                return out
            return self.index.recent(limit)

    def history(self, path: str, limit: int = 20) -> list[dict]:
        with self.lock:
            if not self.store.git.enabled:
                return []
            return [{k: c[k] for k in ("commit", "author", "date", "message")}
                    for c in self.store.git.log(limit, path=path)]

    # ------------------------------------------------------------ Suche mit optionaler Semantik

    def search(self, text: str, mode: str = "text", **kw) -> list[dict]:
        with self.lock:
            if mode == "semantic":
                if not self.semantic:
                    raise ValueError("Semantische Suche ist nicht eingeschaltet ([semantic] enabled = true)")
                return self.semantic.search(text, limit=kw.get("limit", 10),
                                            include_archive=kw.get("include_archive", False))
            hits = self.index.search(text, **kw)
            if mode == "auto" and self.semantic and len(hits) < 3:
                try:
                    seen = {(h["path"], h["heading_path"]) for h in hits}
                    for h in self.semantic.search(text, limit=kw.get("limit", 10),
                                                  include_archive=kw.get("include_archive", False)):
                        if (h["path"], h["heading_path"]) not in seen:
                            hits.append({**h, "semantic": True})
                except Exception as e:  # Ollama nicht erreichbar: Volltext reicht
                    log.warning("semantische Suche fehlgeschlagen: %s", e)
            return hits

    # ------------------------------------------------------------ Hintergrund

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="vaultserver-bg", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        c = self.config
        last = {"pull": 0.0, "push": 0.0, "links": 0.0, "sem": 0.0}
        while not self._stop.wait(max(1.0, c.watch_seconds or 5.0)):
            now = time.time()
            try:
                if c.watch_seconds:
                    with self.lock:
                        st = self.index.sync()
                    if st.added or st.updated or st.removed:
                        log.info("Datei-Wächter: +%d ~%d -%d", st.added, st.updated, st.removed)
                if c.git_pull_seconds and now - last["pull"] >= c.git_pull_seconds:
                    last["pull"] = now
                    r = self.store.pull()
                    if r.get("pulled"):
                        log.info("git pull: %s", r)
                        self.store.ensure_bin()  # falls jemand den Papierkorb woanders gelöscht hat
                if c.git_push and self.store.dirty_push and now - last["push"] >= 10:
                    last["push"] = now
                    self.store.push()
                if c.commit_link_minutes and now - last["links"] >= c.commit_link_minutes * 60:
                    last["links"] = now
                    r = self.store.link_commits("vaultserver")
                    if r["updated"]:
                        log.info("Commits verknüpft: %s", r["updated"])
                if self.semantic and now - last["sem"] >= 300:
                    last["sem"] = now
                    n = self.semantic.update(lock=self.lock)
                    if n:
                        log.info("Embeddings: %d neu", n)
                self.last_error = ""
            except Exception as e:  # nie den Hintergrund-Thread verlieren
                self.last_error = f"{time.strftime('%H:%M:%S')} {e}"
                log.exception("Hintergrundfehler")
