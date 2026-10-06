"""Größenregeln: Hinweise, harte Grenze für neue Notizen, optimize per MCP, guide, lint, Web."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vaultserver.config import Area, Config
from vaultserver.web import create_app, hash_password

H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer tok"}


def big(n_sections: int, size: int = 400) -> str:
    return "# Titel\n\nEinleitung.\n\n" + "".join(f"## Teil {i}\n\n{'Text ' * (size // 5)}\n\n" for i in range(1, n_sections + 1))


@pytest.fixture
def env(tmp_path: Path):
    v = tmp_path / "vault"
    (v / "Moon").mkdir(parents=True)
    (v / "Finance").mkdir()
    (v / "Finance" / "Fixliste").mkdir()
    (v / "Moon" / "_Regeln.md").write_text("# Regeln\n\nKapitel je Datei.\n", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite", users={"oliver": hash_password("altes-passwort")},
                 tokens={"tok": "ubuntu1"}, git_commit=False, git_pull_seconds=0, watch_seconds=0,
                 soft_kb=2, hard_kb=6, folder_notes=3,
                 projects={"moon": {"folder": "Moon", "soft_kb": 1}, "finance": "Finance"},
                 areas=[Area(name="Fixliste", folder="Finance/Fixliste", overview="Finance/Fixliste.md")])
    c = TestClient(create_app(cfg, start_background=False))
    c.__enter__()
    yield c, v
    c.__exit__(None, None, None)


def mcp(c, tool, args=None, url="/mcp"):
    r = c.post(url, headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}})
    res = r.json()["result"]
    if res.get("isError"):
        return "error", res["content"][0]["text"]
    d = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    return 200, d.get("result", d) if isinstance(d, dict) and set(d) == {"result"} else d


def test_guide_and_write_hints(env):
    c, v = env
    _, g = mcp(c, "guide")
    assert g["groesse"]["weich_kb"] == 2 and g["groesse"]["abweichend"]["moon"]["weich_kb"] == 1 and "Thema/" in g["groesse"]["konvention"]
    _, gm = mcp(c, "guide", url="/mcp/moon")
    assert gm["groesse"]["weich_kb"] == 1 and gm["projekt_regeln"]["datei"] == "_Regeln.md" and "Kapitel je Datei" in gm["projekt_regeln"]["text"]
    # neue Notiz über der harten Grenze: abgelehnt, mit Vorschlag
    st, r = mcp(c, "write", {"path": "Finance/Riesig.md", "content": big(20)})
    msg = json.dumps(r, ensure_ascii=False)
    assert "version" not in r and "zu groß" in msg and "Finance/Riesig/" in msg and "## in 21 Teile" in msg
    assert not (v / "Finance" / "Riesig.md").exists()
    # zwischen weich und hart: gespeichert, mit Hinweis und Vorschlag
    st, w = mcp(c, "write", {"path": "Finance/Mittel.md", "content": big(6)})
    assert st == 200 and w["groesse"]["grenze_kb"] == 2 and w["groesse"]["aufteilen"]["teile"] == 7
    # klein: kein Hinweis; patch_section lässt sie wachsen -> Hinweis
    _, k = mcp(c, "write", {"path": "Finance/Klein.md", "content": "# K\n\n## A\nx\n"})
    assert "groesse" not in k
    _, p = mcp(c, "patch_section", {"path": "Finance/Klein.md", "section": "A", "mode": "append", "content": "y " * 1500, "base_version": k["version"]})
    assert p["groesse"]["groesse_kb"] > 2
    # Projekt-Grenze (Moon 1 KB) und relative Pfade im Hinweis
    _, m = mcp(c, "write", {"path": "Kapitel.md", "content": big(3, 600)}, url="/mcp/moon")
    assert m["groesse"]["grenze_kb"] == 1 and m["groesse"]["aufteilen"]["ordner"] == "Kapitel"
    # Ordner voll (Grenze 3): Finance hat jetzt Mittel, Klein + 2 weitere
    mcp(c, "write", {"path": "Finance/D.md", "content": "# D\n"})
    _, e = mcp(c, "write", {"path": "Finance/E.md", "content": "# E\n"})
    assert e["ordner"]["notizen"] == 4 and e["ordner"]["grenze"] == 3


def test_optimize_tool_lint_web(env):
    c, v = env
    _, w = mcp(c, "write", {"path": "Kapitel.md", "content": big(4, 600)}, url="/mcp/moon")
    _, plan = mcp(c, "optimize", {"path": "Kapitel.md"}, url="/mcp/moon")
    assert plan["folder"] == "Kapitel" and [x["path"] for x in plan["parts"]][:2] == ["Kapitel/00 Einleitung.md", "Kapitel/01 Teil 1.md"]
    assert plan["check"]["ok"] and plan["version"] == w["version"] and "apply=true" in plan["next"]
    assert mcp(c, "optimize", {"path": "Kapitel.md", "apply": True}, url="/mcp/moon")[0] == "error"     # ohne version
    _, done = mcp(c, "optimize", {"path": "Kapitel.md", "apply": True, "version": plan["version"]}, url="/mcp/moon")
    assert done["folder"] == "Kapitel" and (v / "Moon" / "Kapitel" / "04 Teil 4.md").is_file()
    assert mcp(c, "recycle_bin", url="/mcp/moon")[1][0]["original"] == "Kapitel.md"
    # lint: zu groß, Ordner voll – Bereichsordner (Fixliste) ausgenommen
    for i in range(5):
        (v / "Finance" / "Fixliste" / f"FIX-00{i}.md").write_text(f"# FIX-00{i}\n", encoding="utf-8")
        (v / "Finance" / f"N{i}.md").write_text(big(6) if i == 0 else "# n\n", encoding="utf-8")
    assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
    lint = c.get("/api/lint").json()
    assert any(x["kind"] == "zu-gross" and x["path"] == "Finance/N0.md" for x in lint)
    (v / "Finance" / "Fixliste" / "FIX-009.md").write_text(big(8), encoding="utf-8")
    c.post("/api/refresh", json={})
    lint = c.get("/api/lint").json()
    assert not any(x["kind"] == "zu-gross" and "Fixliste" in x["path"] for x in lint)   # Bereichseintrag ausgenommen
    full = [x["path"] for x in lint if x["kind"] == "ordner-voll"]
    assert "Finance" in full and "Moon/Kapitel" in full and "Finance/Fixliste" not in full   # Bereichsordner ausgenommen
    # Web: Baum markiert große Notizen, neue zu große Notiz abgelehnt, Hinweis beim Speichern
    tree = c.get("/api/tree").json()
    assert next(n for n in tree["notes"] if n["path"] == "Finance/N0.md")["big"] == 2
    assert c.put("/api/note", json={"path": "Finance/Neu.md", "text": big(20)}).status_code == 422
    r = c.put("/api/note", json={"path": "Finance/Neu2.md", "text": big(6)}).json()
    assert r["groesse"]["grenze_kb"] == 2
