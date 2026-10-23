"""Рыночные и макропоказатели: сбор, резкие движения, блок «показатели на дату» для пакета.

Источники (официальные, бесплатные):
- Банк России: официальные курсы валют (XML), ключевая ставка (таблица на сайте), учётная цена золота (XML);
- Московская биржа, ISS API: индексы IMOEX и RGBI.

Резкое движение превращается в «событие по показателю» в формате news-two-views-1.0 по шаблону,
без LLM: числа точные, статус — reported_fact, механизм — шаблонная гипотеза. Такие события
проходят тот же поиск и ту же оценку применимости, что и новости.
"""

from __future__ import annotations

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from . import store

UA = {"User-Agent": "Mozilla/5.0 (alfa-hack prototype)"}
CBR_DAILY = "https://www.cbr.ru/scripts/XML_daily.asp?date_req={d:%d/%m/%Y}"
CBR_DYNAMIC = "https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1={a:%d/%m/%Y}&date_req2={b:%d/%m/%Y}&VAL_NM_RQ={id}"
CBR_KEYRATE = "https://www.cbr.ru/hd_base/KeyRate/?UniDbQuery.Posted=True&UniDbQuery.From={a:%d.%m.%Y}&UniDbQuery.To={b:%d.%m.%Y}"
CBR_METALS = "https://www.cbr.ru/scripts/xml_metall.asp?date_req1={a:%d/%m/%Y}&date_req2={b:%d/%m/%Y}"
MOEX_INDEX = ("https://iss.moex.com/iss/history/engines/stock/markets/index/securities/{code}.json"
              "?from={a:%Y-%m-%d}&iss.meta=off&history.columns=TRADEDATE,CLOSE")

CURRENCIES = ["USD", "EUR", "CNY", "TRY", "KZT", "AED", "INR", "BYN", "HKD", "DKK"]
COUNTRY_CURRENCY = {"КИТАЙ": "CNY", "ГОНКОНГ": "HKD", "ТУРЦИЯ": "TRY", "КАЗАХСТАН": "KZT", "ОАЭ": "AED",
                    "ИНДИЯ": "INR", "БЕЛАРУСЬ": "BYN", "БЕЛОРУССИЯ": "BYN", "ДАНИЯ": "DKK"}

# код → (название, единица, источник, порог недельного изменения в %; None — любое изменение)
INDICATORS = {
    "key_rate": ("Ключевая ставка Банка России", "% годовых", "Банк России", None),
    "imoex": ("Индекс МосБиржи (IMOEX)", "пунктов", "Московская биржа", 5.0),
    "rgbi": ("Индекс гособлигаций RGBI", "пунктов", "Московская биржа", 2.0),
    "gold": ("Учётная цена золота Банка России", "руб./г", "Банк России", 5.0),
    **{f"fx_{c}": (f"Официальный курс {c}/RUB", "руб.", "Банк России", 3.0) for c in CURRENCIES},
}

MECHANISMS = {
    "key_rate": ("Изменение ключевой ставки передаётся в стоимость кредитов, лизинга и доходность депозитов с лагом.",
                 "Как изменение ключевой ставки влияет на стоимость заёмного финансирования и доходность размещения средств компаний?",
                 "Российские компании с кредитами, лизингом или депозитами; эффект зависит от доли плавающих ставок и сроков."),
    "fx": ("Изменение курса рубля меняет рублёвую стоимость импорта и выручку от экспорта в валюте.",
           "Как изменение курса рубля к валюте влияет на стоимость импортных закупок и экспортную выручку компаний?",
           "Компании с внешнеторговыми контрактами в этой валюте; направление эффекта зависит от импорта или экспорта."),
    "imoex": ("Движение фондового рынка отражает ожидания инвесторов и влияет на стоимость акционерного капитала.",
              "Как изменение российского фондового рынка связано с условиями привлечения капитала компаниями?",
              "Публичные компании и эмитенты; для непубличных компаний — косвенный индикатор настроений."),
    "rgbi": ("Цены гособлигаций отражают ожидания по ставкам и задают ориентир стоимости долгового финансирования.",
             "Как изменение доходностей гособлигаций влияет на стоимость облигационного и банковского финансирования?",
             "Эмитенты облигаций и заёмщики; эффект с лагом через ставки рынка."),
    "gold": ("Изменение цены золота влияет на выручку золотодобытчиков и стоимость сырья для ювелирной отрасли.",
             "Как изменение цены золота влияет на компании золотодобычи и переработки драгоценных металлов?",
             "Добыча и переработка драгоценных металлов, ювелирное производство."),
}


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read()


