"""„Optimieren“: eine große Notiz an den Überschriften in kleine Notizen aufteilen.

Aus `Ordner/Name.md` wird
- `Ordner/Name/00 Einleitung.md` (Text vor der ersten Überschrift, falls vorhanden),
- `Ordner/Name/01 <Überschrift>.md`, `02 …` je Abschnitt der gewählten Ebene,
- eine neue `Ordner/Name.md` mit Frontmatter und Titel des Originals, einer kurzen Beschreibung und dem
  Inhaltsverzeichnis. Der Name bleibt gleich, Links auf `[[Name]]` funktionieren weiter.
Das Original geht in den Papierkorb. Links auf Abschnitte (`[[Name#Kapitel]]`, auch `[[#Kapitel]]` innerhalb
der Notiz) werden auf die neue Teildatei umgeschrieben, relative Markdown-Links in den Teilen eine Ebene
höher gesetzt.

`plan` ändert nichts und liefert alles für die Vorschau, inklusive Vollständigkeitsprüfung: Jede nicht leere
Zeile des Originals muss genau einmal und in derselben Reihenfolge in den neuen Dateien stehen (Linkziele
werden beim Vergleich ausgeblendet, nur der sichtbare Text zählt).
"""
from __future__ import annotations

import difflib
import re
import time
from pathlib import PurePosixPath

from .index import Index
from .parser import FENCE_RE, HEADING_RE, MDLINK_RE, WIKILINK_RE, _split_frontmatter
from .store import Conflict, Rejected

BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]`]')
BLOCK_ID = re.compile(r"\s\^([A-Za-z0-9-]+)\s*$")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def _headings(lines: list[str], start: int) -> list[tuple[int, int, str]]:
    """(Zeile, Ebene, Text) aller Überschriften außerhalb von Code-Blöcken."""
    out, fence = [], False
    for i in range(start, len(lines)):
        if FENCE_RE.match(lines[i]):
            fence = not fence
            continue
        if not fence:
            m = HEADING_RE.match(lines[i])
            if m:
                out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def _file_name(title: str) -> str:
    t = re.sub(r"\s+", " ", BAD_CHARS.sub(" ", title)).strip().strip(".")
    return t[:70].rstrip() or "Abschnitt"


def _alias(title: str) -> str:
    """Überschrift als Linktext: ohne Markdown-Zeichen, die einen Wikilink stören."""
    t = re.sub(r"\s+", " ", re.sub(r"[`\[\]|]", " ", title).replace("**", "")).strip()
    return t or "Abschnitt"


def _key(heading: str) -> str:
    return re.sub(r"\s+", " ", heading).strip().lower()


def _label(m: re.Match) -> str:
    """Sichtbarer Text eines Wikilinks (für die Prüfung): Alias, sonst Abschnitt, sonst Name."""
    body = m.group(2).replace("\\|", "|")
    target, _, alias = body.partition("|")
    tgt, _, sec = target.partition("#")
    return f"{m.group(1)}[[{alias or sec.lstrip('^') or PurePosixPath(tgt).name}]]"


def _norm(line: str) -> str:
    line = WIKILINK_RE.sub(_label, line)
    return MDLINK_RE.sub(lambda m: f"{m.group(1)}[{m.group(2)}]()", line).rstrip()


def _summary(lines: list[str]) -> str:
    for ln in lines:
        s = ln.strip()
        if not s or HEADING_RE.match(s) or s.startswith(("```", "~~~", "|", "---")):
            continue
        s = re.sub(r"^([-*+>]|\d+\.)\s+", "", s)
        s = WIKILINK_RE.sub(lambda m: _label(m)[2:-2], s)
        s = MDLINK_RE.sub(lambda m: m.group(2), s)
        s = re.sub(r"[*_`]", "", s).strip()
        if s:
            return s if len(s) <= 110 else s[:107].rstrip() + " …"
    return ""


