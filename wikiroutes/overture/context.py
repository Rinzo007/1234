"""Неизменяемый контекст вычислений, разделяемый между потоками."""

from dataclasses import dataclass

import numpy as np
from shapely import STRtree

from .config import OvertureConfig


@dataclass(frozen=True, slots=True)
class _OvertureContext:
    """Неизменяемый контекст вычислений, разделяемый между потоками.

    Массив геометрий делается read-only перед публикацией контекста; конфигурация
    и индекс входят в единый снимок состояния pipeline.
    """

    polygon_geometries: np.ndarray
    tree: STRtree
    epsg: int
    buffer_m: float
    city: str
    sig: str
    config: OvertureConfig
