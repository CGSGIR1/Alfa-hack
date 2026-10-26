"""Отчёт в Markdown — для чтения и демонстрации (интерфейс будет брать JSON)."""

from __future__ import annotations

from .impact import label as impact_label

LABELS = {"applicable": "применимо", "conditional": "условно применимо",
          "background": "фон", "rejected": "отклонено"}
TYPES = {"news_fact": "Факт из новости", "bank_view": "Позиция Альфа-Банка",
         "third_party_view": "Оценка третьей стороны", "ai_inference": "Вывод AI"}
DIR = {"risk": "риск", "opportunity": "возможность", "mixed": "разнонаправленно", "unclear": "неясно"}
HORIZON = {"0-3m": "до 3 мес.", "3-12m": "3–12 мес.", "12m+": "больше года", "unclear": "срок неясен"}
CONF = {"low": "низкая", "medium": "средняя", "high": "высокая"}
FACTOR_RU = {"exposure": "экспозиция", "magnitude": "масштаб", "certainty": "определённость",
             "transmission": "передача", "mitigation": "смягчающие факторы"}


def _impact(imp: dict) -> str:
    head = (f"**Сила влияния:** {imp.get('summary') or impact_label(imp)}  \n"
            f"<sub>{DIR.get(imp['direction'], imp['direction'])}{' (разнонаправленно)' if imp.get('mixed') else ''} · {HORIZON.get(imp['horizon'], imp['horizon'])} · "
            f"уверенность {CONF.get(imp['confidence'], imp['confidence'])}</sub>")
    rows = [f"  - {FACTOR_RU[k]}: {v['score']} — {v.get('note') or ''}" for k, v in imp["rubric"].items()]
    return head + "\n" + "\n".join(rows)


SECTIONS = [("what_happened", "Что произошло"), ("why_it_matters", "Почему это важно компании"),
            ("risks", "Риски"), ("opportunities", "Возможности"), ("bank_view", "Оценки банка и других аналитиков")]


def _claim(c: dict) -> str:
    refs = ", ".join(c.get("refs") or [])
    basis = ", ".join(c.get("company_basis") or [])
    tail = "; ".join(x for x in [f"источники: {refs}" if refs else "", f"факты компании: {basis}" if basis else ""] if x)
    return f"- **[{TYPES.get(c['type'], c['type'])}]** {c['text']}" + (f"  \n  <sub>{tail}</sub>" if tail else "")


def _brief(part: dict) -> list[str]:
    b = part.get("brief") if isinstance(part.get("brief"), dict) else {}
    out = []
    if b.get("takeaway"):
        out.append(f"**Коротко:** {b['takeaway']}")
    if b.get("recommendation"):
        out.append(f"**Рекомендация:** {b['recommendation']}")
    return [" \n".join(out), ""] if out else []


def to_markdown(doc: dict) -> str:
    out = [f"# Отчёт для компании {doc['company_id']} на {doc['as_of']}", "",
           "> Не является инвестиционной рекомендацией.", "", doc.get("summary") or "", ""]
    if doc.get("key_recommendations"):
        out += ["**Ключевые рекомендации:**", *(f"{i}. {k['text']}" for i, k in enumerate(doc["key_recommendations"], 1)), ""]
    for item in doc["items"]:
        meta = " · ".join(x for x in [item.get("topic"), item.get("publisher"), item.get("published_at")] if x)
        out += [f"## {item.get('short_title') or item.get('headline')}",
                f"*{LABELS.get(item['label'], item['label'])}*" + (f" · {meta}" if meta else "") + f" · `{item['news_id']}`", ""]
        out += _brief(item)
        if item.get("key_figures"):
            out += [" · ".join(f"**{f['value']}** {f['label']}" for f in item["key_figures"]), ""]
        if item.get("brief"):
            out += ["<details><summary>Подробнее</summary>", ""]
        if item.get("short_title") and item.get("headline"):
            out += [f"*{item['headline']}*", ""]
        if item.get("why_you_see_this"):
            out += [f"**Почему вы это видите:** {item['why_you_see_this']}", ""]
        if item.get("impact"):
            out += [_impact(item["impact"]), ""]
        if item.get("channels"):
            out += ["**Каналы влияния:** " + "; ".join(f"{c.get('channel')} ({DIR.get(c.get('direction'), c.get('direction'))})"
                                                      for c in item["channels"] if isinstance(c, dict)), ""]
        for key, title in SECTIONS:
            if item.get(key):
                out += [f"### {title}", *(_claim(c) for c in item[key]), ""]
        for key, title in (("not_established", "Что не установлено"), ("to_verify", "Что стоит проверить"),
                           ("next_steps", "Рекомендации")):
            if item.get(key):
                out += [f"### {title}", *(f"- {x}" for x in item[key]), ""]
        if item.get("brief"):
            out += ["</details>", ""]
    if doc["independent_risks"]:
        out += ["## Риски, важные компании независимо от новостей", ""]
        for r in doc["independent_risks"]:
            out += [f"### {r.get('headline')}", ""] + _brief(r) + ([_impact(r["impact"]), ""] if r.get("impact") else [])
            out += [*(_claim(c) for c in r.get("why_it_matters") or [])]
            out += [*(f"- Рекомендация: {x}" for x in r.get("next_steps") or []),
                    *(f"- Не установлено: {x}" for x in r.get("not_established") or []),
                    *(f"- Проверить: {x}" for x in r.get("to_verify") or []), ""]
    if doc["dismissed"]:
        out += ["## Отобрано поиском, но не влияет на компанию", ""]
        out += [f"- **{d['title']}** ({d.get('publisher')}) — *{LABELS.get(d['label'], d['label'])}*: {d['reason']}"
                for d in doc["dismissed"]]
        out.append("")
    if doc["missing_information"]:
        out += ["## Каких сведений не хватает для точной оценки", *(f"- {x}" for x in doc["missing_information"]), ""]
    q = doc["quality"]
    extra = []
    if "assessment_agreement" in q:
        extra.append(f"согласие двух оценок применимости {q['assessment_agreement']:.0%}"
                     + (f", спорных пунктов {len(q['disputed'])} (решал арбитр)" if q.get("disputed") else ""))
    if q.get("assessment_auto_fixes"):
        extra.append(f"автоисправлений оценки {len(q['assessment_auto_fixes'])}")
    if q.get("skipped_steps"):
        extra.append(f"пропущено по бюджету: {len(q['skipped_steps'])}")
    models = ", ".join(f"{k}: {v}" for k, v in doc["models"].items())
    out += ["---", f"<sub>Проверка: найдено замечаний {q['report_issues_found']}, осталось {len(q['report_issues_remaining'])}"
            + (f"; {'; '.join(extra)}" if extra else "") + f". Модели — {models}. "
            f"Стоимость {doc['usage']['cost']:.2f} ₽, время {doc['seconds']} с.</sub>"]
    return "\n".join(out)
