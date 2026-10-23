"""Подготовка новостей для семантического поиска (схема news-two-views-1.0).

    from src.news_prep import make_request, prepare_news

    request = make_request("id", "Заголовок", "2026-10-21", url, "Издание", paragraphs)
    document, report = prepare_news(request)
"""

from .pipeline import make_request, prepare_news

__all__ = ["make_request", "prepare_news"]
