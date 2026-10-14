# Plug-ins

Erweiterungen, die sich in einen laufenden VaultServer einhängen, ohne ihn zu kopieren oder zu ändern: eigene
Routen, Hintergrunddienste, Prüfungen vor dem Schreiben, Reaktionen nach dem Schreiben und eine interne
Schnittstelle zum Vault. Ohne freigeschaltetes Plug-in verhält sich der Server exakt wie ohne diese Funktion.

## Aufbau und Freischaltung

Ein Plug-in ist ein installiertes Python-Paket mit einem Entry Point der Gruppe `vaultserver.plugins`. Er zeigt auf
ein Modul oder Objekt mit einer Funktion `register(ctx)`:

```toml
# pyproject.toml des Plug-ins
[project.entry-points."vaultserver.plugins"]
meinplugin = "meinplugin"          # Modul (oder "paket.modul:objekt")
```

Installiert wird es in dieselbe Umgebung wie VaultServer (`pip install …`, im Container per eigenem Image).
Geladen wird es nur, wenn die Konfiguration es **ausdrücklich** freischaltet:

```toml
[plugins]
enabled = ["meinplugin"]           # Reihenfolge = Ladereihenfolge = Reihenfolge der Hooks
[plugins.meinplugin]               # beliebige Einstellungen -> ctx.settings
intervall = 30
```

- Nicht freigeschaltete Plug-ins werden nicht einmal importiert – ein zufällig installiertes Paket bleibt wirkungslos.
- Fehlt ein freigeschaltetes Plug-in, scheitert sein Import oder `register`, oder hängt es eine Route an einen Pfad
  des Servers, **startet der Server nicht** (`PluginError`, Meldung `Plug-in: …` auf der Kommandozeile). Lieber
  kein Start als ein halb laufender Server.
- Plug-ins laufen im Prozess des Servers mit allen seinen Rechten. Freischalten heißt vertrauen.

## Lebenszyklus

1. `vaultserver serve` erstellt die Anwendung: Index, Store, MCP, eigene Routen.
2. Danach werden die freigeschalteten Plug-ins in der Reihenfolge von `enabled` geladen und `register(ctx)` aufgerufen.
   Eigene Routen des Servers haben dadurch Vorrang.
3. Beim Start: Hintergrund-Dienst des Servers, dann alle `on_startup`-Funktionen (Reihenfolge der Registrierung,
   sync oder async). Ein Fehler hier beendet den Start.
4. Beim Beenden: alle `on_shutdown`-Funktionen in umgekehrter Reihenfolge (Fehler werden nur protokolliert), dann
   der Hintergrund-Dienst.

Plug-ins gibt es nur im Server. Kommandozeilen-Befehle, die schreiben (`overview`, `link-commits`), laufen ohne Plug-ins
und ohne Hooks.

## `ctx` – was ein Plug-in bekommt

| Name | Bedeutung |
| --- | --- |
| `ctx.name`, `ctx.settings` | Name aus `enabled`, Einstellungen aus `[plugins.<name>]` (dict) |
| `ctx.app` | die FastAPI-Anwendung |
| `ctx.service`, `ctx.store`, `ctx.config` | Kern-Dienst (Index, Suche, Projekte), Store (alle Schreibzugriffe), Konfiguration |
| `ctx.log` | Logger `vaultserver.plugin.<name>` |
| `ctx.include_router(router, prefix, auth="vaultserver")` | Router einhängen, siehe „Routen und Anmeldung“ |
| `ctx.on_startup(fn)`, `ctx.on_shutdown(fn)` | Start/Stopp, z. B. für einen eigenen Worker-Thread |
| `ctx.add_hook("before_write" \| "after_write", fn)` | Schreib-Hooks, siehe unten |
| `ctx.vault(author, scope=None)` | interne Schnittstelle (`InternalVault`), siehe unten |

## Routen und Anmeldung

`ctx.include_router(router, prefix="/meinplugin")` hängt die Routen unter dem Präfix ein. Sie laufen durch dieselbe
Anmeldung wie `/api/…`: Web-Sitzung oder Bearer-Token; ein projektgebundener Token wird abgewiesen (403). In der Route
stehen `request.state.user` (Web-Benutzer oder `None`) und `request.state.agent` (`web/<benutzer>` bzw. `<rechner>/api`).

`auth="own"` nimmt ein Präfix ausdrücklich von der Anmeldung des Servers aus – für Endpunkte mit eigener Prüfung
(z. B. eine signierte Einmal-Adresse). Das gilt nur für dieses Präfix, wird beim Start ins Protokoll geschrieben, und
eine vorhandene Anmeldung steht trotzdem in `request.state`. Präfixe des Servers (`/`, `/api`, `/mcp…`, `/static`,
`/healthz`, `/favicon.ico`, `/api/drop`) sind nicht erlaubt; unter `/api/<eigenes>` geht es.

Wer Routen direkt an `ctx.app` hängt statt über `include_router`, bekommt außerhalb von `/api/` **keine** Anmeldung.

## Hooks vor und nach dem Schreiben

Alle Schreibstellen des Stores lösen sie aus, gleich ob der Aufruf aus der Web-Oberfläche, über MCP oder über die
interne Schnittstelle kommt:

| Operation | Auslöser | `path` | Besonderheiten |
| --- | --- | --- | --- |
| `write` | Notiz anlegen/ersetzen (auch Checkbox in der Oberfläche) | Notiz | `content_after` |
| `patch_section` | Abschnitt ändern | Notiz | `section`, `content_after` = ganze neue Notiz |
| `set_property` | Eigenschaft setzen (auch durch `claim`) | Notiz | `content_after`; unverändert = kein Ereignis |
| `create_from_template` | neuer Eintrag aus Vorlage | neue Notiz | `paths` enthält ggf. die Übersicht |
| `move` | verschieben/umbenennen | Quelle | `target`; nachgezogene Links nur in `after.paths` |
| `delete` | endgültig löschen (`Store.delete`) | Notiz | |
| `trash` | in den Papierkorb (Web/MCP „löschen“) | Notiz/Datei/Ordner | `paths` = betroffene Dateien |
| `upload` | Datei hochladen | endgültiger Pfad | `content_after` = None (binär) |
| `mkdir` | Ordner anlegen | Ordner | |
| `restore` | aus dem Papierkorb zurück | ursprünglicher Pfad | `content_after` bei Notizen |
| `purge` | Papierkorb-Eintrag / Papierkorb leeren | Eintrag bzw. Papierkorb | |

Nicht ausgelöst werden sie von den automatischen Inhalten (`refresh_overview`, Statusblock, `link_commits`, Anlegen
des Papierkorbs) und von Änderungen außerhalb des Stores (`git pull`, Datei-Wächter).

### Ereignis `WriteEvent` (`vaultserver.hooks`)

`operation`, `path`, `agent` (Commit-Autor), `target`, `paths` (Tupel aller geänderten Dateien),
`section`, `properties_before` / `properties_after` (Eigenschaften der Notiz, Schlüssel klein geschrieben, Werte wie
im Text), `content_after`, `version_before` / `version_after`, `commit` (nur nach dem Schreiben; `None` ohne
Git-Commits). Das Ereignis ist unveränderlich.

### `before_write`

- Läuft nach den eigenen Prüfungen des Servers (Version, Claim, Bereichsregeln) und **vor** der ersten Änderung an
  Dateien – ohne Seiteneffekte auf den Vault.
- Ablehnen mit `raise vaultserver.store.Rejected("Grund", ["Feld: Problem", …])`: keine Datei geändert, kein Commit.
  Web antwortet mit 422 und `{"error": "rejected", "message", "problems"}`, MCP liefert dasselbe als Ergebnis (wie bei
  Regelverstößen), die interne Schnittstelle wirft `Rejected`.
- Mehrere Hooks laufen in der Reihenfolge ihrer Registrierung; der erste ablehnende gewinnt, die übrigen laufen nicht.
- Jede andere Exception blockiert ebenfalls („Prüfung durch Plug-in … fehlgeschlagen“) und wird protokolliert.
- Läuft unter der Sperre des Stores: kurz halten, nicht selbst schreiben.

### `after_write`

- Läuft nach erfolgreichem Commit, sobald der äußerste Store-Aufruf fertig und die Sperre wieder frei ist (bei
  `claim` → `set_property` also erst am Ende von `claim`), im Thread des Aufrufers.
- Exceptions werden protokolliert und ändern weder das Schreiben noch die Antwort.
- **Darf nicht blockieren:** Die Antwort an Web/MCP wartet auf den Hook. Längere Arbeit in einen eigenen Thread oder
  eine Warteschlange geben (z. B. ein Worker aus `on_startup`). Schreiben aus dem Hook ist möglich (löst wieder Hooks
  aus) – auf Endlosschleifen achten.

## Interne Schnittstelle

`ctx.vault(author, scope=None)` liefert ein `InternalVault` (`vaultserver.internal`) für Aufrufer im selben Prozess:

```python
v = ctx.vault("agent:pruefer")                       # Commit-Autor "agent:pruefer"
n = v.read("Ordner/Notiz.md", section="Offen")       # dict mit text, version, …
v.patch_section("Ordner/Notiz.md", "Offen", "- [ ] neu", base_version=n["version"], mode="append")
v.set_property("Ordner/Notiz.md", "Status", "fertig")
treffer = v.search("Suchwort"); notizen = v.query(["Status=offen"]); gliederung = v.outline("Ordner/Notiz.md")
v.create_from_template("Bereich", "Titel", {"Status": "offen"}); v.claim("Ordner/Notiz.md"); v.release("Ordner/Notiz.md")
```

- Gleiche Semantik wie MCP: `Conflict` bei veralteter `base_version`, `Rejected` bei Regeln/Claims/Hooks, dieselben Hooks.
- `scope`: Projektname (wie `/mcp/<projekt>`) oder Ordner im Vault. Pfade gehen dann relativ hinein und heraus,
  alles außerhalb wird mit `ValueError` abgelehnt – wie bei projektgebundenen Tokens.
- Claims gelten je Rechnerteil des Autors (vor dem ersten `/`), wie bei Web und MCP.

## Grenzen

- Keine eigenen MCP-Werkzeuge für Plug-ins (noch nicht vorgesehen).
- Keine Isolation: ein Plug-in kann alles, was der Server kann.
- Hooks sehen nur Änderungen über den Store, nicht `git pull` oder direkte Dateiänderungen.
- Ein Plug-in-Fehler beim Laden verhindert den Start; zum Abschalten aus `enabled` streichen.