class Optimizer:
    def __init__(self, store):
        self.store = store
        self.config = store.config

    # ------------------------------------------------------------ Analyse

    def plan(self, path: str, level: int | None = None, description: str | None = None) -> dict:
        st = self.store
        path = st._norm_path(path)
        if not path.endswith(".md"):
            raise Rejected("Nur Notizen (.md) lassen sich optimieren")
        if st._in_bin(path):
            raise Rejected("Notizen im Papierkorb lassen sich nicht optimieren")
        area = self.config.area_for(path)
        if area:
            raise Rejected(f"Einträge im Bereich „{area.name}“ bleiben eine Datei (Bereichsregeln) – nicht aufteilbar")
        text, version = st.current(path)
        if text is None:
            raise KeyError(f"Notiz nicht gefunden: {path}")
        lines = text.split("\n")
        _, body = _split_frontmatter(lines)
        hs = _headings(lines, body)
        counts: dict[int, int] = {}
        for _, lv, _t in hs:
            counts[lv] = counts.get(lv, 0) + 1
        if not hs:
            raise Rejected("Die Notiz hat keine Überschriften – nichts zum Aufteilen")

        # Titel: genau eine #-Überschrift am Anfang (nur Leerzeilen davor) gilt als Titel der Notiz
        title_idx = None
        first = hs[0]
        if (first[1] == 1 and counts.get(1) == 1
                and all(not lines[i].strip() for i in range(body, first[0]))):
            title_idx = first[0]
        usable = {lv: n for lv, n in counts.items() if not (title_idx is not None and lv == 1)}
        if level is None:
            level = min((lv for lv, n in usable.items() if n >= 2), default=min(usable, default=None))
        if level is None:
            raise Rejected("Unter dem Titel gibt es keine weiteren Überschriften – nichts zum Aufteilen")
        bounds = [h for h in hs if h[1] <= level and h[0] != title_idx]
        if not bounds:
            raise Rejected(f"Keine Überschrift der Ebene {'#' * level} – andere Ebene wählen")

        folder = path[:-3]
        stem = PurePosixPath(folder).name
        intro = [i for i in range(body, bounds[0][0]) if i != title_idx]
        parts: list[dict] = []
        used: set[str] = set()

        def add(title: str, idx: list[int], nr: int):
            name = f"{nr:02d} {_file_name(title)}"
            base, k = name, 2
            while name.lower() in used:
                name, k = f"{base} ({k})", k + 1
            used.add(name.lower())
            parts.append({"title": title, "path": f"{folder}/{name}.md", "idx": idx})

        if any(lines[i].strip() for i in intro):
            add("Einleitung", intro, 0)
        for n, (start, _lv, title) in enumerate(bounds):
            end = bounds[n + 1][0] if n + 1 < len(bounds) else len(lines)
            add(title, list(range(start, end)), n + 1)

        # Welche Überschrift / welcher Block-Anker liegt in welchem Teil
        where: dict[str, str] = {}
        tops: dict[str, str] = {}
        for p in parts:
            for i in p["idx"]:
                m = HEADING_RE.match(lines[i])
                if m:
                    where.setdefault(_key(m.group(2)), p["path"])
                b = BLOCK_ID.search(lines[i])
                if b:
                    where.setdefault("^" + b.group(1), p["path"])
            if p["title"] != "Einleitung":
                tops[_key(p["title"])] = p["path"]

        def target_for(sec: str) -> tuple[str, str] | None:
            """(neuer Pfad ohne .md, Abschnitt oder "") für einen Abschnitt der alten Notiz."""
            k = sec.strip() if sec.strip().startswith("^") else _key(sec)
            p = where.get(k)
            if not p:
                return None
            return p[:-3], ("" if tops.get(k) == p else sec)

        # Teile: Inhalt, Links innerhalb der Notiz, relative Markdown-Links eine Ebene höher
        def adjust(line: str, own: str) -> str:
            def wiki(m: re.Match) -> str:
                body_ = m.group(2).replace("\\|", "|")
                target, sep, alias = body_.partition("|")
                tgt, hsep, sec = target.partition("#")
                if tgt.strip() or not hsep:
                    return m.group(0)
                t = target_for(sec)
                if not t or t[0] + ".md" == own:
                    return m.group(0)
                new = t[0] + (f"#{t[1]}" if t[1] else "")
                return f"{m.group(1)}[[{new}|{alias or _alias(sec.lstrip('^'))}]]"

            def md(m: re.Match) -> str:
                href = m.group(3)
                if SCHEME.match(href) or href.startswith(("/", "#")):
                    return m.group(0)
                return f"{m.group(1)}[{m.group(2)}](../{href})"
            if FENCE_RE.match(line):
                return line
            line = WIKILINK_RE.sub(wiki, line)
            return MDLINK_RE.sub(md, line)

        for p in parts:
            fence, new_lines = False, []
            for i in p["idx"]:
                ln = lines[i]
                if FENCE_RE.match(ln):
                    fence = not fence
                    new_lines.append(ln)
                    continue
                new_lines.append(ln if fence else adjust(ln, p["path"]))
            while new_lines and not new_lines[-1].strip():
                new_lines.pop()
            while new_lines and not new_lines[0].strip():
                new_lines.pop(0)
            p["content"] = "\n".join(new_lines) + "\n"
            p["lines"] = [p["idx"][0] + 1, p["idx"][-1] + 1] if p["idx"] else None
            p["summary"] = _summary([lines[i] for i in p["idx"]][1 if p["title"] != "Einleitung" else 0:])

        # neue Inhaltsverzeichnis-Datei
        kept = list(range(0, body)) + ([title_idx] if title_idx is not None else [])
        intro_text = [lines[i] for i in intro if lines[i].strip()]
        default_desc = _summary(intro_text) if intro_text else ""
        if not default_desc:
            names = ", ".join(_alias(p["title"]) for p in parts if p["title"] != "Einleitung")
            default_desc = f"Inhalt: {names}." if len(names) < 300 else f"{len(parts)} Abschnitte."
        desc = default_desc if description is None else description.strip()
        index_lines = [lines[i] for i in range(0, body)]
        index_lines.append(lines[title_idx] if title_idx is not None else f"# {stem}")
        index_lines += ["", desc, "", "## Inhalt", ""] if desc else ["", "## Inhalt", ""]
        for p in parts:
            index_lines.append(f"- [[{p['path'][:-3]}|{_alias(p['title'])}]]" + (f" – {p['summary']}" if p["summary"] else ""))
        index_lines += ["", f"*Aufgeteilt am {time.strftime('%d.%m.%Y')} in {len(parts)} Teile; "
                            "die ursprüngliche Datei liegt im Papierkorb.*", ""]
        index_content = "\n".join(index_lines)

        # Prüfung: Inhalt des Originals = Frontmatter/Titel der neuen Notiz + alle Teile (sichtbarer Text)
        expected = [(i + 1, _norm(lines[i])) for i in range(len(lines)) if lines[i].strip()]
        actual = [_norm(lines[i]) for i in kept if lines[i].strip()]
        for p in parts:
            actual += [_norm(x) for x in p["content"].split("\n") if x.strip()]
        sm = difflib.SequenceMatcher(None, [e[1] for e in expected], actual, autojunk=False)
        missing, extra = [], []
        for op, a1, a2, b1, b2 in sm.get_opcodes():
            if op in ("delete", "replace"):
                missing += [{"line": expected[k][0], "text": expected[k][1][:160]} for k in range(a1, a2)]
            if op in ("insert", "replace"):
                extra += [x[:160] for x in actual[b1:b2]]
        covered = sum(b.size for b in sm.get_matching_blocks())

        # Links aus anderen Notizen auf Abschnitte
        other = self._other_links(path, target_for)

        blocking = []
        if (self.config.vault_path / folder).exists():
            blocking.append(f"Den Ordner „{folder}“ gibt es schon")
        if len(parts) < 2:
            blocking.append("Es entstünde nur ein Teil – andere Ebene wählen")
        return {
            "path": path, "version": version, "level": level,
            "levels": {str(lv): n for lv, n in sorted(usable.items())},
            "folder": folder, "description": desc, "default_description": default_desc,
            "index": {"path": path, "content": index_content},
            "parts": [{k: p[k] for k in ("path", "title", "content", "lines", "summary")} for p in parts],
            "original": text,
            "check": {"ok": not missing and not extra, "total": len(expected), "covered": covered,
                      "missing": missing[:50], "extra": extra[:50], "files": len(parts) + 1},
            "links": [{"note": n, "changes": len(ch)} for n, (_t, ch) in other.items()],
            "blocking": blocking,
            "_other": other,
        }

    def _other_links(self, path: str, target_for) -> dict[str, tuple[str, list[str]]]:
        """Andere Notizen mit Links auf Abschnitte dieser Notiz -> (neuer Text, geänderte Links)."""
        st, idx = self.store, self.store.index
        files = [r["path"] for r in idx.db.execute("SELECT path FROM notes UNION SELECT path FROM attachments")]
        by_lower = {f.lower(): f for f in files}
        by_name: dict[str, list[str]] = {}
        for f in files:
            n = PurePosixPath(f).name.lower()
            by_name.setdefault(n, []).append(f)
            if n.endswith(".md"):
                by_name.setdefault(n[:-3], []).append(f)
        aliases = {r["alias_norm"]: r["path"] for r in idx.db.execute(
            "SELECT a.alias_norm, n.path FROM aliases a JOIN notes n ON n.id = a.note_id")}
        out = {}
        for note in sorted({b["path"] for b in st._raw_backlinks(path)} - {path}):
            text = (self.config.vault_path / note).read_text(encoding="utf-8")
            changes: list[str] = []

            def wiki(m: re.Match) -> str:
                body_ = m.group(2).replace("\\|", "|")
                target, _sep, alias = body_.partition("|")
                tgt, hsep, sec = target.partition("#")
                if not hsep or not tgt.strip():
                    return m.group(0)
                if Index._resolve(tgt.strip(), "wiki", note, by_lower, by_name, aliases) != path:
                    return m.group(0)
                t = target_for(sec)
                if not t:
                    return m.group(0)
                new = t[0] + (f"#{t[1]}" if t[1] else "") + (f"|{alias}" if alias else "")
                changes.append(f"[[{body_}]] → [[{new}]]")
                return f"{m.group(1)}[[{new}]]"
            res, fence = [], False
            for ln in text.split("\n"):
                if FENCE_RE.match(ln):
                    fence = not fence
                res.append(ln if fence else WIKILINK_RE.sub(wiki, ln))
            if changes:
                out[note] = ("\n".join(res), changes)
        return out

    # ------------------------------------------------------------ Ausführen

    def apply(self, path: str, agent: str, base_version: str, level: int | None = None,
              description: str | None = None) -> dict:
        st = self.store
        with st.lock:
            p = self.plan(path, level, description)
            if p["version"] != base_version:
                text, v = st.current(p["path"])
                raise Conflict(p["path"], v, text, "Die Notiz wurde inzwischen geändert – Vorschau neu laden")
            if p["blocking"]:
                raise Rejected("; ".join(p["blocking"]))
            if not p["check"]["ok"]:
                raise Rejected("Die Prüfung ist nicht vollständig – nichts geändert")
            trashed = st.trash(p["path"], agent, base_version=base_version,
                               message=f"Optimieren: {p['path']} in den Papierkorb")
            written = []
            for part in p["parts"]:
                st._write_file(part["path"], part["content"])
                written.append(part["path"])
            st._write_file(p["path"], p["index"]["content"])
            written.append(p["path"])
            for note, (text, _ch) in p["_other"].items():
                st._write_file(note, text)
                written.append(note)
            commit = st._finish(written, f"Optimiert: {p['path']} → {len(p['parts'])} Teile in {p['folder']}/", agent)
            return {"path": p["path"], "folder": p["folder"], "parts": [x["path"] for x in p["parts"]],
                    "trashed": trashed["id"], "updated_notes": list(p["_other"]), "commit": commit,
                    "version": st.current(p["path"])[1]}
