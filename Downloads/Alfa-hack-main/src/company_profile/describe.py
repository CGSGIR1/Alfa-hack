"""Текстовое описание компании и группы компаний по шаблону, без LLM.

`describe_short` — паспорт компании в одном абзаце.
`describe_full` — описание для промпта агента: факты из таблиц по разделам,
затем отдельно помеченные производные выводы (теги факторов с причинами)
и ограничения данных.
`describe_group` — такой же отчёт по группе компаний.
"""

from __future__ import annotations

from .group import GroupProfile
from .profile import LARGE_BUSINESS_REVENUE, CompanyProfile, CounterpartySide
from .text import money, percent, plural

# Доли меньше этой в перечислениях отраслей не показываем, чтобы не засорять текст.
MIN_SHOWN_SHARE = 0.03

# Факторы группы, касающиеся меньшей доли выручки, перечисляются одной строкой без причин.
MIN_GROUP_TAG_SHARE = 0.05

TAG_SOURCES = {
    "product": "продукты банка",
    "okved": "отрасль",
    "fts": "ВЭД",
    "counterparty": "косвенно через контрагентов",
}

CLIENT_TYPES = {
    "CLIENT": "действующий клиент банка",
    "PROSPECT": "потенциальный клиент, продуктами банка не пользуется",
    # В кейсе описаны только CLIENT и PROSPECT; смысл CLOSE_CLIENT подтвердили организаторы.
    # РКО у таких компаний нет, но другие продуктовые флаги бывают (вероятно, исторические).
    "CLOSE_CLIENT": "закрытый клиент банка (бывший клиент)",
}

CLIENT_TYPES_SHORT = {
    "CLIENT": "действующий клиент",
    "PROSPECT": "потенциальный клиент",
    "CLOSE_CLIENT": "закрытый клиент",
}

CLIENT_TYPES_PLURAL = {
    "CLIENT": "действующие клиенты",
    "PROSPECT": "потенциальные клиенты",
    "CLOSE_CLIENT": "закрытые клиенты",
}

RATE_EXPOSURE = {
    "borrower": "рост ставки увеличивает стоимость заёмного финансирования, снижение — уменьшает",
    "depositor": "высокая ставка повышает доходность размещения свободных средств",
    "mixed": "ставка влияет в обе стороны: удорожает заёмные средства, но повышает доход по депозитам",
}


def years(gradation: str) -> str:
    upper = int(gradation.split("-")[-1])
    return f"{gradation.replace('-', '–')} {plural(upper, 'год', 'года', 'лет')}"


def workers(gradation: str) -> str:
    text = gradation.replace("-", "–")
    return text[0].lower() + text[1:] + " человек"


def shares(items: list[dict]) -> str:
    return ", ".join(f"{m['name']} ({percent(m['share'])})" for m in items if m["share"] >= MIN_SHOWN_SHARE)


def describe_short(p: CompanyProfile) -> str:
    stage = f" Стадия развития бизнеса по оценке банка — «{p.eco_stage}»." if p.eco_stage else ""
    place = p.city if p.region in p.city else f"{p.city}, {p.region}"
    return (
        f"Компания занимается деятельностью «{p.industry_name}» (ОКВЭД {p.okved_code}). "
        f"Зарегистрирована: {place}. "
        f"Форма собственности — {p.ownership.lower()}. "
        f"Возраст компании — {years(p.age)}.{stage} "
        f"По данным ФНС, численность работников — {workers(p.workers)}."
    )


def _scale(p: CompanyProfile) -> str:
    if p.is_large:
        size = f"Это крупный бизнес по критерию кейса (выручка от {money(LARGE_BUSINESS_REVENUE)})."
    elif p.group_revenue >= LARGE_BUSINESS_REVENUE:
        size = "Сама компания ниже порога крупного бизнеса, но входит в группу, которая ему соответствует."
    else:
        size = "Компания и её группа ниже порога крупного бизнеса в кейсе."
    if p.class_size == 1:
        position = f"Других компаний с ОКВЭД {p.okved_class} в данных банка нет."
    else:
        position = (
            f"По выручке компания на {p.class_rank}-м месте из {p.class_size} компаний "
            f"с ОКВЭД {p.okved_class} в данных банка, на неё приходится {percent(p.class_share)} их суммарной выручки "
            f"(это доля внутри выборки банка, а не рыночная доля)."
        )
    return f"Годовая выручка — {money(p.revenue)}. {size} {position}"


