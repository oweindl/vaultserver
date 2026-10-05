"""Synthetischen Vault mit den Konventionen von Olivers Vault erzeugen (für Messungen).

Größenordnung wie im Konzept: ~250 Notizen, ~3,5 MB, einzelne Dateien 140–200 KB.
Aufruf: python scripts/make_sample_vault.py /pfad/zum/ziel
"""

import random
import sys
from pathlib import Path

WORDS = ("Abnahme Export PDF Kunde Fehler Prüfung Vorlage Archiv Benutzer Rechte Suche Index "
         "Datei Ordner Status Phase Server Dienst Anmeldung Token Übersicht Größe Änderung "
         "Termin Rechnung Vertrag Druck Signatur Zertifikat Ablage Postfach Workflow Freigabe").split()
STATUS = ["offen", "in Arbeit", "erledigt", "erledigt (Test)", "zurückgestellt"]
PRIO = ["hoch", "mittel", "niedrig"]

rnd = random.Random(42)


def para(n=60):
    return " ".join(rnd.choice(WORDS).lower() if i % 7 else rnd.choice(WORDS) for i in range(n)) + "."


def main(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "Fixliste").mkdir(exist_ok=True)
    (target / "Features").mkdir(exist_ok=True)
    (target / "Archiv").mkdir(exist_ok=True)
    (target / "Bilder").mkdir(exist_ok=True)

    rows = []
    for i in range(1, 121):
        fid = f"FIX-{i:03d}"
        rows.append(f"| [[Fixliste/{fid}\\|{fid}]] | {rnd.choice(STATUS)} |")
        body = [f"# {fid} {rnd.choice(WORDS)} {rnd.choice(WORDS)}", "",
                f"- Status: {rnd.choice(STATUS)}", f"- Priorität: {rnd.choice(PRIO)}",
                f"- Feature: [[Features/{rnd.randint(1, 60):02d}-Feature|Feature]]", "",
                "## Beschreibung", "", para(120), "", "## Akzeptanzkriterien", ""]
        body += [f"- [{'x' if rnd.random() < .5 else ' '}] {para(10)}" for _ in range(4)]
        body += ["", "## Umsetzung", "", para(80), ""]
        (target / "Fixliste" / f"{fid}.md").write_text("\n".join(body), encoding="utf-8")
    (target / "Fixliste.md").write_text(
        "# Fixliste\n\nRegeln für Claude Code: Status immer setzen.\n\n| Fix | Status |\n|---|---|\n"
        + "\n".join(rows) + "\n", encoding="utf-8")

    for i in range(1, 61):
        name = f"{i:02d}-Feature"
        parts = [f"---\naliases: [Feature {i}]\nstatus: {rnd.choice(STATUS)}\ntags: [feature]\n---",
                 f"# Feature {i}", "", f"- Priorität: {rnd.choice(PRIO)}", ""]
        stages = 12 if i % 10 == 0 else 3  # einige große Dateien (50–60 KB)
        for s in range(1, stages + 1):
            parts += [f"## Stufe {s}", "", para(300), "", "### Tests", "", para(150), "",
                      f"- [ ] {para(8)} #offen", ""]
        (target / "Features" / f"{name}.md").write_text("\n".join(parts), encoding="utf-8")

    plan = ["# Phasenplan", ""]
    for p in range(1, 41):
        plan += [f"## Phase {p}", "", para(450), "", f"Siehe [[Feature {rnd.randint(1, 60)}]].", ""]
    (target / "Phasenplan.md").write_text("\n".join(plan), encoding="utf-8")

    for a in range(1, 4):
        arch = [f"# Archiv {2024 + a}", ""]
        for p in range(1, 60):
            arch += [f"## Eintrag {p}", "", "- Status: erledigt", "", para(450), ""]
        (target / "Archiv" / f"Archiv-{2024 + a}.md").write_text("\n".join(arch), encoding="utf-8")

    for n in range(1, 66):
        (target / f"Notiz {n}.md").write_text(
            f"# Notiz {n}\n\n{para(200)}\n\n![[Bilder/bild{n % 36}.png]]\n", encoding="utf-8")
    for b in range(36):
        (target / "Bilder" / f"bild{b}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(100))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
