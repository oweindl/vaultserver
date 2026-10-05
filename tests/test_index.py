from pathlib import Path

import pytest

from vaultserver.config import Config
from vaultserver.index import Index
from vaultserver.parser import parse_note

FIX = """---
aliases: [Fix 54]
tags: [bug]
---
# FIX-054 PDF-Export bricht ab

- **Status:** erledigt (Test)
- Priorität: hoch
- Link: https://example.com/x
- https://example.com/nackt
- a. sammeln: kein Schlüssel
- Ist: was heute passiert
- Soll (Oliver, 2026-10-05): anders
- Akzeptanzkriterien:
- [ ] Status: das ist eine Aufgabe, keine Eigenschaft

## Beschreibung

Beim Export großer Dateien bricht die Prüfung ab. #export

```
# kein Heading
- Status: nicht im Code
```

## Akzeptanzkriterien

- [x] Export läuft durch
- [ ] Prüfung grün

### Tests

Siehe [[Features/34-Signatur#Stufe 2|Feature 34]] und [Notiz](../Notiz%20A.md).
"""


@pytest.fixture
def vault(tmp_path: Path) -> Index:
    v = tmp_path / "vault"
    (v / "Fixliste").mkdir(parents=True)
    (v / "Features").mkdir()
    (v / "Archiv").mkdir()
    (v / "Bilder").mkdir()
    (v / ".obsidian").mkdir()
    (v / "Fixliste" / "FIX-054.md").write_text(FIX, encoding="utf-8")
    (v / "Fixliste" / "FIX-055.md").write_text("# FIX-055\n\n- Status: offen\n- Priorität: hoch\n", encoding="utf-8")
    (v / "Fixliste" / "FIX-056.md").write_text("# FIX-056\n\n- Status: offen\n- Priorität: niedrig\n", encoding="utf-8")
    (v / "Fixliste.md").write_text(
        "# Fixliste\n\n| Fix | Text |\n|---|---|\n| [[Fixliste/FIX-054\\|FIX-054]] | x |\n"
        "| [[Fix 54]] | Alias |\n| [[Gibt es nicht]] | kaputt |\n\n### [[FIX-055|FIX-055 · Titel]]\n", encoding="utf-8")
    (v / "Features" / "34-Signatur.md").write_text(
        "# Feature 34\n\n## Stufe 1\n\nText eins\n\n## Stufe 2\n\nText zwei Prüfung\n\n### Tests\n\nT\n\n## Stufe 3\n\nDrei\n",
        encoding="utf-8")
    (v / "Notiz A.md").write_text("# Notiz A\n\n![[bild.png]] Prüfung im Alltag\n", encoding="utf-8")
    (v / "Archiv" / "Alt.md").write_text("# Alt\n\nPrüfung von früher\n", encoding="utf-8")
    (v / "Bilder" / "bild.png").write_bytes(b"\x89PNG")
    (v / ".obsidian" / "x.md").write_text("ignorieren", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "idx.sqlite",
                 normalize={"status": {"erledigt (test)": "erledigt"}})
    idx = Index(cfg)
    idx.sync()
    yield idx
    idx.close()


def test_parser_properties_and_sections():
    n = parse_note(FIX, "FIX-054")
    assert n.title == "FIX-054 PDF-Export bricht ab"
    props = {(p.key, p.value) for p in n.properties}
    assert ("Status", "erledigt (Test)") in props
    assert ("Priorität", "hoch") in props
    assert ("Link", "https://example.com/x") in props
    assert not any(p.key == "https" for p in n.properties)
    assert not any(p.key.startswith("a.") for p in n.properties)
    assert ("Ist", "was heute passiert") in props
    assert ("Soll", "anders") in props and ("Akzeptanzkriterien", "") in props
    assert not any(p.value.startswith("nicht im Code") for p in n.properties)
    assert n.aliases == ["Fix 54"]
    assert n.tags == {"bug", "export"}
    assert [s.heading_path for s in n.sections] == [
        "FIX-054 PDF-Export bricht ab",
        "FIX-054 PDF-Export bricht ab > Beschreibung",
        "FIX-054 PDF-Export bricht ab > Akzeptanzkriterien",
        "FIX-054 PDF-Export bricht ab > Akzeptanzkriterien > Tests",
    ]
    assert [(t.text, t.done) for t in n.tasks][-2:] == [("Export läuft durch", True), ("Prüfung grün", False)]
    wl = next(l for l in n.links if l.kind == "wiki")
    assert (wl.target, wl.target_section, wl.alias) == ("Features/34-Signatur", "Stufe 2", "Feature 34")


def test_sync_incremental(vault: Index):
    st = vault.sync()
    assert st.unchanged == 7 and st.added == st.updated == 0
    p = vault.config.vault_path / "Fixliste" / "FIX-056.md"
    p.write_text("# FIX-056\n\n- Status: erledigt\n", encoding="utf-8")
    (vault.config.vault_path / "Notiz A.md").unlink()
    st = vault.sync()
    assert (st.updated, st.removed) == (1, 1)
    assert vault.stats()["notes"] == 6


def test_search_umlauts_archive_and_snippet(vault: Index):
    hits = vault.search("prufung")  # ohne Umlaut findet "Prüfung"
    paths = {h["path"] for h in hits}
    assert "Notiz A.md" in paths and "Archiv/Alt.md" not in paths
    assert "Archiv/Alt.md" in {h["path"] for h in vault.search("Prüfung", include_archive=True)}
    hit = next(h for h in hits if h["path"] == "Features/34-Signatur.md")
    assert hit["heading_path"] == "Feature 34 > Stufe 2"
    assert "«" in hit["snippet"]
    assert vault.search("Prüf") and vault.search('"; DROP') == []


