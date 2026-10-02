"""Граница интеграции Overture с родительским приложением.

Внутренние модули пакета не импортируют ``..cache``, ``..models`` и прочую
инфраструктуру напрямую. Для переноса Overture в отдельный пакет достаточно
заменить этот адаптер.
"""

from __future__ import annotations

# Адаптер — единственное место, где Overture дотягивается до инфраструктуры
# родительского приложения. Импорты плоские, как принято в пакете
# ``wikiroutes``: каталог сам добавлен в ``sys.path`` (см. ``__init__.py``),
# поэтому относительный ``..cache`` здесь уводил бы за пределы пакета.
from cache import JsonCache
from common import resolve_sources, utm_epsg
from metrics import OvertureStats
from models import RouteData
from units import dir_geo_sig

__all__ = [
    "JsonCache",
    "OvertureStats",
    "RouteData",
    "dir_geo_sig",
    "resolve_sources",
    "utm_epsg",
]
