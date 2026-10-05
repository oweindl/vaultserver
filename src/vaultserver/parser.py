"""Markdown-Notiz in Abschnitte, Links, Eigenschaften, Tags und Aufgaben zerlegen.

Reine Funktionen ohne Datenbank, damit sie einzeln testbar sind.
Zeilennummern sind 1-basiert und beziehen sich auf die ganze Datei.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
TASK_RE = re.compile(r"^[ \t]*[-*+][ \t]+\[([ xX])\][ \t]+(.*)$")
# "- Schlüssel: Wert", auch "- **Schlüssel:** Wert"; Schlüssel max. 4 Wörter
PROP_RE = re.compile(
    r"^[-*+][ \t]+(?!\[[ xX]\])"       # nur Aufzählungen ohne Einrückung; eingerückte sind Unterpunkte
    r"([A-Za-zÄÖÜäöüß][\wÄÖÜäöüß./-]*(?:[ \t][\wÄÖÜäöüß./-]+){0,3})"
    r"(?:[ \t]*\([^)\n]*\))?"          # "- Ist (Oliver, 2026-10-05): …" -> Schlüssel "Ist"
    r"[ \t]*:(?:[ \t]+(\S.*))?[ \t]*$"   # leerer Wert erlaubt ("- Akzeptanzkriterien:" + Liste)
)
WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\n]+?)\]\]")
MDLINK_RE = re.compile(r"(!?)\[([^\]\n]*)\]\(([^)\s]+)\)")
TAG_RE = re.compile(r"(?<![\w/#&\[])#([A-Za-zÄÖÜäöüß][\wÄÖÜäöüß/-]*)")
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# Aufzählungsmarken wie "a. sammeln", "a.5", "B Filter" sind keine Schlüssel
ENUM_KEY_RE = re.compile(r"^[A-Za-zÄÖÜäöü](?:\.|\s|$)")


@dataclass
class Section:
    ord: int
    heading: str
    level: int
    heading_path: str
    line_start: int
    line_end: int
    text: str


@dataclass
class Link:
    section_ord: int
    line: int
    target: str          # Notiz-/Dateiziel ohne #Abschnitt, leer = selbe Notiz
    target_section: str  # Teil nach '#', sonst ""
    alias: str
    is_embed: bool
    kind: str            # "wiki" | "md"


@dataclass
class Property:
    key: str
    value: str
    source: str          # "frontmatter" | "inline"
    section_ord: int | None
    line: int | None


@dataclass
class Task:
    section_ord: int
    line: int
    text: str
    done: bool


@dataclass
class ParsedNote:
    title: str
    frontmatter: dict
    sections: list[Section] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    properties: list[Property] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    tasks: list[Task] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


def _split_frontmatter(lines: list[str]) -> tuple[dict, int]:
    """Gibt (frontmatter, Index der ersten Inhaltszeile) zurück."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            try:
                data = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                data = {}
            return (data if isinstance(data, dict) else {}), i + 1
    return {}, 0


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str) and "," in value:
        return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_wikilink(inner: str) -> tuple[str, str, str]:
    # In Tabellen wird der Alias-Strich als "\|" geschrieben
    inner = inner.replace("\\|", "|")
    target, _, alias = inner.partition("|")
    target, _, section = target.partition("#")
    return target.strip().rstrip("\\"), section.strip(), alias.strip()


def parse_note(text: str, stem: str) -> ParsedNote:
    lines = text.splitlines()
    fm, start = _split_frontmatter(lines)
    note = ParsedNote(title="", frontmatter=fm)

    for key, value in fm.items():
        k = str(key)
        if k.lower() in ("aliases", "alias"):
            note.aliases.extend(str(a) for a in _as_list(value))
        if k.lower() in ("tags", "tag"):
            note.tags.update(str(t).lstrip("#") for t in _as_list(value))
        for v in _as_list(value) if isinstance(value, list) else [value]:
            if v is None or isinstance(v, (dict, list)):
                continue
            note.properties.append(Property(k, _scalar(v), "frontmatter", None, None))

    # Abschnitte: Abschnitt 0 ist der Text vor der ersten Überschrift
    stack: list[tuple[int, str]] = []
    cur = Section(0, "", 0, "", start + 1, start, "")
    buf: list[str] = []
    in_fence = False
    first_h1 = ""

    def close(end_line: int) -> None:
        cur.line_end = end_line
        cur.text = "\n".join(buf).strip("\n")
        if cur.level > 0 or cur.text.strip():
            note.sections.append(cur)

    for idx in range(start, len(lines)):
        line = lines[idx]
        lineno = idx + 1
        if FENCE_RE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            close(lineno - 1)
            level = len(m.group(1))
            # Wikilinks in Überschriften als angezeigter Text: "### [[FIX-001|FIX-001 · X]]" -> "FIX-001 · X"
            heading = WIKILINK_RE.sub(lambda w: (_parse_wikilink(w.group(2))[2] or _parse_wikilink(w.group(2))[0]),
                                      m.group(2)).strip()
            if level == 1 and not first_h1:
                first_h1 = heading
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            cur = Section(
                ord=len(note.sections),
                heading=heading,
                level=level,
                heading_path=" > ".join(h for _, h in stack),
                line_start=lineno,
                line_end=lineno,
                text="",
            )
            buf = [line]
        else:
            buf.append(line)
        if in_fence:
            continue

        sord = cur.ord
        t = None if m else TASK_RE.match(line)
        if m:
            pass  # Überschrift: nur Links auswerten
        elif t:
            note.tasks.append(Task(sord, lineno, t.group(2).strip(), t.group(1) != " "))
        else:
            p = PROP_RE.match(line.replace("**", "").replace("__", ""))
            value = (p.group(2) or "").strip() if p else ""
            if p and not value.startswith("//") and not ENUM_KEY_RE.match(p.group(1)):
                note.properties.append(Property(p.group(1).strip(), value, "inline", sord, lineno))

        plain = INLINE_CODE_RE.sub("", line)
        for wm in WIKILINK_RE.finditer(plain):
            target, section, alias = _parse_wikilink(wm.group(2))
            note.links.append(Link(sord, lineno, target, section, alias, wm.group(1) == "!", "wiki"))
        for mm in MDLINK_RE.finditer(plain):
            href = mm.group(3)
            if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):
                continue  # externe URL
            target, _, section = href.partition("#")
            note.links.append(
                Link(sord, lineno, target.replace("%20", " "), section, mm.group(2), mm.group(1) == "!", "md")
            )
        if not m:
            note.tags.update(m.group(1) for m in TAG_RE.finditer(WIKILINK_RE.sub("", plain)))

    close(len(lines))
    note.title = str(fm.get("title") or first_h1 or stem)
    return note
