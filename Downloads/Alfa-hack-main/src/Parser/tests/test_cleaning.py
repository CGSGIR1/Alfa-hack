r"""
Тесты очистки на реальных новостях из news.db (длинные тексты сокращены).

Запуск из папки Hackaton:
  .venv/bin/python -m unittest discover -s Parser/tests -v        # macOS, Linux
  .venv\Scripts\python -m unittest discover -s Parser\tests -v    # Windows
"""

import os
import sys
import unittest

# модули парсера лежат на уровень выше папки tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cleaning import (
    EMBED_MAX_CHARS, build_embed_text, clean_body, content_hash, detect_paywall,
    find_boilerplate, guess_deck, pick_lead, split_title, to_utc_iso,
)
from parser import choose_text

# Коммерсант, статья из печатного номера: заголовок и подзаголовок склеены через « // »,
# в тексте они идут первыми строками, а в конце — реквизиты номера.
KOMMERSANT_TITLE = ("Смешать, но не взбалтывать // Металлурги просят готовить специалистов "
                    "отрасли по отдельной группе учебных программ")
KOMMERSANT_TEXT = "\n".join([
    "Смешать, но не взбалтывать",
    "Металлурги просят готовить специалистов отрасли по отдельной группе учебных программ",
    "Ассоциация «Русская сталь» не согласна с готовящимся Минобрнауки в рамках реформы высшего "
    "образования вариантом укрупнения группировки специальностей, востребованных "
    "металлургической отраслью.",
    "«Русская сталь» в письме предлагает выделить учебные программы для металлургов "
    "в самостоятельную УГС «Металлургия и технологии материалов».",
    "В Минобрнауки не ответили на запрос “Ъ”.",
    "Газета «Коммерсантъ» №193/П",
    "от 19.10.2026, стр. 2",
])

# Ведомости, статья за пейволлом: вторая строка — врезка (она же description в RSS),
# последняя — «Вы видите 10% этого материала».
PAYWALL_TITLE = "Россияне оказались готовы к ломбардам"
PAYWALL_DECK = "Большинство граждан допускают возможность обратиться туда вместо банка или МФО"
PAYWALL_TEXT = "\n".join([
    PAYWALL_TITLE,
    PAYWALL_DECK,
    "Большинство граждан в некоторых случаях не прочь обратиться в ломбард вместо банка или "
    "микрофинансовой организации (МФО). Об этом заявили 68% участников опроса финансового "
    "маркетплейса «Выберу.ру», с результатами которого ознакомились «Ведомости». При этом каждый "
    "третий участник ответил, что стал относиться к ломбардам лучше и теперь воспринимает их как "
    "обычный финансовый институт.",
    "Исследование проводилось в октябре 2026 г. в формате массового количественного опроса. "
    "В нем поучаствовало 3014 человек в возрасте от 18 до 56 лет со всей России, в основном "
    "экономически активные жители крупных городов.",
    "Вы видите 10% этого материала",
])

# Ведомости, обычная новость: первая строка текста повторяет заголовок,
# внутри — атрибуция «(цитата по «РИА Новости»)», которую трогать нельзя.
MROT_TITLE = "Котяков: МРОТ в России составит 28 935 рублей в 2027 году"
MROT_LEAD = ("Минимальный размер оплаты труда в России составит 28 935 руб. в следующем году, "
             "объявил министр труда и социальной защиты РФ Антон Котяков на заседании комитета "
             "по бюджету и налогам в Госдуме.")
MROT_TEXT = "\n".join([
    MROT_TITLE,
    MROT_LEAD,
    "«В 2027 г. предусмотрено увеличение МРОТа до 28 935 руб.», – сказал он "
    "(цитата по «РИА Новости»).",
    "МРОТ в 2026 г. составляет 27 093 руб в месяц.",
])

# Две новости Ведомостей о курсе доллара с одним и тем же справочным абзацем в конце.
USD_SHARED = ("Опрошенные Центробанком аналитики прогнозируют, что доллар в октябре – декабре "
              "будет стоить 85,3 руб. В 2027 г. курс составит 89,4 руб., в 2028 г. – 94,8 руб., "
              "в 2029 г. – 96,4 руб.")
USD1_TITLE = "Банк России понизил официальный курс доллара до 83,62 рубля"
USD1_TEXT = "\n".join([
    USD1_TITLE,
    "Банк России установил официальный курс доллара на 17 октября на отметке 83,62 руб. "
    "Регулятор снизил его на 0,14 руб.",
    "Евро подешевел незначительно – всего на 0,05 руб. Валюта стоит 93,72 руб. Курс юаня "
    "снизился на тысячные доли, поэтому остался на прежнем уровне – 12,5 руб.",
    USD_SHARED,
])
USD2_TITLE = "Официальный курс доллара установлен ниже 83 рублей впервые с 22 августа"
USD2_TEXT = "\n".join([
    USD2_TITLE,
    "Банк России установил официальный курс доллара на 20 октября на отметке 82,85 руб. "
    "Регулятор снизил его на 0,77 руб.",
    "Последний раз доллар стоил меньше 83 руб. 22 августа, тогда официальный курс составлял "
    "82,92 руб.",
    USD_SHARED,
])


