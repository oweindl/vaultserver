"""SQLite-FTS5-Index über dem Vault.

Der Index ist vollständig aus den Dateien ableitbar. `sync()` gleicht über
Prüfsummen ab und indexiert nur geänderte Notizen neu; `rebuild()` baut alles neu.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .config import Config
from .parser import parse_note

# Erhöhen, wenn sich Parser oder Schema ändern: der Index baut sich dann beim Start neu auf
INDEX_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    folder TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    heading TEXT NOT NULL,
    level INTEGER NOT NULL,
    heading_path TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    size INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sections_note ON sections(note_id, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
    title, heading_path, text,
    tokenize = "unicode61 remove_diacritics 2"
);
CREATE TABLE IF NOT EXISTS links (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    section_ord INTEGER NOT NULL,
    line INTEGER NOT NULL,
    target TEXT NOT NULL,
    target_section TEXT NOT NULL,
    alias TEXT NOT NULL,
    is_embed INTEGER NOT NULL,
    kind TEXT NOT NULL,
    resolved_path TEXT
);
CREATE INDEX IF NOT EXISTS links_note ON links(note_id);
CREATE INDEX IF NOT EXISTS links_resolved ON links(resolved_path);
CREATE TABLE IF NOT EXISTS properties (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    key_norm TEXT NOT NULL,
    value TEXT NOT NULL,
    value_norm TEXT NOT NULL,
    source TEXT NOT NULL,
    section_ord INTEGER,
    line INTEGER
);
CREATE INDEX IF NOT EXISTS properties_kv ON properties(key_norm, value_norm);
CREATE INDEX IF NOT EXISTS properties_note ON properties(note_id);
CREATE TABLE IF NOT EXISTS tags (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS tasks (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    section_ord INTEGER NOT NULL,
    line INTEGER NOT NULL,
    text TEXT NOT NULL,
    done INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_note ON tasks(note_id);
CREATE TABLE IF NOT EXISTS aliases (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    alias_norm TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS aliases_norm ON aliases(alias_norm);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Zustand, der nicht aus den Dateien ableitbar ist (überlebt rebuild)
CREATE TABLE IF NOT EXISTS claims (
    path TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    note TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS embeddings (
    text_hash TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    vector BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    path TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL
);
"""


@dataclass
class SyncStats:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    attachments: int = 0
    seconds: float = 0.0
    changed_paths: list = field(default_factory=list)   # neu oder geändert (Notizen und Anhänge)
    removed_paths: list = field(default_factory=list)   # nicht mehr vorhanden


def norm_key(key: str) -> str:
    return " ".join(key.strip().lower().split())


