"""Граница города из площади зданий Overture (городская территория).

Двухэтапная аналогия «кирпичей и раствора» (U.S. Census Bureau): вокруг
центра города строится окно радиусом `RADIUS_M`, площадь footprints зданий
Overture агрегируется до ячеек 750 м. «Кирпичи» (ядро) — ячейки с долей
>= 10 % застройки (прокси >1000 чел/милю²); «раствор» — смежные ячейки
с долей >= 5 % (прокси >500 чел/милю²), через которые ядро прорастает.

Правило разрывов: ареалы смыкаются при разрыве менее 0,5 мили (~800 м).

Исключения: незастроенные территории внутри ареала (парки, водоёмы,
кладбища) включаются в него, если разрыв не превышает 6 миль.

Минимальный размер — прокси 50 000 жителей (~25 км²). Возвращается
полигон, содержащий центр. Используется как запасной источник границы,
когда OSM-граница непригодна.

Источник зданий: явный файл/каталог (parquet/geojson/geoparquet) либо
автозагрузка темы `buildings`/`building` Overture (DuckDB cloud scan,
затем HTTP STAC) в окно вокруг центра.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .errors import MissingDependencyError

logger = logging.getLogger("wikiroutes.urban")

RADIUS_M = 60000.0
CORE_THRESHOLD = 0.10
MORTAR_THRESHOLD = 0.05
MIN_AREA_KM2 = 25.0
CELL_M = 750.0

# Правило разрывов (U.S. Census Bureau): ареалы смыкаются, если разрыв
# по дорогам менее 0,5 мили (~800 м).
GAP_MERGE_M = 800.0

# Исключения (парки, водоёмы, кладбища) внутри ареала включаются в него,
# если разрыв не превышает 6 миль.
HOLE_MAX_M = 6 * 1609.344

_SUBSET_CACHE_VERSION = "v1"
_RESULT_CACHE_VERSION = "v2"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _import_gis() -> dict[str, Any]:
    """Лениво импортирует rasterio, pyproj, shapely; бросает MissingDependencyError."""
    try:
        from pyproj import Transformer
        from shapely.geometry import Point
        from shapely.geometry import shape as shapely_shape
        from shapely.ops import transform as shapely_transform
        from shapely.ops import unary_union as shapely_unary_union
    except ImportError as exc:  # pragma: no cover
        raise MissingDependencyError(
            "Для построения границы из зданий Overture нужны: "
            "pip install pyproj shapely rasterio"
        ) from exc

    try:
        import rasterio  # noqa: F401 — нужен features/transform для сетки
    except ImportError as exc:  # pragma: no cover
        raise MissingDependencyError(
            "Для построения границы из зданий Overture нужны: "
            "pip install rasterio pyproj shapely"
        ) from exc

    return {
        "Transformer": Transformer,
        "Point": Point,
        "shape": shapely_shape,
        "transform": shapely_transform,
        "unary_union": shapely_unary_union,
    }


def _window_bbox_lonlat(
    lon: float,
    lat: float,
    radius_m: float,
) -> tuple[float, float, float, float]:
    """Квадратное окно вокруг центра в (min_lat, min_lon, max_lat, max_lon)."""
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.2))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def _extreme_points_bbox(geom: Any) -> tuple[float, float, float, float] | None:
    """bbox (minx, miny, maxx, maxy) по крайним точкам геометрии.

    Ось-выровненный прямоугольник по экстремумам координат всех точек
    геометрии (Multi*/GeometryCollection разворачиваются автоматически).
    None — для пустой или нечисловой геометрии.
    """
    import shapely

    if geom is None or shapely.is_empty(geom):
        return None

    bounds = tuple(float(v) for v in shapely.bounds(geom))

    if not all(math.isfinite(v) for v in bounds):
        return None

    return bounds  # type: ignore[return-value]


def _fill_small_holes(
    mask: np.ndarray,
    max_gap_m: float = HOLE_MAX_M,
    cell_m: float = CELL_M,
) -> np.ndarray:
    """Включает внутренние незастроенные анклавы (парки, водоёмы, кладбища).

    Заливается только анклав, чей максимальный габарит не превышает
    ``max_gap_m`` (6 миль): более крупный разрыв делит ареал. Фоновые
    компоненты, связаные с краем окна, никогда не заливаются.
    """
    from scipy import ndimage

    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return mask

    background, n = ndimage.label(~mask, structure=np.ones((3, 3)))
    if n == 0:
        return mask

    border_labels = set(np.unique(background[0, :]).tolist())
    border_labels.update(np.unique(background[-1, :]).tolist())
    border_labels.update(np.unique(background[:, 0]).tolist())
    border_labels.update(np.unique(background[:, -1]).tolist())
    border_labels.discard(0)

    # Рамки компонент (find_objects[i] <-> метка i+1) вместо полного
    # сравнения массива на каждую дыру: O(ячейки), а не O(дыры × ячейки).
    filled = mask.copy()

    for lbl_id, sl in enumerate(ndimage.find_objects(background), start=1):
        if sl is None or lbl_id in border_labels:
            continue

        height = sl[0].stop - sl[0].start
        width = sl[1].stop - sl[1].start
        extent_m = max(height, width) * cell_m

        if extent_m <= max_gap_m:
            patch = filled[sl]
            patch[background[sl] == lbl_id] = True

    return filled


def _apply_urban_morphology(
    frac: np.ndarray,
    *,
    core_threshold: float,
    mortar_threshold: float,
    min_area_km2: float,
    cell_m: float = CELL_M,
) -> np.ndarray:
    """Морфология «городской территории» US Census Bureau по долям застройки.

    «Кирпичи» — ячейки с долей >= ``core_threshold`` (10 %, прокси
    >1000 чел/милю²), «раствор» — >= ``mortar_threshold`` (5 %, прокси
    >500 чел/милю²): связная заливка от ядер по раствору, closing 3×3,
    включение внутренних анклавов разрывом до 6 миль, отсечение компонент
    меньше ``min_area_km2`` (прокси 50 000 жителей). Возвращает bool-маску
    в сетке ``frac``.
    """
    from scipy import ndimage

    frac_arr = np.asarray(frac, dtype=np.float64)

    bricks = frac_arr >= core_threshold
    mortar_mask = frac_arr >= mortar_threshold

    urban = ndimage.binary_propagation(bricks, mask=mortar_mask)
    urban = ndimage.binary_closing(urban, structure=np.ones((3, 3)))
    urban = _fill_small_holes(urban, HOLE_MAX_M, cell_m)

    lbl, n = ndimage.label(urban)
    if n == 0:
        return np.zeros_like(urban, dtype=bool)

    cell_km2 = (cell_m / 1000.0) ** 2
    sizes_km2 = (
        ndimage.sum(urban, lbl, index=np.arange(1, n + 1)) * cell_km2
    )

    keep = np.flatnonzero(sizes_km2 >= min_area_km2) + 1
    if keep.size == 0:
        return np.zeros_like(urban, dtype=bool)

    return np.isin(lbl, keep)


def _building_fraction_grid(
    geoms_utm: np.ndarray,
    x0: float,
    y0: float,
    nx: int,
    ny: int,
    cell_m: float = CELL_M,
) -> np.ndarray:
    """Доля площади зданий Overture в ячейках сетки.

    Площадь каждого здания целиком относится к ячейке его центроида.
    Массив зданий разбивается на чанки и обрабатывается параллельно
    в потоках. Результаты (частичные массивы `frac`) суммируются.

    ``(x0, y0)`` — левый нижний угол окна в метрах проекции; строки сетки
    считаются сверху (растровое соглашение rasterio: строка 0 — север).
    """
    import concurrent.futures
    import shapely

    frac = np.zeros((ny, nx), dtype=np.float64)
    total = geoms_utm.size

    if total == 0:
        return frac

    y_top = y0 + ny * cell_m
    cell_area = float(cell_m) * float(cell_m)

    def accumulate(chunk: np.ndarray) -> np.ndarray:
        if chunk.size == 0:
            return np.zeros((ny, nx), dtype=np.float64)

        centroids = shapely.centroid(chunk)
        xs = shapely.get_x(centroids)
        ys = shapely.get_y(centroids)
        areas = shapely.area(chunk)

        valid = (
            np.isfinite(xs)
            & np.isfinite(ys)
            & np.isfinite(areas)
            & (areas > 0.0)
        )

        if not valid.any():
            return np.zeros((ny, nx), dtype=np.float64)

        xs = xs[valid]
        ys = ys[valid]
        areas = areas[valid]

        ix = np.floor((xs - x0) / cell_m).astype(np.int64)
        iy = np.floor((y_top - ys) / cell_m).astype(np.int64)

        inside = (ix >= 0) & (ix < nx) & (iy >= 0) & (iy < ny)
        if not inside.any():
            return np.zeros((ny, nx), dtype=np.float64)

        # np.bincount обычно заметно быстрее, чем np.add.at для такой
        # агрегации площадей по плоским индексам ячеек.
        flat = (iy[inside] * nx + ix[inside]).astype(np.intp, copy=False)
        weights = areas[inside]

        sums = np.bincount(flat, weights=weights, minlength=nx * ny)
        return sums.reshape((ny, nx))

    chunk_size = max(50_000, _env_int("URBAN_BUILDING_CHUNK", 250_000))

    if total <= chunk_size:
        frac = accumulate(geoms_utm)
    else:
        chunks = [
            geoms_utm[i : i + chunk_size]
            for i in range(0, total, chunk_size)
        ]

        workers = max(1, _env_int("URBAN_BUILDING_WORKERS", 4))
        workers = min(workers, len(chunks))

        if workers <= 1:
            for chunk in chunks:
                frac += accumulate(chunk)
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers
            ) as executor:
                for partial in executor.map(accumulate, chunks):
                    frac += partial

    frac /= cell_area
    return frac


def _resolve_building_paths(
    buildings: str | Path | list[str | Path] | None,
) -> list[str]:
    """Резолвит явные источники зданий Overture (файлы/каталоги)."""
    if buildings is None:
        return []

    from .overture.load import overture_resolve_sources

    if isinstance(buildings, (str, Path)):
        buildings = [buildings]

    paths: list[str] = []

    for item in buildings:
        path = Path(item)

        if path.is_dir():
            paths.extend(overture_resolve_sources(str(path)))
        elif path.is_file():
            paths.append(str(path))
        else:
            logger.warning("Overture: источник зданий не найден: %s", item)

    return paths


def _download_buildings_utm(
    bbox: tuple[float, float, float, float],
    release: str | None,
    cache_dir: str | Path | None,
    retries: int,
    retry_delay: float,
) -> tuple[np.ndarray, int | None]:
    """Автозагрузка темы buildings/building и проекция в UTM.

    Каскад: HTTP STAC-части, затем DuckDB cloud scan (S3, Azure).
    Возвращает ``(геометрии_utm, epsg)``; пустой массив при неудаче.
    Скачанное складывается во временный geoparquet и чистится после чтения.
    """
    from .overture.load import load_overture_geometries
    from .overture.release import OvertureReleaseError, resolve_overture_release

    try:
        release_eff = resolve_overture_release(release)
    except OvertureReleaseError as exc:
        logger.warning("Overture: %s", exc)
        return np.asarray([], dtype=object), None

    cleanup_dir = False

    if cache_dir is not None:
        tmp_dir = Path(cache_dir) / "urban_buildings"
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="urban_buildings_"))
        cleanup_dir = True

    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / "buildings_subset.geoparquet"

    try:
        def _load_via_http() -> tuple[np.ndarray, int | None] | None:
            """HTTP STAC-части: скачивание в кэш и чтение через общий loader."""
            import geopandas as gpd

            from .overture.http import (
                _concat_part_frames,
                _download_overture_parts,
                _http_resolve_stac_part_files,
                _part_local_path,
                _read_part_frames,
            )

            keys = _http_resolve_stac_part_files(
                release_eff,
                "buildings",
                "building",
                bbox,
                retries=retries,
                retry_delay=retry_delay,
            )

            if not keys:
                logger.warning(
                    "Overture: STAC не нашёл buildings-файлов для bbox %s",
                    bbox,
                )
                return None

            _download_overture_parts(keys, tmp_dir, retries, retry_delay)

            local_files = [
                str(path)
                for k in keys
                if (path := _part_local_path(k, tmp_dir)).exists()
            ]

            if not local_files:
                return None

            bbox_filter = (
                min(bbox[1], bbox[3]),
                min(bbox[0], bbox[2]),
                max(bbox[1], bbox[3]),
                max(bbox[0], bbox[2]),
            )

            frames = _read_part_frames(local_files, bbox_filter, gpd)
            if not frames:
                return None

            gdf = _concat_part_frames(frames, gpd)
            if gdf is None or len(gdf) == 0:
                return None

            gdf.to_parquet(tmp_file)
            return load_overture_geometries([str(tmp_file)], bbox, 0, None)

        # 1) HTTP STAC-части — основной путь.
        result = _load_via_http()
        if result is not None:
            return result

        # 2) DuckDB cloud scan — резервный транспорт без кэша частей.
        try:
            from .overture.http import _duckdb_read_overture

            for provider in ("s3", "azure"):
                try:
                    gdf = _duckdb_read_overture(
                        release_eff,
                        "buildings",
                        "building",
                        bbox,
                        provider=provider,
                    )
                except ImportError as exc:
                    logger.warning(
                        "Overture: DuckDB/%s недоступен: %s",
                        provider,
                        exc,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 — следующий транспорт
                    logger.warning(
                        "Overture: DuckDB/%s buildings не сработал: %s",
                        provider,
                        exc,
                    )
                    continue

                if gdf is not None and len(gdf) > 0:
                    gdf.to_parquet(tmp_file)
                    return load_overture_geometries(
                        [str(tmp_file)],
                        bbox,
                        0,
                        None,
                    )
        except ImportError:
            pass

    except Exception as exc:  # noqa: BLE001 — сеть/формат: граница не строится
        logger.warning("Overture: автозагрузка buildings не удалась: %s", exc)
        return np.asarray([], dtype=object), None

    finally:
        with contextlib.suppress(OSError):
            tmp_file.unlink(missing_ok=True)

        if cleanup_dir:
            with contextlib.suppress(OSError):
                tmp_dir.rmdir()


def _subset_cache_key(
    *,
    kind: str,
    bbox: tuple[float, float, float, float],
    release: str | None,
    sources_sig: str,
) -> str:
    """Ключ кэша спроецированного subset'а зданий (bbox + источник)."""
    digest = hashlib.sha1(
        "|".join(
            (
                _SUBSET_CACHE_VERSION,
                kind,
                release or "",
                ",".join(f"{v:.6f}" for v in bbox),
                sources_sig,
            )
        ).encode()
    ).hexdigest()[:16]

    return f"urban_subset_{_SUBSET_CACHE_VERSION}_{digest}.geoparquet"


