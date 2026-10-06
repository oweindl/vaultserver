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
