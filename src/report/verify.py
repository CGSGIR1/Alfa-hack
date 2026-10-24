"""Шаг E: проверка отчёта кодом и сообщения для критика и доработки."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .package import PackageIndex, normalize_pointer

CRITIC_PROMPT = (Path(__file__).parent / "prompts" / "critic_system.txt").read_text(encoding="utf-8")
CLAIM_TYPES = {"news_fact", "bank_view", "third_party_view", "ai_inference"}
CLAIM_LISTS = ["what_happened", "why_it_matters", "risks", "opportunities", "bank_view"]
TEXT_LISTS = ["not_established", "to_verify", "next_steps"]  # действия и вопросы — строки, не утверждения
HEDGES = re.compile(r"(\bесли\b|\bможет\b|\bмогут\b|\bмог(ла|ли|ло)?\b|возможн|при условии|\bпри\b.{0,40}\b(подтвержд|наличии|выполнени)|"
                    r"вероятн|не исключ|способн|потенциальн|в случае|только если|зависит|предположительн|условн|не установлен)", re.I)
CATEGORICAL = re.compile(r"(приведёт к|приведет к|неизбежно|гарантированно|обязательно (вырастет|снизится|увеличится|сократится)|точно (вырастет|снизится))", re.I)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

REVISE = """Проверка отчёта нашла замечания. Исправь их и верни отчёт целиком в том же формате JSON.
Не добавляй новых фактов, только исправляй указанное (можно удалить неподтверждённое утверждение).

