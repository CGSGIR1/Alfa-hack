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
        "1 — косвенно: через рынок, на котором работает её продукт или контрагенты (рынок лизинга, а не её платежи; "
        "отрасль покупателей; импортный товар без записи о ВЭД)",
        "2 — прямо или при подтверждении условия: её товар (рыба для оптовика рыбы), валюта её контрактов (есть ВЭД), "
        "её кредит или лизинг при изменении ставки, налог, который она платит",
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
        "0 — явная полная компенсация, подтверждённая фактами: событие давно ожидалось и уже учтено в договорах",
        "1 — частичная компенсация, видная из фактов компании: есть и кредиты, и депозиты при изменении ставки; "
        "есть и экспорт, и импорт при изменении курса",
        "2 — компенсирующих факторов в фактах компании не видно. Общее «можно переложить в цены» без фактов — "
        "не компенсатор, это 2",
    ]),
}
MAX_SCORE = sum(m for m, _ in FACTORS.values())  # 12
# коды уровней — как в интерфейсе (high/medium/low), подписи — как там же
LEVELS = [(5, "low"), (8, "medium"), (MAX_SCORE, "high")]
LEVEL_RU = {"low": "низкое", "medium": "среднее", "high": "высокое"}
DIRECTIONS = {"risk", "opportunity", "mixed", "unclear"}
HORIZONS = {"0-3m", "3-12m", "12m+", "unclear"}
CONFIDENCE = {"low", "medium", "high"}
ORDER = {"low": 0, "medium": 1, "high": 2}


def rubric_prompt() -> str:
    lines = []
    for name, (mx, anchors) in FACTORS.items():
        lines.append(f"- {name} (0–{mx}):")
        lines += [f"    {a}" for a in anchors]
    return "\n".join(lines) + (
        f"\nУровень считает приложение по сумме баллов (0–{MAX_SCORE}): 0–5 — низкое, 6–8 — среднее, 9–12 — высокое; "
        "высокое — только при прямой связи (экспозиция 2), при слабой связи (экспозиция + передача ≤ 1) или мелком "
        "событии с косвенной связью — низкое, обсуждаемое событие — не выше среднего.\n"
        "Балл 1 не означает «не знаю». Если связь с компанией не подтверждена (метка conditional), ставь баллы так, "
        "как если бы условие подтвердилось (условие «платежи по лизингу привязаны к ставке» → экспозиция 2), — "
        "неуверенность отражается меткой «оценка условная», а не баллами.")


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
    r = {k: v["score"] for k, v in rubric.items()}
    if r["exposure"] + r["transmission"] <= 1 and ORDER[level] > ORDER["low"]:
        level, caps = "low", caps + ["слабая связь (экспозиция + передача ≤ 1) → не выше низкого"]
    if r["exposure"] <= 1 and r["magnitude"] <= 1 and ORDER[level] > ORDER["low"]:
        level, caps = "low", caps + ["мелкое событие с косвенной связью → не выше низкого"]
    if r["exposure"] <= 1 and ORDER[level] > ORDER["medium"]:
        level, caps = "medium", caps + ["связь косвенная (экспозиция ≤ 1) → не выше среднего"]
    if rubric["certainty"]["score"] == 0 and ORDER[level] > ORDER["medium"]:
        level, caps = "medium", caps + ["событие не состоялось или только обсуждается → не выше среднего"]

    impact = {
        "level": level,
        "level_ru": LEVEL_RU[level],
        "conditional": label == "conditional",
        # балл не выше верхней границы итогового уровня: шкала в интерфейсе не расходится с уровнем
        "score": min(total, next(limit for limit, lv in LEVELS if lv == level)),
        "raw_score": total,
        "max_score": MAX_SCORE,
        "direction": raw.get("direction") if raw.get("direction") in DIRECTIONS else "unclear",
        "horizon": raw.get("horizon") if raw.get("horizon") in HORIZONS else "unclear",
        "confidence": raw.get("confidence") if raw.get("confidence") in CONFIDENCE else "low",
        "rubric": rubric,
        "explanation": (raw.get("explanation") or "").strip() or None,
        "caps_applied": caps,
    }
    if label == "conditional":
        impact["caps_applied"].append("conditional → оценка действует только при выполнении условий")
        if impact["confidence"] == "high":
            impact["confidence"] = "medium"
            impact["caps_applied"].append("conditional → уверенность не выше средней")
    if not impact["explanation"]:
        issues.append("impact.explanation: нет словесного пояснения силы влияния (одно предложение)")
    impact["summary"] = summary(impact)
    return impact, issues


def summary(impact: dict) -> str:
    """Словесная оценка для руководителя: «Среднее влияние (6 из 12), оценка условная: …»."""
    head = f"{impact['level_ru'].capitalize()} влияние ({impact['score']} из {impact['max_score']})"
    if impact.get("conditional"):
        head += ", оценка условная"
    text = impact.get("explanation")
    if not text:
        return head + "."
    # «Компания…» → «компания…», но аббревиатуры («НДС…») не трогаем
    return f"{head}: {text[0].lower() + text[1:] if text[1:2].islower() else text}"


