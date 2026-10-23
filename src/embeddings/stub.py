"""Заглушка эмбеддингов: косинус по символьным n-граммам (как группировка событий в парсере).

Нужна, чтобы пайплайн работал сквозь до появления настоящего модуля эмбеддингов.
Векторы хранятся в памяти и в JSON-файле, сходство — тематическое, а не смысловое,
поэтому пороги в config.RETRIEVAL подобраны под неё и для эмбеддингов не годятся.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from pathlib import Path

from .base import EmbeddingStore, Match

NGRAMS = (3, 4, 5)
TOKEN = re.compile(r"[a-zа-яё0-9]+")


def vectorize(text: str | None) -> dict[str, float]:
    grams = Counter()
    for word in TOKEN.findall((text or "").lower()):
        w = f" {word} "
        for n in NGRAMS:
            grams.update(w[i:i + n] for i in range(max(len(w) - n + 1, 1)))
    norm = math.sqrt(sum(v * v for v in grams.values())) or 1.0
    return {g: v / norm for g, v in grams.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def mechanism_text(m: dict) -> str:
    mech = m.get("mechanism") or m
    ctx = m.get("context") or {}
    return " ".join(str(x) for x in [m.get("title"), mech.get("cause"), mech.get("channel"),
                                      mech.get("effect"), ctx.get("conditions") or m.get("conditions")] if x)


class StubStore(EmbeddingStore):
    name = "stub-char-ngrams"

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.companies: dict[str, dict[str, dict]] = {}   # company -> aspect_id -> vector
        self.fragments: dict[str, dict] = {}              # fragment_id -> {news_id, company_vec, analytics_vec}
        self.mechanisms: dict[str, dict] = {}             # mechanism_id -> vector
        if path.exists():
            data = json.loads(path.read_text())
            self.companies, self.fragments, self.mechanisms = data["companies"], data["fragments"], data["mechanisms"]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"companies": self.companies, "fragments": self.fragments,
                                   "mechanisms": self.mechanisms}))
        tmp.replace(self.path)

    def index_company(self, company_id, aspects):
        with self.lock:
            self.companies[company_id] = {a["id"]: vectorize(a["text"]) for a in aspects}
            self._save()

    def index_news(self, news_id, fragments):
        with self.lock:
            for f in fragments:
                self.fragments[f["id"]] = {
                    "news_id": news_id,
                    "company": vectorize(f.get("company_embedding_text")),
                    "analytics": vectorize(f.get("analytics_embedding_text")) if f.get("analytics_embedding_text") else None,
                }
            self._save()

    def index_mechanisms(self, mechanisms):
        with self.lock:
            for m in mechanisms:
                self.mechanisms[m["id"]] = vectorize(mechanism_text(m))
            self._save()

    def delete_news(self, news_ids):
        ids = set(news_ids)
        with self.lock:
            self.fragments = {k: v for k, v in self.fragments.items() if v["news_id"] not in ids}
            self._save()

    def search_news_for_company(self, company_id, news_ids, top_k):
        allowed = set(news_ids)
        aspects = self.companies.get(company_id) or {}
        matches = []
        for fid, f in self.fragments.items():
            if f["news_id"] not in allowed:
                continue
            for aid, vec in aspects.items():
                matches.append(Match(fid, aid, round(cosine(vec, f["company"]), 6)))
        return sorted(matches, key=lambda m: m.score, reverse=True)[:top_k]

    def search_mechanisms_for_fragment(self, fragment_id, top_k):
        vec = (self.fragments.get(fragment_id) or {}).get("analytics")
        if not vec:
            return []
        matches = [Match(mid, fragment_id, round(cosine(vec, mvec), 6)) for mid, mvec in self.mechanisms.items()]
        return sorted(matches, key=lambda m: m.score, reverse=True)[:top_k]
