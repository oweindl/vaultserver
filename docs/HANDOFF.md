# VaultServer – Übergabe (Stand 2026-10-05)

Diese Datei übergibt den Stand an die nächste Claude-Sitzung. Start mit: „Lies docs/HANDOFF.md und docs/KONZEPT.md und mach weiter.“

## Was steht

- **Konzept:** `docs/KONZEPT.md` (Live-Doc: https://claude.ai/code/artifact/c6090794-dc51-42e7-9285-c325fdac0748)
- **Repo:** `oweindl/vaultserver`, public, MIT. Erster Commit enthält README, LICENSE, .gitignore, `docs/`.
- **Architektur:** Markdown-Dateien im Git-Repo = Quelle der Wahrheit; SQLite-FTS5 = Index (Abschnitte, Links, Eigenschaften, Tags, Aufgaben); ein FastAPI-Dienst liefert Web-UI, REST und MCP.

## Entscheidungen von Oliver

| Thema | Entscheidung |
| --- | --- |
| Datenhaltung | Markdown + Git + SQLite-Index |
| Host | Ubuntu-Rechner, systemd-Dienst |
| Port | **8100** für alles: `/` Web-Viewer/Editor, `/mcp` MCP-Server, `/api/…` REST |
| Name | VaultServer |
| Eigenschaften | Alle `- Schlüssel: Wert`-Zeilen und Frontmatter-Felder (keine feste Liste) |
| Agentenpflicht | Claude-Code-Instanzen setzen und nutzen die Eigenschaften konsequent; Server erzwingt das |
| Zusatzfunktionen | Ideen 1–8 alle im Umfang (bestätigt 2026-10-05) |
| Host (konkret) | `webtest`, 192.168.1.32; Port 8100 frei (geprüft 2026-10-05) |
| Obsidian | wird abgelöst, VaultServer ist einziger Schreiber |
| Vault-Git | `oweindl/obsidian-oweindl` (privat), geklont nach `~/obsidian-oweindl`; Zugriff über `gh` (HTTPS, Konto oweindl). Achtung: `~/.ssh/id_ed25519` ist nur Deploy-Key für PDFCloud-Pro |
| GitHub-Issues | erst nach Abschluss der Definition |

## Noch offen (Oliver)

- [ ] Kanonische Statuswerte bestätigen (Vorschlag in `vaultserver.example.toml`, Abschnitt `[canonical]`): erledigt-ausgerollt, erledigt-abgenommen, erledigt-test, teilweise-test, behoben-pruefen, offen, geplant, entwurf, aktiv, fertig
- [ ] Sollen Agenten künftig nur noch kanonische Werte schreiben (Details wie Commits in eigene Zeile `- Umsetzung:`)? Das wäre die Grundlage für `guide`/`lint` (Ideen 1, 6)

## Stand Phase 1 (2026-10-05)

- Gebaut: `src/vaultserver/` mit `parser.py` (Abschnitte, Links, Eigenschaften, Tags, Aufgaben), `index.py` (SQLite-FTS5, inkrementeller Abgleich über Prüfsummen, Link-Auflösung wie Obsidian, search/query/outline/read/backlinks/tasks/list), `cli.py`.
- Tests: `.venv/bin/pytest` (7 grün).
- Messung mit synthetischem Vault (250 Notizen, Dateien bis 205 KB, `scripts/make_sample_vault.py`): Neuaufbau 0,29 s, Suche Median 4,4 ms / p95 5,6 ms, Eigenschafts-Abfrage 0,4 ms.
- Archiv (Idee 7) ist im Index schon umgesetzt: `Archiv/` nur mit `include_archive`.
- **Echter Vault** (`~/obsidian-oweindl`, 251 Notizen, 2,7 MB Text, 45 Anhänge): Neuaufbau 0,58 s, Suche Median 2,1 ms / p95 3,2 ms, Abfrage 0,2 ms. 3 kaputte Wikilinks (`Willkommen.md` → „Neuer Link“, 2× „Aegis Aerospace“ in `Moon/`).
- **Befund Eigenschaften:** ~1160 Schlüssel, davon 93 in ≥ 3 Notizen; Rest ist Fließtext in Aufzählungen. `Ist`/`Soll`/`Wenn unklar` sind echte Felder der Fixliste-Vorlage, deshalb kein Filter nach Position. `vaultserver keys` zeigt die Schlüssel mit Häufigkeit (Grundlage für `guide`).
- **Befund Status:** 50 verschiedene Freitext-Werte (mit Commits, Branches, Kommentaren). Regeln unter `[canonical]` in der Konfiguration bilden sie auf 10 Werte ab; Rohtext bleibt unverändert. Offene Fixes: `vaultserver query "Status!~erledigt" --folder ContentManagementTool/Fixliste` → FIX-050, 055, 056.

## Nächste Schritte

1. GitHub-Issues: angelegt (#1–#6 Epics je Phase, #1 geschlossen; #7–#19 MCP-Werkzeuge; #20–#27 Zusatzfunktionen 1–8, alle unter Epic #2).
2. Phase 2 (#2): MCP-Server (FastAPI + Streamable HTTP) auf Port 8100, Lesewerkzeuge, dann Schreiben mit Versionsprüfung und Git-Commit.

## Hinweise für die nächste Sitzung

- `gh` liegt in `~/.local/bin/gh`, angemeldet als oweindl (HTTPS, `gh auth setup-git`). Push und Issues laufen darüber.
- Vault-Konventionen: Übersichtsdatei + Datei je Eintrag (`Fixliste.md` → `Fixliste/FIX-0nn.md`), Wikilinks mit Alias/Pfad, Eigenschaften teils als `- Status: …` im Text, teils Frontmatter, Checkboxen in Akzeptanzkriterien. Größte Dateien: Archiv ~200 KB, `Phasenplan.md` 140 KB.
- Regeln für Claude Code stehen heute oben in `ContentManagementTool/Fixliste.md` (Vorlage für das `guide`-Werkzeug).