class Index:
    def __init__(self, config: Config):
        self.config = config
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'index_version'").fetchone()
        if not row or row[0] != INDEX_VERSION:
            with self.db:
                for table in ("notes", "sections_fts", "attachments"):
                    self.db.execute(f"DELETE FROM {table}")
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('index_version', ?)", (INDEX_VERSION,))

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- Abgleich

    def _walk(self):
        root = self.config.vault_path
        excluded = set(self.config.exclude)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in excluded and not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                full = Path(dirpath) / name
                yield full, full.relative_to(root).as_posix()

    def rebuild(self) -> SyncStats:
        with self.db:
            for table in ("notes", "sections_fts", "attachments"):
                self.db.execute(f"DELETE FROM {table}")
        return self.sync()

    def sync(self) -> SyncStats:
        t0 = time.perf_counter()
        stats = SyncStats()
        known = {r["path"]: (r["version"], r["mtime"], r["size"])
                 for r in self.db.execute("SELECT path, version, mtime, size FROM notes")}
        seen_notes: set[str] = set()
        seen_att: set[str] = set()
        known_att = {r["path"]: (r["size"], r["mtime"]) for r in self.db.execute("SELECT path, size, mtime FROM attachments")}
        with self.db:
            for full, rel in self._walk():
                if rel.lower().endswith(".md"):
                    seen_notes.add(rel)
                    st = full.stat()
                    old = known.get(rel)
                    if old and old[1] == st.st_mtime and old[2] == st.st_size:
                        stats.unchanged += 1
                        continue
                    data = full.read_bytes()
                    version = hashlib.sha256(data).hexdigest()[:16]
                    if old and old[0] == version:
                        self.db.execute("UPDATE notes SET mtime = ? WHERE path = ?", (st.st_mtime, rel))
                        stats.unchanged += 1
                        continue
                    if rel in known:
                        stats.updated += 1
                    else:
                        stats.added += 1
                    stats.changed_paths.append(rel)
                    self._index_note(rel, data, version, st.st_mtime)
                else:
                    seen_att.add(rel)
                    st = full.stat()
                    if known_att.get(rel) != (st.st_size, st.st_mtime):
                        stats.changed_paths.append(rel)
                    mime = mimetypes.guess_type(rel)[0] or "application/octet-stream"
                    self.db.execute(
                        "INSERT OR REPLACE INTO attachments(path, type, size, mtime) VALUES (?,?,?,?)",
                        (rel, mime, st.st_size, st.st_mtime),
                    )
            for rel in set(known) - seen_notes:
                self._remove_note(rel)
                stats.removed += 1
                stats.removed_paths.append(rel)
            for (rel,) in self.db.execute("SELECT path FROM attachments").fetchall():
                if rel not in seen_att:
                    self.db.execute("DELETE FROM attachments WHERE path = ?", (rel,))
                    stats.removed_paths.append(rel)
            stats.attachments = len(seen_att)
            if stats.added or stats.updated or stats.removed:
                self._resolve_links()
        stats.seconds = time.perf_counter() - t0
        return stats

    def _remove_note(self, rel: str) -> None:
        row = self.db.execute("SELECT id FROM notes WHERE path = ?", (rel,)).fetchone()
        if not row:
            return
        self.db.execute(
            "DELETE FROM sections_fts WHERE rowid IN (SELECT id FROM sections WHERE note_id = ?)",
            (row["id"],),
        )
        self.db.execute("DELETE FROM notes WHERE id = ?", (row["id"],))

    def _index_note(self, rel: str, data: bytes, version: str, mtime: float) -> None:
        self._remove_note(rel)
        text = data.decode("utf-8", errors="replace")
        p = PurePosixPath(rel)
        parsed = parse_note(text, p.stem)
        folder = "" if str(p.parent) == "." else str(p.parent)
        cur = self.db.execute(
            "INSERT INTO notes(path, title, folder, size, mtime, version) VALUES (?,?,?,?,?,?)",
            (rel, parsed.title, folder, len(data), mtime, version),
        )
        nid = cur.lastrowid
        for s in parsed.sections:
            sc = self.db.execute(
                "INSERT INTO sections(note_id, ord, heading, level, heading_path, line_start, line_end, size, text)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (nid, s.ord, s.heading, s.level, s.heading_path, s.line_start, s.line_end,
                 len(s.text.encode()), s.text),
            )
            self.db.execute(
                "INSERT INTO sections_fts(rowid, title, heading_path, text) VALUES (?,?,?,?)",
                (sc.lastrowid, parsed.title, s.heading_path, s.text),
            )
        self.db.executemany(
            "INSERT INTO links VALUES (?,?,?,?,?,?,?,?,NULL)",
            [(nid, l.section_ord, l.line, l.target, l.target_section, l.alias, int(l.is_embed), l.kind)
             for l in parsed.links],
        )
        rows = []
        for pr in parsed.properties:
            kn = norm_key(pr.key)
            rows.append((nid, pr.key, kn, pr.value, self.config.normalize_value(kn, pr.value),
                         pr.source, pr.section_ord, pr.line))
        self.db.executemany("INSERT INTO properties VALUES (?,?,?,?,?,?,?,?)", rows)
        self.db.executemany("INSERT INTO tags VALUES (?,?)", [(nid, t.lower()) for t in parsed.tags])
        self.db.executemany(
            "INSERT INTO tasks VALUES (?,?,?,?,?)",
            [(nid, t.section_ord, t.line, t.text, int(t.done)) for t in parsed.tasks],
        )
        self.db.executemany(
            "INSERT INTO aliases VALUES (?,?,?)", [(nid, a, a.lower()) for a in parsed.aliases]
        )

    # ------------------------------------------------------- Link-Auflösung

    def _resolve_links(self) -> None:
        """Wikilinks wie Obsidian auflösen: Pfad, Pfad-Ende, Dateiname, Alias."""
        notes = [r["path"] for r in self.db.execute("SELECT path FROM notes")]
        files = notes + [r["path"] for r in self.db.execute("SELECT path FROM attachments")]
        by_lower = {f.lower(): f for f in files}
        by_name: dict[str, list[str]] = {}
        for f in files:
            name = PurePosixPath(f).name.lower()
            by_name.setdefault(name, []).append(f)
            if name.endswith(".md"):
                by_name.setdefault(name[:-3], []).append(f)
        alias_map = {
            r["alias_norm"]: r["path"]
            for r in self.db.execute("SELECT a.alias_norm, n.path FROM aliases a JOIN notes n ON n.id = a.note_id")
        }

        updates = []
        for r in self.db.execute(
            "SELECT l.rowid AS rid, l.target, l.kind, n.path AS src FROM links l JOIN notes n ON n.id = l.note_id"
        ):
            updates.append((self._resolve(r["target"], r["kind"], r["src"], by_lower, by_name, alias_map), r["rid"]))
        self.db.executemany("UPDATE links SET resolved_path = ? WHERE rowid = ?", updates)

    @staticmethod
    def _resolve(target, kind, src, by_lower, by_name, alias_map):
        if not target:
            return src  # [[#Abschnitt]] verweist auf die eigene Notiz
        t = target.strip().lstrip("/")
        if kind == "md":
            base = PurePosixPath(src).parent
            joined = os.path.normpath(str(base / t)).replace("\\", "/")
            for cand in (joined, t):
                if cand.lower() in by_lower:
                    return by_lower[cand.lower()]
        low = t.lower()
        for cand in (low, low + ".md"):
            if cand in by_lower:
                return by_lower[cand]
        # Pfad-Ende: [[Fixliste/FIX-054]] passt auf "Projekt/Fixliste/FIX-054.md"
        if "/" in low:
            for cand in (low, low + ".md"):
                hits = [f for lf, f in by_lower.items() if lf.endswith("/" + cand)]
                if hits:
                    return min(hits, key=len)
        hits = by_name.get(PurePosixPath(low).name)
        if hits:
            return min(hits, key=len)
        return alias_map.get(low)

    # ----------------------------------------------------------- Abfragen

    def _archive_clause(self, include_archive: bool, alias: str = "n") -> tuple[str, list]:
        if include_archive or not self.config.archive_folders:
            return "", []
        parts, args = [], []
        for f in self.config.archive_folders:
            parts.append(f"({alias}.path NOT LIKE ? AND {alias}.path NOT LIKE ?)")
            args += [f"{f}/%", f"%/{f}/%"]
        return " AND " + " AND ".join(parts), args

    @staticmethod
    def fts_query(text: str) -> str:
        """Freitext in eine sichere FTS5-Abfrage übersetzen (UND, letztes Wort als Präfix)."""
        tokens = re.findall(r"[\wÄÖÜäöüß]+", text)
        if not tokens:
            return ""
        parts = ['"' + t.replace('"', "") + '"' for t in tokens]
        parts[-1] += "*"
        return " ".join(parts)

    def search(self, text: str, folder: str | None = None, tag: str | None = None,
               properties: dict[str, str] | None = None, include_archive: bool = False,
               limit: int = 20, raw: bool = False) -> list[dict]:
        q = text if raw else self.fts_query(text)
        if not q:
            return []
        where, args = ["sections_fts MATCH ?"], [q]
        if folder:
            where.append("(n.folder = ? OR n.folder LIKE ?)")
            args += [folder.strip("/"), folder.strip("/") + "/%"]
        if tag:
            where.append("n.id IN (SELECT note_id FROM tags WHERE tag = ?)")
            args.append(tag.lstrip("#").lower())
        for k, v in (properties or {}).items():
            kn = norm_key(k)
            where.append("n.id IN (SELECT note_id FROM properties WHERE key_norm = ? AND value_norm = ?)")
            args += [kn, self.config.normalize_value(kn, v)]
        arch, arch_args = self._archive_clause(include_archive)
        sql = (
            "SELECT n.path, n.title, s.heading_path, s.line_start, s.line_end, s.size,"
            " snippet(sections_fts, 2, '«', '»', '…', 16) AS snippet,"
            " bm25(sections_fts, 8.0, 4.0, 1.0) AS rank"
            " FROM sections_fts JOIN sections s ON s.id = sections_fts.rowid"
            " JOIN notes n ON n.id = s.note_id"
            f" WHERE {' AND '.join(where)}{arch} ORDER BY rank LIMIT ?"
        )
        return [dict(r) for r in self.db.execute(sql, args + arch_args + [limit])]

    def query(self, filters: list[tuple[str, str, str]], folder: str | None = None,
              fields: list[str] | None = None, include_archive: bool = False,
              limit: int = 200) -> list[dict]:
        """Notizen über Eigenschaften filtern.

        filters: (Schlüssel, Operator, Wert); Operatoren: = != ~ (enthält) !~ exists missing.
        Wert "@gruppe" steht für alle Werte einer Gruppe aus der Konfiguration (nur = und !=).
        """
        where, args = ["1=1"], []
        for key, op, value in filters:
            kn = norm_key(key)
            sub = "SELECT note_id FROM properties WHERE key_norm = ?"
            group = self.config.expand_group(kn, value) if value else None
            if group is not None and op in ("=", "!="):
                marks = ",".join("?" * len(group))
                neg = "NOT " if op == "!=" else ""
                where.append(f"n.id {neg}IN ({sub} AND value_norm IN ({marks}))"); args += [kn, *group]
                continue
            vn = self.config.normalize_value(kn, value) if value else ""
            if op == "=":
                where.append(f"n.id IN ({sub} AND value_norm = ?)"); args += [kn, vn]
            elif op == "!=":
                where.append(f"n.id NOT IN ({sub} AND value_norm = ?)"); args += [kn, vn]
            elif op == "~":
                where.append(f"n.id IN ({sub} AND value_norm LIKE ?)"); args += [kn, f"%{vn}%"]
            elif op == "!~":
                where.append(f"n.id NOT IN ({sub} AND value_norm LIKE ?)"); args += [kn, f"%{vn}%"]
            elif op == "exists":
                where.append(f"n.id IN ({sub})"); args.append(kn)
            elif op == "missing":
                where.append(f"n.id NOT IN ({sub})"); args.append(kn)
            else:
                raise ValueError(f"Unbekannter Operator: {op}")
        if folder:
            where.append("(n.folder = ? OR n.folder LIKE ?)")
            args += [folder.strip("/"), folder.strip("/") + "/%"]
        arch, arch_args = self._archive_clause(include_archive)
        rows = self.db.execute(
            f"SELECT n.id, n.path, n.title FROM notes n WHERE {' AND '.join(where)}{arch}"
            " ORDER BY n.path LIMIT ?",
            args + arch_args + [limit],
        ).fetchall()
        want = [norm_key(f) for f in (fields or [k for k, _, _ in filters])]
        out = []
        for r in rows:
            props: dict[str, str] = {}
            if want:
                marks = ",".join("?" * len(want))
                for p in self.db.execute(
                    f"SELECT key_norm, value_norm FROM properties WHERE note_id = ? AND key_norm IN ({marks})",
                    [r["id"], *want],
                ):
                    props.setdefault(p["key_norm"], p["value_norm"])
            out.append({"path": r["path"], "title": r["title"], "properties": props})
        return out

    def _note(self, path: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM notes WHERE path = ?", (path,)).fetchone()
        if not row:
            raise KeyError(f"Notiz nicht gefunden: {path}")
        return row

    def version(self, path: str) -> str | None:
        row = self.db.execute("SELECT version FROM notes WHERE path = ?", (path,)).fetchone()
        return row["version"] if row else None

    def index_file(self, rel: str) -> None:
        """Eine Datei nach einer Änderung durch den Server sofort nachziehen."""
        full = self.config.vault_path / rel
        parts = rel.split("/")
        hidden = any(x.startswith(".") for x in parts) or bool(set(parts[:-1]) & set(self.config.exclude))
        with self.db:
            if hidden or not full.exists():  # versteckte/ausgeschlossene Dateien nie im Index
                self._remove_note(rel)
                self.db.execute("DELETE FROM attachments WHERE path = ?", (rel,))
            elif rel.lower().endswith(".md"):
                data = full.read_bytes()
                self._index_note(rel, data, hashlib.sha256(data).hexdigest()[:16], full.stat().st_mtime)
            else:
                st = full.stat()
                mime = mimetypes.guess_type(rel)[0] or "application/octet-stream"
                self.db.execute("INSERT OR REPLACE INTO attachments(path, type, size, mtime) VALUES (?,?,?,?)",
                                (rel, mime, st.st_size, st.st_mtime))
            self._resolve_links()

    def outline(self, path: str) -> list[dict]:
        n = self._note(path)
        return [dict(r) for r in self.db.execute(
            "SELECT ord, heading, level, heading_path, line_start, line_end, size"
            " FROM sections WHERE note_id = ? ORDER BY ord", (n["id"],))]

    def read(self, path: str, section: str | None = None,
             lines: tuple[int, int] | None = None) -> dict:
        """Ganze Notiz, einen Abschnitt (Überschrift oder Pfad-Ende) oder einen Zeilenbereich."""
        n = self._note(path)
        full = (self.config.vault_path / path).read_text(encoding="utf-8")
        all_lines = full.splitlines()
        if section:
            want = section.strip().lower()
            secs = self.db.execute(
                "SELECT heading, heading_path, level, ord, line_start FROM sections"
                " WHERE note_id = ? ORDER BY ord", (n["id"],)).fetchall()
            match = next((s for s in secs if s["heading_path"].lower() == want), None) or next(
                (s for s in secs if s["heading_path"].lower().endswith(want)
                 or s["heading"].lower() == want), None)
            if not match:
                raise KeyError(f"Abschnitt nicht gefunden: {section}")
            # Abschnitt inkl. aller Unterabschnitte bis zur nächsten gleich hohen Überschrift
            end = len(all_lines)
            for s in secs:
                if s["ord"] > match["ord"] and 0 < s["level"] <= match["level"]:
                    end = s["line_start"] - 1
                    break
            start = match["line_start"]
            body = "\n".join(all_lines[start - 1:end])
            return {"path": path, "version": n["version"], "section": match["heading_path"],
                    "line_start": start, "line_end": end, "text": body}
        if lines:
            a, b = max(1, lines[0]), min(len(all_lines), lines[1])
            return {"path": path, "version": n["version"], "line_start": a, "line_end": b,
                    "text": "\n".join(all_lines[a - 1:b])}
        return {"path": path, "version": n["version"], "text": full}

    def backlinks(self, path: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT n.path, s.heading_path, l.line, l.alias FROM links l"
            " JOIN notes n ON n.id = l.note_id"
            " LEFT JOIN sections s ON s.note_id = l.note_id AND s.ord = l.section_ord"
            " WHERE l.resolved_path = ? AND n.path != ? ORDER BY n.path, l.line", (path, path))]

    def broken_links(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT n.path, l.line, l.target FROM links l JOIN notes n ON n.id = l.note_id"
            " WHERE l.resolved_path IS NULL ORDER BY n.path, l.line")]

    def tasks(self, folder: str | None = None, done: bool | None = False,
              include_archive: bool = False, limit: int = 500) -> list[dict]:
        where, args = ["1=1"], []
        if done is not None:
            where.append("t.done = ?"); args.append(int(done))
        if folder:
            where.append("(n.folder = ? OR n.folder LIKE ?)")
            args += [folder.strip("/"), folder.strip("/") + "/%"]
        arch, arch_args = self._archive_clause(include_archive)
        return [dict(r) for r in self.db.execute(
            "SELECT n.path, s.heading_path, t.line, t.text, t.done FROM tasks t"
            " JOIN notes n ON n.id = t.note_id"
            " LEFT JOIN sections s ON s.note_id = t.note_id AND s.ord = t.section_ord"
            f" WHERE {' AND '.join(where)}{arch} ORDER BY n.path, t.line LIMIT ?",
            args + arch_args + [limit])]

    def list(self, folder: str = "", depth: int = 1) -> dict:
        folder = folder.strip("/")
        prefix = folder + "/" if folder else ""
        notes = self.db.execute(
            "SELECT path, title, size, mtime FROM notes WHERE path LIKE ? ORDER BY path",
            (prefix + "%",)).fetchall()
        atts = self.db.execute(
            "SELECT path, type, size, mtime FROM attachments WHERE path LIKE ? ORDER BY path",
            (prefix + "%",)).fetchall()
        folders: set[str] = set()
        entries = []
        for r, kind in [(r, "note") for r in notes] + [(r, "attachment") for r in atts]:
            rest = r["path"][len(prefix):].split("/")
            for i in range(1, min(len(rest), depth + 1)):
                folders.add(prefix + "/".join(rest[:i]))
            if len(rest) <= depth:
                e = dict(r); e["kind"] = kind
                entries.append(e)
        return {"folder": folder, "folders": sorted(folders), "entries": entries}

    def recent(self, limit: int = 20) -> list[dict]:
        # Phase 2 ergänzt Commit-Nachrichten aus Git
        return [dict(r) for r in self.db.execute(
            "SELECT path, title, mtime FROM notes ORDER BY mtime DESC LIMIT ?", (limit,))]

    def property_keys(self, min_notes: int = 1, folder: str | None = None) -> list[dict]:
        """Alle Eigenschafts-Schlüssel mit Häufigkeit und Beispielwerten (Grundlage für guide/lint)."""
        where, args = "", []
        if folder:
            where = " WHERE (n.folder = ? OR n.folder LIKE ?)"
            args = [folder.strip("/"), folder.strip("/") + "/%"]
        return [dict(r) for r in self.db.execute(
            "SELECT p.key_norm AS key, COUNT(DISTINCT p.note_id) AS notes, COUNT(DISTINCT p.value_norm) AS values_,"
            " GROUP_CONCAT(DISTINCT p.source) AS source FROM properties p JOIN notes n ON n.id = p.note_id"
            f"{where} GROUP BY p.key_norm HAVING notes >= ? ORDER BY notes DESC, key",
            args + [min_notes])]

    def note_properties(self, path: str) -> list[dict]:
        n = self._note(path)
        return [dict(r) for r in self.db.execute(
            "SELECT key, key_norm, value, value_norm, source, line FROM properties WHERE note_id = ?"
            " ORDER BY line", (n["id"],))]

    def stats(self) -> dict:
        one = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "notes": one("SELECT COUNT(*) FROM notes"),
            "bytes": one("SELECT COALESCE(SUM(size),0) FROM notes"),
            "sections": one("SELECT COUNT(*) FROM sections"),
            "links": one("SELECT COUNT(*) FROM links"),
            "broken_links": one("SELECT COUNT(*) FROM links WHERE resolved_path IS NULL"),
            "properties": one("SELECT COUNT(*) FROM properties"),
            "property_keys": one("SELECT COUNT(DISTINCT key_norm) FROM properties"),
            "tags": one("SELECT COUNT(DISTINCT tag) FROM tags"),
            "tasks_open": one("SELECT COUNT(*) FROM tasks WHERE done = 0"),
            "attachments": one("SELECT COUNT(*) FROM attachments"),
        }