def _num(text: str) -> float:
    return float(text.replace("\xa0", "").replace(" ", "").replace(",", "."))


def fetch_currencies(start: date, end: date) -> list[tuple[str, str, float]]:
    root = ET.fromstring(_get(CBR_DAILY.format(d=end)))
    ids = {v.findtext("CharCode"): v.get("ID") for v in root.iter("Valute")}
    rows = []
    for code in CURRENCIES:
        if code not in ids:
            continue
        dyn = ET.fromstring(_get(CBR_DYNAMIC.format(a=start, b=end, id=ids[code])))
        for rec in dyn.iter("Record"):
            value = _num(rec.findtext("Value")) / float(rec.findtext("Nominal") or 1)
            rows.append((f"fx_{code}", datetime.strptime(rec.get("Date"), "%d.%m.%Y").date().isoformat(), value))
    return rows


def fetch_key_rate(start: date, end: date) -> list[tuple[str, str, float]]:
    html = _get(CBR_KEYRATE.format(a=start, b=end)).decode("utf-8", errors="replace")
    pairs = re.findall(r"<td>(\d{2}\.\d{2}\.\d{4})</td>\s*<td>([\d,]+)</td>", html)
    return [("key_rate", datetime.strptime(d, "%d.%m.%Y").date().isoformat(), _num(v)) for d, v in pairs]


def fetch_gold(start: date, end: date) -> list[tuple[str, str, float]]:
    root = ET.fromstring(_get(CBR_METALS.format(a=start, b=end)))
    return [("gold", datetime.strptime(r.get("Date"), "%d.%m.%Y").date().isoformat(), _num(r.findtext("Buy")))
            for r in root.iter("Record") if r.get("Code") == "1"]


def fetch_moex(code: str, start: date) -> list[tuple[str, str, float]]:
    data = json.loads(_get(MOEX_INDEX.format(code=code.upper(), a=start)))
    return [(code, d, float(v)) for d, v in data["history"]["data"] if v is not None]


def collect(days: int = 60, log=print) -> int:
    """Загрузить историю показателей за days дней и сохранить в БД."""
    end, start = date.today(), date.today() - timedelta(days=days)
    rows, errors = [], []
    for name, fn in [("курсы", lambda: fetch_currencies(start, end)), ("ставка", lambda: fetch_key_rate(start, end)),
                     ("золото", lambda: fetch_gold(start, end)), ("IMOEX", lambda: fetch_moex("imoex", start)),
                     ("RGBI", lambda: fetch_moex("rgbi", start))]:
        try:
            rows += fn()
        except Exception as error:  # недоступность одного источника не должна ломать остальные
            errors.append(f"{name}: {type(error).__name__}: {str(error)[:100]}")
    ts = store.now()
    with store.connect() as con:
        con.executemany(
            "insert into indicators (code, date, value, source, fetched_at) values (?,?,?,?,?)"
            " on conflict(code, date) do update set value=excluded.value, fetched_at=excluded.fetched_at",
            [(code, d, v, INDICATORS[code][2], ts) for code, d, v in rows])
    log(f"Показатели: сохранено {len(rows)} значений" + (f"; ошибки: {errors}" if errors else ""))
    return len(rows)


def history(code: str, days: int = 60) -> list[tuple[str, float]]:
    since = (date.today() - timedelta(days=days)).isoformat()
    with store.connect() as con:
        return [(r["date"], r["value"]) for r in con.execute(
            "select date, value from indicators where code = ? and date >= ? order by date", (code, since))]


def _value_before(series: list[tuple[str, float]], days: int) -> tuple[str, float] | None:
    target = (date.fromisoformat(series[-1][0]) - timedelta(days=days)).isoformat()
    earlier = [p for p in series if p[0] <= target]
    return earlier[-1] if earlier else None


def snapshot(codes: list[str]) -> list[dict]:
    """Последние значения и изменения за неделю и месяц — блок «показатели на дату» для пакета."""
    result = []
    for code in codes:
        series = history(code, 60)
        if not series:
            continue
        last_date, last = series[-1]
        item = {"code": code, "name": INDICATORS[code][0], "unit": INDICATORS[code][1],
                "source": INDICATORS[code][2], "date": last_date, "value": round(last, 4)}
        for label, days in (("change_1w", 7), ("change_1m", 30)):
            before = _value_before(series, days)
            if before:
                item[label] = {"since": before[0], "value": round(before[1], 4),
                               "pct": round((last / before[1] - 1) * 100, 2) if before[1] else None}
        result.append(item)
    return result


