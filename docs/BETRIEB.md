# VaultServer – Betrieb

## Wo was liegt

| Was | Wo |
| --- | --- |
| Dienst | systemd-Benutzerdienst `vaultserver` auf `webtest` (192.168.1.32), Port 8100 |
| Code | `~/projects/vaultserver` (Repo `oweindl/vaultserver`) |
| Konfiguration | `~/projects/vaultserver/vaultserver.toml` (nicht im Repo; Vorlage `vaultserver.example.toml`) |
| Vault | `~/obsidian-oweindl` (Repo `oweindl/obsidian-oweindl`, privat) |
| Index | `~/projects/vaultserver/data/index.sqlite`, jederzeit neu aufbaubar |
| Zugangsdaten für die Rechner | `~/projects/vaultserver/data/clients.md` (nicht im Repo) |

Git-Zugriff auf GitHub läuft über `gh` (`~/.local/bin/gh`, angemeldet als oweindl, HTTPS). Der SSH-Schlüssel des Rechners ist nur ein Deploy-Key für PDFCloud-Pro.

## Dienst

```
systemctl --user status vaultserver
systemctl --user restart vaultserver          # nach Änderungen an Code oder Konfiguration
journalctl --user -u vaultserver -f           # Protokoll
curl http://192.168.1.32:8100/healthz
```

Der Dienst läuft ohne Anmeldung weiter, weil Linger für den Benutzer aktiv ist. Die Unit-Datei liegt in `deploy/vaultserver.service`.

Im Hintergrund prüft der Dienst alle 3 Sekunden, ob sich Dateien geändert haben, holt jede Minute Änderungen vom Git-Remote (`pull --rebase`) und pusht etwa 10 Sekunden nach jeder eigenen Änderung. Fehler im Hintergrund erscheinen im Protokoll und auf der Startseite der Web-Oberfläche.

## Vault aus einem Kunden-Repo

Statt eines von Hand geklonten Ordners kann der Vault aus einem beliebigen Git-Repo über HTTPS kommen (GitHub, GitLab, Gitea):

```toml
[git]
url = "https://github.com/kunde/vault.git"
branch = "main"
username = "x-access-token"     # GitHub; GitLab: "oauth2"; Gitea: Benutzername
committer = "VaultServer <vaultserver@kunde.example>"
push = true
```

- Ist `vault_path` leer oder fehlt, klont VaultServer beim Start dorthin.
- Liegt dort schon ein Repo, muss sein Remote (`[git] remote`, Standard `origin`) auf dasselbe Repo zeigen (https- und ssh-Schreibweise gelten als gleich) und, wenn `branch` gesetzt ist, dieser Branch ausgecheckt sein. Sonst startet der Server nicht. Ein nicht leerer Ordner ohne Git wird nie überschrieben.
- Das Token steht nur in der Umgebungsvariable `VS_GIT_TOKEN` (Name änderbar mit `token_env`). VaultServer gibt es nur für den Host aus `url` an git weiter, über die Prozess-Umgebung, nie auf der Kommandozeile. In Fehlermeldungen, Protokoll und Oberfläche erscheint es nie; Zugangsdaten in URLs werden entfernt.
- Token-Rechte so klein wie möglich: GitHub fine-grained Token nur für dieses Repo mit „Contents: read and write“, GitLab Projekt-Token mit `write_repository`.
- Ohne `url` bleibt alles wie bisher: Pull und Push gehen an den Remote des vorhandenen Clones, mit den Zugangsdaten des Rechners.

Die Einrichtungsseite zeigt Remote, Branch und das Ergebnis des letzten Pull/Push (mit Fehlertext). `/healthz` ist ohne Anmeldung erreichbar und zeigt deshalb nur, ob Pull und Push zuletzt geklappt haben.

## Mehrere Repos einbinden (z. B. je Kunde)

Neben dem Vault selbst kann der Server beliebig viele weitere Git-Repos anbieten. Jedes Repo wird als **Ordner der obersten Ebene** in den Vault geklont und ist damit ein eigenes Projekt:

- eigene MCP-Adresse `/mcp/<name>`, eigener Eintrag in der Vault-Auswahl der Oberfläche,
- Zugänge (Einrichtung › 3) lassen sich auf genau ein Repo beschränken,
- Änderungen werden im jeweiligen Repo committet (mit dessen Commit-Kennung), geholt und gepusht; der Papierkorb liegt im Repo (`<name>/RecycleBin`), Gelöschtes wandert also nie in ein anderes Repo,
- das Repo im Vault-Stamm ignoriert die eingebundenen Ordner (Eintrag in `.git/info/exclude`).

**In der Oberfläche:** Einrichtung › Repositories › „Repository einbinden …“: Adresse, Name, Token, „Verbindung testen“ (zeigt die Branches), Branch wählen, „Klonen und einbinden“. Danach je Repo: Abgleichen (jetzt pushen und holen), Ändern (Token, Branch, Commit-Kennung, Push, Abstand), Entfernen. Nur mit Web-Anmeldung, nicht mit MCP-Tokens.

