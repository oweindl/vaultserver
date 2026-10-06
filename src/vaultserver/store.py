"""Schreibzugriffe auf den Vault: Versionsprüfung, Regeln, Claims, ein Git-Commit je Änderung.

Alle Änderungen laufen über `Store`. Er hält eine Sperre, damit Index, Datei und
Commit immer zusammenpassen; Lesezugriffe nehmen dieselbe Sperre (SQLite-Verbindung
wird zwischen Threads geteilt).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from .config import Area, Config
from .gitops import Git
from .index import Index, norm_key
from .parser import MDLINK_RE, WIKILINK_RE, parse_note

OVERVIEW_START = "<!-- vaultserver:overview:start -->"
OVERVIEW_END = "<!-- vaultserver:overview:end -->"
STATUS_START = "<!-- vaultserver:status:start -->"
STATUS_END = "<!-- vaultserver:status:end -->"
COMMITS_START = "<!-- vaultserver:commits:start -->"
COMMITS_END = "<!-- vaultserver:commits:end -->"


class Conflict(Exception):
    """Die Notiz hat sich seit dem Lesen geändert."""

    def __init__(self, path: str, version: str | None, text: str | None, reason: str = ""):
        super().__init__(reason or f"Versionskonflikt: {path} wurde inzwischen geändert")
        self.path, self.version, self.text = path, version, text

    def as_dict(self) -> dict:
        return {"error": "conflict", "message": str(self), "path": self.path,
                "current_version": self.version, "current_text": self.text}


class Rejected(Exception):
    """Änderung verletzt Regeln (Pflichtfelder, erlaubte Werte, Claim, Pfad)."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []

    def as_dict(self) -> dict:
        return {"error": "rejected", "message": str(self), "problems": self.problems}


@dataclass
class SectionSpan:
    heading: str
    heading_path: str
    level: int
    start: int   # Index der Überschriftszeile (0-basiert); bei Abschnitt 0 erste Inhaltszeile
    end: int     # exklusiv, inkl. Unterabschnitte


def version_of(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()[:16]


def slugify(title: str) -> str:
    t = title.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:80] or "notiz"


def section_spans(text: str) -> list[SectionSpan]:
    lines = text.splitlines()
    parsed = parse_note(text, "x")
    spans = []
    secs = parsed.sections
    for i, s in enumerate(secs):
        start = s.line_start - 1
        end = len(lines)
        for t in secs[i + 1:]:
            if s.level == 0 or 0 < t.level <= s.level:
                end = t.line_start - 1
                break
        spans.append(SectionSpan(s.heading, s.heading_path, s.level, start, end))
    return spans


def find_section(text: str, section: str) -> SectionSpan:
    want = section.strip().lstrip("#").strip().lower()
    spans = section_spans(text)
    for pred in (lambda s: s.heading_path.lower() == want,
                 lambda s: s.heading.lower() == want,
                 lambda s: s.heading_path.lower().endswith(want)):
        hits = [s for s in spans if s.level > 0 and pred(s)]
        if hits:
            return hits[0]
    raise KeyError(f"Abschnitt nicht gefunden: {section}")


def _join(lines: list[str], trailing_newline: bool = True) -> str:
    return "\n".join(lines) + ("\n" if trailing_newline else "")


def replace_block(text: str, start_marker: str, end_marker: str, body: str,
                  fallback_heading: str) -> str:
    """Generierten Block zwischen Markern ersetzen; fehlt er, am Ende anhängen."""
    block = f"{start_marker}\n{body.rstrip()}\n{end_marker}"
    if start_marker in text and end_marker in text:
        a = text.index(start_marker)
        b = text.index(end_marker) + len(end_marker)
        return text[:a] + block + text[b:]
    return text.rstrip("\n") + f"\n\n{fallback_heading}\n\n{block}\n"


