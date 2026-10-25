"""Компании: кеш → шаблонное описание → аспекты влияния (LLM) → кеш → эмбеддинги.

    company = get_company("U_102920")       # компания
    company = get_company("group_133117_sb")  # группа компаний

Если компании или группы нет в данных банка — CompanyNotFound.
"""

from __future__ import annotations

from functools import lru_cache

from ..company_profile import DataStore, build_group_profile, build_profile, describe_full, describe_group
from ..company_profile.impact import get_impact_queries, is_fallback
from ..company_profile.profile import PRODUCTS, CompanyProfile
from ..embeddings import get_store
from . import store, users

PRODUCT_KEYS = {
    "CL_PRODUCT_RKO_MARK": "settlement_services", "CL_PRODUCT_LOA_MARK": "loans", "CL_PRODUCT_LEA_MARK": "leasing",
    "CL_PRODUCT_DEP_MARK": "deposits", "CL_PRODUCT_ZPP_MARK": "payroll_project",
    "CL_PRODUCT_GAR_MARK": "bank_guarantees", "FACTORING_MARK": "factoring",
}
# Откуда в фактах берётся основание аспекта по его категории — для original_company_source_refs.
CATEGORY_REFS = {
    "finance": ["/facts/banking_product_flags"], "foreign_trade": ["/facts/foreign_trade"],
    "macro": ["/facts/foreign_trade", "/facts/banking_product_flags"], "region": ["/facts/registration"],
    "labor": ["/facts/employees", "/facts/banking_product_flags/payroll_project"],
    "supply_chain": ["/facts/counterparties"], "sales": ["/facts/counterparties"],
    "related": ["/facts/group_id"], "industry": ["/facts/industry"], "commodities": ["/facts/industry"],
    "regulation": ["/facts/industry"],
}
BASE_LIMITATIONS = [
    "Компания обезличена; сопоставление её названия с внешними новостями не установлено.",
    "Год, валюта и единица измерения выручки не определены в предоставленных данных.",
    "Продуктовые флаги относятся к данным банка; нулевой флаг не доказывает отсутствие продукта в других банках.",
    "ОКВЭД не устанавливает фактический ассортимент и конкретные операции компании.",
    "Платежи отражают только операции, видимые банку; период и валюта платежей не установлены.",
]


class CompanyNotFound(LookupError):
    pass


@lru_cache(maxsize=1)
def data_store() -> DataStore:
    return DataStore()


def company_facts(p: CompanyProfile) -> dict:
    f = p.features
    return {
        "annual_revenue": {"currency": None, "source_value": str(p.revenue), "unit": None, "year": None},
        "bank_client_status_source": p.client_type,
        "banking_product_flags": {PRODUCT_KEYS[col]: f.get(col) == 1 for col in PRODUCTS},
        "business_stage_source": p.eco_stage,
        "company_age_years_band": p.age,
        "employees": {"fns_band": p.workers, "spark_band": f.get("AMOUNT_WORKERS_SPARK_GRADATION")},
        "foreign_trade": {
            "countries": p.countries, "direction": None, "fts_record_present": p.foreign_trade,
            "interpretation": "Флаг 0 означает отсутствие записи в этих данных, а не доказанное отсутствие ВЭД.",
            "settlement_currencies": None,
        },
        "group_id": p.group_id,
        "industry": {"main_okved_code": p.okved_code, "main_okved_name": p.industry_name},
        "key_owner_marker": p.negative_mark,
        "organization_type_source": f.get("ORG_TYPE"),
        "ownership_form_source": p.ownership,
        "registration": {"city": p.city, "region": p.region},
        "counterparties": {
            "suppliers_industry_mix": p.suppliers.industry_mix,
            "buyers_industry_mix": p.buyers.industry_mix,
            "suppliers_region_mix": p.suppliers.region_mix,
            "buyers_region_mix": p.buyers.region_mix,
            "classified_share": {"suppliers": round(p.suppliers.known_share, 4), "buyers": round(p.buyers.known_share, 4)},
        },
    }


