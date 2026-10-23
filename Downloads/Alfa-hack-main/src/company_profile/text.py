"""Форматирование чисел и склонения для русских текстов."""

from __future__ import annotations


def money(value: float) -> str:
    for threshold, unit in ((1e12, "трлн"), (1e9, "млрд"), (1e6, "млн"), (1e3, "тыс.")):
        if abs(value) >= threshold:
            number = value / threshold
            text = f"{number:.1f}" if number < 100 else f"{number:.0f}"
            return f"{text.replace('.', ',').removesuffix(',0')} {unit} ₽"
    return f"{value:.0f} ₽"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def percent(share: float) -> str:
    return f"{share:.0%}" if share >= 0.01 else "менее 1%"
