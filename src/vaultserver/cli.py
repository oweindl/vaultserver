"""Kommandozeile: Index aufbauen, suchen, abfragen, messen."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

from .config import Config
from .index import Index

FILTER_RE = re.compile(r"^\s*([^=!~]+?)\s*(!=|!~|=|~)\s*(.*)$")


def parse_filter(expr: str) -> tuple[str, str, str]:
    if expr.endswith("?"):
        return expr[:-1], "exists", ""
    if expr.startswith("!") and "=" not in expr:
        return expr[1:], "missing", ""
    m = FILTER_RE.match(expr)
    if not m:
        raise argparse.ArgumentTypeError(f"Filter nicht verständlich: {expr}")
    return m.group(1), m.group(2), m.group(3)


def _out(data, as_json: bool) -> None:
    if as_json or not isinstance(data, list):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    for row in data:
        print(" | ".join(" ".join(str(v).split()) for v in row.values()))


def cmd_bench(idx: Index, args) -> dict:
    t0 = time.perf_counter()
    stats = idx.rebuild()
    rebuild = time.perf_counter() - t0
    terms = args.terms or ["status", "abnahme", "fehler", "prüfung", "export pdf", "phase", "kunde"]
    times = []
    for _ in range(args.rounds):
        for t in terms:
            s = time.perf_counter()
            idx.search(t, include_archive=True)
            times.append((time.perf_counter() - s) * 1000)
    q_times = []
    for _ in range(args.rounds):
        s = time.perf_counter()
        idx.query([("status", "!=", "erledigt"), ("priorität", "=", "hoch")])
        q_times.append((time.perf_counter() - s) * 1000)
    times.sort(); q_times.sort()
    pct = lambda xs, p: round(xs[min(len(xs) - 1, int(len(xs) * p))], 2)
    return {
        "index": idx.stats(),
        "rebuild_seconds": round(rebuild, 3),
        "notes_indexed": stats.added + stats.updated,
        "search_ms": {"n": len(times), "median": round(statistics.median(times), 2),
                      "p95": pct(times, 0.95), "max": round(times[-1], 2)},
        "query_ms": {"n": len(q_times), "median": round(statistics.median(q_times), 2),
                     "p95": pct(q_times, 0.95)},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vaultserver")
    ap.add_argument("-c", "--config", default="vaultserver.toml")
    ap.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="Index abgleichen")
    p.add_argument("--rebuild", action="store_true", help="komplett neu aufbauen")
    sub.add_parser("stats", help="Kennzahlen des Index")

    p = sub.add_parser("search", help="Volltextsuche")
    p.add_argument("text")
    p.add_argument("--folder"); p.add_argument("--tag")
    p.add_argument("--prop", action="append", default=[], help="Schlüssel=Wert")
    p.add_argument("--archive", action="store_true", help="Archiv einbeziehen")
    p.add_argument("--raw", action="store_true", help="FTS5-Syntax direkt")
    p.add_argument("-n", "--limit", type=int, default=20)

    p = sub.add_parser("query", help="Abfrage über Eigenschaften")
    p.add_argument("filters", nargs="*", type=parse_filter,
                   help='"Status!~erledigt" "Priorität=hoch" "Titel~pdf" "Owner?" "!Owner"')
    p.add_argument("--folder"); p.add_argument("--field", action="append")
    p.add_argument("--archive", action="store_true")

    p = sub.add_parser("outline", help="Gliederung einer Notiz"); p.add_argument("path")
    p = sub.add_parser("read", help="Notiz oder Abschnitt lesen")
    p.add_argument("path"); p.add_argument("--section"); p.add_argument("--lines", help="von-bis")
    p = sub.add_parser("backlinks"); p.add_argument("path")
    p = sub.add_parser("tasks"); p.add_argument("--folder"); p.add_argument("--done", action="store_true")
    p.add_argument("--archive", action="store_true")
    p = sub.add_parser("list"); p.add_argument("folder", nargs="?", default="")
    p.add_argument("--depth", type=int, default=1)
    sub.add_parser("broken-links")
    p = sub.add_parser("keys", help="Eigenschafts-Schlüssel mit Häufigkeit")
    p.add_argument("--min", type=int, default=2, help="mindestens in so vielen Notizen"); p.add_argument("--folder")
    p = sub.add_parser("serve", help="Server starten (Web, REST, MCP)")
    p.add_argument("--host"); p.add_argument("--port", type=int)
    sub.add_parser("hash-password", help="Passwort-Hash für [server.users] erzeugen (liest Passwort von stdin)")
    sub.add_parser("new-token", help="Zufälliges Token für [server.tokens] erzeugen")
    p = sub.add_parser("lint", help="Regeln, Links, Nummern prüfen"); p.add_argument("--area")
    p = sub.add_parser("overview", help="Automatische Übersicht erneuern"); p.add_argument("area", nargs="?")
    sub.add_parser("link-commits", help="Commits der Code-Repos in Notizen eintragen")
    p = sub.add_parser("changes", help="Änderungen seit Commit/Zeitpunkt"); p.add_argument("since")
    sub.add_parser("embed", help="Embeddings für semantische Suche berechnen")
    p = sub.add_parser("bench", help="Neuaufbau und Suchzeiten messen")
    p.add_argument("--rounds", type=int, default=20); p.add_argument("terms", nargs="*")

    args = ap.parse_args(argv)
    if args.cmd == "hash-password":
        import getpass
        from .web import hash_password
        pw = getpass.getpass("Passwort: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        print(hash_password(pw))
        return 0
    if args.cmd == "new-token":
        import secrets
        print(secrets.token_urlsafe(32))
        return 0
    if not Path(args.config).exists():
        print(f"Konfiguration fehlt: {args.config} (Vorlage: vaultserver.example.toml)", file=sys.stderr)
        return 2
    config = Config.load(args.config)
    if args.cmd == "serve":
        import logging
        import uvicorn
        from .web import create_app
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        from .gitops import GitError
        try:
            app = create_app(config)
        except GitError as e:
            print(f"Vault: {e}", file=sys.stderr)
            return 2
        uvicorn.run(app, host=args.host or config.host, port=args.port or config.port,
                    log_level="info", access_log=False)
        return 0
    if args.cmd in ("lint", "overview", "link-commits", "changes", "embed"):
        from .store import Store
        store = Store(config)
        store.index.sync()
        match args.cmd:
            case "lint": data = store.lint(args.area)
            case "overview":
                data = store.refresh_overview(args.area, "cli") if args.area else store.refresh_status("cli")
            case "link-commits": data = store.link_commits("cli")
            case "changes": data = store.changes_since(args.since)
            case "embed":
                from .semantic import Semantic
                data = {"new": Semantic(store.index, config.ollama_url, config.ollama_model).update()}
        _out(data, args.json)
        return 0
    idx = Index(config)
    try:
        if args.cmd == "index":
            st = idx.rebuild() if args.rebuild else idx.sync()
            _out(vars(st), True)
            return 0
        if args.cmd not in ("bench",):
            idx.sync()
        match args.cmd:
            case "stats": data = idx.stats()
            case "search":
                props = dict(x.split("=", 1) for x in args.prop)
                data = idx.search(args.text, folder=args.folder, tag=args.tag, properties=props,
                                  include_archive=args.archive, limit=args.limit, raw=args.raw)
            case "query":
                data = idx.query(args.filters, folder=args.folder, fields=args.field,
                                 include_archive=args.archive)
            case "outline": data = idx.outline(args.path)
            case "read":
                lines = tuple(int(x) for x in args.lines.split("-")) if args.lines else None
                data = idx.read(args.path, section=args.section, lines=lines)
                if not args.json:
                    print(data["text"]); return 0
            case "backlinks": data = idx.backlinks(args.path)
            case "tasks": data = idx.tasks(folder=args.folder, done=args.done, include_archive=args.archive)
            case "list": data = idx.list(args.folder, args.depth)
            case "broken-links": data = idx.broken_links()
            case "keys": data = idx.property_keys(args.min, args.folder)
            case "bench": data = cmd_bench(idx, args)
        _out(data, args.json)
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 1
    finally:
        idx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
