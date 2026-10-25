"""Модуль эмбеддингов. Сейчас — заглушка; настоящую реализацию подключать в get_store()."""

from __future__ import annotations

from functools import lru_cache

from .base import EmbeddingStore, Match

STUB_PATH_NAME = "embeddings_stub.json"


@lru_cache(maxsize=1)
def get_store() -> EmbeddingStore:
    from ..pipeline.config import DATA
    from .stub import StubStore

    return StubStore(DATA / STUB_PATH_NAME)


__all__ = ["EmbeddingStore", "Match", "get_store"]
