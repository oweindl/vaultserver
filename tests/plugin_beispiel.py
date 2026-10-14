"""Test-Plug-in: hängt Routen ein, merkt sich Start/Stopp und Schreib-Ereignisse (nur für die Tests)."""
from fastapi import APIRouter, Request

from vaultserver.store import Rejected

CALLS: list = []
EVENTS: list = []
REJECT = False      # True: before_write lehnt alles ab


def register(ctx):
    CALLS.append(("register", ctx.name, dict(ctx.settings)))
    geschuetzt = APIRouter()

    @geschuetzt.get("/hallo")
    def hallo(request: Request):
        return {"hallo": ctx.settings.get("gruss", "welt"), "agent": getattr(request.state, "agent", None)}

    offen = APIRouter()

    @offen.get("/ping")
    def ping():
        return {"pong": True}

    ctx.include_router(geschuetzt, prefix="/beispiel")
    ctx.include_router(offen, prefix="/beispiel-offen", auth="own")
    ctx.on_startup(lambda: CALLS.append(("startup", ctx.name)))
    ctx.on_shutdown(lambda: CALLS.append(("shutdown", ctx.name)))

    def before(ev):
        EVENTS.append(("before", ev))
        if REJECT or "VERBOTEN" in (ev.content_after or ""):
            raise Rejected("Beispiel-Plug-in lehnt ab", ["Inhalt: VERBOTEN"])

    def after(ev):
        EVENTS.append(("after", ev))
        if "KAPUTT" in (ev.content_after or ""):
            raise RuntimeError("after-Hook kaputt")

    ctx.add_hook("before_write", before)
    ctx.add_hook("after_write", after)