- Gespeichert in `data/repos.json` (Rechte 600). Tokens darin verschlüsselt mit dem Server-Geheimnis `data/secret`; sie werden nie wieder angezeigt, nicht protokolliert und aus Fehlermeldungen entfernt. Geht `data/secret` verloren, müssen die Tokens neu eingegeben werden.
- Erlaubt sind nur `https://`-Adressen (`[git] repo_schemes`), ohne Zugangsdaten in der Adresse.
- Branch wechseln geht nur ohne nicht committete Änderungen; offene Commits werden vorher gepusht.
- Entfernen verschiebt den Ordner nach `data/removed-repos/<name>-<Zeit>` (nichts geht verloren). Gibt es nicht gepushte Commits oder Änderungen, fragt die Oberfläche nach.
- Ein eingebundener Ordner lässt sich nicht löschen oder verschieben (nur über die Einrichtung).
- **Ändern** kann auch Adresse und Name: eine neue Adresse nur für dasselbe Repo an anderer Stelle (umbenannt/umgezogen; geprüft wird, ob der bisherige Stand dort vorhanden ist, sonst bleibt alles wie vorher). Ein neuer Name verschiebt den Ordner und ändert die MCP-Adresse; auf das Repo (oder einen Unter-Vault) beschränkte Zugänge werden umgestellt.

**Unterordner als eigene Vaults** (Häkchen im Dialog, in der toml `split = true`): Standard ist ein Vault je Repo (`/mcp/<name>`). Mit Aufteilung ist zusätzlich jeder Ordner der obersten Ebene im Repo ein eigener Vault `/mcp/<name>/<ordner>` – eigene Auswahl in der Oberfläche, relative Pfade, Zugänge lassen sich auf genau einen Unter-Vault beschränken, optionale `_Regeln.md` je Unterordner. So wie die Ordner im Stamm-Vault. `/mcp/<name>` bleibt als Adresse für das ganze Repo. Neue Ordner (auch per Pull) werden sofort zu Vaults. Git bleibt ein Repo (ein Klon, gemeinsame Commits, Pull/Push und Papierkorb).

**Fest in der Konfiguration** (nur lesbar in der Oberfläche; Ordner fehlt oder ist leer → wird beim Start geklont):

```toml
[[repos]]
name = "acme"                       # Ordner und Projekt
url = "https://github.com/acme/vault.git"
branch = "main"
committer = "VaultServer <vaultserver@acme.example>"
push = true
pull_seconds = 60
split = false                       # true: jeder Ordner der obersten Ebene ist ein eigener Vault /mcp/acme/<ordner>
# Token aus der Umgebung, Standard VS_GIT_TOKEN_<NAME> (hier VS_GIT_TOKEN_ACME); anderer Name: token_env = "…"
```

## Konfiguration exportieren und importieren

Einrichtung › „Konfiguration sichern und übertragen“. Gilt für das, was in der Oberfläche eingerichtet wurde; `vaultserver.toml` gehört nicht dazu.

- **Export** als JSON-Datei, wählbar: Repositories (Adresse, Branch, Commit-Kennung, Push, Abstand, Aufteilung), Zugänge (Name, Projektbindung, Token-Hash – Rechner behalten ihre Tokens), Web-Benutzer (Passwort-Hash). Repo-Tokens nur mit Passphrase (mind. 10 Zeichen), verschlüsselt mit einem per scrypt daraus abgeleiteten Schlüssel; ohne Passphrase fehlen sie und werden nach dem Import neu eingegeben. Die Datei trotzdem wie ein Passwort behandeln.
- **Import**: Datei wählen, Teile wählen, Passphrase (falls Tokens enthalten), „Prüfen“ zeigt eine Vorschau, „Importieren“ übernimmt. Es wird nur ergänzt, was es noch nicht gibt (gleicher Name); Vorhandenes bleibt unverändert. Repos werden dabei geklont. Falsche Passphrase → Abbruch ohne Änderung.
- Typischer Umzug: alter Server exportieren (alle Teile, mit Passphrase) → neuer Server (z. B. Container) mit eigenem ersten Benutzer starten → importieren. Rechner und Benutzer arbeiten danach ohne neue Tokens oder Passwörter weiter.

## Docker

Dateien: `Dockerfile`, `compose.yaml`, `.env.example`, `vaultserver.kunde.example.toml`.

```
cp .env.example .env                                  # Ports, Pfade, VS_GIT_TOKEN
cp vaultserver.kunde.example.toml vaultserver.toml    # [git] url, branch, committer
docker compose up -d --build
docker compose logs -f
docker compose exec vaultserver vaultserver -c /config/vaultserver.toml hash-password   # erster Web-Benutzer
```

