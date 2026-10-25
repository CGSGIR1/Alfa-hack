"""Пользователи пайплайна поверх классификатора крупного бизнеса (src/big_business).

Данные синтетические и живут только в этих тестах (во временной папке).
Запуск из корня проекта: python -m unittest src.pipeline.tests.test_users -v
"""

import csv
import tempfile
import unittest
from pathlib import Path

from src.company_profile.data import DataStore
from src.pipeline import users

COLS = ["SYNTHETIC_U", "ORG_TYPE", "OKFS_NAME", "COMPANY_AGE_YEARS_GRADATION", "ECO_MARK_LABEL", "MAINOKVED_CODE",
        "MAINOKVED_NAME", "NEGATIVEMARK", "AMOUNT_WORKERS_FNS_GRADATION", "AMOUNT_WORKERS_SPARK_GRADATION", "CL_TYPE",
        "CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK", "CL_PRODUCT_LEA_MARK",
        "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK", "FTS_FLAG", "FTS_COUNTRY_NAME", "FACTORING_MARK",
        "REVENUE_COMPANY", "GROUP_ID", "CITY", "REGION"]
B = 1e9


def company(uid, revenue, group, own="ЧАСТНАЯ СОБСТВЕННОСТЬ", workers="101-250"):
    row = dict.fromkeys(COLS, 0)
    row.update(SYNTHETIC_U=uid, MAINOKVED_CODE="62.01", MAINOKVED_NAME="ПО", REVENUE_COMPANY=revenue, GROUP_ID=group,
               CITY="г. Москва", REGION="Москва", CL_TYPE="CLOSE_CLIENT", FTS_COUNTRY_NAME="", OKFS_NAME=own,
               AMOUNT_WORKERS_FNS_GRADATION=workers)
    return row


ROWS = [
    company("U_BIG", 20 * B, "group_solo"),                                          # BIG
    company("U_STATE", 30 * B, "group_state", own="ФЕДЕРАЛЬНАЯ СОБСТВЕННОСТЬ"),      # BIG_REVIEW — пускаем
    company("U_G1", 6 * B, "group_via"), company("U_G2", 6 * B, "group_via"), company("U_G3", 5 * B, "group_via"),   # группа 17 млрд
    company("U_TINY", 0.5 * B, "group_via"),                                         # член группы, но 0,5 млрд < 2 млрд
    company("U_EDGE", 15 * B, "group_edge"),                                         # ровно 15 — не «свыше»
    company("U_SMALL", 3 * B, "group_small"), company("U_SMALL2", 9 * B, "group_small"), # группа 12 млрд
    company("U_428697", 100 * B, "group_via"), company("U_428697", 100 * B, "group_other"),   # заглушка
]


class UsersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        data = Path(cls.tmp.name)
        with (data / "df_Uid_data_hack.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, COLS, delimiter=";")
            w.writeheader()
            w.writerows(ROWS)
        with (data / "df_Uid_payment_hack.csv").open("w", encoding="utf-8-sig", newline="") as f:
            f.write("BENEFICIARY;PAYER;PAY_AMT;PAY_CNT\nU_BIG;U_G1;1000.0;3\n")
        cls.ds = DataStore(data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.db = Path(self.tmp.name) / "app.db"
        if self.db.exists():
            self.db.unlink()
        users.sync(self.ds, self.db)

    def cat(self, cid):
        return users.describe(cid, self.db)["category"]

    def test_categories(self):
        self.assertEqual(self.cat("U_BIG"), "BIG")
        self.assertEqual(self.cat("U_STATE"), "BIG_REVIEW")
        self.assertEqual(self.cat("U_G1"), "BIG_VIA_GROUP")
        self.assertEqual(self.cat("U_TINY"), "NOT_BIG")          # группа крупная, но своя выручка меньше 2 млрд
        self.assertEqual(self.cat("U_EDGE"), "BORDERLINE")       # «свыше» строго
        self.assertEqual(self.cat("U_SMALL2"), "NOT_BIG")        # 9 млрд: ниже диапазона 10–15, пользователем не станет
        self.assertEqual(self.cat("U_428697"), "INVALID_DATA")

    def test_users_are_exactly_the_configured_categories(self):
        self.assertEqual(users.user_ids(self.db), ["U_BIG", "U_G1", "U_G2", "U_G3", "U_STATE"])

    def test_register_rejects_everyone_else_with_reason(self):
        for cid in ("U_TINY", "U_EDGE", "U_SMALL", "U_428697"):
            with self.assertRaises(users.NotEligible, msg=cid):
                users.register(cid, ds=self.ds, db=self.db)
        with self.assertRaises(users.NotEligible) as e:
            users.register("U_NOBODY", ds=self.ds, db=self.db)
        self.assertIn("нет в данных", str(e.exception))
        self.assertEqual(users.register("U_BIG", ds=self.ds, db=self.db), "exists")

    def test_register_on_empty_database_loads_clients_first(self):
        self.db.unlink()
        self.assertEqual(users.register("U_G1", ds=self.ds, db=self.db), "added")   # группа считается по всем клиентам
        with self.assertRaises(users.NotEligible):
            users.register("U_TINY", ds=self.ds, db=self.db)

    def test_group_subject_needs_a_big_member(self):
        users.require("group_via", db=self.db)                      # у g_via есть клиенты «наших» категорий
        with self.assertRaises(users.NotEligible):
            users.require("group_small", db=self.db)

    def test_new_client_flips_group_members(self):
        # группа g_new: 6 + 4 + 1 = 11 млрд — пока ниже порога
        for r in (company("N_1", 6 * B, "group_new"), company("N_2", 4 * B, "group_new"), company("N_3", 1 * B, "group_new")):
            users.add_client(r, self.db)
        self.assertNotIn("N_1", users.user_ids(self.db))
        res = users.add_client(company("N_4", 5 * B, "group_new"), self.db)               # группа стала 16 млрд
        self.assertEqual(res["category"], "BIG_VIA_GROUP")
        self.assertEqual(self.cat("N_1"), "BIG_VIA_GROUP")                              # старые участники пересчитаны
        self.assertEqual(self.cat("N_3"), "NOT_BIG")                                    # 1 млрд < 2 млрд
        self.assertTrue(users.is_user("N_2", self.db))

    def test_new_small_client_is_stored_but_not_a_user(self):
        res = users.add_client(company("N_9", 1 * B, "group_none"), self.db)
        self.assertEqual(res["category"], "NOT_BIG")
        self.assertFalse(users.is_user("N_9", self.db))
        with self.assertRaises(users.NotEligible):
            users.register("N_9", ds=self.ds, db=self.db)

    def test_site_uses_the_same_rules(self):
        self.assertEqual(sorted(users.eligible_table(self.ds).index), ["U_BIG", "U_G1", "U_G2", "U_G3", "U_STATE"])


if __name__ == "__main__":
    unittest.main()
