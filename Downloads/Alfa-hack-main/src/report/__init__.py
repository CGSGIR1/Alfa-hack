"""Генерация персонализированного отчёта по пакету report-generation-input-1.0.

    from src.report import generate
    document, trace = generate(package)
"""

from .pipeline import generate

__all__ = ["generate"]