class KommersantTitleTest(unittest.TestCase):
    def test_split_title(self):
        title, subtitle = split_title(KOMMERSANT_TITLE)
        self.assertEqual(title, "Смешать, но не взбалтывать")
        self.assertEqual(subtitle, "Металлурги просят готовить специалистов отрасли "
                                   "по отдельной группе учебных программ")
        self.assertEqual(split_title(MROT_TITLE), (MROT_TITLE, None))

    def test_body_without_header_and_print_details(self):
        title, subtitle = split_title(KOMMERSANT_TITLE)
        body = clean_body(KOMMERSANT_TEXT, title, subtitle, KOMMERSANT_TITLE)
        self.assertTrue(body.startswith("Ассоциация «Русская сталь» не согласна"))
        self.assertNotIn("Газета «Коммерсантъ»", body)
        self.assertNotIn("стр. 2", body)
        self.assertTrue(body.endswith("В Минобрнауки не ответили на запрос “Ъ”."))
        self.assertTrue(pick_lead(body).startswith("Ассоциация «Русская сталь» не согласна"))

    def test_embed_text_limit(self):
        title, subtitle = split_title(KOMMERSANT_TITLE)
        body = clean_body(KOMMERSANT_TEXT, title, subtitle, KOMMERSANT_TITLE)
        embed = build_embed_text(title, subtitle, body, limit=300)
        self.assertLessEqual(len(embed), 300)
        self.assertTrue(embed.startswith(f"{title}\n{subtitle}\nАссоциация «Русская сталь»"))


class VedomostiPaywallTest(unittest.TestCase):
    def test_paywall_detected(self):
        self.assertEqual(detect_paywall(PAYWALL_TEXT), 10)
        self.assertIsNone(detect_paywall(MROT_TEXT))

    def test_deck_and_body(self):
        deck = guess_deck(PAYWALL_DECK, PAYWALL_TEXT, PAYWALL_TITLE)
        self.assertEqual(deck, PAYWALL_DECK)
        body = clean_body(PAYWALL_TEXT, PAYWALL_TITLE, deck)
        self.assertTrue(body.startswith("Большинство граждан в некоторых случаях"))
        self.assertNotIn(PAYWALL_DECK, body)
        self.assertNotIn("Вы видите", body)

    def test_not_full(self):
        _, _, is_full, error = choose_text(PAYWALL_TEXT, PAYWALL_DECK, None)
        self.assertEqual(is_full, 0)
        self.assertEqual(error, "пейволл: видно 10% материала")


class TitleRepeatTest(unittest.TestCase):
    def test_title_removed_attribution_kept(self):
        body = clean_body(MROT_TEXT, MROT_TITLE)
        self.assertFalse(body.startswith(MROT_TITLE))
        self.assertIn("(цитата по «РИА Новости»)", body)
        self.assertEqual(pick_lead(body), MROT_LEAD)
        # первый абзац кончается точкой — это не врезка
        self.assertIsNone(guess_deck(MROT_LEAD, MROT_TEXT, MROT_TITLE))

    def test_interview_title_without_name(self):
        title = "Сергей Черногаев: «Работникам выгодно объединяться»"
        deck = "Председатель ФНПР – о борьбе с теневой занятостью, четырехдневке и рисках использования ИИ в работе"
        intro = ("Российский рынок труда переживает масштабные изменения, которые требуют пересмотра "
                 "привычных подходов к регулированию занятости и социальной защиты.")
        text = "\n".join(["«Работникам выгодно объединяться»", deck, intro])
        self.assertEqual(guess_deck(deck, text, title), deck)
        self.assertEqual(clean_body(text, title, deck), intro)


class BoilerplateTest(unittest.TestCase):
    ARTICLES = [
        {"id": "usd1", "outlet": "vedomosti", "title": USD1_TITLE, "text": USD1_TEXT},
        {"id": "usd2", "outlet": "vedomosti", "title": USD2_TITLE, "text": USD2_TEXT},
        {"id": "mrot", "outlet": "vedomosti", "title": MROT_TITLE, "text": MROT_TEXT},
    ]

    def test_shared_paragraph_found_and_removed(self):
        found = find_boilerplate(self.ARTICLES)
        self.assertEqual(found, {"vedomosti": {USD_SHARED: 2}})
        body = clean_body(USD1_TEXT, USD1_TITLE, boilerplate=found["vedomosti"])
        self.assertNotIn("Опрошенные Центробанком аналитики", body)
        self.assertIn("Евро подешевел незначительно", body)

    def test_embed_text(self):
        body = clean_body(USD1_TEXT, USD1_TITLE, boilerplate=find_boilerplate(self.ARTICLES)["vedomosti"])
        embed = build_embed_text(USD1_TITLE, None, body)
        self.assertLessEqual(len(embed), EMBED_MAX_CHARS)
        self.assertTrue(embed.startswith(f"{USD1_TITLE}\nБанк России установил официальный курс"))
        self.assertEqual(len(content_hash(embed)), 64)


class DateTest(unittest.TestCase):
    def test_to_utc_iso(self):
        self.assertEqual(to_utc_iso("Fri, 16 Oct 2026 00:32:41 +0300"), "2026-10-15T21:32:41+00:00")
        self.assertIsNone(to_utc_iso(""))

    def test_sorting(self):
        raw = ["Fri, 16 Oct 2026 00:32:41 +0300", "Mon, 19 Oct 2026 10:21:42 +0300",
               "Thu, 15 Oct 2026 18:10:31 +0300"]
        self.assertEqual(min(raw), raw[0])  # по алфавиту «Fri» раньше «Thu» — это и была ошибка
        self.assertEqual(min(map(to_utc_iso, raw)), "2026-10-15T15:10:31+00:00")


if __name__ == "__main__":
    unittest.main()