def _result_cache_key(
    *,
    bbox: tuple[float, float, float, float],
    cell_m: float,
    core_threshold: float,
    mortar_threshold: float,
    min_area_km2: float,
    radius_m: float,
) -> str:
    """Ключ кэша конечного результата (граница) по всем параметрам."""
    digest = hashlib.sha1(
        "|".join(
            (
                _RESULT_CACHE_VERSION,
                ",".join(f"{v:.6f}" for v in bbox),
                f"{cell_m:.4f}",
                f"{core_threshold:.6f}",
                f"{mortar_threshold:.6f}",
                f"{min_area_km2:.6f}",
                f"{radius_m:.4f}",
            )
        ).encode()
    ).hexdigest()[:16]

    return f"urban_result_{_RESULT_CACHE_VERSION}_{digest}.wkb"


def _sources_signature(paths: list[str]) -> str:
    """Сигнатура явных файлов: путь + mtime + размер (детект изменений)."""
    parts: list[str] = []

    for path in sorted(paths):
        try:
            stat = Path(path).stat()
            parts.append(f"{path}|{stat.st_mtime_ns}|{stat.st_size}")
        except OSError:
            parts.append(f"{path}|missing")

    return ";".join(parts)


def _read_subset_cache(
    path: Path,
) -> tuple[np.ndarray, int | None] | None:
    """Читает кэшированный subset (UTM-геометрии, epsg); None при промахе."""
    try:
        import geopandas as gpd

        if not path.is_file():
            return None

        try:
            gdf = gpd.read_parquet(path, columns=["geometry"])
        except Exception:  # noqa: BLE001 — старый/нестандартный кэш
            gdf = gpd.read_parquet(path)

        if gdf is None or len(gdf) == 0 or "geometry" not in gdf.columns:
            return None

        epsg = gdf.crs.to_epsg() if gdf.crs is not None else None
        if epsg is None:
            return None

        return np.asarray(gdf.geometry.to_numpy(), dtype=object), int(epsg)

    except Exception:  # noqa: BLE001 — битый кэш лечится перезаписью
        return None


