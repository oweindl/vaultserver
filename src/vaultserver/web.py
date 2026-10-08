"""FastAPI-Anwendung: Web-Oberfläche (/), REST (/api/…) und MCP (/mcp) auf einem Port."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import secrets
import time
from pathlib import Path, PurePosixPath

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Config
from .gitops import ensure_vault
from .secretbox import server_secret
from .index import Index
from .mcp_server import build_mcp
from .render import render
from . import sizes
from .optimize import Optimizer
from .scope import limits_for, load_projects
from .scope import SCOPE_HEADER
from .service import Service
from .store import Conflict, Rejected

log = logging.getLogger("vaultserver")
STATIC = Path(__file__).parent / "static"
COOKIE = "vs_session"
SESSION_DAYS = 30


# ------------------------------------------------------------------ Passwörter und Sitzungen

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), h)
    except ValueError:
        return False


def _secret(config: Config) -> bytes:
    return server_secret(config)


MIN_PASSWORD = 8


class Users:
    """Web-Benutzer: Hashes aus der Konfiguration, im Browser geänderte Passwörter in
    data/users.json (hat Vorrang). Sitzungen hängen am Passwort-Hash: ein neues
    Passwort beendet alle anderen Sitzungen."""

    def __init__(self, config: Config):
        self.base = dict(config.users)
        self.file = config.db_path.parent / "users.json"

    def _overrides(self) -> dict[str, str]:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, user: str) -> str | None:
        return self._overrides().get(user) or self.base.get(user)

    def set(self, user: str, password: str) -> None:
        data = self._overrides()
        data[user] = hash_password(password)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.file)

    def stamp(self, user: str) -> str:
        stored = self.get(user) or ""
        return hashlib.sha256(stored.encode()).hexdigest()[:12]


def make_session(user: str, key: bytes, stamp: str) -> str:
    payload = f"{user}|{int(time.time()) + SESSION_DAYS * 86400}|{stamp}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def read_session(cookie: str, key: bytes, users: Users) -> str | None:
    try:
        user, exp, stamp, sig = base64.urlsafe_b64decode(cookie.encode()).decode().rsplit("|", 3)
    except Exception:
        return None
    good = hmac.new(key, f"{user}|{exp}|{stamp}".encode(), hashlib.sha256).hexdigest()
    if (hmac.compare_digest(good, sig) and int(exp) > time.time()
            and users.get(user) and hmac.compare_digest(stamp, users.stamp(user))):
        return user
    return None


# ------------------------------------------------------------------ Anwendung

def git_info(config: Config, svc: Service) -> dict:
    """Remote (ohne Zugangsdaten), Branch und letzter Pull/Push des Vault-Repos."""
    git = svc.store.git.root
    if not git.enabled:
        return {"enabled": False}
    return {"enabled": True, "remote": git.remote_url(config.git_remote), "branch": git.current_branch(),
            "push": config.git_push, **svc.store.git_status}


def create_app(config: Config, start_background: bool = True) -> FastAPI:
    done = ensure_vault(config)
    if done:
        log.info("Vault %s", done)
    svc = Service(config)
    mcp = build_mcp(svc)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    key = _secret(config)
    users = Users(config)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_background:
            svc.start()
        async with mcp.session_manager.run():
            yield
        svc.stop()

    app = FastAPI(title="VaultServer", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.svc = svc

    class Auth(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            auth = request.headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            found = svc.clients.lookup_full(token) if token else None
            machine, bound = found if found else (None, None)
            if path == "/mcp" or path.startswith("/mcp/"):
                if not machine:
                    return JSONResponse({"error": "Bearer-Token fehlt oder ist ungültig"}, status_code=401)
                # /mcp/<projekt> -> /mcp mit Projekt-Kopfzeile; ein projektgebundener Token gilt immer nur dort
                wanted = path[len("/mcp/"):].strip("/") if path.startswith("/mcp/") else ""
                if wanted and wanted not in svc.projects():
                    return JSONResponse({"error": f"Projekt „{wanted}“ gibt es nicht",
                                         "projects": sorted(svc.projects())}, status_code=404)
                if bound and wanted and wanted != bound:
                    return JSONResponse({"error": f"Dieser Zugang gilt nur für das Projekt „{bound}“"}, status_code=403)
                scope_name = bound or wanted
                request.scope["path"] = "/mcp"
                request.scope["raw_path"] = b"/mcp"
                hdrs = [(k, v) for k, v in request.scope["headers"] if k.lower() != SCOPE_HEADER.encode()]
                if scope_name:
                    hdrs.append((SCOPE_HEADER.encode(), scope_name.encode()))
                request.scope["headers"] = hdrs
                return await call_next(request)
            if path.startswith("/api/drop/"):
                return await call_next(request)   # Einmal-Adresse ist selbst das Geheimnis
            if path.startswith("/api/") and path not in ("/api/login", "/api/logout"):
                user = read_session(request.cookies.get(COOKIE, ""), key, users)
                if not user and not machine:
                    return JSONResponse({"error": "Anmeldung erforderlich"}, status_code=401)
                if not user and bound and path != "/api/me":
                    return JSONResponse({"error": f"Dieser Zugang gilt nur für MCP im Projekt „{bound}“"}, status_code=403)
                request.state.user = user
                request.state.agent = f"web/{user}" if user else f"{machine}/api"
            return await call_next(request)

    app.add_middleware(Auth)

    def agent(request: Request) -> str:
        return getattr(request.state, "agent", "web/unbekannt")

    def resolver(note: str):
        idx: Index = svc.index
        files = [r["path"] for r in idx.db.execute("SELECT path FROM notes UNION ALL SELECT path FROM attachments")]
        by_lower = {f.lower(): f for f in files}
        by_name: dict[str, list[str]] = {}
        for f in files:
            n = PurePosixPath(f).name.lower()
            by_name.setdefault(n, []).append(f)
            if n.endswith(".md"):
                by_name.setdefault(n[:-3], []).append(f)
        aliases = {r["alias_norm"]: r["path"] for r in idx.db.execute(
            "SELECT a.alias_norm, n.path FROM aliases a JOIN notes n ON n.id = a.note_id")}
        return lambda target, kind: Index._resolve(target, kind, note, by_lower, by_name, aliases)

    def err(e: Exception):
        if isinstance(e, Conflict):
            return JSONResponse(e.as_dict(), status_code=409)
        if isinstance(e, Rejected):
            return JSONResponse(e.as_dict(), status_code=422)
        if isinstance(e, KeyError):
            return JSONResponse({"error": "not_found", "message": e.args[0] if e.args else str(e)}, status_code=404)
        if isinstance(e, ValueError):
            return JSONResponse({"error": "bad_request", "message": str(e)}, status_code=400)
        raise e

    # -------------------------------------------------------------- Anmeldung

    @app.post("/api/login")
    def login(data: dict = Body(...)):
        user, pw = data.get("user", ""), data.get("password", "")
        stored = users.get(user)
        if not stored or not check_password(pw, stored):
            time.sleep(0.5)
            return JSONResponse({"error": "Benutzer oder Passwort falsch"}, status_code=401)
        resp = JSONResponse({"user": user})
        resp.set_cookie(COOKIE, make_session(user, key, users.stamp(user)), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax")
        return resp

    @app.post("/api/password")
    def change_password(request: Request, data: dict = Body(...)):
        user = getattr(request.state, "user", None)
        if not user:
            return JSONResponse({"error": "Nur mit Web-Anmeldung möglich"}, status_code=403)
        old, new = data.get("old", ""), data.get("new", "")
        if not check_password(old, users.get(user) or ""):
            time.sleep(0.5)
            return JSONResponse({"error": "Bisheriges Passwort ist falsch"}, status_code=400)
        if len(new) < MIN_PASSWORD:
            return JSONResponse({"error": f"Neues Passwort braucht mindestens {MIN_PASSWORD} Zeichen"}, status_code=400)
        if new == old:
            return JSONResponse({"error": "Neues Passwort ist gleich dem bisherigen"}, status_code=400)
        users.set(user, new)
        log.info("Passwort geändert für %s", user)
        # Diese Sitzung bleibt angemeldet, alle anderen enden
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, make_session(user, key, users.stamp(user)), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="lax")
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/me")
    def me(request: Request):
        return {"agent": agent(request), "user": getattr(request.state, "user", None), "areas": [a.name for a in config.areas],
                "semantic": bool(svc.semantic), "last_error": svc.last_error,
                "git": svc.store.git.enabled, "push": config.git_push or any(r.push for r in svc.repos.list())}

    # -------------------------------------------------------------- Einrichtung: MCP-Zugänge

    def web_user(request: Request) -> str | None:
        return getattr(request.state, "user", None)

    def only_web():
        return JSONResponse({"error": "Nur mit Web-Anmeldung möglich"}, status_code=403)

    @app.get("/api/setup")
    async def setup(request: Request):
        if not web_user(request):
            return only_web()
        tools = await mcp.list_tools()
        return {
            "tools": [{"name": t.name, "description": (t.description or "").split("\n")[0],
                       "readonly": bool(t.annotations and getattr(t.annotations, "read_only_hint", getattr(t.annotations, "readOnlyHint", False)))} for t in tools],
            "clients": svc.clients.list(),
            "projects": [{"name": p.name, "folder": p.folder, "start": p.start or None,
                          "repo": p.folder in config.repo_folders} for p in svc.projects().values()],
            "areas": [a.name for a in config.areas],
            "vault": config.vault_path.name,
            "git_push": config.git_push,
            "git": git_info(config, svc),
        }

    # -------------------------------------------------------------- Einrichtung: eingebundene Repos

    def repo_rows() -> list[dict]:
        rows = []
        for r in svc.repos.list():
            try:
                rows.append(svc.repos.info(r.name))
            except Exception as e:  # noqa: BLE001 – ein kaputtes Repo darf die Liste nicht verhindern
                rows.append({**r.public(), "error": svc.store.git.redact(str(e))})
        return rows

    def repo_err(e: Exception):
        return JSONResponse({"error": svc.store.git.redact(str(e))}, status_code=400)

    def push_pending(name: str) -> None:
        """Offene Commits eines Repos vor Branch-Wechsel oder Entfernen pushen (Fehler stehen im Status)."""
        if name in svc.store.dirty:
            try:
                svc.store.push()
            except Exception:  # noqa: BLE001
                pass

    @app.get("/api/repos")
    def repos_list(request: Request):
        if not web_user(request):
            return only_web()
        g = svc.store.git.root
        root = {"name": config.vault_path.name, "cloned": g.enabled, **git_info(config, svc)}
        if g.enabled:
            root["head"] = g.head()[:10]
        return {"root": root, "repos": repo_rows(), "schemes": config.repo_schemes}

    @app.post("/api/vault/sync")
    def vault_sync(request: Request):
        """Stamm-Vault jetzt pushen (falls etwas offen ist) und holen."""
        if not web_user(request):
            return only_web()
        svc.store.dirty.add("")
        for step in (svc.store.push, lambda: svc.store.pull(only={""})):
            try:
                step()
            except Exception:  # noqa: BLE001 – steht im Status
                pass
        return git_info(config, svc)

    @app.post("/api/repos/test")
    def repos_test(request: Request, data: dict = Body(...)):
        if not web_user(request):
            return only_web()
        try:
            return svc.repos.test(data.get("url", ""), data.get("token", ""), data.get("username") or "x-access-token",
                                  data.get("name", ""))
        except Exception as e:  # noqa: BLE001
            return repo_err(e)

    @app.post("/api/repos")
    def repos_add(request: Request, data: dict = Body(...)):
        if not web_user(request):
            return only_web()
        try:
            r = svc.repos.add(data.get("name", ""), data.get("url", ""), branch=data.get("branch", ""),
                              token=data.get("token", ""), username=data.get("username") or "x-access-token",
                              committer=data.get("committer", ""), push=data.get("push", True),
                              pull_seconds=data.get("pull_seconds"), agent=f"web/{web_user(request)}")
        except Exception as e:  # noqa: BLE001
            return repo_err(e)
        with svc.lock:
            svc.index.sync()
        svc.store.ensure_bin()
        svc.feed.publish(["."], "Repo eingebunden", tree=True)
        log.info("Repo eingebunden: %s (%s, von %s)", r.name, r.public()["url"], web_user(request))
        return svc.repos.info(r.name)

    @app.put("/api/repos/{name}")
    def repos_update(request: Request, name: str, data: dict = Body(...)):
        if not web_user(request):
            return only_web()
        if data.get("branch"):
            push_pending(name)
        try:
            r = svc.repos.update(name, token=data.get("token"), branch=data.get("branch"),
                                 username=data.get("username"), committer=data.get("committer"),
                                 push=data.get("push"), pull_seconds=data.get("pull_seconds"))
        except Exception as e:  # noqa: BLE001
            return repo_err(e)
        with svc.lock:
            svc.index.sync()
        log.info("Repo geändert: %s (von %s)", r.name, web_user(request))
        return svc.repos.info(r.name)

    @app.post("/api/repos/{name}/sync")
    def repos_sync(request: Request, name: str):
        if not web_user(request):
            return only_web()
        try:
            svc.repos.get(name)
            svc.store.dirty.add(name)
            try:
                svc.store.push()
            except Exception:  # noqa: BLE001 – steht im Status
                pass
            try:
                svc.store.pull(only={name})
            except Exception:  # noqa: BLE001
                pass
            return svc.repos.info(name)
        except Exception as e:  # noqa: BLE001
            return repo_err(e)

    @app.delete("/api/repos/{name}")
    def repos_remove(request: Request, name: str, force: bool = False):
        if not web_user(request):
            return only_web()
        if not force:
            push_pending(name)
        try:
            out = svc.repos.remove(name, force=force)
        except Exception as e:  # noqa: BLE001
            return repo_err(e)
        with svc.lock:
            svc.index.sync()
        svc.store.dirty.discard(name)
        svc.feed.publish(["."], "Repo entfernt", tree=True)
        log.info("Repo entfernt: %s (von %s)", name, web_user(request))
        return out

    @app.post("/api/clients")
    def client_create(request: Request, data: dict = Body(...)):
        if not web_user(request):
            return only_web()
        try:
            project = data.get("project") or None
            if project and project not in svc.projects():
                raise ValueError(f"Projekt „{project}“ gibt es nicht")
            out = svc.clients.create(data.get("name", ""), project)
        except ValueError as e:
            return err(e)
        log.info("MCP-Zugang angelegt: %s (von %s)", out["name"], web_user(request))
        return out

    @app.put("/api/clients/{cid}")
    def client_update(request: Request, cid: str, data: dict = Body(...)):
        if not web_user(request):
            return only_web()
        project = data.get("project") or None
        try:
            if project and project not in svc.projects():
                raise ValueError(f"Projekt „{project}“ gibt es nicht")
            out = svc.clients.set_project(cid, project)
        except (KeyError, ValueError) as e:
            return err(e)
        log.info("MCP-Zugang %s: Projekt %s (von %s)", out["name"], project or "alle", web_user(request))
        return out

    @app.post("/api/clients/{cid}/renew")
    def client_renew(request: Request, cid: str):
        if not web_user(request):
            return only_web()
        try:
            out = svc.clients.renew(cid)
        except (KeyError, ValueError) as e:
            return err(e)
        log.info("MCP-Zugang erneuert: %s (von %s)", out["name"], web_user(request))
        return out

    @app.delete("/api/clients/{cid}")
    def client_revoke(request: Request, cid: str):
        if not web_user(request):
            return only_web()
        try:
            name = svc.clients.revoke(cid)
        except KeyError as e:
            return err(e)
        log.info("MCP-Zugang gesperrt: %s (von %s)", name, web_user(request))
        return {"ok": True, "name": name}

    # -------------------------------------------------------------- lesen

    @app.get("/api/tree")
    def tree():
        with svc.lock:
            notes = [dict(r) for r in svc.index.db.execute("SELECT path, title, size, mtime FROM notes ORDER BY path")]
            atts = [dict(r) for r in svc.index.db.execute("SELECT path, type, size, mtime FROM attachments ORDER BY path")]
        vault, skip = config.vault_path, set(config.exclude)
        folders = []
        for dirpath, dirs, _files in os.walk(vault):
            dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))
            rel = Path(dirpath).relative_to(vault).as_posix()
            if rel != ".":
                folders.append(rel)
        projects = load_projects(config)
        for n in notes:
            lim = limits_for(config, n["path"], projects)
            if (n["size"] or 0) > lim.soft_kb * 1024:
                n["big"] = lim.soft_kb
        return {"notes": notes, "attachments": atts, "folders": folders,
                "bin": {"folder": config.recycle_folder, "count": len(svc.store.bin_list())}}

    @app.get("/api/note")
    def note(path: str, raw: bool = False):
        try:
            with svc.lock:
                res = svc.store.read_tracked(path)
                if raw:
                    return res
                res["html"] = render(res["text"], path, resolver(path))
                res["outline"] = svc.index.outline(path)
                res["backlinks"] = svc.index.backlinks(path)
                # Seitenleiste: nur der Eigenschaftsblock oben (wie bei der Regelprüfung)
                top_end = next((o["line_start"] for o in res.get("outline") or svc.index.outline(path)
                                if o["level"] >= 2), 10**9)
                res["properties"] = [p for p in svc.index.note_properties(path)
                                     if p["source"] == "frontmatter" or (p["line"] or 0) < top_end]
                res["title"] = svc.index._note(path)["title"]
                area = config.area_for(path)
                res["area"] = area.name if area else None
                res["violations"] = sorted(svc.store.violations(path, res["text"]))
            res["history"] = svc.history(path, 15)
            return res
        except Exception as e:
            return err(e)

    @app.get("/api/file/{path:path}")
    def file(path: str):
        try:
            full = svc.store._abs(path)
        except Rejected as e:
            return err(e)
        if not full.is_file():
            raise HTTPException(404)
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return FileResponse(full, media_type=mime)

    @app.get("/api/search")
    def search(q: str, folder: str | None = None, archive: bool = False, mode: str = "text", limit: int = 30):
        try:
            return svc.search(q, mode=mode, folder=folder, include_archive=archive, limit=limit)
        except Exception as e:
            return err(e)

    @app.get("/api/query")
    def query(f: list[str] = [], folder: str | None = None, archive: bool = False):
        from .cli import parse_filter
        try:
            with svc.lock:
                return svc.index.query([parse_filter(x) for x in f], folder=folder, include_archive=archive)
        except Exception as e:
            return err(e)

    @app.get("/api/complete")
    def complete(q: str = "", limit: int = 15):
        """Vorschläge für die Wikilink-Vervollständigung im Editor."""
        like = f"%{q.lower()}%"
        with svc.lock:
            rows = svc.index.db.execute(
                "SELECT path, title FROM notes WHERE lower(path) LIKE ? OR lower(title) LIKE ?"
                " ORDER BY length(path) LIMIT ?", (like, like, limit)).fetchall()
            atts = svc.index.db.execute(
                "SELECT path FROM attachments WHERE lower(path) LIKE ? LIMIT 5", (like,)).fetchall()
        return [{"path": r["path"], "title": r["title"], "link": PurePosixPath(r["path"]).stem} for r in rows] + \
               [{"path": r["path"], "title": PurePosixPath(r["path"]).name, "link": PurePosixPath(r["path"]).name}
                for r in atts]

    @app.get("/api/recent")
    def recent(limit: int = 30):
        return svc.recent(limit)

    @app.get("/api/tasks")
    def tasks(folder: str | None = None, done: bool = False):
        with svc.lock:
            return svc.index.tasks(folder=folder, done=done)

    @app.get("/api/lint")
    def lint(area: str | None = None):
        try:
            return svc.store.lint(area)
        except Exception as e:
            return err(e)

    @app.get("/api/claims")
    def claims():
        with svc.lock:
            return svc.store.claims()

    @app.get("/api/guide")
    def guide(area: str | None = None):
        try:
            return svc.guide(area)
        except Exception as e:
            return err(e)

    # -------------------------------------------------------------- schreiben

    @app.put("/api/note")
    def put_note(request: Request, data: dict = Body(...)):
        try:
            path = svc.store._norm_path(data["path"])
            if not (config.vault_path / path).exists():
                sizes.check_new_note(svc.store, path, data["text"])
            res = svc.store.write(path, data["text"], agent(request),
                                  base_version=data.get("base_version"), message=data.get("message"),
                                  force=bool(data.get("force")))
            res.update(sizes.hints(svc.store, path))
            return res
        except Exception as e:
            return err(e)

    @app.post("/api/task")
    def toggle_task(request: Request, data: dict = Body(...)):
        """Checkbox in Zeile `line` umschalten (Version wird geprüft)."""
        path, line = data["path"], int(data["line"])
        try:
            with svc.lock:
                text, ver = svc.store.current(path)
                if text is None:
                    raise KeyError(path)
                if data.get("base_version") and data["base_version"] != ver:
                    raise Conflict(path, ver, text)
                lines = text.split("\n")
                l = lines[line - 1]
                import re
                m = re.match(r"^(\s*[-*+]\s+\[)([ xX])(\].*)$", l)
                if not m:
                    raise Rejected(f"Zeile {line} ist keine Checkbox")
                lines[line - 1] = m.group(1) + (" " if m.group(2) != " " else "x") + m.group(3)
                return svc.store.write(path, "\n".join(lines), agent(request), base_version=ver,
                                       message=f"{PurePosixPath(path).stem}: Checkbox Zeile {line}")
        except Exception as e:
            return err(e)

    @app.post("/api/property")
    def set_prop(request: Request, data: dict = Body(...)):
        try:
            return svc.store.set_property(data["path"], data["key"], data["value"], agent(request),
                                          base_version=data.get("base_version"))
        except Exception as e:
            return err(e)

    @app.post("/api/move")
    def move(request: Request, data: dict = Body(...)):
        try:
            return svc.store.move(data["source"], data["target"], agent(request))
        except Exception as e:
            return err(e)

    @app.delete("/api/note")
    def delete(request: Request, path: str, base_version: str | None = None):
        """Notiz, Anhang oder Ordner in den Papierkorb."""
        try:
            return svc.store.trash(path, agent(request), base_version=base_version)
        except Exception as e:
            return err(e)

    @app.post("/api/folder")
    def folder_create(request: Request, data: dict = Body(...)):
        try:
            return svc.store.mkdir(data.get("path", ""), agent(request))
        except Exception as e:
            return err(e)

    @app.get("/api/trash")
    def trash_list():
        return svc.store.bin_list()

    @app.post("/api/trash/{entry_id}/restore")
    def trash_restore(request: Request, entry_id: str):
        try:
            return svc.store.restore(entry_id, agent(request))
        except Exception as e:
            return err(e)

    @app.delete("/api/trash/{entry_id}")
    def trash_purge(request: Request, entry_id: str):
        try:
            return svc.store.purge(entry_id, agent(request))
        except Exception as e:
            return err(e)

    @app.delete("/api/trash")
    def trash_empty(request: Request):
        try:
            return svc.store.purge(None, agent(request))
        except Exception as e:
            return err(e)

    @app.post("/api/optimize/plan")
    def optimize_plan(data: dict = Body(...)):
        try:
            with svc.lock:
                p = Optimizer(svc.store).plan(data.get("path", ""), data.get("level") or None, data.get("description"))
            p.pop("_other", None)
            return p
        except Exception as e:
            return err(e)

    @app.post("/api/optimize/apply")
    def optimize_apply(request: Request, data: dict = Body(...)):
        try:
            return Optimizer(svc.store).apply(data.get("path", ""), agent(request), data.get("base_version", ""),
                                              data.get("level") or None, data.get("description"))
        except Exception as e:
            return err(e)

    @app.get("/api/events")
    async def events(request: Request):
        """Server-Sent Events: Änderungen am Vault (Baum, offene Notiz, Listen live aktualisieren)."""
        last = request.headers.get("last-event-id") or request.query_params.get("since")
        start = int(last) if last and last.isdigit() else svc.feed.rev

        async def stream():
            rev, idle = start, 0.0
            yield f"retry: 3000\nid: {rev}\ndata: {json.dumps({'hello': True, 'rev': rev})}\n\n"
            while not await request.is_disconnected():
                evs, reset = svc.feed.since(rev)
                if reset:
                    rev = svc.feed.rev
                    yield f"id: {rev}\ndata: {json.dumps({'reset': True, 'rev': rev})}\n\n"
                for e in evs:
                    rev = e["rev"]
                    yield f"id: {rev}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                idle = 0.0 if evs else idle + 0.5
                if idle >= 20:
                    idle = 0.0
                    yield ": ping\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/projects")
    def projects():
        return [{"name": p.name, "folder": p.folder, "start": p.start or None} for p in svc.projects().values()]

    @app.post("/api/create")
    def create(request: Request, data: dict = Body(...)):
        try:
            return svc.store.create_from_template(data["area"], data["title"], data.get("fields", {}),
                                                  agent(request), summary=data.get("summary", ""))
        except Exception as e:
            return err(e)

    @app.post("/api/upload")
    async def upload(request: Request, folder: str, file: UploadFile, name: str | None = None, overwrite: bool = False):
        """Datei hochladen (beliebiger Typ). name: anderer Dateiname (z. B. für eingefügte Bilder)."""
        limit = config.max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        fname = PurePosixPath((name or file.filename or "").replace("\\", "/")).name
        target = f"{folder.strip('/')}/{fname}" if folder.strip("/") else fname
        try:
            return svc.store.upload(target, data, agent(request), overwrite=overwrite)
        except Exception as e:
            return err(e)

    @app.api_route("/api/drop/{did}", methods=["PUT", "POST"])
    async def drop(request: Request, did: str):
        """Einmal-Upload aus upload_link: PUT mit der Datei als Body oder POST multipart (Feld file)."""
        d = svc.drops.take(did)
        if not d:
            return JSONResponse({"error": "not_found", "message": "Adresse unbekannt, abgelaufen oder schon benutzt"}, status_code=404)
        limit = config.max_upload_mb * 1024 * 1024
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            f = form.get("file")
            if f is None or not hasattr(f, "read"):
                return JSONResponse({"error": "bad_request", "message": "Feld „file“ fehlt"}, status_code=400)
            data = await f.read(limit + 1)
        else:
            buf = bytearray()
            async for chunk in request.stream():
                buf += chunk
                if len(buf) > limit:
                    break
            data = bytes(buf)
        try:
            res = svc.store.upload(d["path"], data, d["agent"], overwrite=d["overwrite"])
            res["embed"] = f"![[{res['path']}]]"
            return res
        except Exception as e:
            return err(e)

    @app.post("/api/claim")
    def claim(request: Request, data: dict = Body(...)):
        try:
            if data.get("release"):
                return svc.store.release(data["path"], agent(request), force=True)
            return svc.store.claim(data["path"], agent(request), note=data.get("note", ""))
        except Exception as e:
            return err(e)

    @app.post("/api/refresh")
    def refresh(request: Request, data: dict = Body(...)):
        try:
            what = data.get("what")
            if what == "status":
                return svc.store.refresh_status(agent(request))
            if what == "commits":
                return svc.store.link_commits(agent(request))
            return svc.store.refresh_overview(data["area"], agent(request))
        except Exception as e:
            return err(e)

    # -------------------------------------------------------------- MCP und Oberfläche

    for route in mcp_app.routes:
        app.router.routes.append(route)

    @app.get("/healthz")
    def health():
        st = svc.store.git_status
        # ohne Anmeldung: nur ob Pull/Push zuletzt geklappt haben, kein Remote, keine Fehlertexte
        git = {k: (v["ok"] if v else None) for k, v in st.items()}
        return {"ok": True, "notes": svc.index.stats()["notes"], "last_error": svc.last_error, "git": git}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "max-age=86400"})

    @app.get("/static/{name:path}")
    def static(name: str):
        f = (STATIC / name).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f, headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-cache"})

    return app
