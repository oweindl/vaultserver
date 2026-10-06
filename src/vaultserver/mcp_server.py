"""MCP-Werkzeuge (Streamable HTTP unter /mcp)."""

from __future__ import annotations

import contextvars
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .scope import SCOPE_HEADER, Scope
from .service import Service
from .store import Conflict, Rejected

INSTRUCTIONS = """VaultServer: Markdown-Vault (Projekte, Fixliste, Features, Status) mit Suchindex.
Zu Beginn jeder Sitzung `guide` aufrufen (mit area, wenn du in einem Bereich arbeitest).
Erst search/query/outline, dann read mit section – keine großen Dateien komplett lesen.
Schreibende Werkzeuge brauchen die zuletzt gelesene version als base_version.
Über /mcp/<projekt> sieht man nur dieses Projekt; alle Pfade sind dann relativ zum Projektordner."""

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def agent_from(ctx: Context | None, svc: Service) -> str:
    headers = (ctx.headers if ctx else None) or {}
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    machine = svc.clients.lookup(token) or "unbekannt"
    tool = headers.get("x-vaultserver-agent", "claude-code")
    return f"{machine}/{tool}"


# Projekt-Kontext des laufenden Werkzeugaufrufs (für Konflikt-/Regelmeldungen mit relativen Pfaden)
_SCOPE: contextvars.ContextVar[Scope] = contextvars.ContextVar("vaultserver_scope", default=Scope(None))


def _guard(fn):
    """Konflikte und Regelverstöße als normales Ergebnis zurückgeben, damit der Agent den
    aktuellen Stand bzw. die Gründe sieht; unbekannte Pfade als Werkzeugfehler."""
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except (Conflict, Rejected) as e:
            return _SCOPE.get().rel(e.as_dict())
        except KeyError as e:  # Meldung soll beim Agenten ankommen, nicht nur "Error executing tool"
            raise ToolError(e.args[0] if e.args else str(e))
        except ValueError as e:
            raise ToolError(str(e))
    return run