def _group(p: CompanyProfile) -> str:
    if p.group_size == 1:
        return "Сведений о группе компаний нет: в своей группе компания единственная."
    divisions = shares(p.group_divisions)
    return (
        f"Входит в группу компаний {p.group_id}: {p.group_size} "
        f"{plural(p.group_size, 'компания', 'компании', 'компаний')} "
        f"в {p.group_regions} {plural(p.group_regions, 'регионе', 'регионах', 'регионах')}, "
        f"суммарная выручка группы — {money(p.group_revenue)}, доля компании — {percent(p.group_revenue_share)}."
        + (f" Основные направления группы по выручке: {divisions}." if divisions else "")
    )


def _bank(p: CompanyProfile) -> str:
    status = CLIENT_TYPES.get(p.client_type, p.client_type)
    if p.products and p.client_type == "CLOSE_CLIENT":
        products = f"В данных отмечены продукты банка: {', '.join(p.products)} (могут быть действующими или прошлыми)."
    elif p.products:
        products = f"Использует продукты банка: {', '.join(p.products)}. Не использует: {', '.join(p.missing_products)}."
    else:
        products = "Продуктов банка в данных нет."
    rate = f" Чувствительность к ключевой ставке: {RATE_EXPOSURE[p.rate_exposure]}." if p.rate_exposure else ""
    return f"Статус — {status}. {products}{rate}"


def _foreign_trade(p: CompanyProfile) -> str:
    if p.countries:
        return f"Ведёт внешнеэкономическую деятельность (данные ФТС), страны: {', '.join(c.title() for c in p.countries)}."
    if p.foreign_trade:
        return "Есть в базе ФТС, страны внешнеэкономической деятельности не указаны."
    return "Сведений о внешнеэкономической деятельности нет."


def _side(side: CounterpartySide, title: str) -> str:
    if not side.count:
        return f"{title}: платежей в данных нет."
    parts = [f"{title}: {side.count} {plural(side.count, 'контрагент', 'контрагента', 'контрагентов')}, {money(side.total_amount)}"]
    if side.intragroup_share >= 0.01:
        parts.append(f"из них внутри группы — {percent(side.intragroup_share)}")
    text = ", ".join(parts) + "."
    mix = shares(side.industry_mix)
    if mix:
        text += f" Отрасли внешних контрагентов (по сумме платежей с известной отраслью): {mix}."
    regions = ", ".join(f"{r['region']} ({percent(r['share'])})" for r in side.region_mix if r["share"] >= MIN_SHOWN_SHARE)
    if regions:
        text += f" Регионы внешних контрагентов: {regions}."
    top = side.top_known_external[:3]
    if top:
        items = "; ".join(f"{c.company_id} — {c.industry.lower()}, {percent(c.share)}" for c in top)
        text += f" Крупнейшие внешние контрагенты с известной отраслью: {items}."
    return text


def _counterparties(p: CompanyProfile) -> str:
    text = "\n\n".join([
        _side(p.suppliers, "Поставщики (исходящие платежи)"),
        _side(p.buyers, "Покупатели (входящие платежи)"),
    ])
    if p.self_transfers:
        text += f"\n\nПереводы между собственными счетами: {money(p.self_transfers)}, в анализ контрагентов не включены."
    return text


