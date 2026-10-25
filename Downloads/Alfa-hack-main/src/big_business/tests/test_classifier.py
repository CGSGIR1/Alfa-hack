"""Тесты. Запуск из корня проекта: python -m src.big_business.tests.test_classifier  (или pytest)."""
import sqlite3

import pandas as pd

from src.big_business.big_business_classifier import (BIG, BIG_REVIEW, BIG_VIA_GROUP, BORDERLINE, EXCLUDED, INVALID_DATA,
                                                     NOT_BIG, Config, GroupInfo, classify_frame, classify_record)
from src.big_business.db_hook import CategoryService

BLN = 1e9


def rec(cid="U_1", rev=20 * BLN, group="g1", workers="101-250", own="ЧАСТНАЯ СОБСТВЕННОСТЬ", okved="62.01"):
    return {"SYNTHETIC_U": cid, "REVENUE_COMPANY": rev, "GROUP_ID": group,
            "AMOUNT_WORKERS_FNS_GRADATION": workers, "OKFS_NAME": own, "MAINOKVED_CODE": okved}


def test_big_plain():
    r = classify_record(rec())
    assert r["category"] == BIG and r["in_category"] == 1 and r["flags"] == ""


def test_threshold_is_strict():
    assert classify_record(rec(rev=15 * BLN))["category"] == BORDERLINE      # ровно 15 млрд — не «свыше»
    assert classify_record(rec(rev=15 * BLN + 1))["category"] == BIG


def test_state_and_low_staff_need_review():
    r = classify_record(rec(rev=62 * BLN, workers="Менее 15", own="ФЕДЕРАЛЬНАЯ СОБСТВЕННОСТЬ", okved="46.90"))
    assert r["category"] == BIG_REVIEW and r["in_category"] == 1
    assert set(r["flags"].split(";")) == {"low_staff", "state_owned", "possible_intermediary"}


def test_foreign_is_info_only():
    r = classify_record(rec(own="СОБСТВЕННОСТЬ ИНОСТРАННЫХ ЮРИДИЧЕСКИХ ЛИЦ"))
    assert r["category"] == BIG and "foreign_owned" in r["flags"]


def test_exclude_flags_config():
    cfg = Config(exclude_flags=frozenset({"state_owned"}))
    r = classify_record(rec(own="МУНИЦИПАЛЬНАЯ СОБСТВЕННОСТЬ"), cfg)
    assert r["category"] == EXCLUDED and r["in_category"] == 0


def test_borderline_and_not_big():
    assert classify_record(rec(rev=12 * BLN))["category"] == BORDERLINE
    assert classify_record(rec(rev=5 * BLN))["category"] == NOT_BIG
    assert classify_record(rec(rev=5 * BLN))["group_checked"] == 0


def test_via_group():
    g = GroupInfo(size=3, revenue_sum=20 * BLN)
    assert classify_record(rec(rev=5 * BLN), group=g)["category"] == BIG_VIA_GROUP
    assert classify_record(rec(rev=1 * BLN), group=g)["category"] == NOT_BIG        # меньше group_member_min_revenue
    assert classify_record(rec(rev=5 * BLN), group=GroupInfo(1, 20 * BLN))["category"] == NOT_BIG  # группа из одного


def test_invalid():
    assert classify_record(rec(cid="U_428697"))["category"] == INVALID_DATA
    assert classify_record(rec(rev=-5))["category"] == INVALID_DATA
    assert classify_record(rec(rev=None))["category"] == INVALID_DATA
    assert classify_record(rec(rev="abc"))["category"] == INVALID_DATA
    assert classify_record(rec(cid=None))["category"] == INVALID_DATA


def test_frame_duplicates_and_placeholder_do_not_feed_group_sums():
    df = pd.DataFrame([
        rec("U_1", 6 * BLN, "g"), rec("U_2", 6 * BLN, "g"),
        rec("U_428697", 100 * BLN, "g"),                       # заглушка не должна «надувать» группу
        rec("U_3", 9 * BLN, "h"), rec("U_3", 9 * BLN, "h"),    # дубль ID
    ])
    res = classify_frame(df)
    assert list(res.category) == [NOT_BIG, NOT_BIG, INVALID_DATA, INVALID_DATA, INVALID_DATA]
    assert res.group_revenue.iloc[0] == 12 * BLN


