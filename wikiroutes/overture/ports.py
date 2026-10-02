"""Порты Overture: минимальные контракты для интеграции с хост-приложением."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CachePort(Protocol):
    """Синхронный интерфейс дискового кэша."""

    def get(self, _namespace: str, key: str) -> Any | None:
        ...

    def put(self, _namespace: str, key: str, value: Any) -> None:
        ...


@runtime_checkable
class DirectionPort(Protocol):
    """Минимальный интерфейс геометрического направления маршрута."""

    @property
    def coords(self) -> Iterable[Sequence[float]]:
        ...


@runtime_checkable
class RoutePort(Protocol):
    """Минимальный контракт маршрута, необходимый Overture."""

    @property
    def route_id(self) -> int:
        ...

    @property
    def directions(self) -> Sequence[DirectionPort]:
        ...

    @property
    def error(self) -> Any:
        ...


@runtime_checkable
class StatsPort(Protocol):
    """Контракт статистики, используемый внутренними слоями."""

    total_area_m2: float
    corridor_m2: float
    count: int
    ok: bool


__all__ = ["CachePort", "DirectionPort", "RoutePort", "StatsPort"]
