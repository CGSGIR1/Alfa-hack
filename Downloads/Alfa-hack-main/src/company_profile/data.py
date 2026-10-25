"""Загрузка и очистка таблиц кейса: компании, связи с физлицами, платежи."""

from __future__ import annotations

import os
from functools import cached_property
from pathlib import Path

import pandas as pd

from .okved import normalize_code, okved_class

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"

FLAG_COLUMNS = [
    "NEGATIVEMARK",
    "CL_PRODUCT_RKO_MARK",
    "CL_PRODUCT_GAR_MARK",
    "CL_PRODUCT_LOA_MARK",
    "CL_PRODUCT_LEA_MARK",
    "CL_PRODUCT_DEP_MARK",
    "CL_PRODUCT_ZPP_MARK",
    "FTS_FLAG",
    "FACTORING_MARK",
]


def _csv(data_dir: Path, name: str) -> Path:
    """Файл кейса: обычный .csv или сжатый .csv.gz (pandas сам распознаёт сжатие)."""
    plain = data_dir / name
    return plain if plain.exists() or not (data_dir / f"{name}.gz").exists() else data_dir / f"{name}.gz"


def _read(path: Path, **kwargs) -> pd.DataFrame:
    return pd.read_csv(path, sep=";", encoding="utf-8-sig", **kwargs)


def parse_countries(value) -> list[str]:
    if pd.isna(value):
        return []
    return [c.strip() for c in str(value).split(",") if c.strip()]


class DataStore:
    """Все таблицы в памяти плюс производные срезы, которые нужны профилю.

    Платежи и связи грузятся лениво, чтобы простые сценарии не ждали
    чтения 1,4 млн строк.
    """

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR)
        companies = _read(
            _csv(self.data_dir, "df_Uid_data_hack.csv"),
            dtype={"MAINOKVED_CODE": str},
            low_memory=False,
        )
        companies["MAINOKVED_CODE"] = companies["MAINOKVED_CODE"].map(normalize_code)
        companies["okved_class"] = companies["MAINOKVED_CODE"].map(okved_class)
        companies["okved_division"] = companies["MAINOKVED_CODE"].str[:2]
        companies[FLAG_COLUMNS] = companies[FLAG_COLUMNS].fillna(0).astype(int)
        companies["countries"] = companies["FTS_COUNTRY_NAME"].map(parse_countries)

        # Один SYNTHETIC_U может встречаться несколько раз с разными атрибутами
        # (шум в данных). Основной считаем запись с наибольшей выручкой,
        # остальные сохраняем, чтобы показать расхождения.
        companies = companies.sort_values("REVENUE_COMPANY", ascending=False, kind="stable")
        dup_counts = companies.groupby("SYNTHETIC_U")["SYNTHETIC_U"].transform("size")
        companies["duplicate_records"] = dup_counts
        self.all_records = companies
        self.companies = companies.drop_duplicates("SYNTHETIC_U").set_index("SYNTHETIC_U", drop=False)

        c = self.companies
        c["class_rank"] = c.groupby("okved_class")["REVENUE_COMPANY"].rank(ascending=False, method="min")
        c["class_size"] = c.groupby("okved_class")["SYNTHETIC_U"].transform("size")
        c["class_share"] = c["REVENUE_COMPANY"] / c.groupby("okved_class")["REVENUE_COMPANY"].transform("sum")
        c["group_size"] = c.groupby("GROUP_ID")["SYNTHETIC_U"].transform("size")
        c["group_revenue"] = c.groupby("GROUP_ID")["REVENUE_COMPANY"].transform("sum")

    @cached_property
    def groups(self) -> pd.DataFrame:
        """Сводка по группам: число компаний, выручка и место по выручке."""
        g = self.companies.groupby("GROUP_ID").agg(
            size=("SYNTHETIC_U", "size"),
            revenue=("REVENUE_COMPANY", "sum"),
        )
        g["rank"] = g["revenue"].rank(ascending=False, method="min").astype(int)
        return g.sort_values("revenue", ascending=False)

    def records_for(self, company_id: str) -> pd.DataFrame:
        return self.all_records[self.all_records["SYNTHETIC_U"] == company_id]

    def group_members(self, group_id: str) -> pd.DataFrame:
        return self.companies[self.companies["GROUP_ID"] == group_id]

    @cached_property
    def payments(self) -> pd.DataFrame:
        return _read(_csv(self.data_dir, "df_Uid_payment_hack.csv"))

    @cached_property
    def payments_out(self) -> pd.DataFrame:
        """Платежи без переводов самому себе, индекс — плательщик."""
        p = self.payments
        return p[p["PAYER"] != p["BENEFICIARY"]].set_index("PAYER").sort_index()

    @cached_property
    def payments_in(self) -> pd.DataFrame:
        p = self.payments
        return p[p["PAYER"] != p["BENEFICIARY"]].set_index("BENEFICIARY").sort_index()

    @cached_property
    def self_transfers(self) -> pd.Series:
        """Суммы платежей, где плательщик и получатель совпадают (вероятно, между своими счетами)."""
        p = self.payments
        return p[p["PAYER"] == p["BENEFICIARY"]].groupby("PAYER")["PAY_AMT"].sum()

    @cached_property
    def companies_with_payments(self) -> set[str]:
        return set(self.payments_out.index) | set(self.payments_in.index)

    @cached_property
    def person_links(self) -> pd.DataFrame:
        """Связи физлиц с компаниями. Файл необязателен: без него связей просто нет."""
        path = _csv(self.data_dir, "df_Fid_Uid_link_hack.csv")
        if not path.exists():
            return pd.DataFrame(columns=["SYNTHETIC_F", "SYNTHETIC_U"])
        return _read(path)

    def outgoing(self, company_id: str) -> pd.DataFrame:
        return _lookup(self.payments_out, company_id, "BENEFICIARY")

    def incoming(self, company_id: str) -> pd.DataFrame:
        return _lookup(self.payments_in, company_id, "PAYER")

    def outgoing_many(self, company_ids) -> pd.DataFrame:
        """Исходящие платежи нескольких компаний, суммированные по получателю."""
        return _aggregate(self.payments_out, company_ids, "BENEFICIARY")

    def incoming_many(self, company_ids) -> pd.DataFrame:
        return _aggregate(self.payments_in, company_ids, "PAYER")


def _aggregate(indexed: pd.DataFrame, company_ids, other_col: str) -> pd.DataFrame:
    rows = indexed[indexed.index.isin(company_ids)]
    return (
        rows.groupby(other_col, as_index=False)[["PAY_AMT", "PAY_CNT"]].sum()
        .sort_values("PAY_AMT", ascending=False, kind="stable")
        .reset_index(drop=True)
    )


def _lookup(indexed: pd.DataFrame, company_id: str, other_col: str) -> pd.DataFrame:
    if company_id not in indexed.index:
        return pd.DataFrame(columns=[other_col, "PAY_AMT", "PAY_CNT"])
    rows = indexed.loc[[company_id]].reset_index(drop=True)
    return rows.sort_values("PAY_AMT", ascending=False, kind="stable")
