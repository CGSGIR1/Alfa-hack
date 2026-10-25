"""Определение «крупного бизнеса»: правила, пакетная разметка и слой для базы.

Код и отчёт: https://claude.ai/artifact/Eh1uD5aiWD2htBUeoSTYRC. Файлы лежат как написаны, кроме импортов (теперь пакетные).
Какие категории считаются «нашими» (пользователи, переключатель, отчёты), решает src/pipeline/config.py.
"""

from .big_business_classifier import (BIG, BIG_REVIEW, BIG_VIA_GROUP, BORDERLINE, EXCLUDED, INVALID_DATA, NOT_BIG,
                                      Config, GroupInfo, classify_frame, classify_record)
from .db_hook import CategoryService

__all__ = ["BIG", "BIG_REVIEW", "BIG_VIA_GROUP", "BORDERLINE", "EXCLUDED", "INVALID_DATA", "NOT_BIG", "Config",
           "GroupInfo", "CategoryService", "classify_frame", "classify_record"]
