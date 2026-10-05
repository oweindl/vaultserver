"""Markdown -> HTML für den Web-Viewer: Wikilinks, Einbettungen, anklickbare Checkboxen."""

from __future__ import annotations

import html
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.front_matter import front_matter_plugin

from .parser import FENCE_RE, WIKILINK_RE, _parse_wikilink

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
TASK_RE = re.compile(r"^\[([ xX])\]\s")


def _slug(s: str) -> str:
    return re.sub(r"[^\w\- ]", "", s.lower()).strip().replace(" ", "-")


def _md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": False})
    md.enable("table").enable("strikethrough")
    md.use(front_matter_plugin)
    md.use(anchors_plugin, min_level=1, max_level=6, slug_func=_slug)

    def tasks(state):
        toks = state.tokens
        for i, t in enumerate(toks):
            if t.type != "inline" or i < 2 or toks[i - 2].type != "list_item_open":
                continue
            m = TASK_RE.match(t.content)
            if not m or not t.children:
                continue
            first = t.children[0]
            if first.type == "text" and TASK_RE.match(first.content):
                line = (t.map[0] + 1) if t.map else 0
                checked = " checked" if m.group(1) != " " else ""
                box = Token("html_inline", "", 0)
                box.content = f'<input type="checkbox" class="task" data-line="{line}"{checked}> '
                first.content = first.content[m.end():]
                t.children.insert(0, box)
                toks[i - 2].attrSet("class", "task-item")

    md.core.ruler.push("vault_tasks", tasks)
    return md


_MD = _md()


def render(text: str, note_path: str, resolve) -> str:
    """resolve(target, kind) -> Pfad im Vault oder None."""

    def link(target: str, section: str, alias: str, embed: bool) -> str:
        path = resolve(target, "wiki") if target else note_path
        label = alias or (f"{target}#{section}" if section else target) or section
        if path is None:
            return f'<a class="wikilink broken" title="Nicht gefunden">{html.escape(label)}</a>'
        ext = PurePosixPath(path).suffix.lower()
        url = "/api/file/" + quote(path)
        if embed and ext in IMAGE_EXT:
            return f'<img src="{url}" alt="{html.escape(alias or target)}" loading="lazy">'
        if embed and ext in (".html", ".htm"):
            return f'<iframe class="mockup" src="{url}" sandbox="allow-scripts" loading="lazy"></iframe>'
        if embed and ext == ".pdf":
            return f'<iframe class="pdf" src="{url}" loading="lazy"></iframe>'
        if ext != ".md":
            return f'<a class="wikilink file" href="{url}" target="_blank">{html.escape(label)}</a>'
        anchor = f"#{_slug(section)}" if section else ""
        return (f'<a class="wikilink" href="#/note/{quote(path)}{anchor}" data-path="{html.escape(path)}">'
                f"{html.escape(label)}</a>")

    out, in_fence = [], False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        if not in_fence and "[[" in line:
            def sub(m):
                target, section, alias = _parse_wikilink(m.group(2))
                return link(target, section, alias, m.group(1) == "!")
            # Inline-Code nicht anfassen
            parts = re.split(r"(`[^`]*`)", line)
            line = "".join(p if p.startswith("`") else WIKILINK_RE.sub(sub, p) for p in parts)
        out.append(line)
    rendered = _MD.render("\n".join(out))

    # relative Markdown-Bilder/Links auf Vault-Dateien umbiegen
    def fix(m):
        attr, val = m.group(1), m.group(2)
        if re.match(r"^([a-z][a-z0-9+.-]*:|#|/)", val, re.I):
            return m.group(0)
        target = val.split("#")[0].replace("%20", " ")
        path = resolve(target, "md")
        if not path:
            return m.group(0)
        if path.endswith(".md"):
            return f'{attr}="#/note/{quote(path)}"'
        return f'{attr}="/api/file/{quote(path)}"'

    return re.sub(r'(href|src)="([^"]+)"', fix, rendered)