def test_batch_equals_single():
    df = pd.DataFrame([rec(f"U_{i}", r, f"g{i % 3}", w, o) for i, (r, w, o) in enumerate([
        (20 * BLN, "Менее 15", "ЧАСТНАЯ СОБСТВЕННОСТЬ"), (12 * BLN, "16-100", "ЧАСТНАЯ СОБСТВЕННОСТЬ"),
        (3 * BLN, "101-250", "ФЕДЕРАЛЬНАЯ СОБСТВЕННОСТЬ"), (9 * BLN, "Более 251", "ЧАСТНАЯ СОБСТВЕННОСТЬ"),
        (16 * BLN, "Более 251", "ЧАСТНАЯ СОБСТВЕННОСТЬ"), (1 * BLN, "Менее 15", "ЧАСТНАЯ СОБСТВЕННОСТЬ")])])
    batch = classify_frame(df)
    for i, row in df.iterrows():
        members = df[df.GROUP_ID == row.GROUP_ID]
        single = classify_record(row.to_dict(), group=GroupInfo(len(members), float(members.REVENUE_COMPANY.sum())))
        assert single["category"] == batch.category.iloc[i], i


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE clients (SYNTHETIC_U TEXT PRIMARY KEY, REVENUE_COMPANY REAL, GROUP_ID TEXT,
                    AMOUNT_WORKERS_FNS_GRADATION TEXT, OKFS_NAME TEXT, MAINOKVED_CODE TEXT)""")
    svc = CategoryService(conn)
    svc.ensure_schema()
    return conn, svc


def _insert(conn, r):
    conn.execute("INSERT INTO clients VALUES (?,?,?,?,?,?)", tuple(r.values()))
    conn.commit()


def test_db_new_client_flips_group_members():
    conn, svc = _db()
    for r in (rec("U_1", 6 * BLN, "g"), rec("U_2", 4 * BLN, "g"), rec("U_3", 1 * BLN, "g")):
        _insert(conn, r)
    svc.backfill()
    assert svc.get("U_1")["category"] == NOT_BIG                     # группа 11 млрд ещё ниже порога

    _insert(conn, rec("U_4", 5 * BLN, "g"))                         # группа стала 16 млрд
    res = svc.classify_company("U_4")
    assert res["category"] == BIG_VIA_GROUP
    assert svc.get("U_1")["category"] == BIG_VIA_GROUP               # старый участник пересчитан
    assert svc.get("U_2")["category"] == BIG_VIA_GROUP
    assert svc.get("U_3")["category"] == NOT_BIG                     # выручка 1 млрд < 2 млрд


def test_db_new_big_client_and_pending():
    conn, svc = _db()
    _insert(conn, rec("U_1", 20 * BLN, "a"))
    _insert(conn, rec("U_2", 1 * BLN, "b"))
    assert svc.classify_pending() == 2
    assert svc.classify_pending() == 0                               # идемпотентно
    _insert(conn, rec("U_3", 30 * BLN, "c", own="ФЕДЕРАЛЬНАЯ СОБСТВЕННОСТЬ"))
    assert svc.classify_pending() == 1
    assert svc.get("U_3")["category"] == BIG_REVIEW and svc.get("U_3")["in_category"] == 1
    assert svc.get("U_2")["in_category"] == 0


def test_db_group_change_and_no_group():
    conn, svc = _db()
    _insert(conn, rec("U_1", 11 * BLN, "g1"))
    _insert(conn, rec("U_2", 8 * BLN, "g1"))
    svc.backfill()
    assert svc.get("U_1")["category"] == BIG_VIA_GROUP               # группа 19 млрд
    conn.execute("UPDATE clients SET GROUP_ID = 'g2' WHERE SYNTHETIC_U = 'U_2'")
    conn.commit()
    svc.classify_company("U_2", old_group_id="g1")
    assert svc.get("U_1")["category"] == BORDERLINE                  # осталась одна в группе, 11 млрд
    _insert(conn, rec("U_5", 3 * BLN, None))
    assert svc.classify_company("U_5")["category"] == NOT_BIG


def test_db_equals_batch_on_random_sample():
    import random
    random.seed(7)
    conn, svc = _db()
    rows = []
    for i in range(600):
        rows.append(rec(f"U_{i}", random.choice([5e8, 3e9, 9e9, 11e9, 16e9, 40e9]) * random.random() + 1e6,
                        f"g{random.randint(0, 80)}",
                        random.choice(["Менее 15", "16-100", "101-250", "Более 251"]),
                        random.choice(["ЧАСТНАЯ СОБСТВЕННОСТЬ", "ФЕДЕРАЛЬНАЯ СОБСТВЕННОСТЬ",
                                       "СОБСТВЕННОСТЬ ИНОСТРАННЫХ ЮРИДИЧЕСКИХ ЛИЦ"]),
                        random.choice(["46.51", "62.01", "41.20"])))
    for r in rows:
        _insert(conn, r)
    svc.backfill()
    batch = classify_frame(pd.DataFrame(rows)).set_index("SYNTHETIC_U").category
    for cid in ("U_0", "U_17", "U_250", "U_599"):
        svc.classify_company(cid)                                    # точечный путь
    cur = conn.execute("SELECT company_id, category FROM client_category")
    db = dict(cur.fetchall())
    assert db == batch.to_dict()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"\nвсе {len(tests)} тестов прошли")