Замечания:
{issues}"""


ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in NUMBER.findall(text or "")}


MONTHS = {"января": "01", "февраля": "02", "марта": "03", "апреля": "04", "мая": "05", "июня": "06", "июля": "07",
          "августа": "08", "сентября": "09", "октября": "10", "ноября": "11", "декабря": "12"}
RU_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", re.I)
# Отрицательные выводы («связь не подтверждается») — констатация, условность в них не нужна.
NEGATIVE_FINDING = re.compile(r"(не подтвержд|не относится|не установлен|не раскрыт|отсутству|не следует|нет данных)", re.I)


def _with_date_variants(text: str) -> str:
    """Добавляет к датам варианты «23.10» и «23.10.2026»: ISO и «30 июня 2026» в источниках, цифры в тексте модели."""
    extra = [f"{d}.{m} {d}.{m}.{y} {int(d)}.{m}" for y, m, d in ISO_DATE.findall(text)]
    for d, month, y in RU_DATE.findall(text):
        dd, mm = f"{int(d):02d}", MONTHS[month.lower()]
        extra.append(f"{dd}.{mm} {int(d)}.{mm}" + (f" {dd}.{mm}.{y}" if y else ""))
    return text + " " + " ".join(extra)


def _supported(number: str, pool: set[str]) -> bool:
    """Число есть в источниках точно или с округлением до того же количества знаков."""
    if number in pool:
        return True
    decimals = len(number.split(".")[1]) if "." in number else 0
    try:
        value = float(number)
    except ValueError:
        return False
    for p in pool:
        try:
            if round(float(p), decimals) == value or round(abs(float(p)), decimals) == value:
                return True
        except ValueError:
            continue
    return False


def _ref_text(ref: str, index: PackageIndex, full_texts: dict | None = None) -> str:
    full_texts = full_texts or {}
    if ref in index.fragments:
        f = index.fragments[ref]
        mech = f.get("mechanism") or {}
        news_id = index.fragment_news.get(ref)
        return " ".join(str(x) for x in [f.get("thesis"), f.get("context"), mech.get("description"),
                                         (mech.get("context") or {}).get("conditions"),
                                         (mech.get("context") or {}).get("event_period"),
                                         full_texts.get(news_id)] if x)
    if ref in index.mechanisms:
        m = index.mechanisms[ref]
        texts = [m.get("cause"), m.get("channel"), m.get("effect"), m.get("conditions")]
        texts += [index.sources[s]["text"] for s in m["source_fragment_ids"] if s in index.sources]
        return " ".join(str(x) for x in texts if x)
    if ref in index.sources:
        return index.sources[ref].get("text") or ""
    if ref in index.news:
        return " ".join([_ref_text(f["id"], index) for f in index.news[ref].get("fragments") or []]
                        + [full_texts.get(ref) or ""])
    return ""


def check_report(report: dict, index: PackageIndex, assessment: dict, facts: dict, profile: str,
                 full_texts: dict | None = None) -> list[str]:
    """Нарушения в отчёте; при этом исправляет то, что можно исправить без модели.

    full_texts — полные тексты новостей (если агент их читал), тоже законный источник чисел.
    """
    issues = []
    news_labels = {a["news_id"]: a["label"] for a in assessment["news_assessments"]}
    independent = {l["mechanism_id"] for l in index.links.values()
                   if any(a["link_id"] == l["id"] and a.get("relevant_to_company_independently")
                          and a["label"] in ("applicable", "conditional") for a in assessment["link_assessments"])}
    known = index.all_ids()

    def check_claim(claim, where: str, label: str | None, news_id: str | None) -> dict | None:
        if not isinstance(claim, dict) or not (claim.get("text") or "").strip():
            issues.append(f"{where}: пустое или неверно оформленное утверждение")
            return None
        ctype = claim.get("type")
        if ctype not in CLAIM_TYPES:
            issues.append(f"{where}: неизвестный тип утверждения «{ctype}»")
        refs = [r for r in claim.get("refs") or [] if isinstance(r, str)]
        unknown = [r for r in refs if r not in known]
        if unknown:
            issues.append(f"{where}: ссылки на несуществующие ID {unknown}")
        refs = [r for r in refs if r in known]
        raw_basis = [p for p in claim.get("company_basis") or [] if isinstance(p, str)]
        normalized = [normalize_pointer(facts, p) for p in raw_basis]
        bad_basis = [p for p, n in zip(raw_basis, normalized) if n is None]
        if bad_basis:
            issues.append(f"{where}: несуществующие пути фактов {bad_basis}")
        basis = list(dict.fromkeys(n for n in normalized if n))

        if ctype == "news_fact" and not any(index.fragment_news.get(r) == news_id or r == news_id for r in refs):
            issues.append(f"{where}: news_fact без ссылки на фрагмент этой новости")
        if ctype == "bank_view":
            kinds = {index.source_kind_of(r) for r in refs} - {None}
            if not refs or kinds != {"bank"}:
                issues.append(f"{where}: bank_view должен ссылаться только на материалы Альфа-Банка (сейчас: {kinds or 'нет ссылок'})")
        if ctype == "ai_inference":
            if not basis:
                issues.append(f"{where}: ai_inference без company_basis")
            if label == "conditional" and not HEDGES.search(claim["text"]) and not NEGATIVE_FINDING.search(claim["text"]):
                issues.append(f"{where}: условный вывод сформулирован без условности")
        if CATEGORICAL.search(claim["text"]):
            issues.append(f"{where}: категоричная формулировка о будущем")
        # даты документов и as_of тоже законный источник чисел (годы, месяцы)
        dates = " ".join(f"{d.get('title') or ''} {d.get('publication_date') or ''}" for d in index.documents.values())
        pool_text = " ".join(_ref_text(r, index, full_texts) for r in refs) + f" {profile} {index.as_of} {dates}"
        if news_id:
            pool_text += " " + _ref_text(news_id, index, full_texts)
        pool = _numbers(_with_date_variants(pool_text + " " + json.dumps(facts, ensure_ascii=False)))
        missing = {n for n in _numbers(claim["text"]) if len(n.replace(".", "")) > 1 and not _supported(n, pool)}
        if missing:
            issues.append(f"{where}: числа не найдены в указанных источниках: {sorted(missing)}")
        return {"text": claim["text"].strip(), "type": ctype, "refs": refs, "company_basis": basis}

    items = []
    for i, item in enumerate(report.get("items") or []):
        nid = item.get("news_id")
        if nid not in news_labels or news_labels[nid] not in ("applicable", "conditional"):
            issues.append(f"items[{i}]: новость {nid} не принята на шаге оценки и удалена из отчёта")
            continue
        item["label"] = news_labels[nid]
        for key in CLAIM_LISTS:
            item[key] = [c for c in (check_claim(c, f"items[{i}].{key}[{j}]", item["label"], nid)
                                     for j, c in enumerate(item.get(key) or [])) if c]
        for key in TEXT_LISTS:
            # модель иногда оформляет действие как утверждение — берём из него текст
            item[key] = [x if isinstance(x, str) else x.get("text", "") for x in item.get(key) or []
                         if isinstance(x, str) or isinstance(x, dict)]
            item[key] = [x.strip() for x in item[key] if x.strip()]
        if not item["what_happened"]:
            issues.append(f"items[{i}]: нет описания события")
        items.append(item)
    report["items"] = items

    risks = []
    for i, r in enumerate(report.get("independent_risks") or []):
        if r.get("mechanism_id") not in independent:
            issues.append(f"independent_risks[{i}]: механизм {r.get('mechanism_id')} не отмечен как самостоятельно важный")
            continue
        r["why_it_matters"] = [c for c in (check_claim(c, f"independent_risks[{i}].why_it_matters[{j}]", "conditional", None)
                                           for j, c in enumerate(r.get("why_it_matters") or [])) if c]
        risks.append(r)
    report["independent_risks"] = risks
    if not (report.get("summary") or "").strip():
        issues.append("нет summary")
    return issues


def critic_messages(report: dict, writer_data: dict) -> list[dict]:
    context = {"company_facts": writer_data["company"]["facts"],
               "analytics_sources": [{"mechanism_id": m["id"], "source_kind": m["source_kind"],
                                      "statement_type": m.get("statement_type")} for m in writer_data["mechanisms"]],
               "assessments": [{"news_id": n["id"], "label": n["assessment"]["label"],
                                "required_conditions": n["assessment"]["required_conditions"]}
                               for n in writer_data["accepted_news"]]}
    user = (f"Контекст (данные):\n{json.dumps(context, ensure_ascii=False)}\n\n"
            f"Отчёт для проверки:\n{json.dumps(report, ensure_ascii=False)}")
    return [{"role": "system", "content": CRITIC_PROMPT}, {"role": "user", "content": user}]


def revise_messages(writer_messages: list[dict], report: dict, issues: list[str]) -> list[dict]:
    return writer_messages + [
        {"role": "assistant", "content": json.dumps(report, ensure_ascii=False)},
        {"role": "user", "content": REVISE.format(issues="\n".join(f"- {x}" for x in issues))},
    ]
