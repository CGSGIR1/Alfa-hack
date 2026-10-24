"""Сила влияния события на компанию: рубрика из пяти факторов, итог считает код.

Модель не выбирает «слабо/средне/сильно» напрямую — она ставит баллы по рубрике с пояснением
к каждому, а уровень вычисляется здесь. Так оценка воспроизводима и объяснима. Это
качественная шкала, а не вероятность и не численный прогноз эффекта.
"""

from __future__ import annotations

FACTORS = {
    # фактор: (максимум, якоря по баллам)
    "exposure": (2, [
        "0 — связь только через отрасль в целом или общий макрофон, без опоры на факты компании",
        "1 — через продукт банка (лизинг → рынок лизинга), страну ВЭД без известного направления торговли, отрасль контрагентов",
        "2 — прямо: её товар (рыба для оптовика рыбы), валюта её контрактов, её кредит при изменении ставки",
    ]),
    "magnitude": (4, [
        "0 — новость об одной компании без влияния на рынок; открытие магазина; назначение",
        "1 — узкая товарная ниша (продажи перепелиных яиц); мера для одного муниципалитета",
        "2 — условия одной отрасли без крупных денег: льготы для радиоэлектроники, маркировка одной товарной группы, цены на лизинг техники",
        "3 — курс рубля ±3–5% за неделю; тарифы РЖД и естественных монополий; отраслевая субсидия на миллиарды рублей",
        "4 — изменение ключевой ставки; курс ±5% и больше за неделю; налоги для всего бизнеса (НДС, прибыль); санкции против отрасли или страны-партнёра",
    ]),
    "certainty": (2, [
        "0 — обсуждается, предложено, законопроект до принятия, заявление о намерениях, прогноз",
        "1 — принято или подписано, но вступает позже; решение объявлено",
        "2 — уже действует или произошло: курс установлен, цены изменились, статистика опубликована",
    ]),
    "transmission": (2, [
        "0 — через два и более звена: ставка → спрос населения → розница → поставщик розницы",
        "1 — через одно звено: курс → цены поставщиков → закупки компании",
        "2 — напрямую: ставка → платёж по её кредиту; пошлина на её импорт",
    ]),
    "mitigation": (2, [
        "0 — явная компенсация: у компании и кредиты, и депозиты при изменении ставки; событие давно ожидалось и учтено",
        "1 — частичная: эффект можно переложить в цены, но не сразу",
        "2 — компенсирующих факторов не видно",
    ]),
}
MAX_SCORE = sum(m for m, _ in FACTORS.values())  # 12
LEVELS = [(4, "weak"), (8, "medium"), (MAX_SCORE, "strong")]
LEVEL_RU = {"weak": "слабое", "medium": "среднее", "strong": "сильное"}
DIRECTIONS = {"risk", "opportunity", "mixed", "unclear"}
HORIZONS = {"0-3m", "3-12m", "12m+", "unclear"}
CONFIDENCE = {"low", "medium", "high"}
ORDER = {"weak": 0, "medium": 1, "strong": 2}


def rubric_prompt() -> str:
    lines = []
    for name, (mx, anchors) in FACTORS.items():
        lines.append(f"- {name} (0–{mx}):")
        lines += [f"    {a}" for a in anchors]
    return "\n".join(lines) + (f"\nУровень считает приложение по сумме баллов (0–{MAX_SCORE}): "
                               "0–4 — слабое, 5–8 — среднее, 9–12 — сильное.")


