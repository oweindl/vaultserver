"""Plug-ins: Freischaltung, Laden, Routen mit Anmeldung, Lebenszyklus, Fehler beim Laden."""
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import plugin_beispiel
from vaultserver import plugins
from vaultserver.config import Config
from vaultserver.plugins import PluginError
from vaultserver.web import create_app, hash_password

INSTALLED = {
    "beispiel": "plugin_beispiel",
    "defekt": "plugin_defekt",
    "fremd": "plugin_fremd",
}


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    """Statt installierter Pakete: Entry Points der Gruppe vaultserver.plugins aus den Testmodulen."""
    eps = [EntryPoint(name=n, value=v, group=plugins.GROUP) for n, v in INSTALLED.items()]
    monkeypatch.setattr(plugins, "entry_points", lambda group: [e for e in eps if e.group == group])
    plugin_beispiel.CALLS.clear()
    plugin_beispiel.EVENTS.clear()


def cfg(tmp_path: Path, enabled=(), settings=None) -> Config:
    v = tmp_path / "vault"
    v.mkdir(exist_ok=True)
    (v / "A.md").write_text("# A\n", encoding="utf-8")
    return Config(vault_path=v, db_path=tmp_path / "data" / "idx.sqlite",
                  users={"oliver": hash_password("altes-passwort")}, tokens={"tok": "ubuntu1"},
                  git_commit=False, git_pull_seconds=0, watch_seconds=0,
                  plugins_enabled=list(enabled), plugin_settings=settings or {})


def test_config_plugins_block(tmp_path: Path):
    toml = tmp_path / "vaultserver.toml"
    toml.write_text('vault_path = "vault"\n[plugins]\nenabled = ["beispiel"]\n[plugins.beispiel]\ngruss = "moin"\n',
                    encoding="utf-8")
    c = Config.load(toml)
    assert c.plugins_enabled == ["beispiel"] and c.plugin_settings == {"beispiel": {"gruss": "moin"}}
    toml.write_text('vault_path = "vault"\n', encoding="utf-8")
    c = Config.load(toml)
    assert c.plugins_enabled == [] and c.plugin_settings == {}


def test_without_plugins_nothing_loaded(tmp_path: Path):
    app = create_app(cfg(tmp_path), start_background=False)
    assert app.state.plugins == []
    with TestClient(app) as c:
        assert c.get("/beispiel/hallo").status_code == 404
    assert plugin_beispiel.CALLS == []
    assert not app.state.svc.store.hooks


def test_enabled_plugin_routes_and_lifecycle(tmp_path: Path):
    app = create_app(cfg(tmp_path, ["beispiel"], {"beispiel": {"gruss": "moin"}}), start_background=False)
    assert [p.name for p in app.state.plugins] == ["beispiel"]
    assert plugin_beispiel.CALLS == [("register", "beispiel", {"gruss": "moin"})]
    with TestClient(app) as c:
        assert ("startup", "beispiel") in plugin_beispiel.CALLS
        # geschützte Route: wie /api nur mit Anmeldung oder Token
        assert c.get("/beispiel/hallo").status_code == 401
        r = c.get("/beispiel/hallo", headers={"Authorization": "Bearer tok"})
        assert r.status_code == 200 and r.json() == {"hallo": "moin", "agent": "ubuntu1/api"}
        assert c.post("/api/login", json={"user": "oliver", "password": "altes-passwort"}).status_code == 200
        assert c.get("/beispiel/hallo").json()["agent"] == "web/oliver"
        # ausdrücklich „eigene Anmeldung“: ohne Anmeldung erreichbar
        assert TestClient(app).get("/beispiel-offen/ping").json() == {"pong": True}
        # alles Übrige bleibt geschützt wie bisher
        assert TestClient(app).get("/api/tree").status_code == 401
    assert plugin_beispiel.CALLS[-1] == ("shutdown", "beispiel")


def test_installed_but_not_enabled_is_not_imported(tmp_path: Path):
    app = create_app(cfg(tmp_path, ["beispiel"]), start_background=False)   # plugin_fremd würde beim Import scheitern
    assert [p.name for p in app.state.plugins] == ["beispiel"]


@pytest.mark.parametrize("name, words", [("defekt", ["defekt", "register schlägt fehl"]),
                                         ("gibtsnicht", ["gibtsnicht", "nicht installiert"]),
                                         ("fremd", ["fremd", "darf nicht importiert"])])
def test_broken_plugin_stops_start(tmp_path: Path, name, words):
    with pytest.raises(PluginError) as e:
        create_app(cfg(tmp_path, [name]), start_background=False)
    assert all(w in str(e.value) for w in words)


def test_cli_serve_reports_plugin_error(tmp_path: Path, capsys, monkeypatch):
    from vaultserver import cli
    (tmp_path / "vault").mkdir()
    toml = tmp_path / "vaultserver.toml"
    toml.write_text('vault_path = "vault"\n[git]\ncommit = false\npull_seconds = 0\n[plugins]\nenabled = ["defekt"]\n',
                    encoding="utf-8")
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("Server darf nicht starten"))
    assert cli.main(["-c", str(toml), "serve"]) == 2
    assert "Plug-in" in capsys.readouterr().err


def test_router_prefix_must_be_own_path(tmp_path: Path, monkeypatch):
    from fastapi import APIRouter
    import types
    mod = types.ModuleType("plugin_falsch")
    mod.register = lambda ctx: ctx.include_router(APIRouter(), prefix="/mcp/x")
    monkeypatch.setitem(__import__("sys").modules, "plugin_falsch", mod)
    INSTALLED["falsch"] = "plugin_falsch"
    try:
        eps = [EntryPoint(name=n, value=v, group=plugins.GROUP) for n, v in INSTALLED.items()]
        monkeypatch.setattr(plugins, "entry_points", lambda group: eps)
        with pytest.raises(PluginError, match="/mcp"):
            create_app(cfg(tmp_path, ["falsch"]), start_background=False)
    finally:
        INSTALLED.pop("falsch")
