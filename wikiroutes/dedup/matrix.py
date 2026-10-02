"""Материализация K2-матрицы для экспорта/отчёта."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .geometry import DedupId


def materialize_dedup_matrix(analysis: dict[str, Any]) -> pd.DataFrame:
    """Материализует плотную K2-матрицу только для экспорта/отчёта.

    Внимание: плотная матрица занимает O(N²) памяти — при ~10⁴ направлений
    это ~800 МБ float64. ``pairs`` уже является long-format представлением
    и для большинства экспортов предпочтительнее. Материализация здесь
    векторизована через numpy-индексацию вместо цикла ``.loc``.
    """
    ids: list[DedupId] = list(analysis.get("ids", []))
    n = len(ids)
    pairs = analysis.get("pairs")
    matrix = pd.DataFrame(0.0, index=ids, columns=ids)
    if not isinstance(pairs, pd.DataFrame) or pairs.empty or n == 0:
        return matrix

    # Локальные индексы xi/yi уже есть в pairs — берём напрямую, без поиска
    # по DirectionId. Резервный путь восстанавливает их через pos.
    if "xi" in pairs.columns and "yi" in pairs.columns:
        xi = np.asarray(pairs["xi"].to_numpy(), dtype=np.int64)
        yi = np.asarray(pairs["yi"].to_numpy(), dtype=np.int64)
    else:
        pos = {rid: k for k, rid in enumerate(ids)}
        xi = np.array([pos.get(v, -1) for v in pairs["x"].tolist()], dtype=np.int64)
        yi = np.array([pos.get(v, -1) for v in pairs["y"].tolist()], dtype=np.int64)
    ok = (xi >= 0) & (yi >= 0)
    if not ok.any():
        return matrix

    k2_xy = np.asarray(pairs["K2_xy"].to_numpy(), dtype=float)[ok]
    k2_yx = np.asarray(pairs["K2_yx"].to_numpy(), dtype=float)[ok]
    xi = xi[ok]
    yi = yi[ok]

    m = np.zeros((n, n), dtype=float)
    m[xi, yi] = k2_xy
    m[yi, xi] = k2_yx
    matrix = pd.DataFrame(m, index=ids, columns=ids)
    return matrix