- Im Container gelten feste Pfade: Vault `/vault`, Daten `/data` (Index, MCP-Zugänge, Web-Benutzer, Sitzungsschlüssel), Konfiguration `/config/vaultserver.toml` (nur lesend).
- `data/` sichern: dort liegen Zugänge und Benutzer. Den Vault sichert das Git-Repo.
- Mehrere Kunden auf einem Rechner: je Kunde ein eigener Ordner mit `.env`, `vaultserver.toml`, `data/`, `vault/` und eigenem Port, gestartet mit `docker compose -p <kunde> up -d`.
- Update: `git pull && docker compose up -d --build`.
- Nie zwei Server auf denselben Vault-Ordner schreiben lassen (vorher `systemctl --user disable --now vaultserver`).

## Einen Rechner anbinden

Jeder Rechner hat ein eigenes Token; der Name vor dem Token erscheint als Autor der Commits (`ubuntu1/claude-code: …`). Die fertigen Befehle je Rechner stehen in `data/clients.md`.

```
claude mcp add --scope user --transport http vaultserver http://192.168.1.32:8100/mcp --header "Authorization: Bearer <token>"
```

Danach den Block aus `deploy/CLAUDE-block.md` in `~/.claude/CLAUDE.md` des Rechners übernehmen. Er verweist die Agenten auf `guide` und die Arbeitsregeln. Das bisherige Obsidian-MCP (Local REST API) auf dem Rechner entfernen, damit Agenten nicht zwei Wege nutzen.

Neues Token: `vaultserver new-token`, unter `[server.tokens]` eintragen, Dienst neu starten.

## Web-Anmeldung

Benutzer stehen unter `[server.users]` in `vaultserver.toml` als scrypt-Hash. Das Passwort ändert man in der Web-Oberfläche unter „Konto › Passwort ändern“ (mindestens 8 Zeichen). Das neue Passwort steht dann als Hash in `data/users.json` und hat Vorrang vor der Konfiguration. Die eigene Sitzung bleibt angemeldet, alle anderen Sitzungen dieses Benutzers enden. Sitzungen gelten sonst 30 Tage.

Passwort vergessen: `data/users.json` löschen (dann gilt wieder der Hash aus der Konfiguration) oder einen neuen Hash erzeugen und eintragen:

```
.venv/bin/vaultserver hash-password      # fragt das Passwort ab, gibt den Hash aus
```

Danach `systemctl --user restart vaultserver`.

## Regeln und kanonische Werte

Bereiche (`[[areas]]`) legen fest, welche Felder Pflicht sind, welche Werte erlaubt sind, wie neue Einträge heißen und wo sie in der Übersicht landen. Der Server lehnt nur neue Verstöße ab; alte Lücken in bestehenden Einträgen meldet `lint`, ohne Änderungen zu blockieren.

Freitext wie „erledigt (Test) · Commit `1e02f53`“ bildet `[canonical]` auf den Wert `erledigt (test)` ab. Die Datei bleibt unverändert, Abfragen und Regeln nutzen den kanonischen Wert. Gruppen unter `[groups.status]` erlauben Abfragen wie `Status!=@erledigt`.

Ändert sich der Parser, erhöht man `INDEX_VERSION` in `index.py`; der Index baut sich beim nächsten Start neu auf.

## Automatische Inhalte

Drei Funktionen schreiben generierte Blöcke zwischen Markern in Notizen. Text außerhalb der Marker bleibt unberührt.

| Funktion | Ziel | Auslöser |
| --- | --- | --- |
| Übersichtstabelle | Übersichtsdatei des Bereichs, Abschnitt „Übersicht (automatisch)“ | `refresh_overview(area)`, Web: Prüfung |
| Statusblock | `ContentManagementTool/Status.md` | `refresh_overview()` ohne Bereich, Web: Prüfung |
| Commit-Verknüpfung | Abschnitt „Commits (automatisch)“ in jeder FIX- oder Feature-Datei mit passenden Commits in `~/pdfcloud-pro` | `link_commits`, Web: Prüfung |

Keine davon läuft automatisch. Die Commit-Verknüpfung würde beim ersten Lauf rund 40 FIX-Dateien ändern; wer das regelmäßig möchte, setzt `commit_link_minutes`.

## Semantische Suche (Phase 6)

Ausgeschaltet, weil auf `webtest` kein Ollama läuft. Zum Einschalten Ollama installieren, `ollama pull nomic-embed-text`, in der Konfiguration `[semantic] enabled = true` setzen und neu starten. Der Dienst berechnet fehlende Embeddings alle 5 Minuten; `search(mode="auto")` ergänzt dann Volltexttreffer um ähnliche Abschnitte.

## Prüfen und reparieren

```
.venv/bin/python scripts/smoke.py http://192.168.1.32:8100/mcp <token>   # nur lesend
.venv/bin/vaultserver lint
.venv/bin/vaultserver index --rebuild
```

`scripts/smoke.py` ruft nur lesende Werkzeuge auf. Andere Testskripte nie gegen den echten Vault laufen lassen; dafür eine Kopie mit eigener Konfiguration (`push = false`) nehmen.

Ein Fehlgriff lässt sich über Git zurücknehmen: Jede Änderung ist ein eigener Commit, `git revert <commit>` im Vault und danach `git push`.