def _write_subset_cache(
    path: Path,
    geoms_utm: np.ndarray,
    epsg: int,
) -> None:
    """Пишет subset в кэш; ошибки игнорируются (расчёт не зависит от кэша)."""
    try:
        import geopandas as gpd

        path.parent.mkdir(parents=True, exist_ok=True)

        gdf = gpd.GeoDataFrame(
            {"geometry": geoms_utm},
            crs=f"EPSG:{int(epsg)}",
        )
        gdf.to_parquet(path)

    except Exception:  # noqa: BLE001 — кэш опционален
        logger.warning("Urban: не удалось записать кэш subset %s", path)


def _read_result_cache(path: Path) -> Any | None:
    """Читает кэшированную границу (бинарный WKB) или None."""
    try:
        import shapely

        if not path.is_file():
            return None

        data = path.read_bytes()
        if not data:
            return None

        return shapely.from_wkb(data)

    except Exception:  # noqa: BLE001 — битый кэш лечится перезаписью
        return None


def _write_result_cache(path: Path, boundary: Any) -> None:
    """Пишет границу (WKB) в кэш; ошибки игнорируются."""
    try:
        import shapely

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(shapely.to_wkb(boundary))

    except Exception:  # noqa: BLE001 — кэш опционален
        logger.warning("Urban: не удалось записать кэш результата %s", path)


