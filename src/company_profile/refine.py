"""LLM-доработка запросов влияния.

Модель получает описание компании (или группы) и черновые запросы из правил.
Она уточняет формулировки под конкретный бизнес, отклоняет неудачные черновики
с причиной и дополняет набор новыми аспектами так, чтобы их было не меньше
MIN_ASPECTS и они покрывали разные категории (сырьё, сбыт, регион, регулирование...).

Ответ проверяется программно: новые аспекты принимаются, только если ссылаются
на признаки, которые реально есть в профиле. Если аспектов всё равно мало,
модели один раз возвращается замечание с просьбой дополнить. При любой ошибке
API остаются запросы из правил.
"""

from __future__ import annotations

import copy
import json

from ..llm import LLMClient, LLMError
from .queries import CATEGORIES, ImpactQuery, QuerySet

PROMPT_VERSION = "impact-queries-v2"
MIN_ASPECTS = 10
MAX_ASPECTS = 15
NEW_QUERY_WEIGHT = 0.5
MIN_TEXT, MAX_TEXT = 15, 250

SYSTEM = """Ты аналитик корпоративного банка. По описанию клиента ты составляешь набор аспектов — \
поисковых запросов, по которым из потока экономических новостей будут отбираться события, \
способные повлиять на его бизнес. От полноты этого набора зависит, какие новости клиент вообще увидит.

Требования к запросу:
- язык новостей: 5–20 слов, конкретная тема, без названия и идентификатора компании;
- один запрос — один канал влияния;
- конкретика из описания: товары, сырьё, страны, регион, отрасли поставщиков и покупателей;
- никаких фактов, которых нет в описании. Знание о типичной цепочке создания стоимости отрасли \
(какое сырьё, кто покупатели, какое регулирование) использовать можно, ссылаясь на код ОКВЭД.

Отвечай только JSON."""

INSTRUCTIONS = """Ниже описание {subject} и черновые аспекты, построенные правилами.

Задача — итоговый набор из {min_aspects}–{max_aspects} аспектов, покрывающий бизнес с разных сторон.

1. Для каждого черновика верни уточнённую формулировку (конкретнее под этот бизнес). \
Если черновик для этого бизнеса неуместен или дублирует другой, верни keep=false и причину.
2. Добавь новые аспекты, чтобы всего получилось не меньше {min_aspects}. Особенно проверь категории, \
которых нет среди черновиков: сырьё и ресурсы, покупатели и сбыт, регулирование отрасли, \
местные новости региона присутствия и регионов контрагентов, связанные компании. \
Для каждого нового аспекта укажи category из списка и sources — признаки, на которых он основан, \
дословно скопированные из списка «Допустимые признаки».

Формат ответа:
{{"refined": [{{"id": 1, "keep": true, "text": "...", "why": "...", "reason": ""}}],
 "new": [{{"category": "...", "channel": "короткое_название", "text": "...", "why": "...", "sources": ["..."]}}]}}

# Категории
{categories}

# Описание
{description}

# Черновые аспекты
{seeds}

# Допустимые признаки
{allowed}"""

TOP_UP = """В итоговом наборе сейчас {count} аспектов, а нужно не меньше {min_aspects}. \
Уже есть категории: {present}. Добавь ещё {missing} новых аспектов в поле "new" (в том же формате, \
с category и sources из списка «Допустимые признаки»), не повторяя существующие:
{existing}"""


def allowed_sources(seeds: QuerySet, features: dict | None = None) -> list[str]:
    """Строки, на которые модель может сослаться в новых аспектах."""
    allowed = []
    for q in seeds.queries:
        allowed += [s for s in q.sources if s not in allowed]
    for key, value in (features or {}).items():
        if value not in (None, "", 0, 0.0) and key != "SYNTHETIC_U":
            item = f"{key}={value}"
            if item not in allowed:
                allowed.append(item)
    return allowed


def build_messages(seeds: QuerySet, description: str, allowed: list[str]) -> list[dict]:
    seed_lines = "\n".join(
        f"{i}. [{q.category}] {q.text} — почему: {q.why}" for i, q in enumerate(seeds.queries, start=1)
    )
    subject = "компании" if seeds.subject_type == "company" else "группы компаний"
    user = INSTRUCTIONS.format(
        subject=subject,
        min_aspects=MIN_ASPECTS,
        max_aspects=MAX_ASPECTS,
        categories="\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items()),
        description=description,
        seeds=seed_lines,
        allowed="\n".join(f"- {a}" for a in allowed),
    )
    return [
        {"role": "system", "content": SYSTEM + f"\n\n(версия промпта: {PROMPT_VERSION})"},
        {"role": "user", "content": user},
    ]


