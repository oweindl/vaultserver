# VaultServer

Ein ständig laufender Ersatz für den Obsidian-MCP-Zugriff: ein Dienst mit Suchindex über einem Markdown-Vault, MCP-Server für Claude Code und Web-Oberfläche zum Lesen und Bearbeiten im Browser.

Die Markdown-Dateien im Git-Repo bleiben die Quelle der Wahrheit. Ein SQLite-FTS5-Index zerlegt sie in Abschnitte, Links, Eigenschaften, Tags und Aufgaben, damit Agenten in Millisekunden finden und nur die Abschnitte lesen, die sie brauchen. Jede Änderung wird ein Git-Commit mit dem Namen des Rechners, der sie gemacht hat.

**Status:** Phasen 1 bis 6 umgesetzt, Betrieb auf `webtest` (192.168.1.32:8100). Konzept: [docs/KONZEPT.md](docs/KONZEPT.md), Betrieb: [docs/BETRIEB.md](docs/BETRIEB.md), Übergabe: [docs/HANDOFF.md](docs/HANDOFF.md).

## Ein Port für alles

| Pfad | Wofür |
| --- | --- |
| `http://<host>:8100/` | Web-Oberfläche (Anmeldung mit Benutzer und Passwort) |
| `http://<host>:8100/mcp` | MCP-Server (Streamable HTTP, Bearer-Token je Rechner) |
| `http://<host>:8100/api/…` | REST für die Web-Oberfläche |

### Vaults, Ordner und Papierkorb

- **Vault-Auswahl über dem Baum:** „all“ oder ein Stammordner (z. B. Moon). Die Auswahl filtert Baum, Suche, Änderungen, Aufgaben, Prüfung und Papierkorb, und der Browser merkt sie sich.
- **„+ Vault“** legt ein neues Stammverzeichnis an.
- **Rechtsklick auf einen Ordner:** neue Notiz, neuer Unterordner, hochladen, umbenennen oder verschieben, Ordner löschen.
- **Rechtsklick auf eine Datei:** öffnen, umbenennen, löschen.
- **Rechtsklick auf die freie Fläche:** neue Notiz bzw. neuer Unterordner im gewählten Vault.
- Leere Ordner bleiben über eine `.gitkeep` in Git erhalten.

**Löschen** (Web und MCP-Werkzeug `delete`) schiebt Notizen, Anhänge und ganze Ordner in den Papierkorb `RecycleBin` im Vault-Stamm:

- Jeder Eintrag liegt als `RecycleBin/<Zeitstempel>-<id>/<alter Pfad>` mit Herkunft in `.recycle.json`.
- Der Papierkorb ist nicht im Suchindex, Links auf Gelöschtes gelten also als kaputt.
- Seite **Papierkorb** (unten im Baum): wiederherstellen an die alte Stelle (Ordner werden zusammengeführt, bei belegten Dateien passiert nichts), endgültig löschen, leeren.
- Geleert wird nur von Hand. Endgültig Gelöschtes bleibt in der Git-Historie.
- MCP: `recycle_bin`, `restore`, `create_folder`.
- Der Ordner `RecycleBin` wird beim Start und nach jedem Pull angelegt, falls er fehlt.

## Einrichtung für Claude

Alles Nötige steht in der Web-Oberfläche unter **Konto › Einrichtung (Claude/MCP)** (`/#/setup`):

- **Verbindung:** MCP-URL, Transport, Kopfzeilen.
- **Zugänge je Rechner:** anlegen, „Neuer Token“, sperren, mit „zuletzt benutzt“.
  - Neue Tokens werden nur einmal angezeigt und nur als SHA-256 in `data/tokens.json` gespeichert.
  - Tokens aus `vaultserver.toml` lassen sich dort sperren oder ersetzen.
- **Anleitungen:**
  - Claude Code, mit dem Block für `CLAUDE.md`.
  - Claude Desktop über `mcp-remote`.
  - Warum claude.ai im Browser nicht geht.
  - Test mit `curl`.