def build_urban_boundary_from_overture(
    lon: float,
    lat: float,
    *,
    buildings: str | Path | list[str | Path] | None = None,
    release: str | None = None,
    cache_dir: str | Path | None = None,
    retries: int = 1,
    retry_delay: float = 2.0,
    radius_m: float = RADIUS_M,
    core_threshold: float = CORE_THRESHOLD,
    mortar_threshold: float = MORTAR_THRESHOLD,
    min_area_km2: float = MIN_AREA_KM2,
    cell_m: float = CELL_M,
) -> Any | None:
    """Возвращает полигон городской территории в WGS84 или None.

    ``lon/lat`` — центр города (обычно центроид OSM-границы). Площадь
    footprints зданий Overture агрегируется в доли застройки ячеек
    ``cell_m`` м, дальше — та же морфология US Census Bureau, что и для
    растра (ядро 10 %, раствор 5 %, мин. 25 км², компонента с центром).

    ``buildings`` — явный файл/каталог/список зданий Overture
    (parquet/geojson/geoparquet) или URL; при ``None`` тема
    buildings/building автозагружается в окно ``radius_m`` вокруг центра.
    """
    import shapely
    from rasterio.features import shapes as rio_shapes
    from rasterio.transform import Affine

    gis = _import_gis()
    Transformer = gis["Transformer"]
    Point = gis["Point"]

    bbox = _window_bbox_lonlat(lon, lat, radius_m)
    paths = _resolve_building_paths(buildings)

    # Персистентный кэш спроецированного subset'а зданий (промежуточный).
    cache_file: Path | None = None
    result_cache_file: Path | None = None

    if cache_dir is not None:
        if paths:
            sig = _sources_signature(paths)
        else:
            sig = "buildings/building"

        cache_file = (
            Path(cache_dir)
            / "urban_buildings"
            / _subset_cache_key(
                kind="files" if paths else "auto",
                bbox=bbox,
                release=release,
                sources_sig=sig,
            )
        )

        result_cache_file = (
            Path(cache_dir)
            / "urban_results"
            / _result_cache_key(
                bbox=bbox,
                cell_m=cell_m,
                core_threshold=core_threshold,
                mortar_threshold=mortar_threshold,
                min_area_km2=min_area_km2,
                radius_m=radius_m,
            )
        )

        result_hit = _read_result_cache(result_cache_file)
        if result_hit is not None:
            return result_hit

        hit = _read_subset_cache(cache_file)
        if hit is not None:
            geoms_utm, epsg = hit
        else:
            geoms_utm, epsg = None, None
    else:
        geoms_utm, epsg = None, None

    if geoms_utm is None:
        if paths:
            from .overture.load import load_overture_geometries

            geoms_utm, epsg = load_overture_geometries(paths, bbox, 0, None)
        else:
            geoms_utm, epsg = _download_buildings_utm(
                bbox,
                release,
                cache_dir,
                retries,
                retry_delay,
            )

        if (
            cache_file is not None
            and epsg is not None
            and geoms_utm is not None
            and len(geoms_utm) > 0
        ):
            _write_subset_cache(cache_file, np.asarray(geoms_utm), int(epsg))

    if epsg is None or geoms_utm is None or len(geoms_utm) == 0:
        logger.warning("Overture: здания для границы не найдены")
        return None

    geoms_utm = np.asarray(geoms_utm, dtype=object)

    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    cx, cy = to_utm.transform(lon, lat)

    x0, y0 = cx - radius_m, cy - radius_m
    n = max(1, int(math.ceil(2.0 * radius_m / cell_m)))

    frac = _building_fraction_grid(geoms_utm, x0, y0, n, n, cell_m)

    if not np.any(frac > 0.0):
        return None

    urban = _apply_urban_morphology(
        frac,
        core_threshold=core_threshold,
        mortar_threshold=mortar_threshold,
        min_area_km2=min_area_km2,
        cell_m=cell_m,
    )

    if not urban.any():
        return None

    transform = Affine(cell_m, 0.0, x0, 0.0, -cell_m, y0 + n * cell_m)
    center_pt = Point(cx, cy)

    geoms = []
    for g, v in rio_shapes(
        urban.astype(np.uint8, copy=False),
        mask=urban,
        transform=transform,
    ):
        if v == 1:
            geoms.append(gis["shape"](g))

    if not geoms:
        return None

    circle = center_pt.buffer(radius_m, quad_segs=64)
    boundary = _pick_boundary_component(geoms, center_pt)

    if boundary is None:
        return None

    boundary = boundary.intersection(circle)
    if shapely.is_empty(boundary):
        return None

    boundary = boundary.simplify(200.0, preserve_topology=True).buffer(0)
    if shapely.is_empty(boundary):
        return None

    to_4326 = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    boundary = gis["transform"](to_4326.transform, boundary)

    result = boundary if not shapely.is_empty(boundary) else None

    if result_cache_file is not None and result is not None:
        _write_result_cache(result_cache_file, result)

    return result