def _links(p: CompanyProfile) -> str | None:
    parts = []
    if p.linked_persons:
        parts.append(
            f"С компанией {plural(p.linked_persons, 'связано', 'связаны', 'связано')} {p.linked_persons} "
            f"{plural(p.linked_persons, 'физлицо', 'физлица', 'физлиц')}"
            + (f", через них — ещё {p.linked_companies_other_groups} "
               f"{plural(p.linked_companies_other_groups, 'компания', 'компании', 'компаний')} вне группы"
               if p.linked_companies_other_groups else "")
            + "."
        )
    if p.affiliated:
        items = "; ".join(f"{a['company_id']} — {a['industry'].lower()}, {a['region']}" for a in p.affiliated)
        parts.append(f"Связанные через общих физлиц компании вне группы: {items}.")
    if p.negative_mark:
        parts.append("В данных стоит признак ключевого собственника (NEGATIVEMARK=1).")
    return " ".join(parts) or None


def _tags(p: CompanyProfile) -> str:
    lines = []
    for tag, reasons in p.tags.items():
        items = "; ".join(f"{r['reason']} [{TAG_SOURCES[r['source']]}]" for r in reasons)
        lines.append(f"- {tag}: {items}")
    return "\n".join(lines) if lines else "- явных факторов не выявлено"


def describe_full(p: CompanyProfile) -> str:
    sections = [
        f"## Компания {p.company_id}",
        describe_short(p),
        "### Масштаб и положение в отрасли",
        _scale(p),
        "### Группа компаний",
        _group(p),
        "### Отношения с банком",
        _bank(p),
        "### Внешнеэкономическая деятельность",
        _foreign_trade(p),
        "### Контрагенты (по платежам через банк)",
        _counterparties(p),
    ]
    links = _links(p)
    if links:
        sections += ["### Связи и отметки", links]
    sections += [
        "### Факторы влияния (выведены правилами из признаков выше, не факты)",
        _tags(p),
        "### Ограничения данных",
        "\n".join(f"- {note}" for note in p.data_notes),
    ]
    return "\n\n".join(sections)


def _group_summary(g: GroupProfile) -> str:
    if g.is_large:
        size = f"Это крупный бизнес по критерию кейса (выручка от {money(LARGE_BUSINESS_REVENUE)})."
    else:
        size = "Группа ниже порога крупного бизнеса в кейсе."
    leader = g.top_companies[0]
    groups_total = f"{g.groups_total:,}".replace(",", " ")
    return (
        f"Группа из {g.size} {plural(g.size, 'компании', 'компаний', 'компаний')} "
        f"в {g.regions_total} {plural(g.regions_total, 'регионе', 'регионах', 'регионах')}, "
        f"суммарная годовая выручка — {money(g.revenue)}. {size} "
        f"По выручке группа на {g.rank}-м месте из {groups_total} групп в данных банка. "
        f"Крупнейшая компания — {leader['company_id']} ({leader['industry'].lower()}), "
        f"{percent(leader['share'])} выручки группы."
    )


def _group_structure(g: GroupProfile) -> str:
    text = f"Компании группы работают в {g.divisions_total} {plural(g.divisions_total, 'разделе', 'разделах', 'разделах')} ОКВЭД."
    divisions = shares(g.divisions)
    if divisions:
        text += f" Основные направления по выручке: {divisions}."
    lines = [
        f"- {c['company_id']} — {c['industry'].lower()} (ОКВЭД {c['okved_code']}), {c['region']}, "
        f"{money(c['revenue'])} ({percent(c['share'])}), {CLIENT_TYPES_SHORT.get(c['client_type'], c['client_type'])}"
        for c in g.top_companies
    ]
    return text + "\n\nКрупнейшие компании группы:\n" + "\n".join(lines)


def _group_geography(g: GroupProfile) -> str:
    regions = ", ".join(f"{r['region']} ({percent(r['share'])})" for r in g.regions if r["share"] >= MIN_SHOWN_SHARE)
    return f"Регионов присутствия: {g.regions_total}. Основные по выручке: {regions}."


