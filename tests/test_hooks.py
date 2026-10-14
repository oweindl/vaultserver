"""Hooks vor und nach dem Schreiben: alle Schreibstellen, alle Wege (Web, MCP, intern), Ablehnung, Fehler."""
import base64
import json
import logging
import subprocess
import threading
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import plugin_beispiel
from vaultserver import plugins
from vaultserver.config import Area, Config
from vaultserver.hooks import WriteEvent
from vaultserver.internal import InternalVault
from vaultserver.store import Rejected, version_of
from vaultserver.web import create_app

H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
AUTH = {"Authorization": "Bearer tok"}
NOTIZ = "# Notiz\n\n- Status: offen\n\n## Teil\n\nText\n"


def git(v: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=v,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(plugins, "entry_points", lambda group: [
        EntryPoint(name="beispiel", value="plugin_beispiel", group=plugins.GROUP)])
    plugin_beispiel.EVENTS.clear()
    plugin_beispiel.REJECT = False
    v = tmp_path / "vault"
    v.mkdir()
    (v / "Notiz.md").write_text(NOTIZ, encoding="utf-8")
    (v / "Weg.md").write_text("# Weg\n", encoding="utf-8")
    git(v, "init", "-q", "-b", "main")
    git(v, "add", "-A")
    git(v, "commit", "-q", "-m", "start")
    area = Area(name="Einträge", folder="Einträge", id_regex=r"^E-(\d+)", filename="E-{n:03d}.md",
                heading="# E-{n:03d} · {title}", required=["Status"])
    cfg = Config(vault_path=v.resolve(), db_path=tmp_path / "data" / "idx.sqlite", tokens={"tok": "ubuntu1"},
                 git_pull_seconds=0, watch_seconds=0, areas=[area], plugins_enabled=["beispiel"])
    app = create_app(cfg, start_background=False)
    with TestClient(app) as c:
        svc = app.state.svc
        bin_id = svc.store.trash("Weg.md", "setup")["id"]
        plugin_beispiel.EVENTS.clear()
        yield {"c": c, "svc": svc, "v": v.resolve(), "bin": bin_id, "ver": svc.store.current("Notiz.md")[1]}
    plugin_beispiel.REJECT = False


def mcp(c, tool, args):
    r = c.post("/mcp", headers={**H, **AUTH},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    res = r.json()["result"]
    data = res.get("structuredContent")
    if data is None:
        data = json.loads(res["content"][0]["text"])
    data = data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
    if res.get("isError"):
        return "error", res["content"][0]["text"]
    return ("rejected" if isinstance(data, dict) and data.get("error") == "rejected" else "ok"), data


def web(resp):
    return {200: "ok", 422: "rejected"}.get(resp.status_code, resp.status_code), resp.json()


def intern(fn):
    try:
        return "ok", fn()
    except Rejected as e:
        return "rejected", e.as_dict()


PNG = b"\x89PNG\r\n"
# (Weg, Operation, Aufruf) – Aufruf bekommt env und liefert (ergebnis, daten)
CASES = [
    ("web", "write", lambda e: web(e["c"].put("/api/note", headers=AUTH, json={"path": "Neu.md", "text": "# Neu\n"}))),
    ("web", "set_property", lambda e: web(e["c"].post("/api/property", headers=AUTH,
                                                      json={"path": "Notiz.md", "key": "Status", "value": "fertig"}))),
    ("web", "move", lambda e: web(e["c"].post("/api/move", headers=AUTH, json={"source": "Notiz.md", "target": "Ordner/Notiz.md"}))),
    ("web", "trash", lambda e: web(e["c"].delete(f"/api/note?path=Notiz.md&base_version={e['ver']}", headers=AUTH))),
    ("web", "mkdir", lambda e: web(e["c"].post("/api/folder", headers=AUTH, json={"path": "Neuer Ordner"}))),
    ("web", "restore", lambda e: web(e["c"].post(f"/api/trash/{e['bin']}/restore", headers=AUTH))),
    ("web", "purge", lambda e: web(e["c"].delete(f"/api/trash/{e['bin']}", headers=AUTH))),
    ("web", "create_from_template", lambda e: web(e["c"].post("/api/create", headers=AUTH,
                                                              json={"area": "Einträge", "title": "Erster", "fields": {"Status": "offen"}}))),
    ("web", "upload", lambda e: web(e["c"].post("/api/upload?folder=Bilder", headers=AUTH, files={"file": ("b.png", PNG)}))),
    ("mcp", "write", lambda e: mcp(e["c"], "write", {"path": "Neu.md", "content": "# Neu\n"})),
    ("mcp", "patch_section", lambda e: mcp(e["c"], "patch_section", {"path": "Notiz.md", "section": "Teil", "content": "Neu",
                                                                     "base_version": e["ver"]})),
    ("mcp", "set_property", lambda e: mcp(e["c"], "set_property", {"path": "Notiz.md", "key": "Status", "value": "fertig"})),
    ("mcp", "move", lambda e: mcp(e["c"], "move", {"source": "Notiz.md", "target": "Ordner/Notiz.md"})),
    ("mcp", "trash", lambda e: mcp(e["c"], "delete", {"path": "Notiz.md", "base_version": e["ver"]})),
    ("mcp", "mkdir", lambda e: mcp(e["c"], "create_folder", {"path": "Neuer Ordner"})),
    ("mcp", "restore", lambda e: mcp(e["c"], "restore", {"id": e["bin"]})),
    ("mcp", "create_from_template", lambda e: mcp(e["c"], "create_from_template",
                                                  {"area": "Einträge", "title": "Erster", "fields": {"Status": "offen"}})),
    ("mcp", "upload", lambda e: mcp(e["c"], "upload", {"path": "Bilder/b.png", "data_base64": base64.b64encode(PNG).decode()})),
    ("intern", "write", lambda e: intern(lambda: InternalVault(e["svc"], "agent:x").write("Neu.md", "# Neu\n"))),
    ("intern", "patch_section", lambda e: intern(lambda: InternalVault(e["svc"], "agent:x").patch_section(
        "Notiz.md", "Teil", "Neu", base_version=e["ver"]))),
    ("intern", "set_property", lambda e: intern(lambda: InternalVault(e["svc"], "agent:x").set_property("Notiz.md", "Status", "fertig"))),
    ("intern", "create_from_template", lambda e: intern(lambda: InternalVault(e["svc"], "agent:x").create_from_template(
        "Einträge", "Erster", {"Status": "offen"}))),
    ("store", "delete", lambda e: intern(lambda: e["svc"].store.delete("Notiz.md", "agent:x", base_version=e["ver"]))),
    ("store", "purge", lambda e: intern(lambda: e["svc"].store.purge(None, "agent:x"))),
]
IDS = [f"{w}-{op}" for w, op, _ in CASES]


def snapshot(v: Path) -> dict:
    return {str(p.relative_to(v)): p.read_bytes() for p in sorted(v.rglob("*")) if p.is_file() and ".git" not in p.parts}


@pytest.mark.parametrize("way, op, run", CASES, ids=IDS)
def test_hooks_fire_everywhere(env, way, op, run):
    head = git(env["v"], "rev-parse", "HEAD")
    result, data = run(env)
    assert result == "ok", data
    kinds = [(k, ev.operation) for k, ev in plugin_beispiel.EVENTS]
    assert kinds == [("before", op), ("after", op)], kinds
    before, after = (ev for _, ev in plugin_beispiel.EVENTS)
    assert isinstance(after, WriteEvent) and before.commit is None
    new_head = git(env["v"], "rev-parse", "HEAD")
    assert new_head != head and after.commit == new_head            # nach dem Commit, mit Commit
    assert before.path == after.path and any(p == after.path or p.startswith(after.path + "/") for p in after.paths)
    assert after.agent == {"web": "ubuntu1/api", "mcp": "ubuntu1/claude-code"}.get(way, "agent:x")


@pytest.mark.parametrize("way, op, run", CASES, ids=IDS)
def test_before_hook_rejects_everywhere(env, way, op, run):
    plugin_beispiel.REJECT = True
    head, files = git(env["v"], "rev-parse", "HEAD"), snapshot(env["v"])
    result, data = run(env)
    assert result == "rejected", data                                  # Web 422, MCP wie heute, intern Rejected
    assert data["message"] == "Beispiel-Plug-in lehnt ab" and data["problems"] == ["Inhalt: VERBOTEN"]
    assert git(env["v"], "rev-parse", "HEAD") == head and snapshot(env["v"]) == files
    assert git(env["v"], "status", "--porcelain") == ""
    assert [k for k, _ in plugin_beispiel.EVENTS] == ["before"]


def test_event_details(env):
    svc = env["svc"]
    vault = InternalVault(svc, "agent:x")
    vault.set_property("Notiz.md", "Status", "fertig")
    (_, b), (_, a) = plugin_beispiel.EVENTS
    assert a.operation == "set_property" and a.path == "Notiz.md" and a.paths == ("Notiz.md",)
    assert a.properties_before == {"status": "offen"} and a.properties_after == {"status": "fertig"}
    assert a.version_before == version_of(NOTIZ) == env["ver"]
    assert a.version_after == version_of(a.content_after) == svc.store.current("Notiz.md")[1]
    assert "- Status: fertig" in a.content_after and b.content_after == a.content_after
    plugin_beispiel.EVENTS.clear()
    vault.patch_section("Notiz.md", "Teil", "Anders", base_version=a.version_after)
    (_, ev) = plugin_beispiel.EVENTS[-1]
    assert ev.section == "Teil" and "Anders" in ev.content_after
    plugin_beispiel.EVENTS.clear()
    svc.store.move("Notiz.md", "Ordner/Notiz.md", "agent:x")
    (_, b), (_, a) = plugin_beispiel.EVENTS
    assert (b.path, b.target, a.target) == ("Notiz.md", "Ordner/Notiz.md", "Ordner/Notiz.md")
    assert set(a.paths) >= {"Notiz.md", "Ordner/Notiz.md"}


def test_after_hook_error_does_not_break_write(env, caplog):
    with caplog.at_level(logging.ERROR):
        r = env["c"].put("/api/note", headers=AUTH, json={"path": "K.md", "text": "# K\n\nKAPUTT\n"})
    assert r.status_code == 200 and "error" not in r.json()
    assert (env["v"] / "K.md").read_text(encoding="utf-8") == "# K\n\nKAPUTT\n"
    assert "after-Hook kaputt" in caplog.text and "beispiel" in caplog.text


def test_before_hook_crash_blocks(env, caplog):
    def crash(ev):
        raise RuntimeError("Absturz im Hook")
    env["svc"].store.hooks.add("before_write", crash, owner="zweites")
    with caplog.at_level(logging.ERROR):
        r = env["c"].put("/api/note", headers=AUTH, json={"path": "C.md", "text": "# C\n"})
    assert r.status_code == 422 and "zweites" in r.json()["message"]
    assert not (env["v"] / "C.md").exists() and "Absturz im Hook" in caplog.text


def test_order_first_rejecting_wins(env):
    hooks, seen = env["svc"].store.hooks, []

    def make(name, reject):
        def h(ev):
            seen.append(name)
            if reject:
                raise Rejected(f"nein von {name}", [name])
        return h
    hooks.add("before_write", make("zwei", True), owner="t")
    hooks.add("before_write", make("drei", True), owner="t")
    with pytest.raises(Rejected, match="nein von zwei"):
        env["svc"].store.write("O.md", "# O\n", "agent:x")
    assert seen == ["zwei"]                                  # beispiel (erster) ließ durch, drei lief nicht mehr


def test_after_hook_runs_outside_lock(env):
    lock, got = env["svc"].store.lock, []

    def after(ev):
        t = threading.Thread(target=lambda: got.append(lock.acquire(timeout=2) and (lock.release() or True)))
        t.start()
        t.join()
    env["svc"].store.hooks.add("after_write", after, owner="t")
    InternalVault(env["svc"], "agent:x").write("L.md", "# L\n")
    assert got == [True]


def test_claim_fires_after_lock_released(env):
    """Verschachtelt (claim -> set_property): after_write erst am Ende des äußeren Aufrufs, außerhalb der Sperre."""
    lock, got = env["svc"].store.lock, []
    env["svc"].store.write("Einträge/E-001.md", "# E-001 · x\n\n- Status: offen\n", "setup")
    env["svc"].config.areas[0].allowed = {"status": ["offen", "in arbeit"]}

    def after(ev):
        t = threading.Thread(target=lambda: got.append((ev.operation, lock.acquire(timeout=2) and (lock.release() or True))))
        t.start()
        t.join()
    env["svc"].store.hooks.add("after_write", after, owner="t")
    InternalVault(env["svc"], "agent:x").claim("Einträge/E-001.md")
    assert got == [("set_property", True)]
