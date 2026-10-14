"""Interne Schnittstelle: Autor im Commit, Versionsprüfung, Ordner-Scope, Lesen und Suchen."""
import subprocess
from pathlib import Path

import pytest

from vaultserver.config import Config
from vaultserver.internal import InternalVault
from vaultserver.service import Service
from vaultserver.store import Conflict, Rejected


def git(v: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=v,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def svc(tmp_path: Path):
    v = tmp_path / "vault"
    (v / "Moon").mkdir(parents=True)
    (v / "Moon" / "Rakete.md").write_text("# Rakete\n\n- Status: offen\n\n## Antrieb\n\nSparschwein auf dem Mond\n", encoding="utf-8")
    (v / "Erde.md").write_text("# Erde\n\nSparschwein zuhause\n", encoding="utf-8")
    git(v, "init", "-q", "-b", "main")
    git(v, "add", "-A")
    git(v, "commit", "-q", "-m", "start")
    s = Service(Config(vault_path=v.resolve(), db_path=tmp_path / "data" / "idx.sqlite",
                       git_pull_seconds=0, watch_seconds=0))
    yield s
    s.index.close()


def test_author_lands_in_commit(svc):
    vault = InternalVault(svc, "agent:x")
    res = vault.write("Neu.md", "# Neu\n", message="angelegt")
    assert res["commit"] and git(svc.config.vault_path, "log", "-1", "--format=%an|%s") == "agent:x|agent:x: angelegt"
    r = vault.read("Moon/Rakete.md", section="Antrieb")
    vault.set_property("Moon/Rakete.md", "Status", "fertig", base_version=None)
    assert git(svc.config.vault_path, "log", "-1", "--format=%an") == "agent:x"
    assert "Sparschwein" in r["text"]


def test_conflict_on_stale_version(svc):
    vault = InternalVault(svc, "agent:x")
    old = vault.read("Erde.md")["version"]
    vault.write("Erde.md", "# Erde\n\nneu\n", base_version=old)
    with pytest.raises(Conflict) as e:
        vault.write("Erde.md", "# Erde\n\nnoch neuer\n", base_version=old)
    assert e.value.text == "# Erde\n\nneu\n"
    with pytest.raises(Conflict):
        vault.write("Erde.md", "# Erde\n\nohne Version\n")
    with pytest.raises(Conflict):
        vault.set_property("Erde.md", "Status", "x", base_version=old)


def test_search_query_outline(svc):
    vault = InternalVault(svc, "agent:x")
    assert {h["path"] for h in vault.search("Sparschwein")} == {"Moon/Rakete.md", "Erde.md"}
    assert [n["path"] for n in vault.query(["Status=offen"])] == ["Moon/Rakete.md"]
    assert [o["heading"] for o in vault.outline("Moon/Rakete.md")][-1] == "Antrieb"


def test_folder_scope(svc):
    moon = InternalVault(svc, "agent:mond", scope="Moon")
    assert [h["path"] for h in moon.search("Sparschwein")] == ["Rakete.md"]       # relativ, nur im Ordner
    assert moon.read("Rakete.md")["path"] == "Rakete.md"
    res = moon.write("Neu.md", "# Neu\n")
    assert res["path"] == "Neu.md" and (svc.config.vault_path / "Moon" / "Neu.md").is_file()
    with pytest.raises(ValueError):
        moon.write("../Erde.md", "# x\n")
    with pytest.raises(ValueError):
        InternalVault(svc, "agent:x", scope="Gibtsnicht")


def test_claim_release(svc):
    a, b = InternalVault(svc, "agent:a"), InternalVault(svc, "agent:b")
    a.claim("Erde.md", note="arbeite")
    with pytest.raises(Rejected):
        b.claim("Erde.md")
    a.release("Erde.md")
    assert b.claim("Erde.md")["agent"] == "agent:b"


def test_bad_author(svc):
    with pytest.raises(ValueError):
        InternalVault(svc, "")