def group_facts(g) -> dict:
    return {
        "group_id": g.group_id,
        "members_count": g.size,
        "annual_revenue_sum": {"currency": None, "source_value": str(g.revenue), "unit": None, "year": None},
        "industry_mix": g.divisions,
        "regions": g.regions,
        "client_types": g.client_types,
        "banking_products": g.products,
        "foreign_trade": {"countries": g.countries, "revenue_share_with_fts_record": g.foreign_trade_share},
        "counterparties": {"suppliers_industry_mix": g.suppliers.industry_mix, "buyers_industry_mix": g.buyers.industry_mix},
    }


def package_aspects(company_id: str, queries) -> list[dict]:
    """Аспекты в формате пакета report-generation-input-1.0."""
    result = []
    for i, q in enumerate(queries.queries, start=1):
        result.append({
            "id": f"{company_id}_Q{i:02d}",
            "channel": q.channel,
            "text": q.text,
            "evidence_status": "hypothesis" if q.origin == "llm" and q.kind == "llm" else "grounded_topic",
            "assumptions": [q.why] if q.kind == "llm" else [],
            "limitations": [],
            "original_company_source_refs": CATEGORY_REFS.get(q.category, ["/facts/industry"]),
            "weight": q.weight,
            "category": q.category,
        })
    return result


def _build(company_id: str) -> dict:
    ds = data_store()
    if company_id.startswith("group_"):
        if company_id not in ds.groups.index:
            raise CompanyNotFound(f"Нет данных о группе компаний {company_id}")
        g = build_group_profile(ds, company_id)
        subject_type, description, facts, notes = "group", describe_group(g), group_facts(g), g.data_notes
    else:
        if company_id not in ds.companies.index:
            raise CompanyNotFound(f"Нет данных о компании {company_id}")
        p = build_profile(ds, company_id)
        subject_type, description, facts, notes = "company", describe_full(p), company_facts(p), p.data_notes
    queries = get_impact_queries(ds, company_id)   # кеш аспектов → иначе LLM на лету → иначе правила
    return {
        "id": company_id,
        "subject_type": subject_type,
        "description": description,
        "facts": facts,
        "limitations": BASE_LIMITATIONS + notes,
        "aspects": package_aspects(company_id, queries),
        "aspects_origin": "rules" if is_fallback(queries) else "llm",
    }


def get_company(company_id: str, refresh: bool = False) -> dict:
    """Компания из кеша пайплайна; при отсутствии — строится, сохраняется и индексируется.

    Только для пользователей (группа с выручкой > 15 млрд ₽): иначе users.NotEligible — и отчёта не будет.
    """
    users.require(company_id)
    with store.connect() as con:
        row = con.execute("select * from companies where id = ?", (company_id,)).fetchone()
        # аспекты из правил (LLM была недоступна) пересобираем при следующем обращении
        if row and not refresh and row["aspects_origin"] == "llm":
            return _row(row)
    company = _build(company_id)
    get_store().index_company(company_id, company["aspects"])
    ts = store.now()
    with store.connect() as con:
        con.execute(
            "insert into companies (id, subject_type, description, facts, limitations, aspects, aspects_origin,"
            " created_at, updated_at, embedded_at) values (?,?,?,?,?,?,?,?,?,?)"
            " on conflict(id) do update set description=excluded.description, facts=excluded.facts,"
            " limitations=excluded.limitations, aspects=excluded.aspects, aspects_origin=excluded.aspects_origin,"
            " updated_at=excluded.updated_at, embedded_at=excluded.embedded_at, tracked=1",
            (company_id, company["subject_type"], company["description"], store.dumps(company["facts"]),
             store.dumps(company["limitations"]), store.dumps(company["aspects"]), company["aspects_origin"],
             ts, ts, ts))
    return company


def tracked_companies() -> list[str]:
    """Пользователи, для которых отчёт уже запускали (профиль построен): их hourly-tick поддерживает.

    Занесение в users само по себе отчёт не запускает: каждый отчёт стоит денег (порядка 8 ₽),
    поэтому для нового пользователя персональная лента начинается с явного `report <id>`.
    """
    with store.connect() as con:
        return [r["id"] for r in con.execute(
            "select c.id from companies c join client_category k on k.company_id = c.id and k.in_category = 1"
            " where c.tracked = 1")]


def _row(row) -> dict:
    return {"id": row["id"], "subject_type": row["subject_type"], "description": row["description"],
            "facts": store.loads(row["facts"]), "limitations": store.loads(row["limitations"]),
            "aspects": store.loads(row["aspects"]), "aspects_origin": row["aspects_origin"]}