def _group_bank(g: GroupProfile) -> str:
    types = ", ".join(
        f"{CLIENT_TYPES_PLURAL.get(t['client_type'], t['client_type'])} — {t['companies']} "
        f"({percent(t['revenue_share'])} выручки)"
        for t in g.client_types
    )
    text = f"Статус компаний группы: {types}."
    if g.products:
        used = ", ".join(
            f"{p['product']} — {p['companies']} {plural(p['companies'], 'компания', 'компании', 'компаний')} "
            f"({percent(p['revenue_share'])} выручки)"
            for p in g.products
        )
        text += f" Продукты банка в группе: {used}."
    if g.unused_products:
        text += f" Ни одна компания группы не использует: {', '.join(g.unused_products)}."
    return text


def _group_foreign_trade(g: GroupProfile) -> str:
    if not g.foreign_trade_companies:
        return "Сведений о внешнеэкономической деятельности компаний группы нет."
    text = (
        f"Внешнеэкономическую деятельность ведут {g.foreign_trade_companies} "
        f"{plural(g.foreign_trade_companies, 'компания', 'компании', 'компаний')} группы "
        f"({percent(g.foreign_trade_share)} выручки группы)."
    )
    if g.countries:
        countries = ", ".join(
            f"{c['country'].title()} ({c['companies']} {plural(c['companies'], 'компания', 'компании', 'компаний')}, "
            f"{percent(c['revenue_share'])} выручки)"
            for c in g.countries
        )
        text += f" Страны: {countries}."
    return text


def _group_counterparties(g: GroupProfile) -> str:
    text = "\n\n".join([
        _side(g.suppliers, "Поставщики группы (исходящие платежи)"),
        _side(g.buyers, "Покупатели группы (входящие платежи)"),
    ])
    if g.intragroup_payments:
        text += f"\n\nПлатежи между компаниями группы: {money(g.intragroup_payments)}."
    return text


def _group_links(g: GroupProfile) -> str | None:
    if not g.linked_persons:
        return None
    n = g.linked_persons
    text = f"С компаниями группы {plural(n, 'связано', 'связаны', 'связано')} {n} {plural(n, 'физлицо', 'физлица', 'физлиц')}"
    if g.linked_companies_outside:
        n = g.linked_companies_outside
        text += f", через них — ещё {n} {plural(n, 'компания', 'компании', 'компаний')} вне группы"
    return text + "."


def _group_tags(g: GroupProfile) -> str:
    lines, minor = [], []
    for tag, info in g.tags.items():
        if info["companies"] and info["revenue_share"] < MIN_GROUP_TAG_SHARE:
            minor.append(tag)
            continue
        if info["companies"]:
            head = (
                f"{percent(info['revenue_share'])} выручки группы, {info['companies']} "
                f"{plural(info['companies'], 'компания', 'компании', 'компаний')}"
            )
        else:
            head = "только косвенно"
        reasons = "; ".join(
            f"{r['reason']} [{TAG_SOURCES[r['source']]}"
            + (f", {percent(r['revenue_share'])} выручки]" if r["revenue_share"] is not None else "]")
            for r in info["reasons"]
            if r["revenue_share"] is None or r["revenue_share"] >= 0.01
        )
        lines.append(f"- {tag} ({head}): {reasons}")
    if minor:
        lines.append(f"- касаются менее {percent(MIN_GROUP_TAG_SHARE)} выручки группы: {', '.join(minor)}")
    return "\n".join(lines) if lines else "- явных факторов не выявлено"


def describe_group(g: GroupProfile) -> str:
    sections = [
        f"## Группа компаний {g.group_id}",
        _group_summary(g),
        "### Структура бизнеса",
        _group_structure(g),
        "### География",
        _group_geography(g),
        "### Отношения с банком",
        _group_bank(g),
        "### Внешнеэкономическая деятельность",
        _group_foreign_trade(g),
        "### Внешние контрагенты группы (по платежам через банк)",
        _group_counterparties(g),
    ]
    links = _group_links(g)
    if links:
        sections += ["### Связи", links]
    sections += [
        "### Факторы влияния (выведены правилами, доля — какой части выручки группы касается фактор)",
        _group_tags(g),
    ]
    if g.data_notes:
        sections += ["### Ограничения данных", "\n".join(f"- {note}" for note in g.data_notes)]
    return "\n\n".join(sections)
