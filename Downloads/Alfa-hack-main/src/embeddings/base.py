"""Интерфейс модуля эмбеддингов. Пайплайн обращается к эмбеддингам только через него.

Настоящая реализация (EmbeddingGemma + векторное хранилище) делается отдельно:
достаточно написать класс с этими методами и вернуть его из get_store() в __init__.py.

Тексты для индексации уже подготовлены пайплайном:
- аспект компании: aspect["text"];
- фрагмент новости для подбора компании: fragment["company_embedding_text"];
- фрагмент новости для поиска аналитики: fragment["analytics_embedding_text"] (может быть None);
- механизм аналитики: cause + channel + effect + conditions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Match:
    item_id: str        # ID найденного объекта (фрагмента новости или механизма)
    query_id: str       # ID запроса (аспекта компании или фрагмента новости)
    score: float        # косинусное сходство, не вероятность


class EmbeddingStore(ABC):
    name = "abstract"

    @abstractmethod
    def index_company(self, company_id: str, aspects: list[dict]) -> None:
        """Проиндексировать аспекты компании (заменяет прежние)."""

    @abstractmethod
    def index_news(self, news_id: str, fragments: list[dict]) -> None:
        """Проиндексировать фрагменты новости (оба представления)."""

    @abstractmethod
    def index_mechanisms(self, mechanisms: list[dict]) -> None:
        """Проиндексировать механизмы аналитики."""

    @abstractmethod
    def delete_news(self, news_ids: list[str]) -> None:
        """Удалить фрагменты новостей (истёк срок хранения)."""

    @abstractmethod
    def search_news_for_company(self, company_id: str, news_ids: list[str], top_k: int) -> list[Match]:
        """Пары (фрагмент новости, аспект компании) с наибольшим сходством среди разрешённых новостей."""

    @abstractmethod
    def search_mechanisms_for_fragment(self, fragment_id: str, top_k: int) -> list[Match]:
        """Механизмы аналитики, ближайшие к analytics_embedding_text фрагмента."""
