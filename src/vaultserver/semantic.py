"""Optionale semantische Suche (Phase 6): Abschnitts-Embeddings über lokales Ollama.

Vektoren liegen als float32-Blob in der Tabelle `embeddings`, Schlüssel ist der Hash
des Abschnittstexts; unveränderte Abschnitte werden also nie neu berechnet. Bei der
Vault-Größe (≈2000 Abschnitte) ist ein Skalarprodukt über alle Vektoren schneller
als jeder Index, deshalb kein sqlite-vec.
"""

from __future__ import annotations

import hashlib

import httpx
import numpy as np

from .index import Index


class Semantic:
    def __init__(self, index: Index, url: str, model: str, embed=None):
        self.index, self.url, self.model = index, url.rstrip("/"), model
        self._embed = embed or self._ollama
        self._cache: tuple[list[str], np.ndarray] | None = None

    def _ollama(self, texts: list[str]) -> list[list[float]]:
        r = httpx.post(f"{self.url}/api/embed", json={"model": self.model, "input": texts}, timeout=120)
        r.raise_for_status()
        return r.json()["embeddings"]

    def available(self) -> bool:
        try:
            self._embed(["test"])
            return True
        except Exception:
            return False

    @staticmethod
    def _key(title: str, heading_path: str, text: str) -> str:
        return hashlib.sha256(f"{title}\n{heading_path}\n{text}".encode()).hexdigest()[:24]

    def update(self, batch: int = 32, lock=None) -> int:
        """Fehlende Embeddings berechnen. Gibt die Anzahl neuer Vektoren zurück.
        Die Sperre wird nur für Datenbankzugriffe gehalten, nicht während Ollama rechnet."""
        import contextlib
        lock = lock or contextlib.nullcontext()
        db = self.index.db
        with lock:
            rows = db.execute(
                "SELECT n.title, s.heading_path, s.text FROM sections s JOIN notes n ON n.id = s.note_id").fetchall()
            have = {r[0] for r in db.execute("SELECT text_hash FROM embeddings WHERE model = ?", (self.model,))}
        todo = []
        for r in rows:
            k = self._key(r["title"], r["heading_path"], r["text"])
            if k not in have:
                todo.append((k, f"{r['title']} > {r['heading_path']}\n{r['text'][:4000]}"))
                have.add(k)
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            vecs = self._embed([t for _, t in chunk])
            with lock, db:
                db.executemany("INSERT OR REPLACE INTO embeddings VALUES (?,?,?)",
                               [(k, self.model, np.asarray(v, dtype=np.float32).tobytes())
                                for (k, _), v in zip(chunk, vecs)])
        if todo:
            self._cache = None
        return len(todo)

    def search(self, text: str, limit: int = 10, include_archive: bool = False) -> list[dict]:
        db = self.index.db
        q = np.asarray(self._embed([text])[0], dtype=np.float32)
        q /= np.linalg.norm(q) or 1.0
        arch, args = self.index._archive_clause(include_archive)
        # Schlüssel ist ein Hash über Titel, Pfad und Text; Zuordnung daher in Python
        secs = db.execute(
            "SELECT n.path, n.title, s.heading_path, s.line_start, s.line_end, s.size, s.text"
            f" FROM sections s JOIN notes n ON n.id = s.note_id WHERE 1=1{arch}", args).fetchall()
        vecs = {r[0]: r[1] for r in db.execute("SELECT text_hash, vector FROM embeddings WHERE model = ?", (self.model,))}
        cand, mats = [], []
        for r in secs:
            v = vecs.get(self._key(r["title"], r["heading_path"], r["text"]))
            if v is not None:
                cand.append(r)
                mats.append(np.frombuffer(v, dtype=np.float32))
        if not cand:
            return []
        m = np.vstack(mats)
        m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        scores = m @ q
        best = np.argsort(-scores)[:limit]
        return [{"path": cand[i]["path"], "title": cand[i]["title"], "heading_path": cand[i]["heading_path"],
                 "line_start": cand[i]["line_start"], "line_end": cand[i]["line_end"],
                 "snippet": " ".join(cand[i]["text"].split())[:240], "score": round(float(scores[i]), 4)}
                for i in best]