def label(impact: dict) -> str:
    prefix = "при выполнении условий — " if impact.get("conditional") else ""
    return f"{prefix}{impact['level_ru']} ({impact['score']}/{impact['max_score']})"


DISCUSSED = ("законопроект", "предложил", "предлагает", "обсужда", "рассматрива", "намерен", "призвал",
             "заявил о необходимости", "планирует ввести")
ADOPTED = ("принят", "подписал", "утвердил", "вступит в силу", "вступает в силу", "постановлени")


def _flags(facts: dict | None) -> dict:
    """Продукты банка и ВЭД из фактов компании: формат пайплайна или строка данных кейса (members[].raw_fields)."""
    facts = facts or {}
    products = facts.get("banking_product_flags") or {}
    trade = facts.get("foreign_trade") or {}
    flags = {"loans": bool(products.get("loans")), "leasing": bool(products.get("leasing")),
             "deposits": bool(products.get("deposits")), "fts": bool(trade.get("fts_record_present")),
             "countries": list(trade.get("countries") or [])}
    for m in facts.get("members") or []:
        raw = m.get("raw_fields") or {}
        on = lambda key: str(raw.get(key) or "").strip() in ("1", "1.0")
        flags["loans"] |= on("CL_PRODUCT_LOA_MARK")
        flags["leasing"] |= on("CL_PRODUCT_LEA_MARK")
        flags["deposits"] |= on("CL_PRODUCT_DEP_MARK")
        flags["fts"] |= on("FTS_FLAG")
        flags["countries"] += [c.strip() for c in str(raw.get("FTS_COUNTRY_NAME") or "").split(",") if c.strip()]
    return flags


def _company_hints(channel: str, f: dict) -> dict:
    """Экспозиция, передача и смягчение по фактам компании для канала события."""
    debt = [name for key, name in (("loans", "кредит"), ("leasing", "лизинг")) if f[key]]
    if channel == "rate":
        if debt:
            hints = {"exposure": {"score": 2, "basis": f"у компании отмечен {' и '.join(debt)} — при плавающей ставке "
                                                       "или новых сделках ставка прямо меняет её платежи (условие)"},
                     "transmission": {"score": 2 if f["loans"] else 1,
                                      "basis": "ставка → платёж по кредиту" if f["loans"] else "ставка → стоимость фондирования лизингодателя → платёж"}}
            hints["mitigation"] = ({"score": 1, "basis": "есть и долг, и депозиты — частичная компенсация"} if f["deposits"]
                                   else {"score": 2, "basis": "депозитов, компенсирующих стоимость долга, в данных нет"})
            return hints
        if f["deposits"]:
            return {"exposure": {"score": 1, "basis": "долга нет, есть депозиты — ставка меняет доход по ним"}}
        return {"exposure": {"score": 0, "basis": "кредитов, лизинга и депозитов в данных нет — только через макрофон"}}
    if channel == "fx":
        if f["fts"]:
            countries = ", ".join(dict.fromkeys(f["countries"])) or "страны не указаны"
            return {"exposure": {"score": 2, "basis": f"есть запись о ВЭД ({countries}) — курс меняет рублёвую стоимость её "
                                                      "контрактов (условие: расчёты в валюте)"},
                    "transmission": {"score": 2, "basis": "курс → рублёвая стоимость валютного контракта"}}
        return {"exposure": {"score": 1, "basis": "записи о ВЭД нет — курс влияет только через цены поставщиков, если товар импортный"},
                "transmission": {"score": 1, "basis": "курс → цены поставщиков → закупки компании"}}
    if channel == "tax":
        return {"exposure": {"score": 2, "basis": "налог для всех плательщиков на общей системе — компания платит его сама"}}
    return {}


FX_WORDS = ("доллар", "рубл", "юан", "евро")


def _channel_of(text: str) -> str | None:
    if "ставк" in text and any(w in text for w in ("ключев", "кредит", "лизинг", "ссуд")):
        return "rate"
    if any(w in text for w in FX_WORDS) and ("курс" in text or "рубль" in text):
        return "fx"
    if any(w in text for w in ("ндс", "налог на прибыль", "налоговая нагрузк")):
        return "tax"
    return None


def _channel(news: dict, text: str) -> str | None:
    """Канал события: код показателя, затем заголовок (статья может касаться и курса, и ставки), затем текст."""
    code = ((news.get("indicator") or {}).get("code") or (news.get("id") or "").split(":")[-1]).lower()
    if code == "key_rate":
        return "rate"
    if code.startswith("fx_"):
        return "fx"
    title = (news.get("title") or "").lower()
    if any(w in title for w in FX_WORDS):
        return "fx"
    return _channel_of(title) or _channel_of(text)


def suggested_scores(news: dict, indicators: list[dict] | None = None, facts: dict | None = None) -> dict:
    """Подсказки баллов от кода; модель может отличаться, но должна объяснить почему."""
    text = " ".join([news.get("title") or ""] + [f"{f.get('thesis') or ''} {f.get('context') or ''}"
                                                 for f in news.get("fragments") or []]).lower()
    channel = _channel(news, text)
    hints = _company_hints(channel, _flags(facts)) if channel and facts is not None else {}
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
    # статья про курс: масштаб по недельному движению основных валют относительно их обычной волатильности
    if channel == "fx" and indicators:
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
