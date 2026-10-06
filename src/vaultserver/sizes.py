"""Größenregeln: Notizen klein halten, große Themen als Ordner mit Unterseiten.

Weiche Grenze (Standard 20 KB): Schreiben gelingt, das Ergebnis enthält einen Hinweis mit Aufteilungsvorschlag.
Harte Grenze (Standard 50 KB): neue Notizen werden abgelehnt (bestehende bekommen nur eine Warnung).
Ordner mit mehr als folder_notes Notizen: Hinweis, Unterordner zu bilden. Grenzen je Projekt einstellbar.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from .optimize import Optimizer
from .scope import Limits, limits_for
from .store import Rejected

CONVENTION = ("Große Themen als Ordner mit Unterseiten anlegen: `Thema.md` als kurze Übersicht mit Inhaltsverzeichnis, "
              "Teile in `Thema/01 …md`, `Thema/02 …md`. Eine Notiz = ein Abschnitt/Thema, der beim Lesen und Ändern "
              "einzeln gebraucht wird. Bestehende große Notizen mit `optimize` aufteilen (erst Plan, dann apply).")


def _proposal(store, path: str, text: str) -> dict:
    try:
        p = Optimizer(store).plan(path, text=text)
        return {"ebene": "#" * p["level"], "teile": len(p["parts"]),
                "titel": [x["title"] for x in p["parts"]][:12], "ordner": p["folder"]}
    except (Rejected, KeyError, ValueError) as e:
        return {"hinweis": f"{e} – erst mit ##-Überschriften gliedern"}


def note_report(store, path: str, text: str, limits: Limits | None = None) -> dict | None:
    """Hinweis für eine Notiz über der weichen Grenze, sonst None."""
    if store.config.area_for(path):   # Bereichseinträge/-übersichten: eine Datei je Eintrag (Bereichsregeln)
        return None
    lim = limits or limits_for(store.config, path)
    kb = len(text.encode("utf-8")) / 1024
    if kb <= lim.soft_kb:
        return None
    return {"groesse_kb": round(kb, 1), "grenze_kb": lim.soft_kb, "harte_grenze_kb": lim.hard_kb,
            "hinweis": (f"Die Notiz ist {kb:.0f} KB groß (Grenze {lim.soft_kb} KB). Große Notizen machen Lesen teuer und "
                        "führen bei parallelen Agenten zu Konflikten. Bitte in Unterseiten aufteilen: "
                        f"optimize(path=\"…\") zeigt den Plan, optimize(…, apply=true, version=…) führt ihn aus."),
            "aufteilen": _proposal(store, path, text)}


def check_new_note(store, path: str, text: str) -> None:
    """Neue Notiz über der harten Grenze ablehnen (mit Vorschlag)."""
    if store.config.area_for(path):
        return
    lim = limits_for(store.config, path)
    kb = len(text.encode("utf-8")) / 1024
    if kb > lim.hard_kb:
        prop = _proposal(store, path, text)
        parts = f" Vorschlag: an {prop['ebene']} in {prop['teile']} Teile im Ordner {prop['ordner']}/" if "teile" in prop else ""
        raise Rejected(f"Neue Notiz mit {kb:.0f} KB ist zu groß (höchstens {lim.hard_kb} KB). Bitte gleich als Ordner mit "
                       f"Unterseiten anlegen: Übersicht {path} plus Teile in {path[:-3]}/.{parts}")


def folder_report(store, path: str, limits: Limits | None = None) -> dict | None:
    """Hinweis, wenn der Ordner der Notiz mehr als folder_notes Notizen direkt enthält."""
    folder = str(PurePosixPath(path).parent)
    if folder in ("", "."):
        return None
    lim = limits or limits_for(store.config, path)
    n = store.index.db.execute(
        "SELECT COUNT(*) FROM notes WHERE path LIKE ? AND path NOT LIKE ?", (f"{folder}/%", f"{folder}/%/%")).fetchone()[0]
    if n <= lim.folder_notes:
        return None
    return {"ordner": folder, "notizen": n, "grenze": lim.folder_notes,
            "hinweis": f"Der Ordner {folder} hat {n} Notizen (Grenze {lim.folder_notes}). Bitte thematische Unterordner bilden "
                       "(create_folder, move)."}


def hints(store, path: str) -> dict:
    """Beide Hinweise für eine gerade geschriebene Notiz (leer, wenn alles im Rahmen)."""
    out = {}
    text, _v = store.current(path) if path.endswith(".md") else (None, None)
    lim = limits_for(store.config, path)
    if text is not None:
        r = note_report(store, path, text, lim)
        if r:
            out["groesse"] = r
    f = folder_report(store, path, lim)
    if f:
        out["ordner"] = f
    return out
