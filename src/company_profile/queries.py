"""Запросы влияния: короткие тексты для поиска новостей, по одному на канал влияния.

Поиск по эмбеддингу описания компании находит новости «про отрасль», но не
находит то, что на компанию реально влияет (ставка, курс, пошлины, поставщики).
Поэтому из профиля строится набор запросов: каждый описывает один канал
влияния языком новостей и помнит, из каких признаков компании он получен.

Базовый генератор работает на правилах (без LLM, детерминированно).
LLM-доработка — в `refine.py`, она опирается на эти же запросы.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .group import GroupProfile
from .okved import division_name
from .profile import CompanyProfile
from .tags import WEAK_TAGS
from .text import plural

MAX_QUERIES = 12

# Насколько надёжен источник признака: продукт банка — факт о компании,
# связь через контрагентов — косвенная.
SOURCE_WEIGHTS = {"product": 1.0, "fts": 0.9, "okved": 0.8, "counterparty": 0.5}
WEAK_TAG_FACTOR = 0.5
INDUSTRY_WEIGHT = 1.0
COUNTRY_WEIGHT = 0.8
REGION_WEIGHT = 0.3
COUNTERPARTY_MIN_SHARE = 0.15
GROUP_MIN_SHARE = 0.05
MAX_COUNTRIES = 3

# {industry} — раздел ОКВЭД компании, {countries} — страны ВЭД.
FACTOR_QUERIES = {
    "ипотека": "ипотечное кредитование, льготная ипотека и спрос на жильё",
    "рынок_труда": "рынок труда: рост зарплат, дефицит кадров, безработица",
    "госзаказ": "госзакупки и бюджетные расходы, госконтракты в сфере «{industry}», банковские гарантии",
    "спрос": "спрос и динамика продаж в сфере «{industry}»",
    "инфляция": "инфляция, рост цен производителей и потребительских цен",
    "валюта": "курс рубля к доллару и евро, валютное регулирование",
    "санкции": "санкции и ограничения внешней торговли{countries_suffix}",
    "юань": "курс юаня к рублю, расчёты и платежи с Китаем",
    "импорт": "импорт и параллельный импорт: пошлины, таможня, сроки поставок в сфере «{industry}»",
    "экспортные_пошлины": "экспортные пошлины и квоты, условия экспорта в сфере «{industry}»",
    "нефть": "цены на нефть и нефтепродукты, топливный рынок, демпфер",
    "металл": "цены на металлы и металлопрокат, спрос на сталь",
    "золото": "цены на золото и рынок драгоценных металлов",
    "зерно": "цены на зерно, урожай и экспорт зерна",
    "погода": "погодные условия и урожай: засуха, заморозки, паводки",
    "продукты": "цены на продукты питания, продовольственный рынок",
    "стройматериалы": "цены на стройматериалы и объёмы строительства",
    "лес": "рынок леса и лесоматериалов, экспорт древесины",
    "химия": "химическая промышленность, цены на удобрения и химикаты",
    "лекарства": "фармацевтический рынок, регулирование цен и импорт лекарств",
    "электроника": "поставки электроники и компьютерной техники, дефицит комплектующих",
    "тарифы": "тарифы на электроэнергию, газ и коммунальные услуги для бизнеса",
    "налоги": "налоговые изменения для бизнеса: НДС, налог на прибыль, льготы в сфере «{industry}»",
    "логистика": "грузоперевозки и логистика: ставки, сроки, ж/д и автоперевозки",
    "аренда": "коммерческая недвижимость: ставки аренды офисов и складов",
    "туризм": "туристический поток, внутренний и выездной туризм",
}

RATE_QUERIES = {
    "borrower": "ключевая ставка ЦБ и стоимость кредитов и лизинга для бизнеса",
    "depositor": "ключевая ставка ЦБ и доходность депозитов для компаний",
    "mixed": "ключевая ставка ЦБ: стоимость кредитов и доходность депозитов для бизнеса",
    None: "ключевая ставка ЦБ и стоимость заёмного финансирования в сфере «{industry}»",
}

CURRENCIES = {
    "КИТАЙ": "юаня", "ГОНКОНГ": "юаня", "ТУРЦИЯ": "лиры", "КАЗАХСТАН": "тенге",
    "ОАЭ": "дирхама", "ИНДИЯ": "рупии", "БЕЛАРУСЬ": "белорусского рубля",
    "УЗБЕКИСТАН": "сума", "КИРГИЗИЯ": "сома", "АРМЕНИЯ": "драма",
}


@dataclass
class ImpactQuery:
    channel: str            # тег фактора или kind:значение (industry:46, country:КИТАЙ, ...)
    kind: str               # factor | industry | counterparty | country | region
    text: str               # текст для поиска по новостям
    why: str                # почему канал важен этой компании (для «почему я это вижу»)
    sources: list[str]      # признаки профиля, из которых получен запрос
    weight: float           # важность канала для компании, 0..1
    origin: str = "rules"   # rules | llm

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QuerySet:
    subject_id: str         # SYNTHETIC_U или GROUP_ID
    subject_type: str       # company | group
    queries: list[ImpactQuery] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # что произошло при доработке (для трейса)

    def to_dict(self) -> dict:
        return asdict(self)


def _factor_text(tag: str, industry: str, countries: list[str], rate_exposure: str | None) -> str | None:
    if tag == "ставка":
        template = RATE_QUERIES[rate_exposure]
    else:
        template = FACTOR_QUERIES.get(tag)
    if template is None:
        return None
    suffix = f" со странами: {', '.join(c.title() for c in countries)}" if countries else ""
    return template.format(industry=industry, countries_suffix=suffix)


def _tag_weight(tag: str, reasons: list[dict]) -> float:
    weight = max(SOURCE_WEIGHTS[r["source"]] for r in reasons)
    return weight * (WEAK_TAG_FACTOR if tag in WEAK_TAGS else 1.0)


def _country_query(country: str, weight: float, why: str, sources: list[str]) -> ImpactQuery:
    currency = CURRENCIES.get(country)
    text = f"внешняя торговля со страной {country.title()}: пошлины, санкции, логистика" + (f", курс {currency}" if currency else "")
    return ImpactQuery(f"country:{country}", "country", text, why, sources, weight)


def _counterparty_queries(mix: list[dict], scale: float = 1.0) -> list[ImpactQuery]:
    result = []
    for item in mix:
        if item["share"] < COUNTERPARTY_MIN_SHARE:
            continue
        if item["role"] == "supplier":
            text = f"{item['name']}: цены, перебои поставок, финансовое положение компаний отрасли"
            why = f"поставщики из сферы «{item['name']}» — {item['share']:.0%} платежей"
        else:
            text = f"{item['name']}: спрос, объёмы заказов и платёжеспособность компаний отрасли"
            why = f"покупатели из сферы «{item['name']}» — {item['share']:.0%} платежей"
        weight = min(1.0, 0.4 + item["share"]) * SOURCE_WEIGHTS["counterparty"] * scale
        result.append(ImpactQuery(f"{item['role']}:{item['division']}", "counterparty", text, why, [why], weight))
    return result


def _finalize(subject_id: str, subject_type: str, queries: list[ImpactQuery], limit: int) -> QuerySet:
    """Объединяет запросы с одинаковым текстом и оставляет самые важные."""
    merged: dict[str, ImpactQuery] = {}
    for q in queries:
        if q.text in merged:
            kept = merged[q.text]
            kept.weight = max(kept.weight, q.weight)
            kept.sources += [s for s in q.sources if s not in kept.sources]
        else:
            merged[q.text] = q
    ranked = sorted(merged.values(), key=lambda q: q.weight, reverse=True)[:limit]
    for q in ranked:
        q.weight = round(q.weight, 3)
    return QuerySet(subject_id, subject_type, ranked)


def company_queries(p: CompanyProfile, limit: int = MAX_QUERIES) -> QuerySet:
    queries = [ImpactQuery(
        channel=f"industry:{p.okved_class}",
        kind="industry",
        text=f"{p.industry_name}: новости отрасли, цены, спрос, регулирование",
        why=f"основной вид деятельности компании — {p.industry_name.lower()}",
        sources=[f"MAINOKVED_CODE={p.okved_code}"],
        weight=INDUSTRY_WEIGHT,
    )]

    for tag, reasons in p.tags.items():
        text = _factor_text(tag, p.division_name, p.countries, p.rate_exposure)
        if text is None:
            continue
        queries.append(ImpactQuery(
            channel=tag,
            kind="factor",
            text=text,
            why="; ".join(r["reason"] for r in reasons),
            sources=[r["reason"] for r in reasons],
            weight=_tag_weight(tag, reasons),
        ))

    for country in p.countries[:MAX_COUNTRIES]:
        reason = f"FTS_COUNTRY_NAME: {country}"
        queries.append(_country_query(country, COUNTRY_WEIGHT, f"компания ведёт ВЭД со страной {country.title()}", [reason]))

    queries += _counterparty_queries(p.suppliers.industry_mix + p.buyers.industry_mix)

    queries.append(ImpactQuery(
        channel=f"region:{p.region}",
        kind="region",
        text=f"{p.region}: экономика региона, инфраструктура, региональные меры поддержки бизнеса",
        why=f"компания зарегистрирована в регионе {p.region}",
        sources=[f"REGION={p.region}"],
        weight=REGION_WEIGHT,
    ))
    return _finalize(p.company_id, "company", queries, limit)


def group_queries(g: GroupProfile, limit: int = MAX_QUERIES) -> QuerySet:
    """Запросы для группы: вес канала умножается на долю выручки группы, которой он касается."""
    queries = []
    for d in g.divisions:
        if d["share"] < GROUP_MIN_SHARE:
            continue
        queries.append(ImpactQuery(
            channel=f"industry:{d['division']}",
            kind="industry",
            text=f"{d['name']}: новости отрасли, цены, спрос, регулирование",
            why=f"направление «{d['name']}» — {d['share']:.0%} выручки группы",
            sources=[f"доля выручки группы по ОКВЭД {d['division']}: {d['share']:.0%}"],
            weight=INDUSTRY_WEIGHT * min(1.0, 0.3 + d["share"]),
        ))

    main_industry = g.divisions[0]["name"] if g.divisions else "бизнес группы"
    countries = [c["country"] for c in g.countries]
    for tag, info in g.tags.items():
        share = info["revenue_share"]
        if info["companies"] and share < GROUP_MIN_SHARE:
            continue
        industry = main_industry
        if tag != "ставка":
            # отрасль, в которой этот фактор весомее всего
            okved = next((r for r in info["reasons"] if r["source"] == "okved"), None)
            if okved:
                industry = division_name(okved["reason"].split()[1][:2])
        text = _factor_text(tag, industry, countries[:MAX_COUNTRIES], _group_rate_exposure(g) if tag == "ставка" else None)
        if text is None:
            continue
        base = max(SOURCE_WEIGHTS[r["source"]] for r in info["reasons"])
        scale = min(1.0, 0.3 + share) if info["companies"] else 1.0
        queries.append(ImpactQuery(
            channel=tag,
            kind="factor",
            text=text,
            why=(f"касается {share:.0%} выручки группы" if info["companies"] else "косвенно через контрагентов группы"),
            sources=[r["reason"] for r in info["reasons"]],
            weight=base * scale * (WEAK_TAG_FACTOR if tag in WEAK_TAGS else 1.0),
        ))

    for c in g.countries[:MAX_COUNTRIES]:
        n, share = c["companies"], c["revenue_share"]
        queries.append(_country_query(
            c["country"], COUNTRY_WEIGHT * min(1.0, 0.3 + share),
            f"ВЭД со страной {c['country'].title()} {plural(n, 'ведёт', 'ведут', 'ведут')} {n} "
            f"{plural(n, 'компания', 'компании', 'компаний')} группы ({share:.0%} выручки)",
            [f"FTS_COUNTRY_NAME: {c['country']} ({n})"],
        ))

    queries += _counterparty_queries(g.suppliers.industry_mix + g.buyers.industry_mix)

    for r in g.regions:
        if r["share"] >= 0.2:
            queries.append(ImpactQuery(
                channel=f"region:{r['region']}",
                kind="region",
                text=f"{r['region']}: экономика региона, инфраструктура, региональные меры поддержки бизнеса",
                why=f"в регионе {r['region']} — {r['share']:.0%} выручки группы",
                sources=[f"REGION={r['region']}"],
                weight=REGION_WEIGHT * r["share"],
            ))
    return _finalize(g.group_id, "group", queries, limit)


def _group_rate_exposure(g: GroupProfile) -> str | None:
    used = {p["product"]: p["revenue_share"] for p in g.products}
    borrower = max(used.get("кредиты", 0), used.get("лизинг", 0)) >= GROUP_MIN_SHARE
    depositor = used.get("депозиты", 0) >= GROUP_MIN_SHARE
    if borrower and depositor:
        return "mixed"
    if borrower:
        return "borrower"
    if depositor:
        return "depositor"
    return None
