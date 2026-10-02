"""Кэш анализа дедупликации: подпись, ключ, чтение/запись.

Выделено из dedup_runtime, чтобы не тащить весь анализ при импорте кэша.
"""
from __future__ import annotations

import hashlib
import os
import pickle
from typing import Any

import numpy as np

from ..models import RouteData
from .constants import _DEDUP_ALGO_VERSION
from .geometry import _dedup_stack, logger


def _hash_unit_coords(h: Any, coords: Any) -> None:
    """Квантует координаты единицы (измерения) в хэш с маркерами сбоя."""
    if not coords:
        h.update(b"0")
        return
    try:
        arr = np.asarray(coords, dtype=np.float64)[:, :2]
    except (TypeError, ValueError):
        h.update(b"?")
        return
    if arr.size == 0:
        h.update(b"0")
        return
    finite = np.isfinite(arr).all(axis=1)
    if finite.any():
        q = np.rint(arr[finite] * 1_000_000.0).astype("int64")
        h.update(q.tobytes())
    else:
        h.update(b"0")


def _hash_skip_coords(h: Any, coords: Any) -> None:
    """Квантует координаты направления в хэш; невалидные участки пропускает."""
    if not coords:
        return
    try:
        arr = np.asarray(coords, dtype=np.float64)[:, :2]
    except (TypeError, ValueError):
        return
    if arr.size == 0:
        return
    finite = np.isfinite(arr).all(axis=1)
    if not finite.any():
        return
    q = np.rint(arr[finite] * 1_000_000.0).astype("int64")
    h.update(q.tobytes())


def _routes_cache_signature(routes: list[RouteData], per_direction: bool) -> str:
    """Стабильный хэш набора маршрутов (без учёта геометрии проекции).

    Геометрия квантуется до ~0.1 м и сериализуется как бинарный буфер
    (``int64.tobytes``) — это на порядки быстрее построения гигантской
    строки ``repr()`` и ``sha256`` по ней на сотнях тысяч остановок.
    """
    h = hashlib.blake2b(digest_size=32)
    h.update(b"wikiroutes-dedup-sig-v1")
    if per_direction:
        from ..units import build_units

        for u in build_units(routes):
            h.update(b"|")
            h.update(repr(u.key).encode("utf-8"))
            _hash_unit_coords(h, getattr(u, "coords", []))
    else:
        for rd in routes:
            try:
                route_id = int(getattr(rd, "route_id", None))
            except (TypeError, ValueError, AttributeError):
                continue
            h.update(b";")
            h.update(str(route_id).encode("utf-8"))
            for d in getattr(rd, "directions", []) or []:
                _hash_skip_coords(h, getattr(d, "coords", []))
    return h.hexdigest()


def _dedup_cache_key(
    routes: list[RouteData],
    buffer_r: float,
    thr: float,
    per_direction: bool,
    compute_unique_segments: bool,
    approximate: bool,
    approx_step: float | None,
    exact_margin: float,
    profile: str,
    compute_unique_net: bool,
    unique_min_m: float,
    dataset_hash: str | None = None,
) -> str:
    """Ключ кэша dedup_analyze: входы + версия алгоритма + версии GEOS/Shapely."""
    # При точном режиме approx_step/exact_margin не влияют на результат —
    # нормализуем их, чтобы избежать лишних промахов кэша.
    if not approximate:
        approx_step = None
        exact_margin = 0.0
    # Если уникальные участки не считаются, unique_min_m не влияет на результат.
    if not compute_unique_segments:
        unique_min_m = 0.0
    stack = _dedup_stack()
    shapely_module = stack["shapely"]
    # Если внешний слой знает версию датасета, переиспользуем готовый хэш и не
    # пересчитываем подпись по всем координатам (5.1).
    sig = (
        dataset_hash
        if dataset_hash is not None
        else _routes_cache_signature(routes, per_direction)
    )
    shapely_ver = getattr(shapely_module, "__version__", "")
    geos_ver = ".".join(
        str(x) for x in getattr(shapely_module, "geos_version", ())
    )
    raw = (
        f"{sig}|{buffer_r}|{thr}|{per_direction}|{compute_unique_segments}|"
        f"{approximate}|{approx_step}|{exact_margin}|{profile}|{compute_unique_net}|"
        f"{unique_min_m}|"
        f"{_DEDUP_ALGO_VERSION}|{shapely_ver}|{geos_ver}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _dedup_cache_path(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, f"dedup_{key}.pkl")


def _load_dedup_cache(cache_dir: str, key: str) -> dict[str, Any] | None:
    path = _dedup_cache_path(cache_dir, key)
    try:
        with open(path, "rb") as fh:
            cached = pickle.load(fh)
    except (OSError, pickle.PickleError, EOFError):
        return None
    # Лёгкий кэш намеренно не хранит эти поля. Проставляем пустые значения,
    # чтобы потребители analysis (старый код) не падали при обращении к ним.
    cached.setdefault("lines", {})
    cached.setdefault("routes_geo", {})
    # `merged` сохраняется в лёгком кэше намеренно; проставляем дефолт для
    # старых/внешних кэш-файлов, у которых этого поля нет.
    cached.setdefault("merged", {})
    return cached


# Поля, которые не нужны для политики/сетевых метрик после построения
# analysis и только раздувают дисковый кэш. Они легко пересчитываются, а
# `merged` (по одной геометрии на направление) оставляем — его использует
# dedup_network_after для быстрых метрик сети без повторного union_all.
_CACHE_HEAVY_FIELDS = ("routes_geo", "lines")


def _save_dedup_cache(cache_dir: str, key: str, analysis: dict[str, Any]) -> None:
    try:
        os.makedirs(cache_dir, exist_ok=True)
        path = _dedup_cache_path(cache_dir, key)
        # Атомарная запись через временный файл: параллельный/упавший процесс
        # не оставит обрезанный .pkl, из-за которого _load_dedup_cache падал бы
        # с EOFError при следующем запуске.
        light = {
            k: v for k, v in analysis.items() if k not in _CACHE_HEAVY_FIELDS
        }
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "wb") as fh:
            pickle.dump(light, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp_path, path)
    except OSError:
        logger.warning("Не удалось сохранить кэш dedup_analyze в %s", cache_dir)


