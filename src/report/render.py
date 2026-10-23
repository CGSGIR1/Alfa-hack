"""Отчёт в Markdown — для чтения и демонстрации (интерфейс будет брать JSON)."""

from __future__ import annotations

LABELS = {"applicable": "применимо", "conditional": "условно применимо",
          "background": "фон", "rejected": "отклонено"}
TYPES = {"news_fact": "Факт из новости", "bank_view": "Позиция Альфа-Банка",
         "third_party_view": "Оценка третьей стороны", "ai_inference": "Вывод AI"}
SECTIONS = [("what_happened", "Что произошло"), ("why_it_matters", "Почему это важно компании"),
            ("risks", "Риски"), ("opportunities", "Возможности"), ("bank_view", "Оценки банка и других аналитиков")]


def _claim(c: dict) -> str:
    refs = ", ".join(c.get("refs") or [])
    basis = ", ".join(c.get("company_basis") or [])
    tail = "; ".join(x for x in [f"источники: {refs}" if refs else "", f"факты компании: {basis}" if basis else ""] if x)
    return f"- **[{TYPES.get(c['type'], c['type'])}]** {c['text']}" + (f"  \n  <sub>{tail}</sub>" if tail else "")


def to_markdown(doc: dict) -> str:
    out = [f"# Отчёт для компании {doc['company_id']} на {doc['as_of']}", "", doc.get("summary") or "", ""]
    for item in doc["items"]:
        out += [f"## {item.get('headline')}", f"*{LABELS.get(item['label'], item['label'])}* · новость `{item['news_id']}`", ""]
        if item.get("why_you_see_this"):
            out += [f"**Почему вы это видите:** {item['why_you_see_this']}", ""]
        for key, title in SECTIONS:
            if item.get(key):
                out += [f"### {title}", *(_claim(c) for c in item[key]), ""]
        for key, title in (("not_established", "Что не установлено"), ("to_verify", "Что стоит проверить"),
                           ("next_steps", "Возможные шаги")):
            if item.get(key):
                out += [f"### {title}", *(f"- {x}" for x in item[key]), ""]
    if doc["independent_risks"]:
        out += ["## Риски, важные компании независимо от новостей", ""]
        for r in doc["independent_risks"]:
            out += [f"### {r.get('headline')}", *(_claim(c) for c in r.get("why_it_matters") or [])]
            out += [*(f"- Не установлено: {x}" for x in r.get("not_established") or []),
                    *(f"- Проверить: {x}" for x in r.get("to_verify") or []), ""]
    if doc["dismissed"]:
        out += ["## Отобрано поиском, но не влияет на компанию", ""]
        out += [f"- **{d['title']}** ({d.get('publisher')}) — *{LABELS.get(d['label'], d['label'])}*: {d['reason']}"
                for d in doc["dismissed"]]
        out.append("")
    if doc["missing_information"]:
        out += ["## Каких сведений не хватает для точной оценки", *(f"- {x}" for x in doc["missing_information"]), ""]
    q = doc["quality"]
    out += ["---", f"<sub>Проверка: найдено замечаний {q['report_issues_found']}, осталось {len(q['report_issues_remaining'])}; "
            f"автоисправлений оценки {len(q['assessment_auto_fixes'])}. Модели: {doc['models']['main']}, критик "
            f"{doc['models']['critic']}. Стоимость {doc['usage']['cost']:.2f} ₽, время {doc['seconds']} с.</sub>"]
    return "\n".join(out)
