"""Структурированный профиль компании: всё, что нужно для описания, ранжирования и агента.

Каждое поле можно проследить до исходных колонок таблиц (см. `features`),
производные признаки хранят причины в `tags`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from .data import DataStore
from .okved import division_name
from .tags import make_tags, rate_exposure
from .text import plural

LARGE_BUSINESS_REVENUE = 15_000_000_000

PRODUCTS = {
    "CL_PRODUCT_RKO_MARK": "расчётно-кассовое обслуживание",
    "CL_PRODUCT_LOA_MARK": "кредиты",
    "CL_PRODUCT_LEA_MARK": "лизинг",
    "CL_PRODUCT_DEP_MARK": "депозиты",
    "CL_PRODUCT_ZPP_MARK": "зарплатный проект",
    "CL_PRODUCT_GAR_MARK": "банковские гарантии",
    "FACTORING_MARK": "факторинг",
}

TOP_COUNTERPARTIES = 5
TOP_MIX = 5


@dataclass
class Counterparty:
    company_id: str
    amount: float
    count: int
    share: float
    industry: str | None
    same_group: bool


@dataclass
class CounterpartySide:
    total_amount: float = 0.0
    count: int = 0
    known_share: float = 0.0          # доля суммы по контрагентам с известной отраслью
    intragroup_share: float = 0.0     # доля суммы внутри своей группы компаний
    top: list[Counterparty] = field(default_factory=list)
    top_known_external: list[Counterparty] = field(default_factory=list)  # крупнейшие вне группы с известной отраслью
    industry_mix: list[dict] = field(default_factory=list)  # внешние контрагенты по разделам ОКВЭД


@dataclass
class CompanyProfile:
    company_id: str
    industry_name: str
    okved_code: str
    okved_class: str
    division: str
    division_name: str
    city: str
    region: str
    ownership: str
    age: str
    workers: str
    eco_stage: str | None
    client_type: str
    revenue: float
    is_large: bool
    class_rank: int
    class_size: int
    class_share: float
    group_id: str
    group_size: int
    group_revenue: float
    group_revenue_share: float
    group_divisions: list[dict]
    group_regions: int
    products: list[str]
    missing_products: list[str]
    rate_exposure: str | None
    foreign_trade: bool
    countries: list[str]
    negative_mark: bool
    suppliers: CounterpartySide
    buyers: CounterpartySide
    self_transfers: float
    linked_persons: int
    linked_companies_other_groups: int
    tags: dict[str, list[str]]
    data_notes: list[str]
    features: dict

    def to_dict(self) -> dict:
        return asdict(self)


def counterparty_side(store: DataStore, rows: pd.DataFrame, other_col: str, group_id: str, role: str) -> CounterpartySide:
    if rows.empty:
        return CounterpartySide()
    companies = store.companies
    total = float(rows["PAY_AMT"].sum())
    known = rows[other_col].isin(companies.index)
    group = rows[other_col].map(companies["GROUP_ID"]) == group_id

    def counterparties(subset: pd.DataFrame) -> list[Counterparty]:
        result = []
        for _, r in subset.head(TOP_COUNTERPARTIES).iterrows():
            cid = r[other_col]
            result.append(Counterparty(
                company_id=cid,
                amount=float(r["PAY_AMT"]),
                count=int(r["PAY_CNT"]),
                share=float(r["PAY_AMT"]) / total,
                industry=companies.at[cid, "MAINOKVED_NAME"] if cid in companies.index else None,
                same_group=bool(cid in companies.index and companies.at[cid, "GROUP_ID"] == group_id),
            ))
        return result

    external = rows[known & ~group]
    mix = []
    if not external.empty:
        by_division = (
            external.assign(division=external[other_col].map(companies["okved_division"]))
            .groupby("division")["PAY_AMT"].sum()
            .sort_values(ascending=False)
        )
        ext_total = by_division.sum()
        for division, amount in by_division.head(TOP_MIX).items():
            mix.append({
                "role": role,
                "division": division,
                "name": division_name(division),
                "share": float(amount / ext_total),
            })

    return CounterpartySide(
        total_amount=total,
        count=int(rows[other_col].nunique()),
        known_share=float(rows.loc[known, "PAY_AMT"].sum() / total),
        intragroup_share=float(rows.loc[group, "PAY_AMT"].sum() / total),
        top=counterparties(rows),
        top_known_external=counterparties(external),
        industry_mix=mix,
    )


def _person_links(store: DataStore, company_id: str, group_id: str) -> tuple[int, int]:
    links = store.person_links
    persons = links.loc[links["SYNTHETIC_U"] == company_id, "SYNTHETIC_F"].unique()
    if len(persons) == 0:
        return 0, 0
    others = links.loc[links["SYNTHETIC_F"].isin(persons) & (links["SYNTHETIC_U"] != company_id), "SYNTHETIC_U"].unique()
    groups = store.companies["GROUP_ID"].reindex(others)
    other_groups = int(((groups != group_id) | groups.isna()).sum())
    return len(persons), other_groups


def _data_notes(store: DataStore, row: pd.Series, suppliers: CounterpartySide, buyers: CounterpartySide) -> list[str]:
    notes = []
    if row["duplicate_records"] > 1:
        records = store.records_for(row["SYNTHETIC_U"])
        differing = [
            col for col in ("MAINOKVED_CODE", "REGION", "REVENUE_COMPANY", "GROUP_ID")
            if records[col].nunique() > 1
        ]
        n = int(row["duplicate_records"])
        notes.append(
            f"В данных {n} {plural(n, 'запись', 'записи', 'записей')} с этим идентификатором"
            + (f", расходятся поля: {', '.join(differing)}" if differing else "")
            + "; использована запись с наибольшей выручкой."
        )
    if pd.isna(row["ECO_MARK_LABEL"]):
        notes.append("Стадия развития бизнеса не указана.")
    if row["FTS_FLAG"] == 1 and not row["countries"]:
        notes.append("Компания есть в базе ФТС, но страны ВЭД не указаны.")
    if suppliers.count == 0 and buyers.count == 0:
        notes.append("Платежей с контрагентами в данных нет, анализ цепочек поставок невозможен.")
    else:
        notes.append("Платежи отражают только операции, видимые банку, а не весь оборот компании.")
        sides = [s for s in (suppliers, buyers) if s.count]
        known = sum(s.known_share * s.total_amount for s in sides) / sum(s.total_amount for s in sides)
        if known < 0.5:
            notes.append(f"Отрасль известна только для {known:.0%} суммы платежей с контрагентами.")
    return notes


def build_profile(store: DataStore, company_id: str) -> CompanyProfile:
    if company_id not in store.companies.index:
        raise KeyError(f"Компания {company_id} не найдена")
    row = store.companies.loc[company_id]
    group_id = row["GROUP_ID"]

    suppliers = counterparty_side(store, store.outgoing(company_id), "BENEFICIARY", group_id, "supplier")
    buyers = counterparty_side(store, store.incoming(company_id), "PAYER", group_id, "buyer")

    members = store.group_members(group_id)
    group_divisions = []
    if len(members) > 1:
        by_division = members.groupby("okved_division")["REVENUE_COMPANY"].sum().sort_values(ascending=False)
        for division, revenue in by_division.head(TOP_MIX).items():
            group_divisions.append({
                "division": division,
                "name": division_name(division),
                "share": float(revenue / by_division.sum()),
            })

    persons, other_groups = _person_links(store, company_id, group_id)
    products = [name for col, name in PRODUCTS.items() if row[col] == 1]
    missing = [name for col, name in PRODUCTS.items() if row[col] != 1]

    features = {
        col: (None if pd.isna(row[col]) else row[col].item() if hasattr(row[col], "item") else row[col])
        for col in store.all_records.columns
        if col.isupper()
    }

    return CompanyProfile(
        company_id=company_id,
        industry_name=row["MAINOKVED_NAME"],
        okved_code=row["MAINOKVED_CODE"],
        okved_class=row["okved_class"],
        division=row["okved_division"],
        division_name=division_name(row["okved_division"]),
        city=row["CITY"],
        region=row["REGION"],
        ownership=row["OKFS_NAME"],
        age=row["COMPANY_AGE_YEARS_GRADATION"],
        workers=row["AMOUNT_WORKERS_FNS_GRADATION"],
        eco_stage=None if pd.isna(row["ECO_MARK_LABEL"]) else row["ECO_MARK_LABEL"],
        client_type=row["CL_TYPE"],
        revenue=float(row["REVENUE_COMPANY"]),
        is_large=bool(row["REVENUE_COMPANY"] >= LARGE_BUSINESS_REVENUE),
        class_rank=int(row["class_rank"]),
        class_size=int(row["class_size"]),
        class_share=float(row["class_share"]),
        group_id=group_id,
        group_size=int(row["group_size"]),
        group_revenue=float(row["group_revenue"]),
        group_revenue_share=float(row["REVENUE_COMPANY"] / row["group_revenue"]),
        group_divisions=group_divisions,
        group_regions=int(members["REGION"].nunique()),
        products=products,
        missing_products=missing,
        rate_exposure=rate_exposure(row),
        foreign_trade=bool(row["FTS_FLAG"] == 1),
        countries=list(row["countries"]),
        negative_mark=bool(row["NEGATIVEMARK"] == 1),
        suppliers=suppliers,
        buyers=buyers,
        self_transfers=float(store.self_transfers.get(company_id, 0.0)),
        linked_persons=persons,
        linked_companies_other_groups=other_groups,
        tags=make_tags(row, suppliers.industry_mix + buyers.industry_mix),
        data_notes=_data_notes(store, row, suppliers, buyers),
        features=features,
    )
