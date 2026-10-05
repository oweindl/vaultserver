import re
import subprocess
from pathlib import Path

import pytest

from vaultserver.config import Area, Config
from vaultserver.store import Conflict, Rejected, Store

FIXLISTE = """# Fixliste

## Regeln für Claude Code

1. Ohne Rückfragen arbeiten.

## Offen

### [[FIX-001|FIX-001 · Erster]]
Priorität: hoch · Status: offen

Kurz.

## Erledigt

Nichts.
"""

FIX1 = """# FIX-001 · Erster

> Zurück zur Übersicht: [[Fixliste]]

- Priorität: hoch
- Bereich: Suche
- Status: offen
- Ist: kaputt
- Soll: heil
- Akzeptanzkriterien:
  - [ ] geht
- Wenn unklar: fragen

## Umsetzung

Noch nichts.

## Tests

Keine.
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def store(tmp_path: Path) -> Store:
    v = tmp_path / "vault"
    (v / "CMT" / "Fixliste").mkdir(parents=True)
    (v / "CMT" / "Fixliste.md").write_text(FIXLISTE, encoding="utf-8")
    (v / "CMT" / "Fixliste" / "FIX-001.md").write_text(FIX1, encoding="utf-8")
    (v / "Notiz.md").write_text("# Notiz\n\nSiehe [[FIX-001]] und [[CMT/Fixliste/FIX-001#Tests|Tests]].\n"
                                "| a | [[FIX-001\\|Tabelle]] |\n![[bild.png]]\n", encoding="utf-8")
    (v / "bild.png").write_bytes(b"\x89PNG")
    git(v, "init", "-q", "-b", "main")
    git(v, "add", "-A")
    git(v, "commit", "-q", "-m", "start")
    area = Area(
        name="Fixliste", folder="CMT/Fixliste", overview="CMT/Fixliste.md",
        rules_section="Regeln für Claude Code", id_regex=r"^FIX-(\d+)", filename="FIX-{n:03d}.md",
        heading="# FIX-{n:03d} · {title}", template="> Zurück zur Übersicht: [[Fixliste]]\n\n{fields}\n",
        overview_entry="### [[{stem}|{stem} · {title}]]\nPriorität: {priorität} · Status: {status}\n\n{summary}",
        overview_section="Offen", commit_regex=r"\bFIX-(\d{3})\b",
        required=["Priorität", "Bereich", "Status", "Ist", "Soll", "Wenn unklar"],
        defaults={"Status": "offen", "Umsetzung": "(füllt Claude Code aus)"},
        allowed={"status": ["offen", "in arbeit", "erledigt (test)", "produktiv"],
                 "priorität": ["hoch", "mittel", "niedrig"]},
    )
    cfg = Config(vault_path=v.resolve(), db_path=tmp_path / "idx.sqlite", areas=[area],
                 canonical={"status": [(re.compile(r"erledigt \(test\)"), "erledigt (test)")]},
                 groups={"status": {"erledigt": ["erledigt (test)", "produktiv"]}},
                 status_note="Status.md")
    s = Store(cfg)
    s.index.sync()
    yield s
    s.index.close()


def log(store: Store) -> list[str]:
    return git(store.config.vault_path, "log", "--format=%an|%s").splitlines()


def test_write_requires_version_and_detects_conflict(store: Store):
    path = "Notiz.md"
    r = store.read_tracked(path)
    with pytest.raises(Conflict):
        store.write(path, "neu", "ubuntu1/cc")  # ohne base_version
    res = store.write(path, r["text"] + "\nmehr\n", "ubuntu1/cc", base_version=r["version"], message="mehr")
    assert res["commit"] and log(store)[0] == "ubuntu1/cc|ubuntu1/cc: mehr"
    with pytest.raises(Conflict) as e:
        store.write(path, "alt", "ubuntu2/cc", base_version=r["version"])
    assert e.value.version == res["version"] and "mehr" in e.value.text


def test_new_note_and_path_safety(store: Store):
    res = store.write("Neu/Idee", "# Idee\n", "web/oliver")
    assert res["created"] and res["path"] == "Neu/Idee.md"
    assert store.index.search("Idee")[0]["path"] == "Neu/Idee.md"
    for bad in ("../x.md", ".git/config", "a/../../b.md"):
        with pytest.raises(Rejected):
            store.write(bad, "x", "web/oliver")


def test_patch_section_modes_and_section_level_conflicts(store: Store):
    p = "CMT/Fixliste/FIX-001.md"
    v0 = store.read_tracked(p)["version"]
    r1 = store.patch_section(p, "Umsetzung", "Commit abc, getestet.", "ubuntu1/cc", base_version=v0)
    text = store.read_tracked(p)["text"]
    assert "## Umsetzung\nCommit abc, getestet.\n\n## Tests" in text
    # anderer Agent mit altem Stand ändert einen ANDEREN Abschnitt: kein Konflikt
    r2 = store.patch_section(p, "Tests", "- Zeile", "ubuntu2/cc", base_version=v0, mode="append")
    assert "Keine.\n- Zeile" in store.read_tracked(p)["text"]
    # … aber derselbe Abschnitt mit altem Stand: Konflikt
    with pytest.raises(Conflict):
        store.patch_section(p, "Umsetzung", "überschrieben", "ubuntu2/cc", base_version=v0)
    r3 = store.patch_section(p, "Tests", "## Ausblick\n\nSpäter.", "ubuntu1/cc",
                             base_version=r2["version"], mode="insert_after")
    assert store.read_tracked(p)["text"].rstrip().endswith("## Ausblick\n\nSpäter.")
    assert r1["commit"] != r3["commit"]


def test_set_property_inline_frontmatter_and_rules(store: Store):
    p = "CMT/Fixliste/FIX-001.md"
    store.set_property(p, "Status", "erledigt (Test)", "ubuntu1/cc")
    assert "- Status: erledigt (Test)\n" in store.read_tracked(p)["text"]
    with pytest.raises(Rejected) as e:
        store.set_property(p, "Status", "fertig irgendwie", "ubuntu1/cc")
    assert "status" in e.value.problems[0]
    store.set_property(p, "Owner", "Oliver", "ubuntu1/cc")
    assert "- Wenn unklar: fragen\n- Owner: Oliver" in store.read_tracked(p)["text"]
    store.write("FM.md", "---\nstatus: offen\n---\n# FM\n", "x")
    store.set_property("FM.md", "status", "fertig", "x")
    assert store.read_tracked("FM.md")["text"].startswith("---\nstatus: fertig\n---")


def test_removing_required_field_is_rejected(store: Store):
    p = "CMT/Fixliste/FIX-001.md"
    r = store.read_tracked(p)
    with pytest.raises(Rejected) as e:
        store.write(p, r["text"].replace("- Soll: heil\n", ""), "x", base_version=r["version"])
    assert e.value.problems == ["Pflichtfeld fehlt: Soll"]


def test_move_rewrites_links(store: Store):
    res = store.move("CMT/Fixliste/FIX-001.md", "CMT/Fixliste/FIX-099.md", "web/oliver")
    note = (store.config.vault_path / "Notiz.md").read_text(encoding="utf-8")
    assert "[[FIX-099]]" in note and "[[CMT/Fixliste/FIX-099#Tests|Tests]]" in note
    assert "[[FIX-099\\|Tabelle]]" in note
    assert "[[FIX-099|FIX-001 · Erster]]" in (store.config.vault_path / "CMT/Fixliste.md").read_text()
    assert set(res["updated_notes"]) == {"Notiz.md", "CMT/Fixliste.md"}
    assert store.index.broken_links() == []
    assert len(git(store.config.vault_path, "show", "--stat", "--format=", "HEAD").strip().splitlines()) >= 3
    store.move("bild.png", "Bilder/bild.png", "web/oliver")
    assert "![[bild.png]]" in (store.config.vault_path / "Notiz.md").read_text()  # Name eindeutig
    assert store.index.broken_links() == []


def test_create_from_template(store: Store):
    with pytest.raises(Rejected) as e:
        store.create_from_template("Fixliste", "Zweiter", {"Priorität": "hoch"}, "ubuntu1/cc")
    assert "Pflichtfeld fehlt: Bereich" in e.value.problems
    res = store.create_from_template(
        "Fixliste", "Zweiter Punkt",
        {"priorität": "mittel", "Bereich": "Viewer", "Ist": "a", "Soll": "b", "Wenn unklar": "c",
         "Akzeptanzkriterien": ["eins", "zwei"]}, "ubuntu1/cc", summary="Kurzbeschreibung.")
    assert res["path"] == "CMT/Fixliste/FIX-002.md" and res["number"] == 2 and res["overview_updated"]
    text = (store.config.vault_path / res["path"]).read_text()
    assert text.startswith("# FIX-002 · Zweiter Punkt\n\n> Zurück zur Übersicht: [[Fixliste]]\n\n- Priorität: mittel\n")
    assert "- Status: offen" in text and "  - [ ] zwei" in text and text.rstrip().endswith("- Umsetzung: (füllt Claude Code aus)")
    ov = (store.config.vault_path / "CMT/Fixliste.md").read_text()
    assert "Kurz.\n\n### [[FIX-002|FIX-002 · Zweiter Punkt]]\nPriorität: mittel · Status: offen\n\nKurzbeschreibung.\n\n## Erledigt" in ov
    assert store.lint("Fixliste") == []


def test_claims(store: Store):
    p = "CMT/Fixliste/FIX-001.md"
    res = store.claim(p, "ubuntu1/cc", note="arbeite dran")
    assert "- Status: in Arbeit" in store.read_tracked(p)["text"] and res["commit"]
    with pytest.raises(Rejected):
        store.set_property(p, "Bereich", "x", "ubuntu2/cc")
    with pytest.raises(Rejected):
        store.claim(p, "ubuntu2/cc")
    store.set_property(p, "Bereich", "y", "ubuntu1/other-tool")  # gleicher Rechner darf
    assert store.read_tracked(p)["claim"]["agent"] == "ubuntu1/cc"
    store.release(p, "ubuntu1/cc")
    store.set_property(p, "Bereich", "z", "ubuntu2/cc")
    assert any(x["kind"] == "claim" for x in store.lint("Fixliste"))


def test_changes_since_reports_sections(store: Store):
    base = git(store.config.vault_path, "rev-parse", "HEAD").strip()
    p = "CMT/Fixliste/FIX-001.md"
    v = store.read_tracked(p)["version"]
    store.patch_section(p, "Tests", "Neu getestet.", "ubuntu1/cc", base_version=v)
    store.write("Neu.md", "# Neu\n", "ubuntu1/cc")
    ch = store.changes_since(base)
    files = {f["path"]: f for f in ch["files"]}
    assert files["Neu.md"]["change"] == "neu"
    assert files[p]["sections"] == ["FIX-001 · Erster > Tests"]
    assert len(ch["commits"]) == 2
    assert store.changes_since("2000-01-01")["files"]


def test_overview_status_and_lint(store: Store):
    r = store.refresh_overview("Fixliste", "cli")
    ov = (store.config.vault_path / "CMT/Fixliste.md").read_text()
    assert "<!-- vaultserver:overview:start -->" in ov and "| 1 | [[FIX-001\\|FIX-001 · Erster]] | hoch | offen |  |" in ov
    assert store.refresh_overview("Fixliste", "cli")["unchanged"]
    assert r["commit"]
    store.refresh_status("cli")
    st = (store.config.vault_path / "Status.md").read_text()
    assert "### Fixliste" in st and "offen: 1" in st and "FIX-001" in st


def test_link_commits(store: Store, tmp_path: Path):
    code = tmp_path / "code"
    code.mkdir()
    git(code, "init", "-q", "-b", "main")
    (code / "a").write_text("1")
    git(code, "add", "-A")
    git(code, "commit", "-q", "-m", "FIX-001: Suche repariert")
    (code / "a").write_text("2")
    git(code, "commit", "-qam", "Sonstiges FIX-777")
    store.config.code_repos = [{"path": str(code), "name": "code", "url": "https://github.com/x/code"}]
    res = store.link_commits("cli")
    assert res["updated"] == ["CMT/Fixliste/FIX-001.md"]
    text = (store.config.vault_path / "CMT/Fixliste/FIX-001.md").read_text()
    assert "## Commits (automatisch)" in text and "FIX-001: Suche repariert" in text
    assert store.link_commits("cli")["commit"] is None  # idempotent