def compute(raw: dict | None, label: str) -> tuple[dict, list[str]]:
    """Проверяет рубрику, считает уровень с ограничениями. Возвращает (impact, замечания)."""
    raw = raw if isinstance(raw, dict) else {}
    rubric_in = raw.get("rubric") if isinstance(raw.get("rubric"), dict) else {}
    issues, rubric = [], {}
    for name, (mx, _) in FACTORS.items():
        item = rubric_in.get(name)
        score = item.get("score") if isinstance(item, dict) else item
        note = item.get("note") if isinstance(item, dict) else None
        if not isinstance(score, int) or not 0 <= score <= mx:
            issues.append(f"рубрика: фактор {name} без корректного балла 0–{mx}")
            score = 0
        if not note:
            issues.append(f"рубрика: фактор {name} без пояснения")
        rubric[name] = {"score": score, "note": note}

    total = sum(v["score"] for v in rubric.values())
    level = next(lv for limit, lv in LEVELS if total <= limit)
    caps = []
    if rubric["exposure"]["score"] == 0 and ORDER[level] > ORDER["weak"]:
        level, caps = "weak", caps + ["экспозиция 0 → не выше слабого"]
    if rubric["certainty"]["score"] == 0 and ORDER[level] > ORDER["medium"]:
        level, caps = "medium", caps + ["событие не состоялось или только обсуждается → не выше среднего"]

    impact = {
        "level": level,
        "level_ru": LEVEL_RU[level],
        "conditional": label == "conditional",
        "score": total,
        "max_score": MAX_SCORE,
        "direction": raw.get("direction") if raw.get("direction") in DIRECTIONS else "unclear",
        "horizon": raw.get("horizon") if raw.get("horizon") in HORIZONS else "unclear",
        "confidence": raw.get("confidence") if raw.get("confidence") in CONFIDENCE else "low",
        "rubric": rubric,
        "caps_applied": caps,
    }
    if label == "conditional":
        impact["caps_applied"].append("conditional → оценка действует только при выполнении условий")
        if impact["confidence"] == "high":
            impact["confidence"] = "medium"
            impact["caps_applied"].append("conditional → уверенность не выше средней")
    return impact, issues


def label(impact: dict) -> str:
    prefix = "при выполнении условий — " if impact.get("conditional") else ""
    return f"{prefix}{impact['level_ru']} ({impact['score']}/{impact['max_score']})"


DISCUSSED = ("законопроект", "предложил", "предлагает", "обсужда", "рассматрива", "намерен", "призвал",
             "заявил о необходимости", "планирует ввести")
ADOPTED = ("принят", "подписал", "утвердил", "вступит в силу", "вступает в силу", "постановлени")


def suggested_scores(news: dict, indicators: list[dict] | None = None) -> dict:
    """Подсказки баллов от кода; модель может отличаться, но должна объяснить почему."""
    hints = {}
    nid = news.get("id") or ""
    ind = news.get("indicator") or {}
    if nid.startswith("indicator:"):
        code = ind.get("code") or nid.split(":")[1]
        z = abs(ind.get("z") or 0)
        if code == "key_rate":
            hints["magnitude"] = {"score": 4, "basis": "изменение ключевой ставки — системное событие"}
        elif z:
            score = 4 if z >= 3 else 3 if z >= 2 else 2
            hints["magnitude"] = {"score": score, "basis": f"движение {ind.get('change')} {ind.get('unit')} — "
                                                           f"{z} обычного недельного колебания (σ = {ind.get('sigma')})"}
        hints["certainty"] = {"score": 2, "basis": "рыночные данные — событие уже произошло"}
        return hints
    text = " ".join(f"{f.get('thesis') or ''} {f.get('context') or ''}" for f in news.get("fragments") or []).lower()
    # статья про курс: масштаб по недельному движению основных валют относительно их обычной волатильности
    if "курс" in text and any(w in text for w in ("доллар", "рубл", "юан", "евро")) and indicators:
        fx = [i for i in indicators if i["code"] in ("fx_USD", "fx_EUR", "cny_moex", "fx_CNY")]
        zs = [abs((i.get("typical_weekly_move") or {}).get("z_1w") or 0) for i in fx]
        if any(zs):
            z = max(zs)
            score = 4 if z >= 3 else 3 if z >= 2 else 2
            hints["magnitude"] = {"score": score, "basis": f"недельное движение основных валют — до {z} обычного колебания"}
        else:
            moves = [abs((i.get("change_1w") or {}).get("pct") or 0) for i in fx]
            if moves:
                pct = max(moves)
                hints["magnitude"] = {"score": 4 if pct >= 5 else 3 if pct >= 3 else 2,
                                      "basis": f"наибольшее недельное движение основных валют {pct}%"}
    if any(w in text for w in ADOPTED):
        hints["certainty"] = {"score": 1, "basis": "в тексте есть признаки принятого, но не вступившего решения"}
    elif any(w in text for w in DISCUSSED):
        hints["certainty"] = {"score": 0, "basis": "в тексте есть признаки обсуждения, предложения или прогноза"}
    return hints
