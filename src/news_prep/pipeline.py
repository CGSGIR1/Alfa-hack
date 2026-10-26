"""Подготовка новости: модель + проверка кодом + точечный дозапрос по непокрытым частям.

1. Основной вызов модели с системным промптом news-two-views-1.0.
2. Нормализация ответа кодом (validate.py): цитаты приводятся к точным отрывкам,
   строки для эмбеддингов перестраиваются по формулам, метаданные берутся из входа.
3. Код находит предложения статьи, не подтверждённые ни одной цитатой.
4. Дозапрос (до MAX_REPAIRS раз): каждое такое предложение модель должна закрыть —
   добавить цитату в существующий фрагмент (add_quotes), создать новый фрагмент
   или объявить несодержательным (not_substantive; код принимает это только для
   коротких предложений без чисел).
5. Остаток непокрытого фиксируется в document_limitations.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..llm import LLMClient, LLMError, LLMResponseError
from .quotes import fix_quote, normalize, uncovered_sentences
from .validate import Report, normalize_document

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "news_two_views_system.txt").read_text(encoding="utf-8")
PROMPT_VERSION = "news-two-views-1.0/p2"
REQUEST_PARAMS = {"max_tokens": 16000, "temperature": 0.1}
SOURCE_ACCESS = {"full_text", "partial_text", "summary_only"}
MAX_REPAIRS = 2
NOT_SUBSTANTIVE_MAX_CHARS = 120

REPAIR_TASK = """Это дозапрос к уже подготовленной новости {document_id}. Проверка кодом показала, что \
предложения ниже не подтверждены ни одной цитатой ни одного фрагмента. Каждое из них нужно закрыть \
ровно одним способом:

1. add_quotes — предложение относится к мысли существующего фрагмента: добавь в этот фрагмент \
точную цитату, содержащую предложение;
2. fragments — предложение несёт самостоятельное событие, оценку, прогноз или механизм: создай новый \
фрагмент по схеме news-two-views-1.0 (все поля, точные цитаты, source_block_ids);
3. not_substantive — только для рекламы, навигации, подписей без данных и служебного текста. \
Содержательные предложения с фактами, числами, оценками или прогнозами сюда не относятся — такие ответы \
отклоняются.

Верни только JSON:
{{"add_quotes": [{{"fragment_id": "F001", "block_id": "B005", "quote": "точная цитата из блока"}}],
 "fragments": [ ...новые фрагменты по схеме... ],
 "not_substantive": [{{"block_id": "B001", "sentence": "предложение", "reason": "почему"}}]}}

Существующие фрагменты:
{existing}

Непокрытые предложения:
{uncovered}

