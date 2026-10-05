# VaultServer

Ein immer erreichbarer Ersatz für den Obsidian-MCP-Zugriff: ein MCP-Server mit schnellem Suchindex über einem Markdown-Vault, plus Web-Viewer und -Editor im Browser.

**Status:** Konzeptphase – siehe [docs/KONZEPT.md](docs/KONZEPT.md).

## Idee in einem Satz

Markdown-Dateien in einem Git-Repo bleiben die Quelle der Wahrheit; ein SQLite-FTS5-Index zerlegt sie in Abschnitte, Links und Eigenschaften, damit Agenten in Millisekunden finden und nur die Abschnitte lesen, die sie brauchen.

## Bausteine

- **Kern-Dienst** (Python, FastAPI): Markdown parsen, Index pflegen, Schreiben mit Versionsprüfung, ein Git-Commit pro Änderung
- **MCP-Endpunkt** (Streamable HTTP, Bearer-Token): `search`, `query`, `outline`, `read`, `list`, `backlinks`, `tasks`, `recent`, `write`, `patch_section`, `set_property`, `move`, `delete`
- **Web-Oberfläche**: Ordnerbaum, gerenderte Notizen mit Wikilinks, Suche, Backlinks, Markdown-Editor
- **Betrieb**: systemd-Dienst auf Ubuntu, optional Docker

## Lizenz

MIT
