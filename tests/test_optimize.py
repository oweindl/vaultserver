"""„Optimieren“: große Notiz an Überschriften aufteilen, prüfen, ausführen."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vaultserver.config import Area, Config
from vaultserver.web import create_app, hash_password

BIG = """---
Status: offen
---
# Großes Dokument

Das ist die Einleitung. Sie erklärt alles.

![Bild](bild.png)

## Kapitel A
Text A mit [[#Kapitel B]] Verweis.
### Unterpunkt A1
Mehr A ^block1

## Kapitel B
```
## kein Kapitel
```
Text B [Link](x.md)

## Kapitel A
Doppelte Überschrift
"""


@pytest.fixture
def client(tmp_path: Path):
    v = tmp_path / "vault"
    (v / "Moon").mkdir(parents=True)
    (v / "Moon" / "Groß.md").write_text(BIG, encoding="utf-8")
    (v / "Moon" / "bild.png").write_bytes(b"\x89PNG")
    (v / "Moon" / "x.md").write_text("# x\n", encoding="utf-8")
    (v / "Moon" / "Andere.md").write_text("Siehe [[Groß#Kapitel B]], [[Groß#Unterpunkt A1|hier]], [[Groß#^block1]] und [[Groß]].\n", encoding="utf-8")
    (v / "Moon" / "Fixliste").mkdir()
    (v / "Moon" / "Fixliste" / "FIX-001.md").write_text("# FIX-001\n\n## A\nx\n## B\ny\n", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite", users={"oliver": hash_password("altes-passwort")},
                 tokens={}, git_commit=False, git_pull_seconds=0, watch_seconds=0,
                 areas=[Area(name="Fixliste", folder="Moon/Fixliste", overview="Moon/Fixliste.md")])
    c = TestClient(create_app(cfg, start_background=False))
    c.__enter__()
    assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
    yield c, v
    c.__exit__(None, None, None)


def test_plan_and_apply(client):
    c, v = client
    p = c.post("/api/optimize/plan", json={"path": "Moon/Groß.md"}).json()
    assert p["level"] == 2 and p["levels"] == {"2": 3, "3": 1}
    assert [x["path"] for x in p["parts"]] == ["Moon/Groß/00 Einleitung.md", "Moon/Groß/01 Kapitel A.md",
                                              "Moon/Groß/02 Kapitel B.md", "Moon/Groß/03 Kapitel A.md"]
    assert p["check"]["ok"] and p["check"]["covered"] == p["check"]["total"] and not p["blocking"]
    parts = {x["path"]: x["content"] for x in p["parts"]}
    assert "![Bild](../bild.png)" in parts["Moon/Groß/00 Einleitung.md"]                 # relativer Link eine Ebene höher
    assert "[[Moon/Groß/02 Kapitel B|Kapitel B]]" in parts["Moon/Groß/01 Kapitel A.md"]    # Link innerhalb der Notiz
    assert "## kein Kapitel" in parts["Moon/Groß/02 Kapitel B.md"]                         # Code-Block nicht geteilt
    idx = p["index"]["content"]
    assert idx.startswith("---\nStatus: offen\n---\n# Großes Dokument") and "[[Moon/Groß/01 Kapitel A|Kapitel A]]" in idx
    assert p["description"] == "Das ist die Einleitung. Sie erklärt alles."
    assert p["links"] == [{"note": "Moon/Andere.md", "changes": 3}]
    # andere Ebene, eigene Beschreibung
    p3 = c.post("/api/optimize/plan", json={"path": "Moon/Groß.md", "level": 3, "description": "Meine Kurzbeschreibung"}).json()
    assert len(p3["parts"]) == 5 and p3["check"]["ok"] and "Meine Kurzbeschreibung" in p3["index"]["content"]
    # Ausführen: veraltete Version -> Konflikt, sonst Teile + Inhaltsverzeichnis, Original im Papierkorb
    assert c.post("/api/optimize/apply", json={"path": "Moon/Groß.md", "base_version": "alt"}).status_code == 409
    r = c.post("/api/optimize/apply", json={"path": "Moon/Groß.md", "base_version": p["version"], "description": "Kurz"}).json()
    assert len(r["parts"]) == 4 and r["updated_notes"] == ["Moon/Andere.md"]
    assert (v / "Moon" / "Groß" / "02 Kapitel B.md").is_file()
    assert "Kurz" in (v / "Moon" / "Groß.md").read_text(encoding="utf-8")
    bin_ = c.get("/api/trash").json()
    assert bin_[0]["original"] == "Moon/Groß.md"
    assert (v / "RecycleBin" / bin_[0]["id"] / "Moon" / "Groß.md").read_text(encoding="utf-8") == BIG
    andere = (v / "Moon" / "Andere.md").read_text(encoding="utf-8")
    assert "[[Moon/Groß/02 Kapitel B]]" in andere and "[[Moon/Groß/01 Kapitel A#Unterpunkt A1|hier]]" in andere
    assert "[[Moon/Groß/01 Kapitel A#^block1]]" in andere and "[[Groß]]" in andere
    assert not [x for x in c.get("/api/lint").json() if x["kind"] == "kaputter-link"]
    # noch einmal geht nicht: Ordner existiert
    p2 = c.post("/api/optimize/plan", json={"path": "Moon/Groß.md"})
    assert p2.status_code == 422 or p2.json()["blocking"]


def test_rejections(client):
    c, v = client
    assert c.post("/api/optimize/plan", json={"path": "Moon/Fixliste/FIX-001.md"}).status_code == 422
    (v / "Moon" / "Flach.md").write_text("Nur Text ohne Überschriften.\n", encoding="utf-8")
    c.post("/api/refresh", json={})
    assert c.post("/api/optimize/plan", json={"path": "Moon/gibtsnicht.md"}).status_code == 404


def test_titles_with_markdown(client):
    c, v = client
    (v / "Moon" / "Code.md").write_text("# T\n\n## Branch `fix-1` | neu\nA\n\n## [Zwei]\nB\n", encoding="utf-8")
    c.post("/api/refresh", json={})
    p = c.post("/api/optimize/plan", json={"path": "Moon/Code.md"}).json()
    assert [x["path"] for x in p["parts"]] == ["Moon/Code/01 Branch fix-1 neu.md", "Moon/Code/02 Zwei.md"]
    assert "[[Moon/Code/01 Branch fix-1 neu|Branch fix-1 neu]]" in p["index"]["content"] and p["check"]["ok"]