- **Liste der Werkzeuge.**

### Projekt-Kontexte

Jeder Ordner der obersten Ebene ist ein Projekt mit eigener MCP-Adresse, z. B. `http://192.168.1.32:8100/mcp/finance-app`. Über diese Adresse gilt:

- Alle Werkzeuge sehen nur diesen Ordner.
- Pfade gehen relativ zum Projektordner hinein und heraus (`Fixliste/FIX-001.md`).
- Schreiben außerhalb des Projekts wird abgelehnt.
- `guide` nennt die Einstiegsnotiz des Projekts (`Projektbeschreibung.md`, `README.md` …).

Eigene Namen oder Einstiegsnotizen legst du in `vaultserver.toml` fest:

```toml
[projects]
cmt = { folder = "ContentManagementTool", start = "Projektbeschreibung.md" }
finance = "Finance App"
```

Ein Zugang lässt sich in der Einrichtungsseite auf ein Projekt beschränken. Er sieht dann auch über `/mcp` nur dieses Projekt. Für andere Projekte kommt 403, und auf die REST-API hat er keinen Zugriff.

Pro Code-Repo kann eine `.mcp.json` die Projekt-Adresse festlegen. Der Token kommt aus der Umgebungsvariable `VAULTSERVER_TOKEN`, deshalb darf die Datei ins Repo:

```json
{ "mcpServers": { "vaultserver": { "type": "http", "url": "http://192.168.1.32:8100/mcp/finance-app",
  "headers": { "Authorization": "Bearer ${VAULTSERVER_TOKEN}" } } } }
```

Kurzfassung für Claude Code:

```
claude mcp add --scope user --transport http vaultserver http://192.168.1.32:8100/mcp --header "Authorization: Bearer <token>"
```

## MCP-Werkzeuge

Lesen: `guide`, `search`, `query`, `outline`, `read`, `list`, `backlinks`, `tasks`, `recent`, `changes_since`, `lint`, `property_keys`, `claims`.
Schreiben: `write`, `patch_section`, `set_property`, `move`, `delete`, `create_from_template`, `claim`, `release`, `refresh_overview`, `link_commits`.

Schreibende Aufrufe brauchen die zuletzt gelesene Version (`base_version`). Hat sich die Notiz inzwischen geändert, kommt der aktuelle Stand zurück. `patch_section` meldet nur dann einen Konflikt, wenn sich genau der betroffene Abschnitt geändert hat. In Bereichen mit Regeln (Fixliste, Features) lehnt der Server Änderungen ab, die Pflichtfelder entfernen oder unbekannte Werte setzen.

## Web-Oberfläche

Ordnerbaum mit Ziehen und Ablegen und Kontextmenü, gerenderte Notizen mit Wikilinks, Bildern, HTML-Mockups und anklickbaren Checkboxen, Gliederung, Eigenschaften (Status direkt umschaltbar), Backlinks und Git-Verlauf. Bilder, PDFs, Mockups und andere Anhänge öffnen sich in einem Ansichtsfenster auf der Seite statt in einem neuen Tab (Strg-Klick öffnet weiterhin einen Tab). Dazu Suche (Strg+K), Editor mit Wikilink-Vervollständigung nach `[[`, Konfliktansicht, neue FIX- und Feature-Einträge aus der Vorlage, Prüfung, Änderungsliste und Passwortänderung. Funktioniert auch auf dem Handy.

## Entwicklung

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp vaultserver.example.toml vaultserver.toml   # Pfade, Tokens, Benutzer anpassen
.venv/bin/pytest
.venv/bin/vaultserver serve
```

Die Kommandozeile kann auch ohne Server suchen und prüfen, zum Beispiel `vaultserver query "Status!=@erledigt" --folder ContentManagementTool/Fixliste`, `vaultserver lint` oder `vaultserver bench`. `scripts/smoke.py` prüft einen laufenden Server nur lesend.

## Lizenz

MIT
