"""LLM-доработка запросов влияния.

Модель получает описание компании (или группы) и черновые запросы из правил.
Она может уточнить формулировку под конкретную компанию, отклонить неудачный
черновик с причиной и добавить до трёх новых каналов. Ответ проверяется
программно: новые каналы принимаются, только если ссылаются на признаки,
которые реально есть в профиле. При любой ошибке остаются запросы из правил.
"""

from __future__ import annotations

import copy

from ..llm import LLMClient, LLMError
from .queries import ImpactQuery, QuerySet

PROMPT_VERSION = "impact-queries-v1"
MAX_NEW = 3
NEW_QUERY_WEIGHT = 0.5
MIN_TEXT, MAX_TEXT = 15, 250

SYSTEM = """Ты аналитик корпоративного банка. По описанию клиента ты формулируешь поисковые запросы, \
по которым из потока экономических новостей будут отбираться события, способные повлиять на его бизнес.

Запросы используются для семантического поиска по новостям, поэтому:
- пиши их языком новостей: 5–20 слов, конкретная тема, без названия и идентификатора компании;
- один запрос — один канал влияния (ставка, курс, пошлины, отрасль поставщиков и т.п.);
- опирайся только на факты из описания, ничего не выдумывай.

Отвечай только JSON."""

INSTRUCTIONS = """Ниже описание {subject} и черновые запросы, построенные правилами.

1. Для каждого черновика верни уточнённую формулировку: конкретнее под этот бизнес (товары, страны, \
отрасли контрагентов из описания). Если черновик для этого бизнеса неуместен, верни keep=false и причину.
2. Добавь не больше {max_new} новых каналов влияния, которые следуют из описания, но не покрыты черновиками. \
Для каждого перечисли в sources признаки, на которых он основан, дословно копируя строки из списка «Допустимые признаки».

Формат ответа:
{{"refined": [{{"id": 1, "keep": true, "text": "...", "why": "...", "reason": ""}}],
 "new": [{{"channel": "короткое_название", "text": "...", "why": "...", "sources": ["..."]}}]}}

# Описание
{description}

# Черновые запросы
{seeds}

# Допустимые признаки
{allowed}"""


def allowed_sources(seeds: QuerySet, features: dict | None = None) -> list[str]:
    """Строки, на которые модель может сослаться в новых каналах."""
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
        f"{i}. [{q.channel}] {q.text} — почему: {q.why}" for i, q in enumerate(seeds.queries, start=1)
    )
    subject = "компании" if seeds.subject_type == "company" else "группы компаний"
    user = INSTRUCTIONS.format(
        subject=subject,
        max_new=MAX_NEW,
        description=description,
        seeds=seed_lines,
        allowed="\n".join(f"- {a}" for a in allowed),
    )
    return [
        {"role": "system", "content": SYSTEM + f"\n\n(версия промпта: {PROMPT_VERSION})"},
        {"role": "user", "content": user},
    ]


def apply_response(seeds: QuerySet, response: dict, allowed: list[str]) -> QuerySet:
    """Проверяет ответ модели и собирает итоговый набор запросов."""
    result = copy.deepcopy(seeds)
    result.queries, notes = [], []
    refined = {}
    for item in response.get("refined") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            refined[item["id"]] = item

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
        result.queries.append(query)

    for item in (response.get("new") or [])[:MAX_NEW]:
        if not isinstance(item, dict) or not _valid_text(item.get("text")):
            notes.append("новый канал отклонён: нет текста запроса")
            continue
        sources = [s for s in item.get("sources") or [] if s in allowed]
        if not sources:
            notes.append(f"новый канал «{item.get('channel')}» отклонён: нет ссылок на признаки профиля")
            continue
        result.queries.append(ImpactQuery(
            channel=f"llm:{str(item.get('channel') or 'other').strip()[:40]}",
            kind="llm",
            text=item["text"].strip(),
            why=str(item.get("why") or "").strip(),
            sources=sources,
            weight=NEW_QUERY_WEIGHT,
            origin="llm",
        ))
        notes.append(f"добавлен новый канал «{item.get('channel')}»")

    if not result.queries:
        notes.append("модель отклонила все запросы, оставлены черновики")
        result.queries = copy.deepcopy(seeds.queries)
    result.notes = notes
    return result


def refine(seeds: QuerySet, description: str, features: dict | None = None,
           client: LLMClient | None = None, model: str | None = None) -> QuerySet:
    client = client or LLMClient()
    allowed = allowed_sources(seeds, features)
    messages = build_messages(seeds, description, allowed)
    try:
        # Шаг определяет, какие новости вообще попадут в анализ, и считается заранее
        # один раз на компанию, поэтому по умолчанию берём основную (сильную) модель.
        response = client.chat_json(model or client.model("queries", fallback="main"), messages, task="impact_queries")
    except LLMError as error:
        fallback = copy.deepcopy(seeds)
        fallback.notes = [f"LLM недоступна ({error}), используются запросы из правил"]
        return fallback
    return apply_response(seeds, response, allowed)


def _valid_text(text) -> bool:
    return isinstance(text, str) and MIN_TEXT <= len(text.strip()) <= MAX_TEXT