def test_query_properties(vault: Index):
    res = vault.query([("Status", "!=", "erledigt"), ("priorität", "=", "hoch")], folder="Fixliste")
    assert [r["path"] for r in res] == ["Fixliste/FIX-055.md"]
    done = vault.query([("status", "=", "erledigt")])
    assert [r["path"] for r in done] == ["Fixliste/FIX-054.md"]  # normalisiert aus "erledigt (Test)"
    assert len(vault.query([("status", "missing", "")], folder="Fixliste")) == 0


def test_links_backlinks_broken(vault: Index):
    bl = vault.backlinks("Fixliste/FIX-054.md")
    assert {b["path"] for b in bl} == {"Fixliste.md"} and len(bl) == 2  # Pfad + Alias
    assert {b["path"] for b in vault.backlinks("Features/34-Signatur.md")} == {"Fixliste/FIX-054.md"}
    assert {b["path"] for b in vault.backlinks("Notiz A.md")} == {"Fixliste/FIX-054.md"}
    assert {b["path"] for b in vault.backlinks("Bilder/bild.png")} == {"Notiz A.md"}
    assert [b["target"] for b in vault.broken_links()] == ["Gibt es nicht"]


def test_outline_and_read_section(vault: Index):
    ol = vault.outline("Features/34-Signatur.md")
    assert [o["heading"] for o in ol] == ["Feature 34", "Stufe 1", "Stufe 2", "Tests", "Stufe 3"]
    r = vault.read("Features/34-Signatur.md", section="Stufe 2")
    assert r["text"].startswith("## Stufe 2") and "### Tests" in r["text"] and "Stufe 3" not in r["text"]
    assert len(r["version"]) == 16
    r = vault.read("Features/34-Signatur.md", lines=(1, 1))
    assert r["text"] == "# Feature 34"


def test_tasks_and_list(vault: Index):
    open_tasks = vault.tasks(folder="Fixliste")
    assert [t["text"] for t in open_tasks] == ["Status: das ist eine Aufgabe, keine Eigenschaft", "Prüfung grün"]
    tree = vault.list("", depth=1)
    assert "Fixliste" in tree["folders"] and ".obsidian" not in tree["folders"]
    assert {e["path"] for e in tree["entries"]} == {"Fixliste.md", "Notiz A.md"}


def test_canonical_rules(tmp_path: Path):
    import re
    cfg = Config(vault_path=tmp_path, db_path=tmp_path / "i.sqlite", canonical={"status": [
        (re.compile("ausgerollt|^produktiv"), "erledigt-ausgerollt"),
        (re.compile(r"^stufe\b.*erledigt \(test\)"), "teilweise-test"),
        (re.compile(r"^(rest\b.*)?erledigt \(test\)"), "erledigt-test"),
        (re.compile(r"^(offen|entwurf)\b"), r"\1"),
    ]})
    n = cfg.normalize_value
    assert n("status", "Erledigt (abgenommen, ausgerollt)") == "erledigt-ausgerollt"
    assert n("status", "Erledigt (Test) · commit `1e02f53`") == "erledigt-test"
    assert n("status", "Rest (Zoom) erledigt (Test) · merge x") == "erledigt-test"
    assert n("status", "Stufe 1 und 2 erledigt (Test)") == "teilweise-test"
    assert n("status", "offen (Option)") == "offen"
    assert n("status", "Irgendwas Neues") == "irgendwas neues"


def test_heading_links_count_as_backlinks(vault: Index):
    assert "Fixliste.md" in {b["path"] for b in vault.backlinks("Fixliste/FIX-055.md")}
    ol = vault.outline("Fixliste.md")
    assert ol[-1]["heading"] == "FIX-055 · Titel"


def test_render_tasks_wikilinks_embeds():
    from vaultserver.render import render
    text = "---\na: 1\n---\n# T\n\n- [ ] offen [[Ziel|Alias]]\n- [x] fertig\n\n![[bild.png]] ![[Fehlt]]\n\n`[[nicht]]`\n"
    res = {"Ziel": "Ordner/Ziel.md", "bild.png": "Bilder/bild.png"}
    html = render(text, "T.md", lambda t, k: res.get(t))
    assert 'data-line="6"' in html and 'data-line="7" checked' in html
    assert 'href="#/note/Ordner/Ziel.md"' in html and ">Alias</a>" in html
    assert '<img src="/api/file/Bilder/bild.png"' in html and "wikilink broken" in html
    assert "<code>[[nicht]]</code>" in html


def test_semantic_with_fake_embedder(vault: Index):
    from vaultserver.semantic import Semantic
    words = ["export", "prüfung", "signatur", "stufe", "alias", "fix"]

    def embed(texts):  # Wortzähler als Vektor: reicht, um Ranking und Cache zu prüfen
        return [[t.lower().count(w) + 0.01 for w in words] for t in texts]

    sem = Semantic(vault, "http://unbenutzt", "fake", embed=embed)
    n = sem.update()
    assert n == vault.stats()["sections"]
    assert sem.update() == 0  # unveränderte Abschnitte werden nicht neu berechnet
    hits = sem.search("signatur stufe", limit=2)
    assert hits[0]["path"] == "Features/34-Signatur.md"
