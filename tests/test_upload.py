"""Hochladen von Bildern/Binärdateien: Web, MCP upload und read_file, kein stilles Überschreiben, Größengrenze."""
import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vaultserver.config import Config
from vaultserver.web import create_app, hash_password

H = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": "Bearer tok"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture
def env(tmp_path: Path):
    v = tmp_path / "vault"
    (v / "Moon").mkdir(parents=True)
    (v / "Moon" / "A.md").write_text("# A\n", encoding="utf-8")
    cfg = Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite", users={"oliver": hash_password("altes-passwort")},
                 tokens={"tok": "ubuntu1"}, git_commit=False, git_pull_seconds=0, watch_seconds=0, max_upload_mb=1)
    c = TestClient(create_app(cfg, start_background=False))
    c.__enter__()
    assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
    yield c, v
    c.__exit__(None, None, None)


def call(c, tool, args, url="/mcp"):
    return c.post(url, headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}).json()["result"]


def test_web_upload(env):
    c, v = env
    up = lambda **kw: c.post("/api/upload", params={"folder": "Moon/Bilder", **kw}, files={"file": ("bild.png", PNG, "image/png")})
    r1 = up().json()
    assert r1["path"] == "Moon/Bilder/bild.png" and not r1["renamed"]
    r2 = up().json()
    assert r2["path"] == "Moon/Bilder/bild (2).png" and r2["renamed"]                      # nicht überschrieben
    assert up(overwrite="true").json()["path"] == "Moon/Bilder/bild.png"                  # bewusst ersetzen
    assert up(name="Bild 2026-10-06 061200.png").json()["path"] == "Moon/Bilder/Bild 2026-10-06 061200.png"
    assert (v / "Moon" / "Bilder" / "bild (2).png").read_bytes() == PNG
    big = c.post("/api/upload", params={"folder": "Moon"}, files={"file": ("gross.bin", b"x" * (1024 * 1024 + 10))})
    assert big.status_code == 422 and "zu groß" in big.json()["message"] and not (v / "Moon" / "gross.bin").exists()
    assert c.post("/api/upload", params={"folder": "RecycleBin"}, files={"file": ("x.png", PNG)}).status_code == 422
    assert c.post("/api/upload", params={"folder": "Moon", "name": "../../etc/x.png"}, files={"file": ("x.png", PNG)}).json()["path"] == "Moon/x.png"
    assert any(a["path"] == "Moon/Bilder/bild.png" for a in c.get("/api/tree").json()["attachments"])


def test_mcp_upload_and_read_file(env):
    c, v = env
    r = call(c, "upload", {"path": "Bilder/plan.png", "data_base64": base64.b64encode(PNG).decode()}, url="/mcp/moon")
    sc = r["structuredContent"]
    assert sc["path"] == "Bilder/plan.png" and sc["embed"] == "![[Moon/Bilder/plan.png]]" and (v / "Moon" / "Bilder" / "plan.png").read_bytes() == PNG
    r2 = call(c, "upload", {"path": "Bilder/plan.png", "data_base64": base64.b64encode(PNG).decode()}, url="/mcp/moon")
    assert r2["structuredContent"]["path"] == "Bilder/plan (2).png"
    assert call(c, "upload", {"path": "x.png", "data_base64": "kein base64!"})["isError"]
    # Bild kommt als Bild zurück (Claude kann es ansehen)
    img = call(c, "read_file", {"path": "Bilder/plan.png"}, url="/mcp/moon")
    kinds = [x["type"] for x in img["content"]]
    assert "image" in kinds and next(x for x in img["content"] if x["type"] == "image")["mimeType"] == "image/png"
    # andere Datei als Base64, Notiz abgelehnt
    (v / "Moon" / "liste.pdf").write_bytes(b"%PDF-1.4 test")
    res = call(c, "read_file", {"path": "Moon/liste.pdf"})
    pdf = res.get("structuredContent") or __import__("json").loads(res["content"][0]["text"])
    assert base64.b64decode(pdf["data_base64"]) == b"%PDF-1.4 test" and pdf["type"] == "application/pdf"
    assert call(c, "read_file", {"path": "Moon/A.md"})["isError"]
    assert call(c, "read_file", {"path": "../Finance/x.png"}, url="/mcp/moon")["isError"]
