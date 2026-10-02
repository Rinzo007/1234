"""Доменные модели: остановки, направления, маршруты, каталог, фильтры.

Все модели — неизменяемые dataclass. Классы с вычисляемыми свойствами
(`Direction`, `RouteData`) намеренно не используют `slots=True`,
поскольку `functools.cached_property` требует наличия `__dict__`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol

from constants import LAT_SHIFT, LON_SHIFT, STRAIGHT_EPS
from enums import RouteType
from geometry import geometry_diameter_km, polyline_km
from type_defs import Coordinate

__all__ = [
    "BBox",
    "Catalog",
    "CatalogSection",
    "Direction",
    "DirectionLike",
    "FilterLimits",
    "ParsedRoute",
    "RouteData",
    "RouteLike",
    "RouteLink",
    "RouteTask",
    "Stop",
    "StopLike",
    "UniqueStop",
]


class StopLike(Protocol):
    """Структурный интерфейс остановки (достаточен для привязки и агрегации)."""

    id: int | None
    name: str
    latitude: float | None
    longitude: float | None


class DirectionLike(Protocol):
    """Структурный интерфейс направления маршрута."""

    stops: tuple[StopLike, ...]
    name: str


class RouteLike(Protocol):
    """Структурный интерфейс маршрута (минимальный набор полей)."""

    route_id: int
    name: str
    route_type: Any
    directions: tuple[DirectionLike, ...]
    ok: bool


def _parse_stop_id(raw_id: Any) -> int | None:
    """Разбирает идентификатор остановки из payload.

    Некорректные значения возвращают ``None``.
    """
    if isinstance(raw_id, bool):
        return None

    if isinstance(raw_id, int):
        return raw_id

    if isinstance(raw_id, float) and raw_id.is_integer():
        return int(raw_id)

    if isinstance(raw_id, str):
        stripped = raw_id.strip()
        # isascii() отсекает unicode-цифры вроде "²" (isdigit()==True,
        # но int("²") бросает ValueError).
        if stripped.isascii() and stripped.isdigit():
            return int(stripped)

    return None


def _parse_finite_range(
    value: Any,
    min_value: float,
    max_value: float,
) -> float | None:
    """Приводит значение к float и проверяет конечность и диапазон."""
    if value is None:
        return None

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(result):
        return None

    if not (min_value <= result <= max_value):
        return None

    return result


@dataclass(frozen=True, slots=True)
class Stop:
    """Неизменяемая модель остановки общественного транспорта."""

    id: int | None
    name: str
    latitude: float | None
    longitude: float | None

    @classmethod
    def from_api(
        cls,
        payload: dict[str, Any],
        shift_index: int | None = None,
    ) -> Stop | None:
        """Создаёт остановку из JSON-подобного payload WikiRoutes.

        При ``shift_index`` применяется обратный API-сдвиг координат по позиции
        остановки в списке (аналог ``unshift_coords``). Некорректные
        идентификаторы и координаты преобразуются в ``None``. Полностью пустая
        запись возвращает ``None``.
        """
        stop_id = _parse_stop_id(payload.get("id"))
        name = str(payload.get("name") or "").strip()

        latitude = _parse_finite_range(
            payload.get("latitude"),
            -90.0,
            90.0,
        )
        longitude = _parse_finite_range(
            payload.get("longitude"),
            -180.0,
            180.0,
        )

        if shift_index is not None and latitude is not None and longitude is not None:
            idx = shift_index & 7
            latitude = round(1e7 * (latitude - LAT_SHIFT[idx])) / 1e7
            longitude = round(1e7 * (longitude - LON_SHIFT[idx])) / 1e7
            latitude = _parse_finite_range(latitude, -90.0, 90.0)
            longitude = _parse_finite_range(longitude, -180.0, 180.0)

        if stop_id is None and not name and latitude is None and longitude is None:
            return None

        return cls(
            id=stop_id,
            name=name,
            latitude=latitude,
            longitude=longitude,
        )

    def coordinate(self) -> Coordinate | None:
        """Возвращает координаты как ``(latitude, longitude)`` или ``None``."""
        if self.latitude is None or self.longitude is None:
            return None

        if not math.isfinite(self.latitude) or not math.isfinite(self.longitude):
            return None

        if not (-90.0 <= self.latitude <= 90.0 and -180.0 <= self.longitude <= 180.0):
            return None

        return self.latitude, self.longitude


@dataclass(frozen=True)
class Direction:
    """Геометрия и остановки одного направления маршрута."""

    coords: tuple[Coordinate, ...]
    stops: tuple[Stop, ...]
    name: str = ""

    @cached_property
    def km(self) -> float:
        """Возвращает длину направления в километрах."""
        return polyline_km(self.coords)

    @cached_property
    def base_km(self) -> float:
        """Возвращает геометрический диаметр направления в километрах."""
        return geometry_diameter_km(self.coords)

    @cached_property
    def curvilinearity(self) -> float:
        """Возвращает коэффициент криволинейности направления."""
        if self.km <= 0.0:
            return 0.0

        if self.base_km < STRAIGHT_EPS:
            return float("inf")

        return self.km / self.base_km


@dataclass(frozen=True)
class RouteData:
    """Полная доменная модель маршрута с направлениями и метаданными."""

    name: str
    route_type: RouteType
    route_id: int
    url: str
    directions: tuple[Direction, ...] = ()
    error: str | None = None
    cached: bool = False
    price: str = ""
    company: str = ""
    active: bool = True
    transport_class: str = ""
    is_idea: bool = False
    # Источник данных маршрута: "wikiroutes", "osm", "idea" или иной slug.
    source: str = "wikiroutes"

    @cached_property
    def min_km(self) -> float:
        """Возвращает минимальную длину направления маршрута в километрах."""
        return min(
            (direction.km for direction in self.directions),
            default=0.0,
        )

    @cached_property
    def curvilinearity(self) -> float:
        """Возвращает максимальный коэффициент криволинейности маршрута."""
        return max(
            (direction.curvilinearity for direction in self.directions),
            default=0.0,
        )

    @property
    def ok(self) -> bool:
        """Возвращает ``True``, если маршрут содержит валидную геометрию."""
        return (
            self.error is None
            and bool(self.directions)
            and any(len(direction.coords) >= 2 for direction in self.directions)
        )


@dataclass(frozen=True, slots=True)
class ParsedRoute:
    """Промежуточный результат разбора payload WikiRoutes."""

    directions: tuple[Direction, ...]
    price: str
    company: str
    active: bool
    transport_class: str


@dataclass(frozen=True, slots=True)
class RouteLink:
    """Ссылка на маршрут в каталоге WikiRoutes."""

    name: str
    route_id: int


@dataclass(frozen=True, slots=True)
class CatalogSection:
    """Раздел каталога маршрутов одного типа транспорта."""

    title: str
    route_type: RouteType
    links: tuple[RouteLink, ...]


@dataclass(frozen=True, slots=True)
class Catalog:
    """Распарсенный каталог маршрутов города."""

    sections: tuple[CatalogSection, ...]
    city_title: str
    city_slug: str
    unrecognized: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteTask:
    """Параметры задачи загрузки одного маршрута."""

    city: str
    route_type: RouteType
    name: str
    route_id: int
    section_title: str = ""


@dataclass(frozen=True, slots=True)
class BBox:
    """Географический ограничивающий прямоугольник в WGS84."""

    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def contains(self, lat: float, lon: float) -> bool:
        """Проверяет, находится ли точка внутри прямоугольника."""
        return (
            self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon
        )


@dataclass(frozen=True, slots=True)
class FilterLimits:
    """Набор ограничений для фильтрации маршрутов."""

    curvilinearity: float = 0.0
    min_length_km: float = 0.0
    max_length_km: float = 0.0
    radius_km: float = 0.0
    center_lat: float | None = None
    center_lon: float | None = None


@dataclass(frozen=True, slots=True)
class UniqueStop:
    """Уникальная остановка после объединения источников и маршрутов."""

    key: str
    id: int | None
    name: str
    latitude: float | None
    longitude: float | None
