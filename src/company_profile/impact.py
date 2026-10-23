"""Аспекты влияния для компании или группы: из кеша, а если их нет — считаются на лету.

    from src.company_profile import DataStore
    from src.company_profile.impact import get_impact_queries

    store = DataStore()
    queries = get_impact_queries(store, "U_9250")          # компания
    queries = get_impact_queries(store, "group_133188_sb")  # группа

Кеш — precomputed/impact_queries/<id>.json (его же заполняет precompute.py).
Подходит запись с текущей версией промпта, модель может быть любой: пересчитывать
готовые аспекты только из-за смены модели слишком дорого (~0,75 ₽ и ~1 мин на субъект).
Если LLM недоступна, возвращаются аспекты из правил и в кеш не пишутся,
чтобы при следующем обращении попробовать снова.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from ..llm import LLMClient, LLMError
from .data import DataStore
from .describe import describe_full, describe_group
from .group import build_group_profile
from .profile import build_profile
from .queries import ImpactQuery, QuerySet, company_queries, group_queries
from .refine import PROMPT_VERSION, refine

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "precomputed" / "impact_queries"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def prepare(store: DataStore, subject: str) -> tuple[QuerySet, str, dict | None]:
    """Черновые аспекты из правил, текстовое описание и признаки для компании или группы."""
    if subject.startswith("group_"):
        g = build_group_profile(store, subject)
        return group_queries(g), describe_group(g), None
    p = build_profile(store, subject)
    return company_queries(p), describe_full(p), p.features


def is_fallback(result: QuerySet) -> bool:
    return any(n.startswith("LLM недоступна") for n in result.notes)


def load_cached(subject: str, cache_dir: Path = CACHE_DIR, model: str | None = None) -> QuerySet | None:
    """Аспекты из кеша, если запись есть и сделана текущей версией промпта (и указанной моделью, если задана)."""
    path = cache_dir / f"{subject}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if data.get("prompt_version") != PROMPT_VERSION or (model and data.get("model") != model):
        return None
    return QuerySet(
        subject_id=data["subject_id"],
        subject_type=data["subject_type"],
        queries=[ImpactQuery(**q) for q in data["queries"]],
        notes=data.get("notes", []),
    )


def save(result: QuerySet, model: str, cache_dir: Path = CACHE_DIR) -> None:
    """Атомарная запись: при падении посреди записи в кеше не останется битого файла."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        **result.to_dict(),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = cache_dir / f"{result.subject_id}.json"
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(path)


def compute(store: DataStore, subject: str, client: LLMClient, model: str,
            cache_dir: Path = CACHE_DIR) -> QuerySet:
    """Считает аспекты через LLM и сохраняет в кеш (откат на правила не сохраняется)."""
    seeds, text, features = prepare(store, subject)
    result = refine(seeds, text, features, client=client, model=model)
    if not is_fallback(result):
        save(result, model, cache_dir)
    return result


def get_impact_queries(store: DataStore, subject: str, client: LLMClient | None = None,
                       refresh: bool = False, cache_dir: Path = CACHE_DIR) -> QuerySet:
    """Аспекты влияния: из кеша, иначе на лету через LLM (около минуты), иначе из правил."""
    if not refresh and (cached := load_cached(subject, cache_dir)) is not None:
        return cached

    with _locks_guard:
        lock = _locks.setdefault(subject, threading.Lock())
    # Пока один поток считает аспекты компании, остальные запросы к ней ждут его результата.
    with lock:
        if not refresh and (cached := load_cached(subject, cache_dir)) is not None:
            return cached
        client = client or LLMClient()
        try:
            model = client.model("queries", fallback="main")
        except LLMError as error:  # нет настроек LLM — работаем на правилах
            seeds, _, _ = prepare(store, subject)
            seeds.notes = [f"LLM недоступна ({error}), используются запросы из правил"]
            return seeds
        return compute(store, subject, client, model, cache_dir)
