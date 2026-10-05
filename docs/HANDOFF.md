# VaultServer – Übergabe (Stand 2026-10-05 abends)

Diese Datei übergibt den Stand an die nächste Claude-Sitzung. Start mit: „Lies docs/HANDOFF.md und docs/BETRIEB.md und mach weiter.“

## Stand

Alle sechs Phasen und die Zusatzfunktionen 1–8 sind umgesetzt. Der Dienst läuft auf `webtest` (192.168.1.32:8100) als systemd-Benutzerdienst gegen den echten Vault `~/obsidian-oweindl` und pusht Änderungen nach `oweindl/obsidian-oweindl`. Claude Code auf `webtest` ist angebunden. Betrieb, Tokens und Konfiguration: [BETRIEB.md](BETRIEB.md).

| Teil | Dateien |
| --- | --- |
| Parser, Index | `parser.py`, `index.py` (FTS5, Link-Auflösung wie Obsidian, `INDEX_VERSION` erzwingt Neuaufbau) |
| Schreiben, Regeln, Claims, Vorlagen, Übersichten, Lint, `changes_since`, Commit-Verknüpfung | `store.py`, `gitops.py` |
| `guide`, Hintergrund (Datei-Wächter, Pull, Push) | `service.py` |
| MCP (23 Werkzeuge) | `mcp_server.py` (MCP-SDK 2.3: `MCPServer`, Fehler für Agenten als `ToolError`) |
| Web und REST, Anmeldung | `web.py`, `render.py`, `static/` (ohne Build, CodeMirror 5 im Repo) |
| Semantische Suche | `semantic.py` (Ollama, ausgeschaltet) |

Tests: `.venv/bin/pytest` (22 grün). Messung echter Vault: Neuaufbau 0,58 s, Suche Median 2,1 ms.

## Entscheidungen

| Thema | Entscheidung |
| --- | --- |
| Datenhaltung | Markdown + Git + SQLite-Index |
| Host, Port | `webtest`, 8100 für Web, REST und MCP |
| Obsidian | wird abgelöst; VaultServer ist einziger Schreiber |
| Eigenschaften | alle `- Schlüssel: Wert`-Zeilen ohne Einrückung und alle Frontmatter-Felder; Regeln prüfen nur den Block oben in der Notiz |
| Statuswerte | wie in `Fixliste.md` festgelegt: offen, in Arbeit, erledigt (Test), abgenommen, produktiv, blockiert; dazu teilweise (Test) für Features. Freitext wird per `[canonical]` abgebildet, die Dateien bleiben unverändert |
| Agentenpflicht | Server lehnt neue Verstöße ab (Pflichtfelder, erlaubte Werte); Details wie Commits gehören in „Umsetzung“ |
| Automatische Inhalte | Übersichten, Statusblock und Commit-Verknüpfung nur auf Aufruf, nicht zeitgesteuert |

Die Statuswerte und die Regel „Details in Umsetzung“ hat Claude festgelegt, weil Oliver „ohne Rückfragen fertigstellen“ vorgegeben hat. Beides steht in der Konfiguration und lässt sich dort ändern.

## Offen

- [ ] ubuntu1, ubuntu2 und die Workstation anbinden (Befehle mit Tokens in `data/clients.md` auf `webtest`), dort das Obsidian-MCP entfernen und den `CLAUDE.md`-Block übernehmen
- [ ] Obsidian und Obsidian-Git auf der Workstation abschalten, sobald alle Rechner umgestellt sind
- [ ] Rechnernamen der Tokens prüfen (angenommen: webtest, ubuntu1, ubuntu2, workstation)
- [ ] Lint-Befunde im Vault: FIX-051, 052, 057, 058 ohne Soll/Akzeptanzkriterien/Wenn unklar, FIX-025 und FIX-054 ohne „Wenn unklar“, drei kaputte Wikilinks (`Willkommen.md`, zweimal „Aegis Aerospace“ in `Moon/`)
- [ ] Ollama installieren, falls semantische Suche gewünscht ist
- [ ] Commit-Verknüpfung einmal bewusst auslösen (ändert rund 40 FIX-Dateien)

## Vorfall beim Einrichten

Beim ersten Test gegen den laufenden Dienst lief versehentlich ein schreibendes Testskript gegen den echten Vault. Es legte FIX-059 an, änderte und löschte ihn wieder (7 Commits von `webtest/claude-code`). Commit `6033b02` stellt `Fixliste.md` wieder her; der Inhalt des Vaults ist identisch mit dem Stand davor (`e0483f4`). Seitdem gibt es `scripts/smoke.py`, das nur lesende Werkzeuge zulässt.

## Hinweise für die nächste Sitzung

- `gh` liegt in `~/.local/bin/gh`, angemeldet als oweindl (HTTPS). Push und Issues laufen darüber.
- Schreibende Tests nur gegen eine Kopie des Vaults mit eigener Konfiguration (`push = false`, anderer Port).
- Vault-Konventionen: Übersichtsdatei plus Datei je Eintrag (`Fixliste.md` → `Fixliste/FIX-0nn.md`, `Features.md` → `Features/nn-slug.md`), Einträge in den Übersichten als Überschrift mit Wikilink.