Исходные блоки, где они находятся:
{blocks}"""


def make_request(document_id: str, title: str, date: str, url: str | None, publisher: str | None,
                 paragraphs: list[str], source_access: str = "full_text") -> dict:
    """Вход для модели: метаданные и упорядоченные блоки B001, B002, ..."""
    return {
        "metadata": {
            "document_id": document_id,
            "document_title": title,
            "document_date": date,
            "source": {"url": url, "publisher": publisher},
        },
        "source_access": source_access,
        "source_blocks": [{"id": f"B{i:03d}", "text": p} for i, p in enumerate(paragraphs, start=1) if p.strip()],
    }


def _check_request(request: dict) -> None:
    meta = request.get("metadata") or {}
    missing = [k for k in ("document_id", "document_title", "document_date") if not meta.get(k)]
    if not request.get("source_blocks"):
        missing.append("source_blocks")
    if request.get("source_access") not in SOURCE_ACCESS:
        missing.append("source_access")
    if missing:
        raise ValueError(f"Во входе не хватает: {', '.join(missing)}")


def _quotes_of(doc: dict) -> list[str]:
    quotes = []
    for f in doc["fragments"]:
        quotes += f["source_quotes"]
        quotes += [a["quote"] for a in f["attributions"] if a.get("quote")]
        quotes += [a["quote"] for a in f["mechanism"]["source_attributions"] if a.get("quote")]
    return quotes


def _messages(user: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _repair_message(doc: dict, uncovered: list[tuple[str, str]], blocks: dict[str, str]) -> str:
    existing = "\n".join(
        f"- {f['id']} (блоки {', '.join(f['source_block_ids'])}): {f.get('title') or ''} — {f['thesis'][:160]}"
        for f in doc["fragments"])
    needed = sorted({b for b, _ in uncovered}, key=list(blocks).index)
    return REPAIR_TASK.format(
        document_id=doc["document_id"],
        existing=existing or "(нет)",
        uncovered="\n".join(f"- {b}: {s}" for b, s in uncovered),
        blocks="\n".join(f"{b}: {blocks[b]}" for b in needed),
    )


def _apply_repair(doc: dict, extra: dict, request: dict, blocks: dict[str, str],
                  uncovered: list[tuple[str, str]], report: Report) -> None:
    by_id = {f["id"]: f for f in doc["fragments"]}
    for item in extra.get("add_quotes") or []:
        if not isinstance(item, dict) or item.get("fragment_id") not in by_id or not item.get("quote"):
            report.note(f"add_quotes отклонён: неизвестный фрагмент или пустая цитата ({str(item)[:60]})")
            continue
        f = by_id[item["fragment_id"]]
        fix = fix_quote(item["quote"], blocks, [item.get("block_id")])
        report.count(fix)
        if fix.status == "dropped":
            report.note(f"add_quotes → {f['id']}: цитата не найдена в тексте, отклонена")
            continue
        for block_id, span in fix.spans:
            if span not in f["source_quotes"]:
                f["source_quotes"].append(span)
            if block_id not in f["source_block_ids"]:
                f["source_block_ids"].append(block_id)
        report.note(f"add_quotes → {f['id']}: добавлена цитата ({fix.status})")

    added = normalize_document({"fragments": extra.get("fragments") or [], **doc_meta(request)}, request, report)
    doc["fragments"] += added["fragments"]
    if added["fragments"]:
        report.note(f"дозапрос: новых фрагментов {len(added['fragments'])}")

    pending = {normalize(s) for _, s in uncovered}
    for item in extra.get("not_substantive") or []:
        if not isinstance(item, dict):
            continue
        sentence = normalize(item.get("sentence") or "")
        if sentence not in pending:
            continue
        if len(sentence) <= NOT_SUBSTANTIVE_MAX_CHARS and not re.search(r"\d", sentence):
            doc["document_limitations"].append(f"Исключено как несодержательное: «{sentence[:80]}» — {item.get('reason')}")
            doc.setdefault("_accepted_not_substantive", []).append(sentence)
        else:
            report.note(f"not_substantive отклонён (содержательное предложение): «{sentence[:60]}…»")


def doc_meta(request: dict) -> dict:
    meta = request["metadata"]
    return {k: meta[k] for k in ("document_id", "document_title", "document_date")}


def _uncovered(doc: dict, blocks: dict[str, str]) -> list[tuple[str, str]]:
    accepted = set(doc.get("_accepted_not_substantive", []))
    return [(b, s) for b, s in uncovered_sentences(blocks, _quotes_of(doc)) if normalize(s) not in accepted]


def prepare_news(request: dict, client: LLMClient | None = None, model: str | None = None,
                 repair: bool = True, params: dict | None = None) -> tuple[dict, dict]:
    """Возвращает (документ news-two-views-1.0, отчёт о проверке и расходе).

    params — дополнительные параметры API для этой модели (например, отключение рассуждений).
    """
    _check_request(request)
    client = client or LLMClient()
    model = model or client.model("news", fallback="fast")
    params = {**REQUEST_PARAMS, **(params or {})}
    blocks = {b["id"]: b["text"] for b in request["source_blocks"]}
    usage_before, started = dict(client.usage), time.monotonic()
    report = Report()
    calls = []

    def call(messages: list[dict], task: str) -> dict:
        before, t = dict(client.usage), time.monotonic()
        result = client.chat_json(model, messages, task=task, params=params)
        calls.append({"task": task, "seconds": round(time.monotonic() - t, 1),
                      **{k: round(client.usage[k] - before[k], 4) for k in ("prompt_tokens", "completion_tokens", "cost")}})
        return result

    raw = call(_messages(json.dumps(request, ensure_ascii=False)), "prepare_news")
    if "error" in raw and "fragments" not in raw:
        raise LLMResponseError(f"Модель отказалась обрабатывать вход: {raw['error']}")
    doc = normalize_document(raw, request, report)
    first = {"fragments": len(doc["fragments"]), "quotes": dict(report.quotes)}
    uncovered = _uncovered(doc, blocks)
    history = [len(uncovered)]

    for _ in range(MAX_REPAIRS if repair else 0):
        if not uncovered:
            break
        try:
            extra = call(_messages(_repair_message(doc, uncovered, blocks)), "prepare_news_repair")
        except LLMError as error:
            report.note(f"дозапрос не удался: {error}")
            break
        _apply_repair(doc, extra, request, blocks, uncovered, report)
        new_uncovered = _uncovered(doc, blocks)
        history.append(len(new_uncovered))
        progress = len(new_uncovered) < len(uncovered)
        uncovered = new_uncovered
        if not progress:
            report.note("дозапрос не дал прогресса, остановка")
            break

    # порядок фрагментов — по первому исходному блоку, затем нумерация заново
    order = {b: i for i, b in enumerate(blocks)}
    for f in doc["fragments"]:
        f["source_block_ids"].sort(key=order.get)
    doc["fragments"].sort(key=lambda f: min((order[b] for b in f["source_block_ids"]), default=len(order)))
    for i, f in enumerate(doc["fragments"], start=1):
        f["id"] = f"F{i:03d}"
    used = {b for f in doc["fragments"] for b in f["source_block_ids"]}
    doc["coverage"]["processed_block_ids"] = [b for b in blocks if b in used]
    excluded = {e["id"] for e in doc["coverage"]["excluded_blocks"]}
    doc["coverage"]["unprocessed_block_ids"] = [b for b in blocks if b not in used and b not in excluded]
    doc.pop("_accepted_not_substantive", None)
    if uncovered:
        doc["document_limitations"].append(
            f"Не подтверждены цитатами {len(uncovered)} предложений исходного текста: "
            + "; ".join(f"{b}: «{s[:70]}…»" for b, s in uncovered[:5]))

    return doc, {
        "model": model,
        "params": params,
        "prompt_version": PROMPT_VERSION,
        "seconds": round(time.monotonic() - started, 1),
        "usage": {k: round(client.usage[k] - usage_before[k], 4) for k in client.usage},
        "calls": calls,
        "first_pass": first,
        "fragments": len(doc["fragments"]),
        "quotes": report.quotes,
        "sentences": len(uncovered_sentences(blocks, [])),
        "uncovered_history": history,
        "uncovered_after": len(uncovered),
        "fixes": report.fixes,
    }
