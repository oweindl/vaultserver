"""Nur lesender Funktionstest gegen einen laufenden VaultServer (sicher für den echten Vault).

Aufruf: python scripts/smoke.py http://<host>:8100/mcp <token>
"""

import asyncio
import sys

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

READ_ONLY = {"guide", "search", "query", "outline", "read", "list", "backlinks", "tasks", "recent",
             "changes_since", "lint", "property_keys", "claims"}


async def main(url: str, token: str) -> int:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with Client(streamable_http_client(url, http_client=http)) as c:
        async def call(name: str, **args):
            assert name in READ_ONLY, f"{name} schreibt – im Smoke-Test verboten"
            r = await c.call_tool(name, args)
            if r.is_error:
                raise RuntimeError(f"{name}: {r.content[0].text}")
            sc = r.structured_content
            return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc

        tools = {t.name for t in (await c.list_tools()).tools}
        print(f"Werkzeuge: {len(tools)}")
        g = await call("guide")
        print("Bereiche:", ", ".join(a["name"] for a in g["areas"]))
        hits = await call("search", text="Status", limit=3)
        print("Suche:", len(hits), "Treffer")
        if hits:
            ol = await call("outline", path=hits[0]["path"])
            r = await call("read", path=hits[0]["path"], section=ol[-1]["heading"] if ol else None)
            print("Lesen:", hits[0]["path"], len(r["text"]), "Zeichen")
        print("Letzte Änderung:", (await call("recent", limit=1))[0]["path"])
        print("Prüfung:", len(await call("lint")), "Hinweise")
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
