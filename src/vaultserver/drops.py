"""Hochladen ohne Base64 durch das Modell.

upload_link: einmal nutzbare Adresse (/api/drop/<id>, 15 Minuten gültig), an die ein Client die Datei direkt
schickt, z. B. `curl -T foto.jpg <url>`. Die Adresse ist das Geheimnis (32 Zufallsbytes), sie braucht keinen
Token; Ziel, Agent und overwrite stehen fest, sobald sie erzeugt wird.
upload_url: der Server holt eine Datei selbst aus dem Internet (nur öffentliche Adressen, Größengrenze).
"""
from __future__ import annotations

import ipaddress
import secrets
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

import httpx

TTL = 15 * 60


class Drops:
    def __init__(self):
        self._lock = threading.Lock()
        self._open: dict[str, dict] = {}

    def create(self, path: str, agent: str, overwrite: bool = False) -> tuple[str, float]:
        did = secrets.token_urlsafe(32)
        exp = time.time() + TTL
        with self._lock:
            now = time.time()
            for k in [k for k, v in self._open.items() if v["expires"] < now]:
                del self._open[k]
            self._open[did] = {"path": path, "agent": agent, "overwrite": overwrite, "expires": exp}
        return did, exp

    def take(self, did: str) -> dict | None:
        """Einmal einlösen: liefert den Auftrag und entfernt ihn (abgelaufen -> None)."""
        with self._lock:
            d = self._open.pop(did, None)
        if not d or d["expires"] < time.time():
            return None
        return d


def _public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"Adresse {host} nicht auflösbar")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"{host} zeigt auf eine interne Adresse ({ip}) – nur öffentliche Adressen erlaubt")


def fetch(url: str, max_bytes: int) -> tuple[bytes, str]:
    """Datei von einer öffentlichen http(s)-Adresse holen (höchstens 3 Weiterleitungen, Größengrenze)."""
    for _ in range(4):
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("Nur http- oder https-Adressen")
        _public(u.hostname)
        with httpx.Client(timeout=30, follow_redirects=False) as c, c.stream("GET", url) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            if r.status_code != 200:
                raise ValueError(f"Abruf fehlgeschlagen: HTTP {r.status_code}")
            if int(r.headers.get("content-length") or 0) > max_bytes:
                raise ValueError(f"Datei größer als {max_bytes // 1048576} MB")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ValueError(f"Datei größer als {max_bytes // 1048576} MB")
            return bytes(buf), r.headers.get("content-type", "")
    raise ValueError("Zu viele Weiterleitungen")
