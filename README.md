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

### Bilder und Dateien

Hochladen geht für jeden Dateityp: Bilder, PDFs und andere Binärdateien, bis `max_upload_mb` (Standard 25 MB, `[limits]`).

- **Web:**
  - Rechtsklick auf einen Ordner › „Datei hochladen …“ oder Dateien auf einen Ordner im Baum ziehen.
  - Im Editor ein Bild **per Strg+V einfügen** (z. B. einen Screenshot), **hineinziehen** oder über **„Bild einfügen …“** wählen. Bilder landen im Unterordner `Bilder` neben der Notiz, z. B. als `Bild 2026-10-06 061200.png`. An der Cursorstelle steht dann `![[Pfad]]`.
- **Gleicher Name:** Eine vorhandene Datei wird nie still überschrieben. Die neue bekommt einen freien Namen (`bild (2).png`), und das Ergebnis nennt den tatsächlichen Pfad. Bewusst ersetzen geht mit `overwrite=true`.
- **MCP:**
  - `upload(path, data_base64)` lädt hoch. Das Ergebnis enthält `embed` mit dem fertigen `![[voller Pfad]]`.
  - `read_file(path)` gibt Bilder (png/jpg/gif/webp bis 5 MB) als Bild zurück, das Claude ansehen kann. Andere Dateien bis 2 MB kommen als Base64.

### Größenregeln: Notizen klein halten

Große Notizen machen das Lesen teuer, und parallel arbeitende Agenten kommen sich häufiger in die Quere. Deshalb gelten Grenzen:

| Grenze | Standard | Wirkung |
| --- | --- | --- |
| weich | 20 KB | Hinweis mit Aufteilungsvorschlag |
| hart | 50 KB | nur für neue Notizen: abgelehnt, mit Vorschlag |
| Notizen je Ordner | 25 | Hinweis, Unterordner zu bilden |

Die Grenzen sind einstellbar unter `[limits]` und je Projekt unter `[projects]`.

So erfahren es die MCP-Clients:
1. **Server-Anweisungen** beim Verbinden und Regel 10 in `guide`.
2. `guide` liefert `groesse` mit den Grenzen und der Konvention: `Thema.md` als Übersicht, Teile in `Thema/`. Dazu `projekt_regeln`, den Inhalt von `_Regeln.md` im Projektordner, falls vorhanden.
3. **Beschreibungen** von `write` und `patch_section`.
4. **Rückmeldung beim Schreiben:** Über der weichen Grenze steht im Ergebnis `groesse` mit Vorschlag (Ebene, Teile, Titel), bei vollem Ordner `ordner`. Neue Notizen über der harten Grenze werden abgelehnt.
5. **Werkzeug `optimize`:** erst den Plan zeigen, dann mit `apply=true` und `version` ausführen. Es arbeitet mit derselben Logik und Prüfung wie „Optimieren“ im Web.
6. **`lint`** meldet `zu-gross` und `ordner-voll`. Archiv und Bereichsordner wie die Fixliste sind ausgenommen.

Im Web zeigt der Baum ⚠ an zu großen Notizen, und beim Speichern erscheint ein Hinweis.

### Live-Aktualisierung

Der Dienst gleicht den Index ständig mit den Dateien ab:
- Datei-Wächter alle `watch_seconds` (Standard 3 s), auch für neue leere Ordner.
- `git pull` alle `pull_seconds`.
- Jede Änderung über Web oder MCP sofort.

Jede Änderung wird als Ereignis gemeldet. Die Web-Oberfläche hört über Server-Sent Events (`/api/events`) mit, der grüne Punkt neben dem Logo zeigt die Verbindung. Bei einem Ereignis gilt:

- **Baum:** lädt neu, wenn Dateien oder Ordner dazukommen oder wegfallen. Der Papierkorb-Zähler läuft mit.
- **Offene Notiz:** wird neu angezeigt, mit gleicher Scrollposition und Hinweis „geändert von …“.
  - Im Editor ohne ungespeicherte Eingaben wird der neue Stand übernommen.
  - Mit Eingaben erscheint ein Hinweis, beim Speichern dann die Konfliktansicht.
  - Ist die Notiz verschoben oder gelöscht, erscheint ein Hinweis.
- **Listen** (Start, Änderungen, Aufgaben, Prüfung, Papierkorb, Suche) laden neu.

Reißt die Verbindung ab, baut der Browser sie selbst wieder auf und holt verpasste Ereignisse nach. Zusätzlich frischt er den Baum alle 2 Minuten auf.

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

### Optimieren: große Notizen aufteilen

Rechtsklick auf eine Notiz (oder ⋯ in der Notiz) › **Optimieren (aufteilen) …** öffnet eine Vorschau. Dabei wird noch nichts geändert.

- **Ebene:** Vorschlag ist die oberste Überschriften-Ebene, die mindestens zweimal vorkommt. Im Fenster lässt sich auf `##`, `###` … umschalten.
- **Links das Original** mit Zeilennummern, **rechts die neuen Dateien** zum Durchklicken (◀ ▶). Die Zeilen der gewählten Teildatei werden im Original markiert.
- **Neue Dateien:**
  - `Name/00 Einleitung.md` für den Text vor der ersten Überschrift.
  - `Name/01 <Überschrift>.md` … für jeden Abschnitt.
  - Eine neue `Name.md` mit Frontmatter und Titel des Originals, einer bearbeitbaren Kurzbeschreibung und dem Inhaltsverzeichnis. Links auf `[[Name]]` bleiben gültig.
- **Prüfung:** Jede Inhaltszeile des Originals muss genau einmal und in derselben Reihenfolge in den neuen Dateien stehen. Ohne vollständige Prüfung ist „Optimierung durchführen“ gesperrt.
- **Links:**
  - Abschnitts-Links aus anderen Notizen (`[[Name#Kapitel]]`, auch Block-Anker `#^id`) und innerhalb der Notiz (`[[#Kapitel]]`) werden auf die Teildatei umgeschrieben.
  - Relative Bild- und Dateilinks in den Teilen bekommen `../`.
- **Ausführen:** Das Original geht in den Papierkorb, dazu kommt ein Commit mit allen neuen und geänderten Dateien.
- **Rückgängig:** Neue `Name.md` und den Ordner löschen (beides in den Papierkorb), dann das Original wiederherstellen.
- **Nicht aufteilbar:** Einträge und Übersichten in Bereichen mit Regeln (Fixliste, Features).

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
