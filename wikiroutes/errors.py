"""Иерархия исключений пакета wikiroutes.

Все специфичные исключения наследуют от WikiroutesError, что позволяет
ловить ошибки пакета как выборочно (по конкретному типу), так и обобщённо
(через WikiroutesError).
"""

__all__ = [
    "CatalogLoadError",
    "MissingDependencyError",
    "WikiroutesError",
]


class WikiroutesError(Exception):
    """Базовое исключение приложения wikiroutes."""


class CatalogLoadError(WikiroutesError):
    """Каталог не может быть загружен или разобран."""


class MissingDependencyError(WikiroutesError):
    """Отсутствует опциональная зависимость (numpy, shapely, rasterio и т.д.)."""
