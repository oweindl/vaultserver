"""MCP-Werkzeuge (Streamable HTTP unter /mcp)."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .service import Service
from .store import Conflict, Rejected

INSTRUCTIONS = """VaultServer: Markdown-Vault (Projekte, Fixliste, Features, Status) mit Suchindex.
Zu Beginn jeder Sitzung `guide` aufrufen (mit area, wenn du in einem Bereich arbeitest).
Erst search/query/outline, dann read mit section – keine großen Dateien komplett lesen.
Schreibende Werkzeuge brauchen die zuletzt gelesene version als base_version."""

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def agent_from(ctx: Context | None, svc: Service) -> str:
    headers = (ctx.headers if ctx else None) or {}
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    machine = svc.config.tokens.get(token, "unbekannt")
    tool = headers.get("x-vaultserver-agent", "claude-code")
    return f"{machine}/{tool}"


def _guard(fn):
    """Konflikte und Regelverstöße als normales Ergebnis zurückgeben, damit der Agent den
    aktuellen Stand bzw. die Gründe sieht; unbekannte Pfade als Werkzeugfehler."""
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except (Conflict, Rejected) as e:
            return e.as_dict()
        except KeyError as e:  # Meldung soll beim Agenten ankommen, nicht nur "Error executing tool"
            raise ToolError(e.args[0] if e.args else str(e))
        except ValueError as e:
            raise ToolError(str(e))
    return run


def build_mcp(svc: Service) -> MCPServer:
    mcp = MCPServer("vaultserver", instructions=INSTRUCTIONS, version="0.2.0")
    st, ix, lock = svc.store, svc.index, svc.lock

    # ---------------------------------------------------------------- lesen

    @mcp.tool(annotations=RO)
    def guide(area: str | None = None) -> dict[str, Any]:
        """Arbeitsregeln, Bereiche, Pflichtfelder und erlaubte Werte. Zu Beginn jeder Sitzung aufrufen;
        mit area (z. B. "Fixliste") kommen die Regeln dieses Bereichs dazu."""
        return _guard(svc.guide)(area)

    @mcp.tool(annotations=RO)
    def search(text: str, folder: str | None = None, tag: str | None = None,
               properties: dict[str, str] | None = None, include_archive: bool = False,
               limit: int = 20, mode: Literal["text", "semantic", "auto"] = "text") -> list[dict[str, Any]]:
        """Volltextsuche über Abschnitte (Umlaute egal, letztes Wort als Präfix). Treffer mit Pfad,
        Überschriftenpfad, Zeilen, Ausschnitt. Filter: Ordner, Tag, Eigenschaften (Schlüssel -> Wert).
        mode=semantic/auto nutzt Embeddings, falls eingeschaltet."""
        return _guard(svc.search)(text, mode=mode, folder=folder, tag=tag, properties=properties,
                                  include_archive=include_archive, limit=limit)

    @mcp.tool(annotations=RO)
    def query(filters: list[str], folder: str | None = None, fields: list[str] | None = None,
              include_archive: bool = False) -> list[dict[str, Any]]:
        """Notizen über Eigenschaften filtern. Filter als Text: "Status=offen", "Status!=@erledigt"
        (@gruppe aus guide), "Priorität=hoch", "Bereich~prüfen" (enthält), "Status!~erledigt",
        "Owner?" (vorhanden), "!Owner" (fehlt). Werte sind kanonisch (siehe guide)."""
        from .cli import parse_filter

        def run():
            with lock:
                return ix.query([parse_filter(f) for f in filters], folder=folder, fields=fields,
                                include_archive=include_archive)
        try:
            return _guard(run)()
        except Exception as e:  # argparse-Fehler bei unverständlichem Filter
            raise ToolError(str(e))

    @mcp.tool(annotations=RO)
    def outline(path: str) -> list[dict[str, Any]]:
        """Gliederung einer Notiz: Überschriften mit Ebene, Zeilenbereich und Größe in Bytes."""
        with lock:
            return _guard(ix.outline)(st._norm_path(path))

    @mcp.tool(annotations=RO)
    def read(path: str, section: str | None = None, line_from: int | None = None,
             line_to: int | None = None) -> dict[str, Any]:
        """Notiz lesen: ganz, nur ein Abschnitt (Überschrift oder Pfad-Ende wie "Stufe 2 > Tests",
        inkl. Unterabschnitte) oder Zeilenbereich. Liefert version für spätere Änderungen."""
        lines = (line_from or 1, line_to or 10**9) if (line_from or line_to) else None
        return _guard(st.read_tracked)(st._norm_path(path), section=section, lines=lines)

    @mcp.tool(name="list", annotations=RO)
    def list_(folder: str = "", depth: int = 1) -> dict[str, Any]:
        """Ordnerinhalt bis Tiefe depth: Unterordner, Notizen (Titel, Größe, Datum) und Anhänge."""
        with lock:
            return ix.list(folder, depth)

    @mcp.tool(annotations=RO)
    def backlinks(path: str) -> list[dict[str, Any]]:
        """Welche Notizen (und Abschnitte) verweisen auf diese Notiz."""
        with lock:
            return ix.backlinks(st._norm_path(path))

    @mcp.tool(annotations=RO)
    def tasks(folder: str | None = None, done: bool | None = False,
              include_archive: bool = False) -> list[dict[str, Any]]:
        """Checkboxen (offen: done=false, erledigt: true, alle: null) mit Notiz, Abschnitt und Zeile."""
        with lock:
            return ix.tasks(folder=folder, done=done, include_archive=include_archive)

    @mcp.tool(annotations=RO)
    def recent(limit: int = 20, include_archive: bool = False) -> list[dict[str, Any]]:
        """Zuletzt geänderte Notizen mit Zeit, Autor und Commit-Nachricht."""
        return svc.recent(limit, include_archive)

    @mcp.tool(annotations=RO)
    def changes_since(since: str) -> dict[str, Any]:
        """Geänderte Notizen und Abschnitte seit einem Commit oder Zeitpunkt (ISO, z. B.
        "2026-10-05T22:00"), plus Commits dazwischen. Für Nachtläufe."""
        return _guard(st.changes_since)(since)

    @mcp.tool(annotations=RO)
    def lint(area: str | None = None) -> list[dict[str, Any]]:
        """Prüfung: kaputte Wikilinks, fehlende Pflichtfelder, unbekannte Werte, doppelte Nummern,
        Einträge ohne Link in der Übersicht, „in Arbeit“ ohne Reservierung."""
        return _guard(st.lint)(area)

    @mcp.tool(annotations=RO)
    def property_keys(folder: str | None = None, min_notes: int = 2) -> list[dict[str, Any]]:
        """Alle Eigenschafts-Schlüssel mit Anzahl Notizen und verschiedener Werte."""
        with lock:
            return ix.property_keys(min_notes, folder)

    # ---------------------------------------------------------------- schreiben

    @mcp.tool(annotations=RW)
    def write(path: str, content: str, base_version: str | None = None, message: str | None = None,
              ctx: Context | None = None) -> dict[str, Any]:
        """Notiz anlegen (ohne base_version) oder komplett ersetzen (mit base_version aus read)."""
        return _guard(st.write)(path, content, agent_from(ctx, svc), base_version=base_version, message=message)

    @mcp.tool(annotations=RW)
    def patch_section(path: str, content: str, base_version: str, section: str | None = None,
                      mode: Literal["replace", "append", "prepend", "insert_after"] = "replace",
                      message: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Abschnitt ändern. replace: Abschnitt inkl. Unterabschnitte ersetzen (ohne Überschrift im
        content bleibt die alte). append/prepend: Text ans Ende/an den Anfang. insert_after: neuer
        Abschnitt (content beginnt mit Überschrift) nach section, ohne section ans Notizende.
        Konflikt nur, wenn sich genau dieser Abschnitt seit base_version geändert hat."""
        return _guard(st.patch_section)(path, section, content, agent_from(ctx, svc),
                                        base_version=base_version, mode=mode, message=message)

    @mcp.tool(annotations=RW)
    def set_property(path: str, key: str, value: str, base_version: str | None = None,
                     message: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Eigenschaft setzen (z. B. Status = "erledigt (Test)"), dort wo sie steht (Frontmatter oder
        "- Schlüssel: Wert"); fehlt sie, wird sie ergänzt. Werte werden gegen die Bereichsregeln geprüft."""
        return _guard(st.set_property)(path, key, value, agent_from(ctx, svc),
                                       base_version=base_version, message=message)

    @mcp.tool(annotations=RW)
    def move(source: str, target: str, message: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Notiz oder Anhang verschieben/umbenennen; Links in anderen Notizen werden nachgezogen."""
        return _guard(st.move)(source, target, agent_from(ctx, svc), message=message)

    @mcp.tool(annotations=DEL)
    def delete(path: str, base_version: str, message: str | None = None,
               ctx: Context | None = None) -> dict[str, Any]:
        """Notiz löschen (bleibt in der Git-Historie)."""
        return _guard(st.delete)(path, agent_from(ctx, svc), base_version=base_version, message=message)

    @mcp.tool(annotations=RW)
    def create_from_template(area: str, title: str, fields: dict[str, Any], summary: str = "",
                             body: str = "", ctx: Context | None = None) -> dict[str, Any]:
        """Neuen Eintrag in einem Bereich anlegen (z. B. area="Fixliste"): nächste freie Nummer, Datei nach
        Vorlage, Eintrag in der Übersicht. fields: Pflichtfelder laut guide (Listen werden Checkboxen,
        z. B. "Akzeptanzkriterien"). summary: Kurzbeschreibung für die Übersicht (höchstens 5 Sätze)."""
        return _guard(st.create_from_template)(area, title, fields, agent_from(ctx, svc), summary=summary, body=body)

    @mcp.tool(annotations=RW)
    def claim(path: str, note: str = "", minutes: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Eintrag für diesen Rechner reservieren (setzt Status „in Arbeit“, wo erlaubt). Verfällt nach
        der Frist ohne Änderung; eigene Änderungen verlängern sie. Andere Rechner werden abgewiesen."""
        return _guard(st.claim)(path, agent_from(ctx, svc), note=note, minutes=minutes)

    @mcp.tool(annotations=RW)
    def release(path: str, ctx: Context | None = None) -> dict[str, Any]:
        """Reservierung aufheben."""
        return _guard(st.release)(path, agent_from(ctx, svc))

    @mcp.tool(annotations=RO)
    def claims() -> list[dict[str, Any]]:
        """Aktive Reservierungen."""
        with lock:
            return st.claims()

    @mcp.tool(annotations=RW)
    def refresh_overview(area: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Automatische Übersicht (Tabelle aus Eigenschaften) in der Übersichtsdatei des Bereichs
        erneuern; ohne area die Statusnotiz."""
        if area:
            return _guard(st.refresh_overview)(area, agent_from(ctx, svc))
        return _guard(st.refresh_status)(agent_from(ctx, svc))

    @mcp.tool(annotations=RW)
    def link_commits(ctx: Context | None = None) -> dict[str, Any]:
        """Commits der Code-Repos (z. B. "FIX-054: …") in die Notizen eintragen (Abschnitt
        „Commits (automatisch)“)."""
        return _guard(st.link_commits)(agent_from(ctx, svc))

    return mcp
