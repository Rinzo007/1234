"""Общие определения типов для пакета wikiroutes."""

from typing import TypeAlias

Coordinate: TypeAlias = tuple[float, float]
DirectionKey: TypeAlias = tuple[int, int]

__all__ = ["Coordinate", "DirectionKey"]
