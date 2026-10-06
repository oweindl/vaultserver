"""Papierkorb (RecycleBin) und Ordner: löschen = verschieben mit Herkunft, wiederherstellen an die alte Stelle."""
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vaultserver.config import Config
from vaultserver.web import create_app, hash_password

H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer tok"}


def make(tmp_path: Path, git: bool = False):
    v = tmp_path / "vault"
    (v / "Moon" / "Missionen").mkdir(parents=True)
    (v / "Moon" / "Rakete.md").write_text("# Rakete\n\nSiehe [[Apollo]]\n", encoding="utf-8")
    (v / "Moon" / "Missionen" / "Apollo.md").write_text("# Apollo\n\nMondlandung\n", encoding="utf-8")
    (v / "Moon" / "Missionen" / "plan.pdf").write_bytes(b"%PDF-1.4 x")
    (v / "Finance App").mkdir()
    (v / "Finance App" / "Konto.md").write_text("# Konto\n", encoding="utf-8")
    if git:
        for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start"]):
            subprocess.run(["git", *cmd], cwd=v, check=True)
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite", users={"oliver": hash_password("altes-passwort")},
                 tokens={"tok": "ubuntu1"}, git_commit=git, git_pull_seconds=0, watch_seconds=0)
    return v, create_app(cfg, start_background=False)


def mcp(c, tool, args=None, url="/mcp"):
    r = c.post(url, headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}})
    res = r.json()["result"]
    if res.get("isError"):
        return "error", res["content"][0]["text"]
    d = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    return 200, d.get("result", d) if isinstance(d, dict) and set(d) == {"result"} else d


def test_bin_folder_and_restore(tmp_path):
    v, app = make(tmp_path)
    with TestClient(app) as c:
        assert (v / "RecycleBin" / ".gitkeep").is_file()                       # Papierkorb immer da
        assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
        assert "recyclebin" not in [p["name"] for p in c.get("/api/projects").json()]
        # neuer Vault und Unterordner (leer, aber im Baum)
        assert c.post("/api/folder", json={"path": "Garten"}).json()["created"]
        assert c.post("/api/folder", json={"path": "Garten/Beete"}).status_code == 200
        assert c.post("/api/folder", json={"path": "Garten"}).status_code == 422          # gibt es schon
        assert c.post("/api/folder", json={"path": "RecycleBin/x"}).status_code == 422
        t = c.get("/api/tree").json()
        assert "Garten/Beete" in t["folders"] and not any(f.startswith("RecycleBin") for f in t["folders"])
        assert not any(".gitkeep" in a["path"] for a in t["attachments"])
        assert "garten" in [p["name"] for p in c.get("/api/projects").json()]
        # Ordner löschen: alles in einen Eintrag, aus Index und Suche verschwunden
        r = c.delete("/api/note", params={"path": "Moon/Missionen"}).json()
        assert r["kind"] == "folder" and r["files"] == 2 and not (v / "Moon" / "Missionen").exists()
        assert (v / "RecycleBin" / r["id"] / "Moon" / "Missionen" / "Apollo.md").is_file()
        assert c.get("/api/search", params={"q": "Mondlandung"}).json() == []
        bins = c.get("/api/trash").json()
        assert bins[0]["original"] == "Moon/Missionen" and bins[0]["deleted_by"] == "web/oliver" and bins[0]["exists"] is False
        assert c.get("/api/tree").json()["bin"]["count"] == 1
        # Link auf die gelöschte Notiz ist kaputt (Papierkorb nicht im Index)
        assert any(x["kind"] == "kaputter-link" and x["path"] == "Moon/Rakete.md" for x in c.get("/api/lint").json())
        # Notiz löschen mit Version, dann Ordner wiederherstellen
        n = c.get("/api/note", params={"path": "Moon/Rakete.md", "raw": 1}).json()
        assert c.delete("/api/note", params={"path": "Moon/Rakete.md", "base_version": "falsch"}).status_code == 409
        r2 = c.delete("/api/note", params={"path": "Moon/Rakete.md", "base_version": n["version"]}).json()
        assert r2["kind"] == "note"
        assert c.post(f"/api/trash/{r['id']}/restore").json()["files"] == 2
        assert (v / "Moon" / "Missionen" / "plan.pdf").read_bytes() == b"%PDF-1.4 x"
        assert c.get("/api/search", params={"q": "Mondlandung"}).json()[0]["path"] == "Moon/Missionen/Apollo.md"
        # Konflikt: alte Stelle wieder belegt -> nichts passiert
        (v / "Moon" / "Rakete.md").write_text("# neu\n", encoding="utf-8")
        assert c.post(f"/api/trash/{r2['id']}/restore").status_code == 422
        assert c.get("/api/trash").json()[0]["exists"] is True
        # endgültig löschen und leeren
        assert c.delete(f"/api/trash/{r2['id']}").json()["purged"] == 1
        c.delete("/api/note", params={"path": "Finance App"})
        c.delete("/api/note", params={"path": "Garten"})
        assert c.delete("/api/trash").json()["purged"] == 2 and c.get("/api/trash").json() == []
        assert (v / "RecycleBin" / ".gitkeep").is_file()
        assert c.delete("/api/note", params={"path": "RecycleBin"}).status_code == 422
        assert c.post("/api/trash/../../x/restore").status_code in (404, 422)


def test_mcp_delete_goes_to_bin_with_git(tmp_path):
    v, app = make(tmp_path, git=True)
    with TestClient(app) as c:
        _, n = mcp(c, "read", {"path": "Rakete.md"}, url="/mcp/moon")
        _, d = mcp(c, "delete", {"path": "Rakete.md", "base_version": n["version"]}, url="/mcp/moon")
        assert d["path"] == "Rakete.md" and d["commit"]
        _, lst = mcp(c, "recycle_bin", url="/mcp/moon")
        assert lst[0]["original"] == "Rakete.md"
        assert mcp(c, "recycle_bin", url="/mcp/finance-app")[1] == []                 # anderes Projekt sieht nichts
        assert mcp(c, "restore", {"id": d["id"]}, url="/mcp/finance-app")[0] == "error"
        _, rs = mcp(c, "restore", {"id": d["id"]}, url="/mcp/moon")
        assert rs["restored"] and (v / "Moon" / "Rakete.md").is_file()
        _, f = mcp(c, "create_folder", {"path": "Neu/Unter"}, url="/mcp/moon")
        assert f["path"] == "Neu/Unter" and (v / "Moon" / "Neu" / "Unter" / ".gitkeep").is_file()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=v, capture_output=True, text=True).stdout
        assert status == ""                                                             # alles committet
        log = subprocess.run(["git", "log", "--format=%s"], cwd=v, capture_output=True, text=True).stdout
        assert "In den Papierkorb: Moon/Rakete.md" in log and "Wiederhergestellt: Moon/Rakete.md" in log