def codes_for_company(facts: dict, aspects: list[dict]) -> list[str]:
    """Показатели, к которым компания чувствительна по своим фактам."""
    codes = ["key_rate", "fx_USD", "fx_EUR", "fx_CNY", "imoex"]
    flags = facts.get("banking_product_flags") or {}
    if flags.get("loans") or flags.get("leasing") or flags.get("deposits"):
        codes.append("rgbi")
    countries = (facts.get("foreign_trade") or {}).get("countries") or []
    for c in countries:
        name = c["country"] if isinstance(c, dict) else c
        if COUNTRY_CURRENCY.get(name) and f"fx_{COUNTRY_CURRENCY[name]}" not in codes:
            codes.append(f"fx_{COUNTRY_CURRENCY[name]}")
    okved = (facts.get("industry") or {}).get("main_okved_code") or ""
    if okved.startswith(("07", "24.41", "32.12", "46.72")):
        codes.append("gold")
    return codes


def _event_doc(code: str, series: list[tuple[str, float]], before: tuple[str, float]) -> dict:
    name, unit, source, _ = INDICATORS[code]
    last_date, last = series[-1]
    pct = (last / before[1] - 1) * 100 if before[1] else 0.0
    direction = "вырос" if last > before[1] else "снизился"
    if code == "key_rate":
        thesis = f"{source}: ключевая ставка изменена с {before[1]:.2f} до {last:.2f} {unit} (действует с {last_date})."
    else:
        thesis = (f"{name} {direction} на {abs(pct):.1f}% за период с {before[0]} по {last_date}: "
                  f"с {before[1]:.4g} до {last:.4g} {unit}.")
    description, question, conditions = MECHANISMS["fx" if code.startswith("fx_") else code]
    doc_id = f"indicator:{code}:{last_date}"
    return {
        "schema_version": "news-two-views-1.0",
        "document_id": doc_id,
        "document_title": thesis.split(":")[0] if code == "key_rate" else f"{name}: {direction} на {abs(pct):.1f}%",
        "document_date": last_date,
        "source": {"url": None, "publisher": source},
        "preparation": {"source_access": "full_text", "prepared_by": "indicator_template"},
        "document_limitations": ["Событие сформировано автоматически по рыночным данным, а не по публикации СМИ."],
        "coverage": {"processed_block_ids": [], "excluded_blocks": [], "unprocessed_block_ids": []},
        "fragments": [{
            "id": "F001",
            "title": name,
            "thesis": thesis,
            "context": f"Источник данных: {source}. Значение на {before[0]}: {before[1]:.4g} {unit}; "
                       f"на {last_date}: {last:.4g} {unit}. Причины изменения в данных не указаны.",
            "company_embedding_text": None,
            "mechanism": {"description": description, "status": "hypothesis", "is_established_cause": False,
                          "source_attributions": [], "boundaries": ["Причина движения показателя данными не установлена."],
                          "context": {"conditions": conditions}},
            "analytics_search_question": question,
            "analytics_embedding_text": f"{question}\nУсловия и границы применимости: {conditions}",
            "source_block_ids": [], "source_quotes": [], "attributions": [{"author": source, "statement": thesis}],
            "sectors": [], "countries": ["Россия"], "regions": [], "organizations": [source], "okved_codes": [],
            "event_date_or_period": f"{before[0]} — {last_date}", "limitations": [],
        }],
    }


def detect_events(log=print) -> list[dict]:
    """Резкие движения показателей → документы-события (сохраняются как новости kind = indicator)."""
    from .news import save_news

    events = []
    for code, (_, _, _, threshold) in INDICATORS.items():
        series = history(code, 60)
        if len(series) < 2:
            continue
        if threshold is None:
            changed = [i for i in range(1, len(series)) if series[i][1] != series[i - 1][1]]
            if not changed:
                continue
            i = changed[-1]
            sub, before = series[: i + 1], series[i - 1]
        else:
            sub, before = series, _value_before(series, 7)
            if not before or not before[1] or abs(series[-1][1] / before[1] - 1) * 100 < threshold:
                continue
        doc = _event_doc(code, sub, before)
        doc["fragments"][0]["company_embedding_text"] = f"{doc['fragments'][0]['thesis']}\n\n{doc['fragments'][0]['context']}"
        save_news(doc, "indicator", f"{sub[-1][0]}T00:00:00+00:00", "normal")
        events.append(doc)
    log(f"Событий по показателям: {len(events)}" + (f" ({', '.join(e['document_title'] for e in events)})" if events else ""))
    return events
