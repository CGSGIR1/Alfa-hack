"""Проверка и нормализация ответа модели по схеме news-two-views-1.0.

Всё, что можно вычислить из входа, перестраивается кодом, а не берётся у модели:
метаданные, source_blocks, обе строки для эмбеддингов, is_established_cause, покрытие.
Каждое исправление записывается в отчёт, чтобы было видно, где модель ошиблась.
"""

from __future__ import annotations

import copy

from .quotes import QuoteFix, fix_quote

SCHEMA_VERSION = "news-two-views-1.0"
STATUSES = {"attributed_in_news", "hypothesis", "not_identified"}
LIST_FIELDS = ["source_block_ids", "source_quotes", "attributions", "sectors", "countries",
               "regions", "organizations", "okved_codes", "limitations"]


class Report:
    def __init__(self):
        self.fixes: list[str] = []
        self.quotes: dict[str, int] = {"exact": 0, "normalized": 0, "fuzzy": 0, "dropped": 0}

    def note(self, text: str) -> None:
        self.fixes.append(text)

    def count(self, fix: QuoteFix) -> None:
        self.quotes[fix.status] += 1


def company_text(f: dict) -> str:
    return f"{f.get('thesis') or ''}\n\n{f.get('context') or ''}"


def analytics_text(question: str | None, conditions: str | None) -> str | None:
    if not question:
        return None
    return f"{question}\nУсловия и границы применимости: {conditions or ''}"


def _fix_quotes(items: list[str], blocks: dict[str, str], preferred: list[str], where: str,
                report: Report) -> tuple[list[str], list[str]]:
    """Возвращает исправленные цитаты и блоки, в которых они найдены."""
    result, found_in = [], []
    for quote in items or []:
        if not isinstance(quote, str) or not quote.strip():
            continue
        fix = fix_quote(quote, blocks, preferred)
        report.count(fix)
        if fix.status == "fuzzy":
            report.note(f"{where}: цитата заменена ближайшим отрывком (сходство {fix.ratio})")
        elif fix.status == "dropped":
            report.note(f"{where}: цитата не найдена в тексте и удалена: «{quote[:60]}…»")
        for block_id, span in fix.spans:
            if span not in result:
                result.append(span)
            if block_id not in found_in:
                found_in.append(block_id)
    return result, found_in


def _fix_attribution(item: dict, blocks: dict[str, str], where: str, report: Report) -> dict | None:
    if not isinstance(item, dict):
        return None
    item = dict(item)
    if item.get("quote"):
        quotes, found = _fix_quotes([item["quote"]], blocks, [item.get("source_block_id")], where, report)
        item["quote"] = quotes[0] if quotes else None
        if found:
            item["source_block_id"] = found[0]
    if item.get("source_block_id") not in blocks:
        item["source_block_id"] = None
    return item


