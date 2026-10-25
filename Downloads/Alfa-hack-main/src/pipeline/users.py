"""Пользователи пайплайна: клиенты, которых классификатор крупного бизнеса относит к «нашим» категориям.

Пользователю строится персональная новостная лента и отчёты. Остальным отчёты не формируются совсем
(ни настоящие, ни «заглушки») и переключения на них на сайте нет.

Правила — в src/big_business (код и отчёт команды), выбор категорий — config.CLASSIFIER.
Пакетная разметка, разметка одного нового клиента и сборка сайта используют одну и ту же функцию `decide`,
поэтому результат для компании не зависит от пути.

    python -m src.pipeline users sync          # загрузить клиентов из CSV кейса и разметить всех
    python -m src.pipeline users add U_10529   # проверить и занести (не прошедшие проверку отклоняются)
    python -m src.pipeline users show U_76     # категория и причина
    python -m src.pipeline users list
"""

from __future__ import annotations

from pathlib import Path

from ..big_business import CategoryService, classify_frame
from . import store
from .config import APP_DB, CLASSIFIER

CLIENT_COLUMNS = CLASSIFIER.cols.all()


class NotEligible(LookupError):
    """Клиент не в «наших» категориях крупного бизнеса: отчёты для него не формируются."""


def _service(con) -> CategoryService:
    return CategoryService(con, cfg=CLASSIFIER, placeholder="?")


def _records(ds):
    """Строки клиентов из DataStore: с дублями, как в CSV (дубли и заглушки классификатор помечает сам)."""
    frame = ds.all_records[CLIENT_COLUMNS]
    return frame.astype(object).where(frame.notna(), None)


# ---------- разметка ----------

def labels(ds):
    """Пакетная разметка без базы (для сборки сайта): таблица как в clients_labeled.csv."""
    return classify_frame(ds.all_records[CLIENT_COLUMNS].reset_index(drop=True), CLASSIFIER)


def eligible_table(ds):
    """Компании «наших» категорий: строки ds.companies + category, group_revenue, group_size."""
    lab = labels(ds)
    lab = lab[lab["in_category"] == 1].drop_duplicates(CLASSIFIER.cols.id).set_index(CLASSIFIER.cols.id)
    table = ds.companies.loc[ds.companies.index.intersection(lab.index)].copy()
    for col in ("category", "group_revenue", "group_size", "reasons"):
        table[col] = lab[col].reindex(table.index)
    return table


def sync(ds, db: Path = APP_DB) -> dict:
    """Загрузить клиентов из CSV в app.db и разметить всех одним проходом."""
    with store.connect(db) as con:
        con.execute("delete from clients")
        marks = ", ".join("?" * len(CLIENT_COLUMNS))
        con.executemany(f"insert into clients ({', '.join(CLIENT_COLUMNS)}) values ({marks})",
                        _records(ds).itertuples(index=False, name=None))
        con.execute("delete from client_category")
        _service(con).backfill()
    return summary(db)


def summary(db: Path = APP_DB) -> dict:
    with store.connect(db) as con:
        by = {r["category"]: r["n"] for r in con.execute("select category, count(*) n from client_category group by 1")}
        total = con.execute("select count(*) from client_category where in_category = 1").fetchone()[0]
    return {"users": total, "by_category": by}


# ---------- пользователи ----------

def user_ids(db: Path = APP_DB) -> list[str]:
    with store.connect(db) as con:
        return [r["company_id"] for r in con.execute(
            "select company_id from client_category where in_category = 1 order by company_id")]


def is_user(company_id: str, db: Path = APP_DB) -> bool:
    with store.connect(db) as con:
        row = _service(con).get(company_id)
    return bool(row and row["in_category"])


def describe(company_id: str, db: Path = APP_DB) -> dict | None:
    with store.connect(db) as con:
        return _service(con).get(company_id)


def add_client(record: dict, db: Path = APP_DB) -> dict:
    """Занесение клиента в базу: сразу проверка признака и пересчёт его группы (метки участников обновляются).

    Возвращает результат классификатора. Клиент не из «наших» категорий в базе остаётся, но пользователем не станет.
    """
    cols = CLASSIFIER.cols
    row = {c: record.get(c) for c in CLIENT_COLUMNS}
    with store.connect(db) as con:
        old = con.execute(f"select {cols.group} from clients where {cols.id} = ?", (row[cols.id],)).fetchone()
        con.execute(f"delete from clients where {cols.id} = ?", (row[cols.id],))
        marks = ", ".join("?" * len(CLIENT_COLUMNS))
        con.execute(f"insert into clients ({', '.join(CLIENT_COLUMNS)}) values ({marks})", [row[c] for c in CLIENT_COLUMNS])
        return _service(con).classify_company(row[cols.id], old_group_id=old[0] if old else None)


def register(company_id: str, ds=None, db: Path = APP_DB) -> str:
    """Проверить компанию и, если она «наша», считать пользователем. 'added' | 'exists'; иначе NotEligible."""
    with store.connect(db) as con:
        known = con.execute("select count(*) from clients").fetchone()[0] > 0
        row = _service(con).get(company_id) if known else None
    if row is not None and row["in_category"]:
        return "exists"
    if row is None:
        if ds is None:
            from .companies import data_store   # CSV читаем только когда без них не обойтись
            ds = data_store()
        if not known:
            sync(ds, db)       # группы считаются по всем клиентам, поэтому единичную проверку делаем после полной загрузки
        row = describe(company_id, db)
        if row is None:
            raise NotEligible(f"{company_id}: нет в данных клиентов")
    if not row["in_category"]:
        raise NotEligible(f"{company_id}: {row['category']} — {row['reasons']}")
    return "added"


def require(company_id: str, db: Path = APP_DB) -> None:
    """Вход для генерации отчётов. Для group_… достаточно одного участника «нашей» категории."""
    if company_id.startswith("group_"):
        with store.connect(db) as con:
            ok = con.execute(
                "select 1 from clients c join client_category k on k.company_id = c.SYNTHETIC_U "
                "where c.GROUP_ID = ? and k.in_category = 1 limit 1", (company_id,)).fetchone()
        if not ok:
            raise NotEligible(f"{company_id}: в группе нет клиентов крупного бизнеса")
        return
    register(company_id, db=db)