def build_urban_boundary(
    lon: float,
    lat: float,
    source: str | Path | list[str | Path] | None = None,
    *,
    radius_m: float = RADIUS_M,
    core_threshold: float = CORE_THRESHOLD,
    mortar_threshold: float = MORTAR_THRESHOLD,
    min_area_km2: float = MIN_AREA_KM2,
    release: str | None = None,
    cache_dir: str | Path | None = None,
) -> Any | None:
    """Возвращает полигон городской территории в WGS84 или None.

    Источник — площадь зданий Overture: source — явный
    файл/каталог/список зданий (parquet/geojson/geoparquet), при None
    тема buildings/building автозагружается в окно вокруг центра.
    """
    return build_urban_boundary_from_overture(
        lon,
        lat,
        buildings=source,
        release=release,
        cache_dir=cache_dir,
        radius_m=radius_m,
        core_threshold=core_threshold,
        mortar_threshold=mortar_threshold,
        min_area_km2=min_area_km2,
    )


def boundary_bbox(boundary: Any) -> Any | None:
    """bbox-прямоугольник в WGS84 по крайним точкам полигона границы.

    Ось-выровненный прямоугольник по экстремумам координат ``boundary``
    (те же точки, что у ``build_urban_bbox``), без пересчёта самой границы.
    None — при пустой/вырожденной (точка-линия) геометрии.
    """
    bounds = _extreme_points_bbox(boundary)
    if bounds is None:
        return None

    minx, miny, maxx, maxy = bounds
    if minx == maxx or miny == maxy:
        return None

    import shapely

    return shapely.box(minx, miny, maxx, maxy)


