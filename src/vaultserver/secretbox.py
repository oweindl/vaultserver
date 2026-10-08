"""Kleine Verschlüsselung für gespeicherte Zugangsdaten (Git-Tokens in data/repos.json).

Schlüssel ist das Server-Geheimnis (data/secret bzw. [server] secret). Nur Standardbibliothek:
Schlüsselstrom aus HMAC-SHA256 im Zählermodus, danach HMAC über Nonce und Geheimtext (encrypt-then-MAC).
Schützt Tokens in Sicherungen und Kopien von repos.json ohne data/secret; wer beides hat, kann entschlüsseln.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from .config import Config

PREFIX = "v1:"


def server_secret(config: Config) -> bytes:
    if config.secret:
        return config.secret.encode()
    f = config.db_path.parent / "secret"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip().encode()


def _keys(secret: bytes) -> tuple[bytes, bytes]:
    return (hmac.new(secret, b"vaultserver-enc", hashlib.sha256).digest(),
            hmac.new(secret, b"vaultserver-mac", hashlib.sha256).digest())


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:n]


def seal(secret: bytes, text: str) -> str:
    if not text:
        return ""
    enc, mac = _keys(secret)
    nonce = secrets.token_bytes(16)
    data = text.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(enc, nonce, len(data))))
    tag = hmac.new(mac, nonce + ct, hashlib.sha256).digest()
    return PREFIX + base64.urlsafe_b64encode(nonce + tag + ct).decode()


def open_(secret: bytes, box: str) -> str:
    if not box:
        return ""
    if not box.startswith(PREFIX):
        raise ValueError("Unbekanntes Format")
    raw = base64.urlsafe_b64decode(box[len(PREFIX):].encode())
    nonce, tag, ct = raw[:16], raw[16:48], raw[48:]
    enc, mac = _keys(secret)
    if not hmac.compare_digest(tag, hmac.new(mac, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("Gespeichertes Token passt nicht zum Server-Geheimnis (data/secret geändert?)")
    return bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct)))).decode()