class Store:
    def __init__(self, config: Config, index: Index | None = None):
        self.config = config
        self.index = index or Index(config)
        self.git = Git(config.vault_path)
        self.lock = threading.RLock()
        # Version -> Text früherer Stände, für Abschnitts-Patches gegen ältere Versionen
        self._history: OrderedDict[str, str] = OrderedDict()
        self.dirty_push = False
        # Rückmeldung an die Live-Anzeige: (Pfade, Agent) – gesetzt vom Service
        self.on_change = None

    # ------------------------------------------------------------ Hilfen

    def _abs(self, path: str) -> Path:
        p = PurePosixPath(path.strip().lstrip("/"))
        if not str(p) or ".." in p.parts or any(part.startswith(".") for part in p.parts):
            raise Rejected(f"Ungültiger Pfad: {path}")
        full = (self.config.vault_path / str(p)).resolve()
        if self.config.vault_path not in full.parents:
            raise Rejected(f"Pfad außerhalb des Vaults: {path}")
        return full

    @staticmethod
    def _norm_path(path: str) -> str:
        path = path.strip().lstrip("/")
        return path if path.lower().endswith(".md") or "." in PurePosixPath(path).name else path + ".md"

    def _remember(self, text: str) -> str:
        v = version_of(text)
        self._history[v] = text
        self._history.move_to_end(v)
        while len(self._history) > 1000:
            self._history.popitem(last=False)
        return v

    def current(self, path: str) -> tuple[str | None, str | None]:
        full = self._abs(path)
        if not full.exists():
            return None, None
        text = full.read_text(encoding="utf-8")
        return text, self._remember(text)

    def read_tracked(self, path: str, **kw) -> dict:
        """Wie Index.read, merkt sich aber den Stand für spätere Abschnitts-Patches."""
        with self.lock:
            res = self.index.read(path, **kw)
            self.current(path)
            claim = self.claim_info(path)
            if claim:
                res["claim"] = claim
            return res

    def _agent_label(self, agent: str) -> str:
        return agent or "unbekannt"

    def _write_file(self, path: str, text: str) -> None:
        full = self._abs(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(f".{full.name}.vaultserver.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, full)

    def _notify(self, paths: list[str], agent: str) -> None:
        if self.on_change and paths:
            try:
                self.on_change(list(paths), agent)
            except Exception:  # noqa: BLE001 – Live-Anzeige darf Schreiben nie stören
                pass

    def _finish(self, paths: list[str], message: str, agent: str) -> str | None:
        for p in paths:
            self.index.index_file(p)
        commit = None
        if self.config.git_commit:
            commit = self.git.commit(paths, f"{self._agent_label(agent)}: {message}", self._agent_label(agent))
            if commit:
                self.dirty_push = True
        self._notify(paths, agent)
        return commit

    def _check_version(self, path: str, base_version: str | None, current_text: str | None,
                       current_version: str | None) -> None:
        if current_text is None:
            return
        if not base_version:
            raise Conflict(path, current_version, current_text,
                           f"{path} existiert; zum Ändern die zuletzt gelesene Version mitgeben (base_version)")
        if base_version != current_version:
            raise Conflict(path, current_version, current_text)

    # ------------------------------------------------------------ Regeln

    def violations(self, path: str, text: str) -> set[str]:
        area = self.config.area_for(path)
        if not area or path == area.overview or not path.startswith(area.folder.rstrip("/") + "/"):
            return set()
        props: dict[str, str] = {}
        parsed = parse_note(text, PurePosixPath(path).stem)
        # Nur der Eigenschaftsblock oben (Frontmatter, Text vor/unter der Titelüberschrift) zählt;
        # Eigenschafts-Zeilen tiefer im Text beschreiben oft etwas anderes.
        top = {s.ord for s in parsed.sections if s.level <= 1}
        for p in parsed.properties:
            if p.source == "inline" and p.section_ord not in top:
                continue
            kn = norm_key(p.key)
            props.setdefault(kn, self.config.normalize_value(kn, p.value))
        out = set()
        for req in area.required:
            if norm_key(req) not in props:
                out.add(f"Pflichtfeld fehlt: {req}")
        for key, allowed in area.allowed.items():
            if key in props and props[key] not in allowed:
                out.add(f"Unbekannter Wert für {key}: „{props[key]}“ (erlaubt: {', '.join(allowed)})")
        return out

    def _validate(self, path: str, new_text: str, old_text: str | None) -> None:
        new = self.violations(path, new_text)
        old = self.violations(path, old_text) if old_text is not None else set()
        added = sorted(new - old)
        if added:
            raise Rejected(f"Regeln des Bereichs verletzt ({path})", added)

    # ------------------------------------------------------------ Claims (Idee 3)

    def claim_info(self, path: str) -> dict | None:
        row = self.index.db.execute(
            "SELECT agent, note, claimed_at, expires_at FROM claims WHERE path = ? AND expires_at > ?",
            (path, time.time())).fetchone()
        return dict(row) if row else None

    def _check_claim(self, path: str, agent: str, force: bool = False) -> None:
        c = self.claim_info(path)
        if c and c["agent"].split("/")[0] != agent.split("/")[0] and not force:
            left = int((c["expires_at"] - time.time()) / 60)
            raise Rejected(f"{path} ist von {c['agent']} reserviert (noch {left} min): {c['note']}")
        if c:  # eigene Änderung verlängert den Claim
            self.index.db.execute("UPDATE claims SET expires_at = ? WHERE path = ?",
                                  (time.time() + self.config.claim_minutes * 60, path))
            self.index.db.commit()

    def claim(self, path: str, agent: str, note: str = "", minutes: int | None = None,
              set_status: bool = True) -> dict:
        path = self._norm_path(path)
        with self.lock:
            text, version = self.current(path)
            if text is None:
                raise KeyError(f"Notiz nicht gefunden: {path}")
            c = self.claim_info(path)
            if c and c["agent"].split("/")[0] != agent.split("/")[0]:
                raise Rejected(f"Bereits reserviert von {c['agent']} bis "
                               f"{time.strftime('%H:%M', time.localtime(c['expires_at']))}")
            now = time.time()
            exp = now + (minutes or self.config.claim_minutes) * 60
            self.index.db.execute("INSERT OR REPLACE INTO claims VALUES (?,?,?,?,?)", (path, agent, note, now, exp))
            self.index.db.commit()
            result = {"path": path, "agent": agent, "expires_at": exp, "version": version}
            area = self.config.area_for(path)
            if set_status and area and "in arbeit" in area.allowed.get("status", []):
                status = next((p for p in self.index.note_properties(path) if p["key_norm"] == "status"), None)
                if status is None or status["value_norm"] != "in arbeit":
                    result.update(self.set_property(path, "Status", "in Arbeit", agent,
                                                    message=f"{PurePosixPath(path).stem} reserviert"))
            return result

    def release(self, path: str, agent: str, force: bool = False) -> dict:
        path = self._norm_path(path)
        with self.lock:
            c = self.claim_info(path)
            if c and c["agent"].split("/")[0] != agent.split("/")[0] and not force:
                raise Rejected(f"Reserviert von {c['agent']}, nicht von {agent}")
            self.index.db.execute("DELETE FROM claims WHERE path = ?", (path,))
            self.index.db.commit()
            return {"path": path, "released": bool(c)}

    def claims(self) -> list[dict]:
        return [dict(r) for r in self.index.db.execute(
            "SELECT path, agent, note, claimed_at, expires_at FROM claims WHERE expires_at > ? ORDER BY path",
            (time.time(),))]

    # ------------------------------------------------------------ Schreibwerkzeuge

    def write(self, path: str, content: str, agent: str, base_version: str | None = None,
              message: str | None = None, force: bool = False) -> dict:
        path = self._norm_path(path)
        with self.lock:
            old, cur_v = self.current(path)
            self._check_version(path, base_version, old, cur_v)
            self._check_claim(path, agent, force)
            if not content.endswith("\n"):
                content += "\n"
            self._validate(path, content, old)
            self._write_file(path, content)
            commit = self._finish([path], message or f"{'Neu' if old is None else 'Geändert'}: {path}", agent)
            return {"path": path, "version": self._remember(content), "commit": commit, "created": old is None}

    def patch_section(self, path: str, section: str | None, content: str, agent: str,
                      base_version: str | None = None, mode: str = "replace",
                      message: str | None = None, force: bool = False) -> dict:
        """mode: replace (Abschnitt inkl. Unterabschnitte ersetzen; ohne Überschrift im Inhalt
        bleibt die alte Überschrift), append (ans Ende des Abschnitts), prepend (direkt unter
        die Überschrift), insert_after (neuer Abschnitt nach dem Abschnitt; ohne section ans Ende)."""
        path = self._norm_path(path)
        if mode not in ("replace", "append", "prepend", "insert_after"):
            raise Rejected(f"Unbekannter Modus: {mode}")
        with self.lock:
            text, cur_v = self.current(path)
            if text is None:
                raise KeyError(f"Notiz nicht gefunden: {path}")
            if not base_version:
                raise Conflict(path, cur_v, text, "base_version fehlt (zuletzt gelesene Version mitgeben)")
            lines = text.splitlines()
            span = find_section(text, section) if section else None
            if base_version != cur_v:
                # Nur Konflikt, wenn sich genau dieser Abschnitt geändert hat
                base = self._history.get(base_version)
                if base is None or span is None:
                    raise Conflict(path, cur_v, text)
                try:
                    bspan = find_section(base, section)
                except KeyError:
                    raise Conflict(path, cur_v, text)
                if base.splitlines()[bspan.start:bspan.end] != lines[span.start:span.end]:
                    raise Conflict(path, cur_v, text, f"Abschnitt „{section}“ wurde inzwischen geändert")
            self._check_claim(path, agent, force)
            new = content.rstrip("\n").splitlines()
            if span is None:
                if mode != "insert_after" and mode != "append":
                    raise Rejected("Ohne section sind nur append und insert_after möglich")
                out = lines + ([""] if lines and lines[-1].strip() else []) + new
            elif mode == "replace":
                starts_with_heading = bool(new) and new[0].lstrip().startswith("#")
                head = [] if starts_with_heading or span.level == 0 else [lines[span.start]]
                tail_blank = [""] if span.end < len(lines) else []
                out = lines[:span.start] + head + new + tail_blank + lines[span.end:]
            elif mode == "append":
                body_end = span.end
                while body_end > span.start + 1 and not lines[body_end - 1].strip():
                    body_end -= 1
                out = lines[:body_end] + new + lines[body_end:]
            elif mode == "prepend":
                at = span.start + (1 if span.level > 0 else 0)
                out = lines[:at] + new + lines[at:]
            else:
                if not new or not new[0].lstrip().startswith("#"):
                    raise Rejected("insert_after braucht Inhalt, der mit einer Überschrift beginnt")
                out = lines[:span.end] + ([""] if lines[span.end - 1].strip() else []) + new + [""] + lines[span.end:]
            result = _join(out)
            self._validate(path, result, text)
            self._write_file(path, result)
            commit = self._finish([path], message or f"{PurePosixPath(path).stem}: Abschnitt {section or 'neu'} ({mode})", agent)
            return {"path": path, "version": self._remember(result), "commit": commit}

    def set_property(self, path: str, key: str, value: str, agent: str,
                     base_version: str | None = None, message: str | None = None,
                     force: bool = False) -> dict:
        """Eigenschaft dort ändern, wo sie steht (Frontmatter oder `- Schlüssel: Wert`);
        fehlt sie, als Zeile nach den vorhandenen Eigenschaften bzw. unter der ersten Überschrift."""
        path = self._norm_path(path)
        value = " ".join(str(value).split())
        with self.lock:
            text, cur_v = self.current(path)
            if text is None:
                raise KeyError(f"Notiz nicht gefunden: {path}")
            if base_version and base_version != cur_v:
                raise Conflict(path, cur_v, text)
            self._check_claim(path, agent, force)
            lines = text.splitlines()
            kn = norm_key(key)
            parsed = parse_note(text, PurePosixPath(path).stem)
            existing = [p for p in parsed.properties if norm_key(p.key) == kn]
            fm_end = self._frontmatter_end(lines)
            if existing and existing[0].source == "inline":
                i = existing[0].line - 1
                pat = re.compile(r"^(\s*[-*+]\s+(?:\*\*|__)?" + re.escape(existing[0].key) +
                                 r"(?:\*\*|__)?\s*:(?:\*\*|__)?)\s*.*$", re.I)
                if not pat.match(lines[i]):
                    raise Rejected(f"Zeile {i + 1} nicht änderbar: {lines[i]}")
                lines[i] = pat.sub(lambda m: m.group(1) + " " + value, lines[i])
            elif existing and fm_end:
                pat = re.compile(r"^(" + re.escape(existing[0].key) + r")\s*:.*$")
                for i in range(1, fm_end - 1):
                    if pat.match(lines[i]):
                        lines[i] = f"{existing[0].key}: " + yaml.safe_dump(value, allow_unicode=True,
                                                                          default_style=None).strip().removesuffix("...").strip()
                        break
            else:
                inline = [p for p in parsed.properties if p.source == "inline" and p.section_ord in (0, 1)]
                if inline:
                    at = max(p.line for p in inline)
                else:
                    at = next((s.line_start for s in parsed.sections if s.level > 0), fm_end)
                    # nach Überschrift und direkt folgenden Zitat-/Leerzeilen einfügen
                    while at < len(lines) and (not lines[at].strip() or lines[at].startswith(">")):
                        at += 1
                    lines.insert(at, "")
                lines.insert(at, f"- {key}: {value}")
            result = _join(lines, text.endswith("\n"))
            if result == text:
                return {"path": path, "version": cur_v, "commit": None, "unchanged": True}
            self._validate(path, result, text)
            self._write_file(path, result)
            commit = self._finish([path], message or f"{PurePosixPath(path).stem} {key} = {value}", agent)
            return {"path": path, "version": self._remember(result), "commit": commit}

    @staticmethod
    def _frontmatter_end(lines: list[str]) -> int:
        if lines and lines[0].strip() == "---":
            for i in range(1, len(lines)):
                if lines[i].strip() in ("---", "..."):
                    return i + 1
        return 0

    def delete(self, path: str, agent: str, base_version: str | None = None,
               message: str | None = None, force: bool = False) -> dict:
        path = self._norm_path(path)
        with self.lock:
            text, cur_v = self.current(path)
            if text is None:
                raise KeyError(f"Notiz nicht gefunden: {path}")
            self._check_version(path, base_version, text, cur_v)
            self._check_claim(path, agent, force)
            self._abs(path).unlink()
            self.index.db.execute("DELETE FROM claims WHERE path = ?", (path,))
            commit = self._finish([path], message or f"Gelöscht: {path}", agent)
            return {"path": path, "deleted": True, "commit": commit}

    def move(self, src: str, dst: str, agent: str, message: str | None = None,
             update_links: bool = True, force: bool = False) -> dict:
        """Notiz oder Anhang verschieben; Wikilinks und Markdown-Links werden nachgezogen."""
        src = src.strip().lstrip("/")
        dst = dst.strip().lstrip("/")
        if src.lower().endswith(".md") or not PurePosixPath(src).suffix:
            src, dst = self._norm_path(src), self._norm_path(dst)
        with self.lock:
            s_full, d_full = self._abs(src), self._abs(dst)
            if not s_full.exists():
                raise KeyError(f"Nicht gefunden: {src}")
            if d_full.exists():
                raise Rejected(f"Ziel existiert bereits: {dst}")
            if src.endswith(".md"):
                self._check_claim(src, agent, force)
            sources = sorted({b["path"] for b in self._raw_backlinks(src)} - {src}) if update_links else []
            d_full.parent.mkdir(parents=True, exist_ok=True)
            os.replace(s_full, d_full)
            changed = [src, dst]
            self.index.index_file(src)
            self.index.index_file(dst)
            for note in sources:
                text = self._abs(note).read_text(encoding="utf-8")
                new = self._rewrite_links(text, note, src, dst)
                if new != text:
                    self._write_file(note, new)
                    changed.append(note)
            self.index.db.execute("UPDATE claims SET path = ? WHERE path = ?", (dst, src))
            commit = self._finish(changed, message or f"Verschoben: {src} → {dst}", agent)
            return {"from": src, "to": dst, "updated_notes": changed[2:], "commit": commit,
                    "version": self.current(dst)[1] if dst.endswith(".md") else None}

    def _raw_backlinks(self, path: str) -> list[dict]:
        return [dict(r) for r in self.index.db.execute(
            "SELECT n.path FROM links l JOIN notes n ON n.id = l.note_id WHERE l.resolved_path = ?", (path,))]

    def _rewrite_links(self, text: str, note: str, src: str, dst: str) -> str:
        idx = self.index
        dst_stem = PurePosixPath(dst).stem if dst.endswith(".md") else PurePosixPath(dst).name
        unique = idx.db.execute("SELECT COUNT(*) FROM notes WHERE path LIKE ? OR path = ?",
                                (f"%/{PurePosixPath(dst).name}", PurePosixPath(dst).name)).fetchone()[0] <= 1
        files = [r["path"] for r in idx.db.execute("SELECT path FROM notes UNION SELECT path FROM attachments")]
        files = [f for f in files if f != dst] + [src]  # Auflösung wie vor dem Verschieben
        by_lower = {f.lower(): f for f in files}
        by_name: dict[str, list[str]] = {}
        for f in files:
            name = PurePosixPath(f).name.lower()
            by_name.setdefault(name, []).append(f)
            if name.endswith(".md"):
                by_name.setdefault(name[:-3], []).append(f)
        aliases = {r["alias_norm"]: src for r in idx.db.execute(
            "SELECT alias_norm FROM aliases a JOIN notes n ON n.id = a.note_id WHERE n.path = ?", (dst,))}

        def wiki(m: re.Match) -> str:
            bang, inner = m.group(1), m.group(2)
            escaped = "\\|" in inner
            body = inner.replace("\\|", "|")
            target, sep, alias = body.partition("|")
            tgt, hsep, sec = target.partition("#")
            if not tgt.strip():
                return m.group(0)
            if Index._resolve(tgt.strip(), "wiki", note, by_lower, by_name, aliases) != src:
                return m.group(0)
            if tgt.strip().lower() in aliases:
                return m.group(0)  # Alias zeigt weiter auf die Notiz
            if "/" in tgt or not unique:
                new_t = dst[:-3] if dst.endswith(".md") else dst
            else:
                new_t = dst_stem if dst.endswith(".md") else PurePosixPath(dst).name
            out = new_t + (hsep + sec if hsep else "") + (("\\|" if escaped else "|") + alias if sep else "")
            return f"{bang}[[{out}]]"

        def md(m: re.Match) -> str:
            href = m.group(3)
            if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):
                return m.group(0)
            target, hsep, sec = href.partition("#")
            if Index._resolve(target.replace("%20", " "), "md", note, by_lower, by_name, {}) != src:
                return m.group(0)
            rel = os.path.relpath(dst, str(PurePosixPath(note).parent) or ".").replace(os.sep, "/")
            return f"{m.group(1)}[{m.group(2)}]({rel.replace(' ', '%20')}{hsep}{sec})"

        out, in_fence = [], False
        for line in text.split("\n"):
            if line.lstrip().startswith(("```", "~~~")):
                in_fence = not in_fence
            if not in_fence and ("[[" in line or "](" in line):
                line = WIKILINK_RE.sub(wiki, line)
                line = MDLINK_RE.sub(md, line)
            out.append(line)
        return "\n".join(out)

    def upload(self, path: str, data: bytes, agent: str, message: str | None = None) -> dict:
        with self.lock:
            full = self._abs(path)
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(data)
            self.index.sync()
            commit = self._finish([path], message or f"Anhang: {path}", agent)
            return {"path": path, "size": len(data), "commit": commit}

    # ------------------------------------------------------------ Ordner und Papierkorb

    KEEP = ".gitkeep"          # hält leere Ordner in Git
    META = ".recycle.json"     # Herkunft eines Papierkorb-Eintrags

    @property
    def bin(self) -> str:
        return self.config.recycle_folder

    def _in_bin(self, path: str) -> bool:
        return bool(self.bin) and (path == self.bin or path.startswith(self.bin + "/"))

    def _files_under(self, rel: str) -> list[str]:
        """Alle Dateien (auch .gitkeep) unter einem Ordner, relativ zum Vault."""
        base = self.config.vault_path / rel
        out = []
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                out.append(str(PurePosixPath(Path(dirpath, f).relative_to(self.config.vault_path).as_posix())))
        return sorted(out)

    def ensure_bin(self) -> str | None:
        """Den Papierkorb-Ordner anlegen, falls er fehlt (er muss immer da sein)."""
        if not self.bin:
            return None
        with self.lock:
            keep = self.config.vault_path / self.bin / self.KEEP
            if keep.exists():
                return None
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_text("", encoding="utf-8")
            return self._finish_raw([f"{self.bin}/{self.KEEP}"], f"Papierkorb {self.bin} angelegt", "vaultserver")

    def mkdir(self, path: str, agent: str, message: str | None = None) -> dict:
        """Neuen (leeren) Ordner anlegen; ohne Schrägstrich = neues Stammverzeichnis („neuer Vault“)."""
        path = path.strip().strip("/")
        if self._in_bin(path):
            raise Rejected(f"Im Papierkorb {self.bin} lassen sich keine Ordner anlegen")
        with self.lock:
            full = self._abs(path)
            if full.exists():
                raise Rejected(f"Gibt es schon: {path}")
            full.mkdir(parents=True)
            (full / self.KEEP).write_text("", encoding="utf-8")
            commit = self._finish_raw([f"{path}/{self.KEEP}"], message or f"Ordner angelegt: {path}", agent)
            return {"path": path, "created": True, "commit": commit}

    def trash(self, path: str, agent: str, base_version: str | None = None, message: str | None = None,
              force: bool = False) -> dict:
        """Notiz, Anhang oder Ordner in den Papierkorb schieben (mit Herkunft, wiederherstellbar)."""
        path = path.strip().strip("/")
        if not path:
            raise Rejected("Der ganze Vault lässt sich nicht löschen")
        if self._in_bin(path):
            raise Rejected(f"Liegt schon im Papierkorb – dort „endgültig löschen“ verwenden")
        with self.lock:
            full = self._abs(path)
            if not full.exists() and not full.is_dir():
                full = self._abs(self._norm_path(path))
                path = self._norm_path(path)
            if not full.exists():
                raise KeyError(f"Nicht gefunden: {path}")
            kind = "folder" if full.is_dir() else "note" if path.endswith(".md") else "file"
            if kind == "note":
                text, cur_v = self.current(path)
                if base_version is not None:
                    self._check_version(path, base_version, text, cur_v)
                self._check_claim(path, agent, force)
                files = [path]
            elif kind == "folder":
                files = self._files_under(path)
                for f in files:
                    if f.endswith(".md"):
                        self._check_claim(f, agent, force)
            else:
                files = [path]
            entry = f"{self.bin}/{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}"
            dest = self.config.vault_path / entry / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(full), str(dest))
            meta = {"original": path, "kind": kind, "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "deleted_by": agent, "files": len(files),
                    "size": sum((self.config.vault_path / entry / f).stat().st_size for f in files
                                if (self.config.vault_path / entry / f).is_file())}
            (self.config.vault_path / entry / self.META).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            for f in files:
                self.index.index_file(f)  # aus dem Index nehmen
                self.index.db.execute("DELETE FROM claims WHERE path = ?", (f,))
            moved = [f"{entry}/{f}" for f in files] + [f"{entry}/{self.META}"]
            commit = self._finish_raw(files + moved, message or f"In den Papierkorb: {path}", agent)
            return {"path": path, "kind": kind, "trashed": True, "id": entry.split("/", 1)[1],
                    "files": len(files), "commit": commit}

    def _finish_raw(self, paths: list[str], message: str, agent: str) -> str | None:
        """Wie _finish, aber ohne Index (Papierkorb ist nicht im Index)."""
        commit = None
        if self.config.git_commit:
            commit = self.git.commit(paths, f"{self._agent_label(agent)}: {message}", self._agent_label(agent))
            if commit:
                self.dirty_push = True
        self._notify(paths, agent)
        return commit

    def _entry(self, entry_id: str) -> tuple[Path, dict]:
        if not re.fullmatch(r"[0-9T]{15}-[0-9a-f]{4}", entry_id or ""):
            raise KeyError(f"Papierkorb-Eintrag nicht gefunden: {entry_id}")
        d = self.config.vault_path / self.bin / entry_id
        try:
            return d, json.loads((d / self.META).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(f"Papierkorb-Eintrag nicht gefunden: {entry_id}") from None

    def bin_list(self) -> list[dict]:
        base = self.config.vault_path / self.bin
        out = []
        with self.lock:
            for d in sorted(base.iterdir(), reverse=True) if base.is_dir() else []:
                if not d.is_dir():
                    continue
                try:
                    meta = json.loads((d / self.META).read_text(encoding="utf-8"))
                except (FileNotFoundError, ValueError):
                    continue
                target = self.config.vault_path / meta["original"]
                out.append({"id": d.name, **meta, "exists": target.exists()})
        return out

    def restore(self, entry_id: str, agent: str, message: str | None = None) -> dict:
        """Eintrag an die alte Stelle zurück. Ordner werden mit einem vorhandenen Ordner zusammengeführt;
        gibt es eine Datei dort schon, wird nichts verschoben und die Konflikte werden genannt."""
        with self.lock:
            d, meta = self._entry(entry_id)
            orig = meta["original"]
            src_root = d / orig
            if not src_root.exists():
                raise KeyError(f"Inhalt von {entry_id} fehlt im Papierkorb")
            rel_files = ([orig] if src_root.is_file()
                         else [f[len(f"{self.bin}/{entry_id}/"):] for f in self._files_under(f"{self.bin}/{entry_id}/{orig}")])
            clash = [f for f in rel_files if (self.config.vault_path / f).exists() and not f.endswith("/" + self.KEEP)]
            if clash:
                raise Rejected(f"Gibt es an der alten Stelle schon: {', '.join(clash[:5])}"
                               + (f" (+{len(clash) - 5})" if len(clash) > 5 else ""))
            for f in rel_files:
                dst = self.config.vault_path / f
                if dst.exists():   # nur .gitkeep
                    (self.config.vault_path / self.bin / entry_id / f).unlink()
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(self.config.vault_path / self.bin / entry_id / f), str(dst))
            shutil.rmtree(d)
            for f in rel_files:
                self.index.index_file(f)
            gone = [f"{self.bin}/{entry_id}/{f}" for f in rel_files] + [f"{self.bin}/{entry_id}/{self.META}"]
            commit = self._finish_raw(rel_files + gone, message or f"Wiederhergestellt: {orig}", agent)
            return {"id": entry_id, "path": orig, "kind": meta["kind"], "restored": True, "files": len(rel_files),
                    "commit": commit}

    def purge(self, entry_id: str | None, agent: str) -> dict:
        """Papierkorb-Eintrag endgültig löschen (bleibt in der Git-Historie); ohne id: Papierkorb leeren."""
        with self.lock:
            ids = [entry_id] if entry_id else [e["id"] for e in self.bin_list()]
            paths = []
            for i in ids:
                d, meta = self._entry(i)
                paths += self._files_under(f"{self.bin}/{i}")
                shutil.rmtree(d)
            if not ids:
                return {"purged": 0, "commit": None}
            label = f"Endgültig gelöscht: {meta['original']}" if entry_id else f"Papierkorb geleert ({len(ids)} Einträge)"
            commit = self._finish_raw(paths, label, agent)
            return {"purged": len(ids), "commit": commit}

    # ------------------------------------------------------------ Vorlagen (Idee 2)

    def _area_numbers(self, area: Area) -> dict[int, str]:
        rx = re.compile(area.id_regex)
        out: dict[int, str] = {}
        prefix = area.folder.rstrip("/") + "/"
        for (p,) in self.index.db.execute("SELECT path FROM notes WHERE path LIKE ?", (prefix + "%",)):
            m = rx.search(PurePosixPath(p).name)
            if m and "/" not in p[len(prefix):]:
                out.setdefault(int(m.group(1)), p)
        return out

    @staticmethod
    def _render_fields(fields: dict[str, object]) -> str:
        out = []
        for k, v in fields.items():
            if isinstance(v, list):
                out.append(f"- {k}:")
                for item in v:
                    item = str(item)
                    out.append(f"  - {item}" if re.match(r"^\[[ xX]\] ", item) else f"  - [ ] {item}")
            elif "\n" in str(v):
                out.append(f"- {k}:")
                out += [f"  {l}" if l.strip() else "" for l in str(v).splitlines()]
            else:
                out.append(f"- {k}: {v}")
        return "\n".join(out)

    def create_from_template(self, area_name: str, title: str, fields: dict[str, object], agent: str,
                             summary: str = "", body: str = "", message: str | None = None) -> dict:
        area = self.config.area_by_name(area_name)
        if not area.filename or not area.id_regex:
            raise Rejected(f"Bereich {area.name} hat keine Vorlage (filename/id_regex fehlen)")
        title = " ".join(title.split())
        with self.lock:
            self.index.sync()
            nums = self._area_numbers(area)
            n = max(nums, default=0) + 1
            # Felder in Reihenfolge: Pflichtfelder zuerst, Schlüssel case-insensitiv zuordnen
            given = {norm_key(k): (k, v) for k, v in {**area.defaults, **fields}.items()}
            for k, v in fields.items():  # Schreibweise des Aufrufers gewinnt nicht über Vorbelegung
                if norm_key(k) in {norm_key(d) for d in area.defaults}:
                    given[norm_key(k)] = (next(d for d in area.defaults if norm_key(d) == norm_key(k)), v)
            ordered: dict[str, object] = {}
            for req in area.required:
                if norm_key(req) in given:
                    ordered[req] = given.pop(norm_key(req))[1]
            # Vorbelegte, nicht verpflichtende Felder (z. B. Umsetzung) ans Ende
            tail = {norm_key(d) for d in area.defaults if norm_key(d) not in {norm_key(r) for r in area.required}}
            tail_items = [given.pop(k) for k in list(given) if k in tail]
            for k, v in list(given.values()) + tail_items:
                ordered[k] = v
            missing = [r for r in area.required if r not in ordered]
            if missing:
                raise Rejected(f"Pflichtfelder fehlen für {area.name}", [f"Pflichtfeld fehlt: {m}" for m in missing])
            vals = {"n": n, "title": title, "slug": slugify(title)}
            vals.update({norm_key(k): v for k, v in ordered.items() if not isinstance(v, list)})
            name = area.filename.format(**vals)
            path = f"{area.folder.rstrip('/')}/{name}"
            heading = area.heading.format(**vals) if area.heading else f"# {title}"
            tmpl = area.template or "{fields}\n"
            text = heading + "\n\n" + tmpl.format(fields=self._render_fields(ordered), **vals).strip("\n") + "\n"
            if body:
                text += "\n" + body.strip("\n") + "\n"
            problems = sorted(self.violations(path, text))
            if problems:
                raise Rejected(f"Vorlage für {area.name} verletzt Regeln", problems)
            if self._abs(path).exists():
                raise Rejected(f"Datei existiert bereits: {path}")
            self._write_file(path, text)
            changed = [path]
            if area.overview and area.overview_entry and self._abs(area.overview).exists():
                vals_e = {**vals, "stem": PurePosixPath(path).stem, "summary": summary.strip() or title,
                          "path": path[:-3]}
                vals_e = {k: (", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in vals_e.items()}
                entry = area.overview_entry.format_map(_Default(vals_e)).strip("\n")
                ov = self._abs(area.overview).read_text(encoding="utf-8")
                self._write_file(area.overview, self._insert_entry(ov, area, entry))
                changed.append(area.overview)
            commit = self._finish(changed, message or f"{PurePosixPath(path).stem} angelegt: {title}", agent)
            return {"path": path, "number": n, "version": self._remember(text), "commit": commit,
                    "overview_updated": area.overview in changed}

    @staticmethod
    def _insert_entry(text: str, area: Area, entry: str) -> str:
        lines = text.splitlines()
        at = len(lines)
        try:
            if area.overview_section:
                at = find_section(text, area.overview_section).end
            elif area.overview_before:
                at = find_section(text, area.overview_before).start
        except KeyError:
            pass
        while at > 0 and not lines[at - 1].strip():
            at -= 1
        new = lines[:at] + ["", *entry.splitlines(), ""] + ([""] if at < len(lines) and lines[at].strip() else []) + lines[at:]
        # doppelte Leerzeilen am Übergang vermeiden
        cleaned = []
        for l in new:
            if not l.strip() and cleaned and not cleaned[-1].strip():
                continue
            cleaned.append(l)
        return _join(cleaned)

    # ------------------------------------------------------------ Übersichten (Idee 5)

    def area_entries(self, area: Area, include_sub: bool = False) -> list[dict]:
        nums = self._area_numbers(area) if area.id_regex else {}
        rows = []
        cols = [norm_key(c) for c in (area.overview_columns or ["priorität", "status"])]
        for n, p in sorted(nums.items()):
            props = {}
            for pr in self.index.note_properties(p):
                if pr["key_norm"] in cols:
                    props.setdefault(pr["key_norm"], pr["value_norm"])
            title = self.index._note(p)["title"]
            rows.append({"n": n, "path": p, "title": title, "props": props})
        return rows

    def refresh_overview(self, area_name: str, agent: str) -> dict:
        area = self.config.area_by_name(area_name)
        if not area.overview:
            raise Rejected(f"Bereich {area.name} hat keine Übersichtsdatei")
        with self.lock:
            self.index.sync()
            cols = area.overview_columns or ["Priorität", "Status"]
            rows = self.area_entries(area)
            claims = {c["path"]: c for c in self.claims()}
            head = "| Nr | Titel | " + " | ".join(cols) + " | Reserviert |\n|---|---|" + "---|" * len(cols) + "---|"
            body = []
            for r in rows:
                stem = PurePosixPath(r["path"]).stem
                title = r["title"].replace("|", "\\|")
                cells = [r["props"].get(norm_key(c), "–") for c in cols]
                claim = claims.get(r["path"], {}).get("agent", "")
                body.append(f"| {r['n']} | [[{stem}\\|{title}]] | " + " | ".join(cells) + f" | {claim} |")
            stamp = time.strftime("%Y-%m-%d %H:%M")
            table = f"*Automatisch erzeugt aus den Eigenschaften der Einträge ({stamp}). Nicht von Hand ändern.*\n\n{head}\n" + "\n".join(body)
            text = self._abs(area.overview).read_text(encoding="utf-8")
            new = replace_block(text, OVERVIEW_START, OVERVIEW_END, table, "## Übersicht (automatisch)")
            if self._strip_stamp(new) == self._strip_stamp(text):
                return {"path": area.overview, "entries": len(rows), "commit": None, "unchanged": True}
            self._write_file(area.overview, new)
            commit = self._finish([area.overview], f"Übersicht {area.name} aktualisiert", agent)
            return {"path": area.overview, "entries": len(rows), "commit": commit}

    @staticmethod
    def _strip_stamp(text: str) -> str:
        return re.sub(r"\(\d{4}-\d\d-\d\d \d\d:\d\d\)", "", text)

    def refresh_status(self, agent: str, note: str | None = None) -> dict:
        note = note or self.config.status_note
        if not note:
            raise Rejected("Keine Statusnotiz konfiguriert (status_note)")
        with self.lock:
            self.index.sync()
            parts = []
            for area in self.config.areas:
                if not area.id_regex:
                    continue
                rows = self.area_entries(area)
                counts: dict[str, int] = {}
                for r in rows:
                    s = r["props"].get("status", "ohne status")
                    counts[s] = counts.get(s, 0) + 1
                done = set(self.config.groups.get("status", {}).get("erledigt", []))
                todo = [r for r in rows if r["props"].get("status", "") not in done]
                parts.append(f"### {area.name}\n\n" + " · ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
                if todo:
                    parts.append("\n".join(
                        f"- [[{PurePosixPath(r['path']).stem}\\|{r['title']}]] – {r['props'].get('status', '–')}"
                        + (f", Priorität {r['props']['priorität']}" if r['props'].get('priorität') else "")
                        for r in todo))
            claims = self.claims()
            if claims:
                parts.append("### Reserviert\n\n" + "\n".join(
                    f"- [[{PurePosixPath(c['path']).stem}]] – {c['agent']} bis "
                    f"{time.strftime('%H:%M', time.localtime(c['expires_at']))}" for c in claims))
            stamp = time.strftime("%Y-%m-%d %H:%M")
            body = f"*Automatisch erzeugt ({stamp}).*\n\n" + "\n\n".join(parts)
            text = self._abs(note).read_text(encoding="utf-8") if self._abs(note).exists() else f"# Status\n"
            new = replace_block(text, STATUS_START, STATUS_END, body, "## Stand aus den Eigenschaften (automatisch)")
            if self._strip_stamp(new) == self._strip_stamp(text):
                return {"path": note, "commit": None, "unchanged": True}
            self._write_file(note, new)
            commit = self._finish([note], "Statusübersicht aktualisiert", agent)
            return {"path": note, "commit": commit}

    # ------------------------------------------------------------ Commit-Verknüpfung (Idee 8)

    def link_commits(self, agent: str = "vaultserver", limit: int = 2000) -> dict:
        found: dict[str, list[dict]] = {}
        with self.lock:
            self.index.sync()
            for repo in self.config.code_repos:
                g = Git(Path(repo["path"]))
                if not g.enabled:
                    continue
                commits = g.run("log", f"-n{limit}", "--no-merges", "--format=%H%x1f%ad%x1f%s",
                                "--date=short", check=False).splitlines()
                for area in self.config.areas:
                    if not area.commit_regex:
                        continue
                    rx = re.compile(area.commit_regex)
                    nums = self._area_numbers(area)
                    for line in commits:
                        h, date, subject = line.split("\x1f", 2)
                        ids = {int(next(g for g in m.groups() if g)) for m in rx.finditer(subject)
                               if any(m.groups())}
                        for i in ids:
                            if i in nums:
                                found.setdefault(nums[i], []).append(
                                    {"commit": h, "date": date, "subject": subject,
                                     "repo": repo.get("name", Path(repo["path"]).name), "url": repo.get("url", "")})
            changed = []
            for path, commits in sorted(found.items()):
                lines = []
                for c in sorted(commits, key=lambda c: c["date"]):
                    ref = f"[`{c['commit'][:7]}`]({c['url'].rstrip('/')}/commit/{c['commit']})" if c["url"] else f"`{c['commit'][:7]}`"
                    lines.append(f"- {ref} {c['date']} ({c['repo']}): {c['subject']}")
                text = self._abs(path).read_text(encoding="utf-8")
                new = replace_block(text, COMMITS_START, COMMITS_END, "\n".join(lines), "## Commits (automatisch)")
                if new != text:
                    self._write_file(path, new)
                    changed.append(path)
            commit = self._finish(changed, f"Commits verknüpft ({len(changed)} Notizen)", agent) if changed else None
            return {"notes_with_commits": len(found), "updated": changed, "commit": commit}

    # ------------------------------------------------------------ Prüfung (Idee 6)

    def lint(self, area_name: str | None = None, include_archive: bool = False) -> list[dict]:
        with self.lock:
            self.index.sync()
            areas = [self.config.area_by_name(area_name)] if area_name else self.config.areas
            out = []
            arch = self.config.archive_folders if not include_archive else []

            def archived(p: str) -> bool:
                return any(p.startswith(f + "/") or f"/{f}/" in p for f in arch)

            for b in self.index.broken_links():
                if area_name and not any(b["path"].startswith(a.folder.rsplit("/", 1)[0]) for a in areas):
                    continue
                if not archived(b["path"]):
                    out.append({"path": b["path"], "line": b["line"], "kind": "kaputter-link",
                                "message": f"Ziel nicht gefunden: [[{b['target']}]]"})
            claims = {c["path"] for c in self.claims()}
            for area in areas:
                prefix = area.folder.rstrip("/") + "/"
                notes = [r["path"] for r in self.index.db.execute(
                    "SELECT path FROM notes WHERE path LIKE ?", (prefix + "%",))]
                for p in notes:
                    if "/" in p[len(prefix):] or archived(p):
                        continue
                    text = self._abs(p).read_text(encoding="utf-8")
                    for v in sorted(self.violations(p, text)):
                        out.append({"path": p, "line": None, "kind": "regel", "message": v})
                    st = next((x for x in self.index.note_properties(p) if x["key_norm"] == "status"), None)
                    if st and st["value_norm"] == "in arbeit" and p not in claims:
                        out.append({"path": p, "line": st["line"], "kind": "claim",
                                    "message": "Status „in Arbeit“, aber keine aktive Reservierung"})
                if area.id_regex:
                    rx = re.compile(area.id_regex)
                    seen: dict[int, str] = {}
                    for p in notes:
                        if "/" in p[len(prefix):]:
                            continue
                        m = rx.search(PurePosixPath(p).name)
                        if not m:
                            continue
                        n = int(m.group(1))
                        if n in seen:
                            out.append({"path": p, "line": None, "kind": "doppelte-nummer",
                                        "message": f"Nummer {n} auch in {seen[n]}"})
                        seen[n] = p
                    if area.overview:
                        linked = {r["resolved_path"] for r in self.index.db.execute(
                            "SELECT l.resolved_path FROM links l JOIN notes n ON n.id = l.note_id WHERE n.path = ?",
                            (area.overview,))}
                        for p in seen.values():
                            if p not in linked:
                                out.append({"path": p, "line": None, "kind": "fehlt-in-uebersicht",
                                            "message": f"Nicht in {area.overview} verlinkt"})
            return out

    # ------------------------------------------------------------ Änderungen (Idee 4)

    def changes_since(self, since: str, include_archive: bool = True) -> dict:
        with self.lock:
            if not self.git.enabled:
                raise Rejected("Vault ist kein Git-Repo")
            base = self.git.resolve_since(since)
            out = []
            for status, path in self.git.changed_files(base):
                entry: dict = {"path": path, "change": {"A": "neu", "M": "geändert", "D": "gelöscht",
                                                         "R": "verschoben"}.get(status, status)}
                if path.endswith(".md") and status in ("M", "R") and self._abs(path).exists():
                    text = self._abs(path).read_text(encoding="utf-8")
                    spans = section_spans(text)
                    touched = []
                    for a, b in self.git.changed_lines(base, path):
                        for line in range(a, b + 1):
                            # innerster Abschnitt, der die (1-basierte) Zeile enthält
                            inner = [sp for sp in spans if sp.start < line <= sp.end]
                            if inner:
                                best = max(inner, key=lambda sp: (sp.level, sp.start))
                                if best.heading_path not in touched:
                                    touched.append(best.heading_path)
                    entry["sections"] = touched
                out.append(entry)
            commits = self.git.log(500, rev_range=f"{base}..HEAD")
            return {"since": base, "head": self.git.head(), "files": out,
                    "commits": [{k: c[k] for k in ("commit", "author", "date", "message")} for c in commits]}

    # ------------------------------------------------------------ Git-Abgleich

    def pull(self) -> dict:
        with self.lock:
            if not self.git.enabled or not self.config.git_pull_seconds:
                return {"pulled": False}
            before = self.git.head()
            self.git.pull(self.config.git_remote, self.config.git_branch)
            after = self.git.head()
            st = self.index.sync() if before != after else None
            if st:
                try:
                    files = [p for _s, p in self.git.changed_files(before)]
                except Exception:  # noqa: BLE001
                    files = st.changed_paths + st.removed_paths
                self._notify(files or ["."], "git pull")
            return {"pulled": before != after, "head": after,
                    "sync": {k: getattr(st, k) for k in ("added", "updated", "removed")} if st else None}

    def push(self) -> dict:
        with self.lock:
            if not (self.git.enabled and self.config.git_push and self.dirty_push):
                return {"pushed": False}
            try:
                self.git.pull(self.config.git_remote, self.config.git_branch)
            except Exception:
                pass
            self.git.push(self.config.git_remote, self.config.git_branch)
            self.dirty_push = False
            self.index.sync()
            return {"pushed": True}


class _Default(dict):
    def __missing__(self, key):
        return "–"
