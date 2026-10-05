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
| Zusatzfunktionen | Ideen 1–8 aus dem Konzept „klingen gut“ – Umfang final bestätigen |
| GitHub-Issues | erst nach Abschluss der Definition |

## Noch offen (Oliver)

- [ ] Obsidian parallel als Editor weiterverwenden oder nicht
- [ ] Git-Remote des Vaults und Push-Berechtigung des Servers
- [ ] Umfang der Zusatzfunktionen 1–8 bestätigen
- [ ] Hostname/IP des Ubuntu-Rechners; Port 8100 dort prüfen: `ss -ltnp | grep 8100`

## Nächste Schritte

1. Offene Punkte klären, Definition abschließen.
2. GitHub-Issues anlegen: je Phase ein Epic (Phasen 1–6), darunter je MCP-Werkzeug und je Zusatzfunktion ein Issue.
3. Phase 1 bauen: Kern + Index; Test gegen Olivers Vault (Kopie auf surfaceold unter `Z:\oweindl`, nur über „Add folder“ erreichbar, UNC-Pfade gehen nicht) mit Messung der Suchzeiten.

## Hinweise für die nächste Sitzung

- Die Sitzung braucht `oweindl/vaultserver` als ausgewähltes Repository, sonst blockiert der Git-Proxy Push und `gh`.
- Vault-Konventionen: Übersichtsdatei + Datei je Eintrag (`Fixliste.md` → `Fixliste/FIX-0nn.md`), Wikilinks mit Alias/Pfad, Eigenschaften teils als `- Status: …` im Text, teils Frontmatter, Checkboxen in Akzeptanzkriterien. Größte Dateien: Archiv ~200 KB, `Phasenplan.md` 140 KB.
- Regeln für Claude Code stehen heute oben in `ContentManagementTool/Fixliste.md` (Vorlage für das `guide`-Werkzeug).
