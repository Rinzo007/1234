"""Конфигурация вычислительного конвейера Overture.

Конфигурация передаётся явно через `OvertureConfig`; чтение переменных среды
происходит только на границе приложения.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass


# Слоты здесь не используются намеренно. Поле ``cache_version`` обязано быть
# одновременно полем (``replace(config, cache_version=13)`` меняет подпись
# алгоритма) и обычным атрибутом класса (``OvertureConfig.cache_version == 12``).
# При ``slots=True`` слот с тем же именем перекрывает атрибут класса, и второе
# обращение возвращало бы дескриптор слота. Конфигурация создаётся один раз
# на запуск, поэтому выигрыш от слотов здесь не окупает потерю доступа.
@dataclass(frozen=True)
class OvertureConfig:
    #: Версия схемы кэша Overture. Входит в подпись алгоритма: смена версии
    #: обязана менять сигнатуру, иначе старый кэш переживёт смену формата.
    cache_version: int = 12

    thread_batch_size: int = 0
    thread_batch_max: int = 64
    dedupe_by_geometry: bool = True
    assume_no_overlap: bool = False
    use_coverage_union: bool = False
    union_grid_size: float | None = None
    min_building_area_m2: float = 0.0
    buffer_quad_segs: int = 4
    line_simplify_m: float = 0.0
    directions_latlon: bool = True
    skip_repair: bool = False
    buffer_cache_max_size: int = 5_000
    projection_chunk_size: int = 50_000

    @classmethod
    def from_env(cls) -> OvertureConfig:
        def _int(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        def _float_or_none(name: str, default: float | None) -> float | None:
            raw = os.getenv(name)
            if raw is None:
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            thread_batch_size=_int("OVERTURE_THREAD_BATCH_SIZE", 0),
            dedupe_by_geometry=os.getenv("OVERTURE_DEDUPE_BY_GEOMETRY", "1") == "1",
            assume_no_overlap=os.getenv("OVERTURE_ASSUME_NO_OVERLAP", "0") == "1",
            use_coverage_union=os.getenv("OVERTURE_USE_COVERAGE_UNION", "0") == "1",
            union_grid_size=_float_or_none("OVERTURE_UNION_GRID_SIZE", None),
            min_building_area_m2=max(
                0.0, _float_or_none("OVERTURE_MIN_BUILDING_AREA_M2", 0.0) or 0.0
            ),
            buffer_quad_segs=max(1, _int("OVERTURE_BUFFER_QUAD_SEGS", 4)),
            line_simplify_m=max(0.0, _float_or_none("OVERTURE_LINE_SIMPLIFY_M", 0.0) or 0.0),
            directions_latlon=os.getenv("OVERTURE_DIRECTIONS_LATLON", "1") == "1",
            skip_repair=os.getenv("OVERTURE_SKIP_REPAIR", "0") == "1",
            buffer_cache_max_size=max(1, _int("OVERTURE_BUFFER_CACHE_MAX_SIZE", 5_000)),
            projection_chunk_size=max(0, _int("OVERTURE_PROJECTION_CHUNK_SIZE", 50_000)),
        )

    def algorithm_signature(self) -> str:
        payload = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()[:16]