def apply_refined(seeds: QuerySet, response: dict, notes: list[str]) -> list[ImpactQuery]:
    """Черновики с учётом правок модели (без новых аспектов)."""
    refined = {}
    for item in response.get("refined") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            refined[item["id"]] = item

    result = []
    for i, seed in enumerate(seeds.queries, start=1):
        query = copy.deepcopy(seed)
        item = refined.get(i)
        if item is None:
            notes.append(f"#{i} [{seed.channel}]: модель не вернула, оставлен черновик")
        elif item.get("keep") is False:
            notes.append(f"#{i} [{seed.channel}]: отклонён моделью — {item.get('reason') or 'причина не указана'}")
            continue
        elif _valid_text(item.get("text")):
            if item["text"].strip() != seed.text:
                query.text = item["text"].strip()
                query.origin = "llm"
            if isinstance(item.get("why"), str) and item["why"].strip():
                query.why = item["why"].strip()
        else:
            notes.append(f"#{i} [{seed.channel}]: невалидная формулировка, оставлен черновик")
        result.append(query)
    return result


def apply_new(items, allowed: list[str], existing: list[ImpactQuery], limit: int, notes: list[str]) -> list[ImpactQuery]:
    """Проверяет новые аспекты: текст, категория, ссылки на признаки профиля, отсутствие дублей."""
    result = []
    texts = {q.text.lower() for q in existing}
    for item in items or []:
        if len(result) >= limit:
            notes.append("лишние новые аспекты отброшены: превышен максимум")
            break
        if not isinstance(item, dict) or not _valid_text(item.get("text")):
            notes.append("новый аспект отклонён: нет текста запроса")
            continue
        text = item["text"].strip()
        channel = str(item.get("channel") or "other").strip()[:40]
        if text.lower() in texts:
            notes.append(f"новый аспект «{channel}» отклонён: дубль")
            continue
        sources = [s for s in item.get("sources") or [] if s in allowed]
        if not sources:
            notes.append(f"новый аспект «{channel}» отклонён: нет ссылок на признаки профиля")
            continue
        category = item.get("category") if item.get("category") in CATEGORIES else "industry"
        result.append(ImpactQuery(
            channel=f"llm:{channel}",
            kind="llm",
            text=text,
            why=str(item.get("why") or "").strip(),
            sources=sources,
            weight=NEW_QUERY_WEIGHT,
            origin="llm",
            category=category,
        ))
        texts.add(text.lower())
        notes.append(f"добавлен аспект «{channel}» ({category})")
    return result


def apply_response(seeds: QuerySet, response: dict, allowed: list[str]) -> QuerySet:
    """Проверяет ответ модели и собирает итоговый набор запросов."""
    result = copy.deepcopy(seeds)
    notes: list[str] = []
    queries = apply_refined(seeds, response, notes)
    queries += apply_new(response.get("new"), allowed, queries, MAX_ASPECTS - len(queries), notes)
    if not queries:
        notes.append("модель отклонила все запросы, оставлены черновики")
        queries = copy.deepcopy(seeds.queries)
    result.queries, result.notes = queries, notes
    return result


def refine(seeds: QuerySet, description: str, features: dict | None = None,
           client: LLMClient | None = None, model: str | None = None) -> QuerySet:
    client = client or LLMClient()
    allowed = allowed_sources(seeds, features)
    messages = build_messages(seeds, description, allowed)
    try:
        # Шаг определяет, какие новости вообще попадут в анализ, и считается заранее
        # один раз на компанию, поэтому по умолчанию берём основную (сильную) модель.
        model = model or client.model("queries", fallback="main")
        response = client.chat_json(model, messages, task="impact_queries")
        result = apply_response(seeds, response, allowed)
        if len(result.queries) < MIN_ASPECTS:
            result = _top_up(client, model, messages, response, result, allowed)
    except LLMError as error:
        fallback = copy.deepcopy(seeds)
        fallback.notes = [f"LLM недоступна ({error}), используются запросы из правил"]
        return fallback
    if len(result.queries) < MIN_ASPECTS:
        result.notes.append(f"аспектов меньше {MIN_ASPECTS}: {len(result.queries)}")
    return result


def _top_up(client: LLMClient, model: str, messages: list[dict], response: dict,
            result: QuerySet, allowed: list[str]) -> QuerySet:
    """Один дополнительный запрос к модели, если аспектов меньше минимума."""
    present = sorted({q.category for q in result.queries})
    follow_up = messages + [
        {"role": "assistant", "content": json.dumps(response, ensure_ascii=False)},
        {"role": "user", "content": TOP_UP.format(
            count=len(result.queries),
            min_aspects=MIN_ASPECTS,
            present=", ".join(present),
            missing=MIN_ASPECTS - len(result.queries),
            existing="\n".join(f"- [{q.category}] {q.text}" for q in result.queries),
        )},
    ]
    extra = client.chat_json(model, follow_up, task="impact_queries_top_up")
    result.notes.append("дозапрос: аспектов было меньше минимума")
    result.queries += apply_new(extra.get("new"), allowed, result.queries, MAX_ASPECTS - len(result.queries), result.notes)
    return result


def _valid_text(text) -> bool:
    return isinstance(text, str) and MIN_TEXT <= len(text.strip()) <= MAX_TEXT