def normalize_fragment(f: dict, blocks: dict[str, str], full_text: str, report: Report) -> dict | None:
    f = copy.deepcopy(f)
    fid = f.get("id") or "?"
    if not (f.get("thesis") or "").strip():
        report.note(f"{fid}: нет тезиса, фрагмент отброшен")
        return None
    for name in LIST_FIELDS:
        if not isinstance(f.get(name), list):
            f[name] = []

    preferred = [b for b in f["source_block_ids"] if b in blocks]
    unknown = [b for b in f["source_block_ids"] if b not in blocks]
    if unknown:
        report.note(f"{fid}: несуществующие блоки {unknown} удалены")
    f["source_quotes"], found_in = _fix_quotes(f["source_quotes"], blocks, preferred, fid, report)
    # блоки фрагмента = заявленные моделью + те, где реально нашлись цитаты
    f["source_block_ids"] = sorted(set(preferred) | set(found_in))
    if not f["source_quotes"]:
        f["limitations"].append("Нет подтверждённых цитат из исходного текста")

    f["attributions"] = [a for a in (_fix_attribution(a, blocks, f"{fid}/attributions", report)
                                     for a in f["attributions"]) if a]

    codes = [c for c in f["okved_codes"] if str(c) in full_text]
    if len(codes) != len(f["okved_codes"]):
        report.note(f"{fid}: коды ОКВЭД, которых нет в тексте, удалены")
    f["okved_codes"] = codes

    m = f.get("mechanism") if isinstance(f.get("mechanism"), dict) else {}
    status = m.get("status") if m.get("status") in STATUSES else "not_identified"
    if m.get("status") != status:
        report.note(f"{fid}: неизвестный статус механизма «{m.get('status')}» → not_identified")
    m["source_attributions"] = [a for a in (_fix_attribution(a, blocks, f"{fid}/mechanism", report)
                                            for a in m.get("source_attributions") or []) if a and a.get("quote")]
    if status == "attributed_in_news" and not m["source_attributions"]:
        report.note(f"{fid}: attributed_in_news без подтверждённой атрибуции → hypothesis")
        status = "hypothesis"
    if m.get("is_established_cause") is not False:
        report.note(f"{fid}: is_established_cause принудительно false")
    context = m.get("context") if isinstance(m.get("context"), dict) else {}
    conditions = (context.get("conditions") or "").strip() or "Условия в тексте не установлены."
    question = (f.get("analytics_search_question") or "").strip() or None
    if status == "not_identified":
        m["description"], question = None, None
        m["source_attributions"] = []
    elif not question:
        report.note(f"{fid}: нет вопроса для поиска аналитики при статусе {status} → not_identified")
        status, m["description"], m["source_attributions"] = "not_identified", None, []
    m.update(status=status, is_established_cause=False, context={"conditions": conditions},
             boundaries=[b for b in m.get("boundaries") or [] if isinstance(b, str)])
    f["mechanism"] = m
    f["analytics_search_question"] = question

    # строки для эмбеддингов всегда строим кодом по формулам промпта
    for name, expected in [("company_embedding_text", company_text(f)),
                           ("analytics_embedding_text", analytics_text(question, conditions))]:
        if f.get(name) != expected:
            report.note(f"{fid}: {name} перестроен по формуле")
        f[name] = expected
    return f


def normalize_document(raw: dict, request: dict, report: Report) -> dict:
    """Собирает итоговый документ: метаданные и блоки из входа, фрагменты — проверенные ответы модели."""
    meta = request["metadata"]
    blocks = {b["id"]: b["text"] for b in request["source_blocks"]}
    full_text = "\n\n".join(blocks.values())
    for key in ("document_id", "document_title", "document_date"):
        if raw.get(key) != meta[key]:
            report.note(f"{key} из ответа модели заменён значением из входа")

    fragments = []
    for f in raw.get("fragments") or []:
        nf = normalize_fragment(f, blocks, full_text, report) if isinstance(f, dict) else None
        if nf:
            fragments.append(nf)
    for i, f in enumerate(fragments, start=1):
        f["id"] = f"F{i:03d}"

    coverage = raw.get("coverage") if isinstance(raw.get("coverage"), dict) else {}
    excluded = [e for e in coverage.get("excluded_blocks") or [] if isinstance(e, dict) and e.get("id") in blocks]
    used = {b for f in fragments for b in f["source_block_ids"]}
    excluded_ids = {e["id"] for e in excluded}
    unprocessed = [b for b in blocks if b not in used and b not in excluded_ids]

    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": meta["document_id"],
        "document_title": meta["document_title"],
        "document_date": meta["document_date"],
        "source": meta["source"],
        "preparation": {"source_access": request["source_access"], "prepared_by": "external_llm"},
        "document_limitations": [x for x in raw.get("document_limitations") or [] if isinstance(x, str)],
        "source_blocks": request["source_blocks"],
        "coverage": {"processed_block_ids": sorted(used), "excluded_blocks": excluded,
                     "unprocessed_block_ids": unprocessed},
        "fragments": fragments,
    }
