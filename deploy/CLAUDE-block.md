## Vault (VaultServer)

Projektwissen, Fixliste, Features und Status liegen im Vault und werden **nur über den MCP-Server `vaultserver`** gelesen und geändert (nicht über das Obsidian-Plugin, nicht per Dateizugriff).

- Zu Beginn jeder Sitzung `guide` aufrufen, bei Arbeit an der Fixliste `guide(area="Fixliste")`.
- Erst `search`/`query`/`outline`, dann `read` mit `section` – keine großen Dateien komplett lesen.
- Schreiben mit `base_version` aus dem letzten `read`; Status mit `set_property`, Abschnitte mit `patch_section`, neue FIX/Features mit `create_from_template`.
- Eigenschaften enthalten nur kanonische Werte (siehe `guide`); Details (Commit, Branch, Test) gehören in „Umsetzung“.
- Vor längerer Arbeit an einem Eintrag `claim`, danach `release`. Nachtläufe beginnen mit `changes_since`.
