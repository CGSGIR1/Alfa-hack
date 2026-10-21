"""Профиль группы компаний (GROUP_ID): крупный бизнес в данных — это в основном группы.

Показатели группы складываются из компаний-участников. Признаки, которые есть
не у всех участников (продукты, ВЭД, теги факторов), взвешиваются долей
выручки: важно не сколько компаний, а какую часть бизнеса группы это касается.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from .data import DataStore
from .okved import division_name
from .profile import LARGE_BUSINESS_REVENUE, PRODUCTS, CounterpartySide, counterparty_side
from .tags import counterparty_tags, make_tags
from .text import plural

TOP_COMPANIES = 5
TOP_ITEMS = 5
TOP_TAG_REASONS = 3


@dataclass
class GroupProfile:
    group_id: str
    size: int
    revenue: float
    is_large: bool
    rank: int
    groups_total: int
    top_companies: list[dict]
    divisions: list[dict]
    divisions_total: int
    regions: list[dict]
    regions_total: int
    client_types: list[dict]
    products: list[dict]
    unused_products: list[str]
    foreign_trade_share: float
    foreign_trade_companies: int
    countries: list[dict]
    suppliers: CounterpartySide
    buyers: CounterpartySide
    intragroup_payments: float
    linked_persons: int
    linked_companies_outside: int
    tags: dict[str, dict]
    data_notes: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _revenue_shares(members: pd.DataFrame, column: str, total: float) -> pd.Series:
    return (members.groupby(column)["REVENUE_COMPANY"].sum() / total).sort_values(ascending=False)


def _aggregate_tags(members: pd.DataFrame, total: float, counterparty_mix: list[dict]) -> dict[str, dict]:
    """Теги участников, взвешенные их долей в выручке группы, плюс косвенные теги группы."""
    weights: dict[str, float] = {}
    companies: dict[str, int] = {}
    reasons: dict[str, dict[tuple[str, str], float]] = {}
    for _, row in members.iterrows():
        share = row["REVENUE_COMPANY"] / total
        for tag, items in make_tags(row).items():
            weights[tag] = weights.get(tag, 0.0) + share
            companies[tag] = companies.get(tag, 0) + 1
            for item in items:
                key = (item["source"], item["reason"])
                reasons.setdefault(tag, {})[key] = reasons.setdefault(tag, {}).get(key, 0.0) + share

    result = {}
    for tag in weights:
        top = sorted(reasons[tag].items(), key=lambda kv: kv[1], reverse=True)[:TOP_TAG_REASONS]
        result[tag] = {
            "revenue_share": weights[tag],
            "companies": companies[tag],
            "reasons": [{"source": s, "reason": r, "revenue_share": w} for (s, r), w in top],
        }
    for tag, items in counterparty_tags(counterparty_mix).items():
        entry = result.setdefault(tag, {"revenue_share": 0.0, "companies": 0, "reasons": []})
        entry["reasons"] += [{**item, "revenue_share": None} for item in items]
    return dict(sorted(result.items(), key=lambda kv: kv[1]["revenue_share"], reverse=True))


def _person_links(store: DataStore, member_ids: set[str]) -> tuple[int, int]:
    links = store.person_links
    persons = links.loc[links["SYNTHETIC_U"].isin(member_ids), "SYNTHETIC_F"].unique()
    outside = links.loc[links["SYNTHETIC_F"].isin(persons) & ~links["SYNTHETIC_U"].isin(member_ids), "SYNTHETIC_U"]
    return len(persons), int(outside.nunique())


def _data_notes(store: DataStore, members: pd.DataFrame, suppliers: CounterpartySide, buyers: CounterpartySide) -> list[str]:
    notes = []
    duplicated = int((members["duplicate_records"] > 1).sum())
    if duplicated:
        notes.append(
            f"У {duplicated} {plural(duplicated, 'компании', 'компаний', 'компаний')} группы несколько "
            "противоречивых записей в данных; использованы записи с наибольшей выручкой."
        )
    no_stage = int(members["ECO_MARK_LABEL"].isna().sum())
    if no_stage:
        notes.append(f"Стадия развития не указана у {no_stage} {plural(no_stage, 'компании', 'компаний', 'компаний')}.")
    without = int((~members.index.isin(store.companies_with_payments)).sum())
    if without:
        notes.append(
            f"У {without} из {len(members)} компаний нет платежей в данных; "
            "контрагенты группы описаны по остальным участникам."
        )
    if suppliers.count or buyers.count:
        notes.append("Платежи отражают только операции, видимые банку, а не весь оборот группы.")
    return notes


def build_group_profile(store: DataStore, group_id: str) -> GroupProfile:
    members = store.group_members(group_id)
    if members.empty:
        raise KeyError(f"Группа {group_id} не найдена")
    total = float(members["REVENUE_COMPANY"].sum())
    member_ids = set(members.index)

    top_companies = [
        {
            "company_id": cid,
            "industry": row["MAINOKVED_NAME"],
            "okved_code": row["MAINOKVED_CODE"],
            "region": row["REGION"],
            "revenue": float(row["REVENUE_COMPANY"]),
            "share": float(row["REVENUE_COMPANY"] / total),
            "client_type": row["CL_TYPE"],
        }
        for cid, row in members.nlargest(TOP_COMPANIES, "REVENUE_COMPANY").iterrows()
    ]

    divisions = _revenue_shares(members, "okved_division", total)
    regions = _revenue_shares(members, "REGION", total)

    client_types = []
    for client_type, sub in members.groupby("CL_TYPE"):
        client_types.append({
            "client_type": client_type,
            "companies": len(sub),
            "revenue_share": float(sub["REVENUE_COMPANY"].sum() / total),
        })
    client_types.sort(key=lambda x: x["revenue_share"], reverse=True)

    products, unused = [], []
    for col, name in PRODUCTS.items():
        users = members[members[col] == 1]
        if users.empty:
            unused.append(name)
        else:
            products.append({
                "product": name,
                "companies": len(users),
                "revenue_share": float(users["REVENUE_COMPANY"].sum() / total),
            })

    traders = members[members["FTS_FLAG"] == 1]
    by_country = members[["countries", "REVENUE_COMPANY"]].explode("countries").dropna(subset=["countries"])
    countries = (
        by_country.groupby("countries")["REVENUE_COMPANY"].agg(["size", "sum"])
        .sort_values(["sum", "size"], ascending=False)
        .head(TOP_ITEMS * 2)
    )

    outgoing = store.outgoing_many(member_ids)
    incoming = store.incoming_many(member_ids)
    suppliers = counterparty_side(store, outgoing, "BENEFICIARY", group_id, "supplier")
    buyers = counterparty_side(store, incoming, "PAYER", group_id, "buyer")
    intragroup = float(outgoing.loc[outgoing["BENEFICIARY"].isin(member_ids), "PAY_AMT"].sum())

    persons, outside = _person_links(store, member_ids)
    info = store.groups.loc[group_id]

    return GroupProfile(
        group_id=group_id,
        size=len(members),
        revenue=total,
        is_large=total >= LARGE_BUSINESS_REVENUE,
        rank=int(info["rank"]),
        groups_total=len(store.groups),
        top_companies=top_companies,
        divisions=[
            {"division": d, "name": division_name(d), "share": float(s)}
            for d, s in divisions.head(TOP_ITEMS).items()
        ],
        divisions_total=len(divisions),
        regions=[{"region": r, "share": float(s)} for r, s in regions.head(TOP_ITEMS).items()],
        regions_total=len(regions),
        client_types=client_types,
        products=products,
        unused_products=unused,
        foreign_trade_share=float(traders["REVENUE_COMPANY"].sum() / total),
        foreign_trade_companies=len(traders),
        countries=[
            {"country": c, "companies": int(row["size"]), "revenue_share": float(row["sum"] / total)}
            for c, row in countries.iterrows()
        ],
        suppliers=suppliers,
        buyers=buyers,
        intragroup_payments=intragroup,
        linked_persons=persons,
        linked_companies_outside=outside,
        tags=_aggregate_tags(members, total, suppliers.industry_mix + buyers.industry_mix),
        data_notes=_data_notes(store, members, suppliers, buyers),
    )