def build_mcp(svc: Service) -> MCPServer:
    mcp = MCPServer("vaultserver", instructions=INSTRUCTIONS, version="0.3.0")
    st, ix, lock = svc.store, svc.index, svc.lock

    def scope_of(ctx: Context | None) -> Scope:
        """Projekt-Kontext aus der Kopfzeile, die nur die Auth-Middleware setzt (/mcp/<projekt> oder
        projektgebundener Token)."""
        name = ((ctx.headers if ctx else None) or {}).get(SCOPE_HEADER, "")
        sc = Scope(None)
        if name:
            project = svc.projects().get(name)
            if not project:
                raise ToolError(f"Projekt „{name}“ gibt es nicht")
            sc = Scope(project)
        _SCOPE.set(sc)
        return sc

    def area_in(sc: Scope, area: str | None) -> None:
        if sc and area:
            a = svc.config.area_by_name(area)
            if not sc.inside(a.folder):
                raise ValueError(f"Bereich „{area}“ gehört nicht zum Projekt {sc.project.name}")

    def path_in(sc: Scope, path: str) -> str:
        return st._norm_path(sc.full(path))

    # ---------------------------------------------------------------- lesen

    @mcp.tool(annotations=RO)
    def guide(area: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Arbeitsregeln, Bereiche, Pflichtfelder und erlaubte Werte. Zu Beginn jeder Sitzung aufrufen;
        mit area (z. B. "Fixliste") kommen die Regeln dieses Bereichs dazu."""
        sc = scope_of(ctx)

        def run():
            area_in(sc, area)
            out = svc.guide(area)
            if not sc:
                return out
            out["areas"] = [a for a in out["areas"] if sc.inside(a["folder"])]
            out = sc.rel(out)
            pr = sc.project
            out["project"] = {
                "name": pr.name, "folder": pr.folder, "start": sc.rel(pr.start) if pr.start else None,
                "hinweis": (f"Du arbeitest im Projekt „{pr.folder}“. Alle Pfade sind relativ zu diesem Ordner, "
                            "andere Projekte sind nicht sichtbar."
                            + (f" Einstieg: zuerst outline/read von „{sc.rel(pr.start)}“." if pr.start else "")),
            }
            return out
        return _guard(run)()

    @mcp.tool(annotations=RO)
    def search(text: str, folder: str | None = None, tag: str | None = None,
               properties: dict[str, str] | None = None, include_archive: bool = False,
               limit: int = 20, mode: Literal["text", "semantic", "auto"] = "text",
               ctx: Context | None = None) -> list[dict[str, Any]]:
        """Volltextsuche über Abschnitte (Umlaute egal, letztes Wort als Präfix). Treffer mit Pfad,
        Überschriftenpfad, Zeilen, Ausschnitt. Filter: Ordner, Tag, Eigenschaften (Schlüssel -> Wert).
        mode=semantic/auto nutzt Embeddings, falls eingeschaltet."""
        sc = scope_of(ctx)
        return _guard(lambda: sc.rel(sc.only(svc.search(text, mode=mode, folder=sc.folder(folder), tag=tag,
                                                        properties=properties, include_archive=include_archive,
                                                        limit=limit))))()

    @mcp.tool(annotations=RO)
    def query(filters: list[str], folder: str | None = None, fields: list[str] | None = None,
              include_archive: bool = False, ctx: Context | None = None) -> list[dict[str, Any]]:
        """Notizen über Eigenschaften filtern. Filter als Text: "Status=offen", "Status!=@erledigt"
        (@gruppe aus guide), "Priorität=hoch", "Bereich~prüfen" (enthält), "Status!~erledigt",
        "Owner?" (vorhanden), "!Owner" (fehlt). Werte sind kanonisch (siehe guide)."""
        from .cli import parse_filter
        sc = scope_of(ctx)

        def run():
            with lock:
                return sc.rel(sc.only(ix.query([parse_filter(f) for f in filters], folder=sc.folder(folder),
                                               fields=fields, include_archive=include_archive)))
        try:
            return _guard(run)()
        except ToolError:
            raise
        except Exception as e:  # argparse-Fehler bei unverständlichem Filter
            raise ToolError(str(e))

    @mcp.tool(annotations=RO)
    def outline(path: str, ctx: Context | None = None) -> list[dict[str, Any]]:
        """Gliederung einer Notiz: Überschriften mit Ebene, Zeilenbereich und Größe in Bytes."""
        sc = scope_of(ctx)

        def run():
            with lock:
                return sc.rel(ix.outline(path_in(sc, path)))
        return _guard(run)()

    @mcp.tool(annotations=RO)
    def read(path: str, section: str | None = None, line_from: int | None = None,
             line_to: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Notiz lesen: ganz, nur ein Abschnitt (Überschrift oder Pfad-Ende wie "Stufe 2 > Tests",
        inkl. Unterabschnitte) oder Zeilenbereich. Liefert version für spätere Änderungen."""
        sc = scope_of(ctx)
        lines = (line_from or 1, line_to or 10**9) if (line_from or line_to) else None
        return _guard(lambda: sc.rel(st.read_tracked(path_in(sc, path), section=section, lines=lines)))()

    @mcp.tool(name="list", annotations=RO)
    def list_(folder: str = "", depth: int = 1, ctx: Context | None = None) -> dict[str, Any]:
        """Ordnerinhalt bis Tiefe depth: Unterordner, Notizen (Titel, Größe, Datum) und Anhänge."""
        sc = scope_of(ctx)

        def run():
            with lock:
                return sc.rel(ix.list(sc.full(folder, allow_root=True) if sc else folder, depth))
        return _guard(run)()

    @mcp.tool(annotations=RO)
    def backlinks(path: str, ctx: Context | None = None) -> list[dict[str, Any]]:
        """Welche Notizen (und Abschnitte) verweisen auf diese Notiz."""
        sc = scope_of(ctx)

        def run():
            with lock:
                return sc.rel(sc.only(ix.backlinks(path_in(sc, path))))
        return _guard(run)()

    @mcp.tool(annotations=RO)
    def tasks(folder: str | None = None, done: bool | None = False,
              include_archive: bool = False, ctx: Context | None = None) -> list[dict[str, Any]]:
        """Checkboxen (offen: done=false, erledigt: true, alle: null) mit Notiz, Abschnitt und Zeile."""
        sc = scope_of(ctx)

        def run():
            with lock:
                return sc.rel(sc.only(ix.tasks(folder=sc.folder(folder), done=done, include_archive=include_archive)))
        return _guard(run)()

    @mcp.tool(annotations=RO)
    def recent(limit: int = 20, include_archive: bool = False, ctx: Context | None = None) -> list[dict[str, Any]]:
        """Zuletzt geänderte Notizen mit Zeit, Autor und Commit-Nachricht."""
        sc = scope_of(ctx)
        if not sc:
            return svc.recent(limit, include_archive)
        return sc.rel(sc.only(svc.recent(max(limit * 10, 200), include_archive))[:limit])

    @mcp.tool(annotations=RO)
    def changes_since(since: str, ctx: Context | None = None) -> dict[str, Any]:
        """Geänderte Notizen und Abschnitte seit einem Commit oder Zeitpunkt (ISO, z. B.
        "2026-10-05T22:00"), plus Commits dazwischen. Für Nachtläufe."""
        sc = scope_of(ctx)

        def run():
            out = st.changes_since(since)
            if sc:
                out["files"] = sc.only(out.get("files", []))
            return sc.rel(out)
        return _guard(run)()

    @mcp.tool(annotations=RO)
    def lint(area: str | None = None, ctx: Context | None = None) -> list[dict[str, Any]]:
        """Prüfung: kaputte Wikilinks, fehlende Pflichtfelder, unbekannte Werte, doppelte Nummern,
        Einträge ohne Link in der Übersicht, „in Arbeit“ ohne Reservierung."""
        sc = scope_of(ctx)

        def run():
            area_in(sc, area)
            return sc.rel(sc.only(st.lint(area)))
        return _guard(run)()

    @mcp.tool(annotations=RO)
    def property_keys(folder: str | None = None, min_notes: int = 2, ctx: Context | None = None) -> list[dict[str, Any]]:
        """Alle Eigenschafts-Schlüssel mit Anzahl Notizen und verschiedener Werte."""
        sc = scope_of(ctx)

        def run():
            with lock:
                return ix.property_keys(min_notes, sc.folder(folder))
        return _guard(run)()

    # ---------------------------------------------------------------- schreiben

    @mcp.tool(annotations=RW)
    def write(path: str, content: str, base_version: str | None = None, message: str | None = None,
              ctx: Context | None = None) -> dict[str, Any]:
        """Notiz anlegen (ohne base_version) oder komplett ersetzen (mit base_version aus read)."""
        sc = scope_of(ctx)
        return _guard(lambda: sc.rel(st.write(sc.full(path), content, agent_from(ctx, svc),
                                              base_version=base_version, message=message)))()

    @mcp.tool(annotations=RW)
    def patch_section(path: str, content: str, base_version: str, section: str | None = None,
                      mode: Literal["replace", "append", "prepend", "insert_after"] = "replace",
                      message: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Abschnitt ändern. replace: Abschnitt inkl. Unterabschnitte ersetzen (ohne Überschrift im
        content bleibt die alte). append/prepend: Text ans Ende/an den Anfang. insert_after: neuer
        Abschnitt (content beginnt mit Überschrift) nach section, ohne section ans Notizende.
        Konflikt nur, wenn sich genau dieser Abschnitt seit base_version geändert hat."""
        sc = scope_of(ctx)
        return _guard(lambda: sc.rel(st.patch_section(sc.full(path), section, content, agent_from(ctx, svc),
                                                      base_version=base_version, mode=mode, message=message)))()

    @mcp.tool(annotations=RW)
    def set_property(path: str, key: str, value: str, base_version: str | None = None,
                     message: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Eigenschaft setzen (z. B. Status = "erledigt (Test)"), dort wo sie steht (Frontmatter oder
        "- Schlüssel: Wert"); fehlt sie, wird sie ergänzt. Werte werden gegen die Bereichsregeln geprüft."""
        sc = scope_of(ctx)
        return _guard(lambda: sc.rel(st.set_property(sc.full(path), key, value, agent_from(ctx, svc),
                                                     base_version=base_version, message=message)))()

    @mcp.tool(annotations=RW)
    def move(source: str, target: str, message: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Notiz oder Anhang verschieben/umbenennen; Links in anderen Notizen werden nachgezogen."""
        sc = scope_of(ctx)
        return _guard(lambda: sc.rel(st.move(sc.full(source), sc.full(target), agent_from(ctx, svc), message=message)))()

    @mcp.tool(annotations=DEL)
    def delete(path: str, base_version: str, message: str | None = None,
               ctx: Context | None = None) -> dict[str, Any]:
        """Notiz löschen (bleibt in der Git-Historie)."""
        sc = scope_of(ctx)
        return _guard(lambda: sc.rel(st.delete(sc.full(path), agent_from(ctx, svc),
                                               base_version=base_version, message=message)))()

    @mcp.tool(annotations=RW)
    def create_from_template(area: str, title: str, fields: dict[str, Any], summary: str = "",
                             body: str = "", ctx: Context | None = None) -> dict[str, Any]:
        """Neuen Eintrag in einem Bereich anlegen (z. B. area="Fixliste"): nächste freie Nummer, Datei nach
        Vorlage, Eintrag in der Übersicht. fields: Pflichtfelder laut guide (Listen werden Checkboxen,
        z. B. "Akzeptanzkriterien"). summary: Kurzbeschreibung für die Übersicht (höchstens 5 Sätze)."""
        sc = scope_of(ctx)

        def run():
            area_in(sc, area)
            return sc.rel(st.create_from_template(area, title, fields, agent_from(ctx, svc), summary=summary, body=body))
        return _guard(run)()

    @mcp.tool(annotations=RW)
    def claim(path: str, note: str = "", minutes: int | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Eintrag für diesen Rechner reservieren (setzt Status „in Arbeit“, wo erlaubt). Verfällt nach
        der Frist ohne Änderung; eigene Änderungen verlängern sie. Andere Rechner werden abgewiesen."""
        sc = scope_of(ctx)
        return _guard(lambda: sc.rel(st.claim(sc.full(path), agent_from(ctx, svc), note=note, minutes=minutes)))()

    @mcp.tool(annotations=RW)
    def release(path: str, ctx: Context | None = None) -> dict[str, Any]:
        """Reservierung aufheben."""
        sc = scope_of(ctx)
        return _guard(lambda: sc.rel(st.release(sc.full(path), agent_from(ctx, svc))))()

    @mcp.tool(annotations=RO)
    def claims(ctx: Context | None = None) -> list[dict[str, Any]]:
        """Aktive Reservierungen."""
        sc = scope_of(ctx)
        with lock:
            return sc.rel(sc.only(st.claims()))

    @mcp.tool(annotations=RW)
    def refresh_overview(area: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Automatische Übersicht (Tabelle aus Eigenschaften) in der Übersichtsdatei des Bereichs
        erneuern; ohne area die Statusnotiz."""
        sc = scope_of(ctx)

        def run():
            if area:
                area_in(sc, area)
                return sc.rel(st.refresh_overview(area, agent_from(ctx, svc)))
            if sc and not sc.inside(svc.config.status_note):
                raise ValueError(f"Die Statusnotiz gehört nicht zum Projekt {sc.project.name}")
            return sc.rel(st.refresh_status(agent_from(ctx, svc)))
        return _guard(run)()

    @mcp.tool(annotations=RW)
    def link_commits(ctx: Context | None = None) -> dict[str, Any]:
        """Commits der Code-Repos (z. B. "FIX-054: …") in die Notizen eintragen (Abschnitt
        „Commits (automatisch)“)."""
        sc = scope_of(ctx)
        if sc:
            raise ToolError("link_commits betrifft den ganzen Vault und geht nur ohne Projekt (/mcp)")
        return _guard(st.link_commits)(agent_from(ctx, svc))

    return mcp
