"""Контракт извлечения метаданных маршрута из payload и текста страницы.

Единая точка входа ``extract_metadata`` объединяет структурированные поля
API (``transportClasses``/``transportTags``, ``is_electrobus``) и, при
необходимости, текст страницы маршрута. Маршрутизация «что брать из
payload, что из страницы» локализована здесь, а не в fetch_route.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .transport_class import (
    detect_electrobus,
    extract_transport_class,
    parse_transport_class,
)

__all__ = ["RouteMetadata", "extract_metadata"]


@dataclass(frozen=True, slots=True)
class RouteMetadata:
    """Метаданные маршрута, извлечённые по единому контракту."""

    transport_class: str
    is_electrobus: bool


def extract_metadata(payload: Any, page_text: str | None = None) -> RouteMetadata:
    """Извлекает метаданные маршрута: класс транспорта и признак электробуса.

    Класс транспорта берётся сначала из структурированных полей payload;
    текст страницы используется только как fallback. Признак электробуса
    проверяется по payload и тексту страницы.
    """
    transport_class = extract_transport_class(payload)

    if not transport_class and page_text:
        transport_class = parse_transport_class(page_text)

    return RouteMetadata(
        transport_class=transport_class,
        is_electrobus=detect_electrobus(payload, page_text),
    )