def build_urban_bbox(
    lon: float,
    lat: float,
    source: str | Path | list[str | Path] | None = None,
    *,
    radius_m: float = RADIUS_M,
    core_threshold: float = CORE_THRESHOLD,
    mortar_threshold: float = MORTAR_THRESHOLD,
    min_area_km2: float = MIN_AREA_KM2,
    release: str | None = None,
    cache_dir: str | Path | None = None,
) -> Any | None:
    """Прямоугольник городской территории в WGS84 или None.

    Граница строится как в :func:`build_urban_boundary`, затем bbox
    собирается по крайним точкам полученного полигона (min/max долготы и
    широты) и возвращается как прямоугольник. None — если граница не
    построилась либо полигон выродился в точку или линию (нулевая площадь
    по одной из осей).
    """
    boundary = build_urban_boundary(
        lon,
        lat,
        source,
        radius_m=radius_m,
        core_threshold=core_threshold,
        mortar_threshold=mortar_threshold,
        min_area_km2=min_area_km2,
        release=release,
        cache_dir=cache_dir,
    )

    return boundary_bbox(boundary)


def _pick_boundary_component(
    geoms: Any,
    center_pt: Any,
    gap_m: float = GAP_MERGE_M,
) -> Any | None:
    """Выбирает «городскую территорию» для центра города.

    Ядро — компонента, содержащая центральную точку; если такой нет —
    ближайшая к центру (центроид границы города может лежать на реке,
    делящей город на берега). Затем к ядру присоединяются все компоненты,
    чьё минимальное расстояние до уже собранного набора не превышает
    ``gap_m`` (правило разрывов: смыкание при разрыве менее 0,5 мили,
    ~800 м по дорогам — напр. зазор между берегами одной реки), пока набор
    растёт. Так сохраняются оба берега (напр. Ульяновск) и не притягивается
    соседний город (напр. Нижний Новгород для Дзержинска, ~10+ км).

    Возвращает объединение кластера или ``None`` при пустом входе.

    Кластеризация — union-find по рёбрам «расстояние <= gap_m» через
    STRtree: тот же связный компонент сида, что и наивный цикл, но
    O(n log n) вместо O(n²) попарных distance.
    """
    if not geoms:
        return None

    import shapely

    parts = list(geoms)
    n = len(parts)

    seed_idx = next(
        (i for i, g in enumerate(parts) if g.intersects(center_pt)),
        None,
    )

    if seed_idx is None:
        seed_idx = min(range(n), key=lambda i: parts[i].distance(center_pt))

    if n == 1:
        return parts[seed_idx]

    tree = shapely.STRtree(parts)
    parent = list(range(n))
    rank = [0] * n

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, geom in enumerate(parts):
        for j in tree.query(geom, predicate="dwithin", distance=gap_m):
            j = int(j)

            if i == j:
                continue

            root_i = find(i)
            root_j = find(j)

            if root_i == root_j:
                continue

            if rank[root_i] < rank[root_j]:
                root_i, root_j = root_j, root_i

            parent[root_j] = root_i

            if rank[root_i] == rank[root_j]:
                rank[root_i] += 1

    seed_root = find(seed_idx)
    cluster = [g for i, g in enumerate(parts) if find(i) == seed_root]

    if len(cluster) == 1:
        return cluster[0]

    return shapely.unary_union(cluster)


__all__ = [
    "CELL_M",
    "boundary_bbox",
    "build_urban_bbox",
    "build_urban_boundary",
    "build_urban_boundary_from_overture",
]