"""Настройки пайплайна. Пути и сроки в одном месте, чтобы их было видно и легко менять."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
APP_DB = DATA / "app.db"                      # компании, новости, показатели, отчёты
PARSER_DB = ROOT / "src" / "parser" / "news.db"
REPORTS_DIR = DATA / "reports"                # готовые отчёты для фронтенда: <company_id>.json
ANALYTICS_STUB = DATA / "analytics_stub.json" # временный корпус аналитики до модуля аналитики

# Хранение новостей: важные дольше, незначительные меньше (дней от публикации).
RETENTION_DAYS = {"high": 30, "normal": 10, "low": 3}

# Отбор материалов для пакета (как в report-generation-input-1.0).
RETRIEVAL = {
    "news_days": 10,
    "top_news": 3,
    "news_fragments": 2,
    "analytics_per_news": 5,
    "max_chunks_per_analytics": 2,
    "max_analytics_age_days": 365,
    # Пороги для заглушки эмбеддингов (символьные n-граммы); для настоящих эмбеддингов подобрать заново.
    "min_news_score": 0.12,
    "min_analytics_score": 0.10,
}

# Обновление отчёта: раз в сутки, либо раньше — если в топ-N фрагментов пришёл новый.
REPORT_MAX_AGE_HOURS = 24
TRIGGER_TOP_N = 5
