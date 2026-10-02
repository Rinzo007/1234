"""Автозагрузка тем Overture в локальный кэш (имя кэша, HTTP, атомарная запись)."""
import contextlib
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import _safe_bbox_key
from .http import (
    _download_overture_parts,
    _http_resolve_stac_part_files,
    _part_local_path,
    _read_overture_parts,
    _sql_literal,
)
# Общие помощники реэкспортируются, а не дублируются: реализация ``_sql_literal``
# принадлежит ``overture.http``, разрешение POI-файла — ``overture.poi``.
# Копии этих функций в этом модуле означали бы, что правка одной не исправляет
# другую, и расхождение обнаруживалось бы только тестами.
from .poi import resolve_poi_place_file
from .release import OvertureReleaseError, resolve_overture_release
from .settings import (
    OVERTURE_CACHE_VERSION,
    OVERTURE_THEME_ALIASES,
)

logger = logging.getLogger("wikiroutes.gis.overture")

# Компоненты имени кэш-файла (theme, release) приходят из CLI/env и попадают
# в путь записи/чтения. Whitelist отсекает path traversal («../», «..\»),
# абсолютные пути и служебные символы (в т. ч. NTFS-потоки «file:stream»).
# Точка разрешена: релизы вида «2024-06-13-beta.1» легитимны.
_SAFE_COMPONENT_RE = re.compile(r"^[a-z0-9_.-]+$")


def _safe_cache_component(value: str, *, kind: str) -> str | None:
    """Нормализует и проверяет компонент имени кэш-файла Overture."""
    normalized = value.strip().lower()
    if not normalized or not _SAFE_COMPONENT_RE.match(normalized):
        logger.warning(
            "Overture: отклонено недопустимое значение %s=%r "
            "(разрешены [a-z0-9_.-])",
            kind,
            value,
        )
        return None
    return normalized


def _sanitize_package_version(version: str) -> str:
    """Мягко очищает версию пакета (метаданные библиотеки, не ввод пользователя)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "", version.strip())
    return cleaned or "unknown"


def build_overture_cache_name(
    normalized_theme: str,
    package_version: str,
    release_key: str,
    bbox_key: str,
) -> str | None:
    """Собирает безопасное имя кэш-файла из компонентов."""
    safe_theme = _safe_cache_component(normalized_theme, kind="тема")
    safe_release = _safe_cache_component(release_key, kind="release")
    if safe_theme is None or safe_release is None:
        return None
    safe_package = _sanitize_package_version(package_version)
    return (
        f"overture_v{OVERTURE_CACHE_VERSION}_{safe_theme}_"
        f"{safe_package}_{safe_release}_{bbox_key}"
    )


def _write_geoparquet_atomic(gdf: Any, target: Path) -> None:
    """Атомарно записывает GeoDataFrame в geoparquet (tmp + `os.replace`)."""
    tmp_file = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        gdf.to_parquet(tmp_file)
        tmp_file.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_file.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class _AutoDownloadSpec:
    """Параметры подготовленной автозагрузки Overture."""
    theme: str
    bbox: tuple[float, float, float, float]
    effective_release: str | None
    release_key: str
    cache_file: Path


def _prepare_auto_download(
    bbox: tuple[float, float, float, float],
    theme: str,
    release: str | None,
    cache_dir: str | Path,
) -> _AutoDownloadSpec | None:
    """Готовит параметры автозагрузки; None при отсутствии библиотеки/имён-не-whitelist."""
    try:
        import overturemaps
    except ImportError:
        logger.warning(
            "Для автозагрузки Overture: pip install overturemaps pyarrow geopandas"
        )
        return None

    theme_normalized = str(theme).strip().lower()
    normalized_theme = OVERTURE_THEME_ALIASES.get(theme_normalized, theme_normalized)
    if normalized_theme != theme_normalized:
        logger.info("Overture: тема '%s' преобразована в '%s'", theme, normalized_theme)

    min_lat, min_lon, max_lat, max_lon = map(float, bbox)
    bbox_key = _safe_bbox_key((min_lat, min_lon, max_lat, max_lon))

    try:
        effective_release = resolve_overture_release(release)
    except OvertureReleaseError as exc:
        logger.warning("Overture: %s", exc)
        return None

    package_version = getattr(overturemaps, "__version__", "unknown")
    release_key = effective_release

    cache_name = build_overture_cache_name(
        normalized_theme, package_version, release_key, bbox_key
    )
    if cache_name is None:
        return None

    cache_file = Path(cache_dir) / "overture_auto" / f"{cache_name}.geoparquet"

    return _AutoDownloadSpec(
        theme=normalized_theme,
        bbox=(min_lat, min_lon, max_lat, max_lon),
        effective_release=effective_release,
        release_key=release_key,
        cache_file=cache_file,
    )


def _download_backend_order(backend: str) -> list[str]:
    """Возвращает каскад транспортов для выбранного режима."""
    normalized = backend.strip().lower()
    if normalized == "auto":
        # HTTP идёт первым: он распараллелен и устойчивее к обрывам TLS,
        # чем прямой DuckDB-скан облачных объектов.
        return ["http", "duckdb_s3", "duckdb_azure"]
    if normalized in {"duckdb_s3", "duckdb_azure", "http"}:
        return [normalized]
    raise ValueError(f"Неизвестный Overture download backend: {backend!r}")


def _duckdb_download_overture_place(
    theme: str,
    bbox: tuple[float, float, float, float],
    release: str,
    output_dir: str | Path,
    *,
    provider: str,
) -> Any | None:
    """Читает Overture напрямую из облака через DuckDB, без STAC."""
    import duckdb
    import geopandas as gpd

    if theme != "place":
        raise ValueError(f"DuckDB-загрузка поддерживает только тему place, получено {theme!r}")

    min_lat, min_lon, max_lat, max_lon = bbox
    target = Path(output_dir) / f".overture_{uuid.uuid4().hex}.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)

    if provider == "duckdb_s3":
        source = (
            f"s3://overturemaps-us-west-2/release/{release}/"
            "theme=places/type=place/*"
        )
    elif provider == "duckdb_azure":
        source = (
            f"az://overturemapswestus2.blob.core.windows.net/release/{release}/"
            "theme=places/type=place/*"
        )
    else:
        raise ValueError(f"Неизвестный DuckDB provider: {provider}")

    def sql_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    conn = duckdb.connect(":memory:")
    try:
        conn.execute("LOAD spatial")
        if provider == "duckdb_s3":
            conn.execute("LOAD httpfs")
            conn.execute("CREATE SECRET overture_s3 (TYPE s3, REGION 'us-west-2')")
        else:
            conn.execute("LOAD azure")
            conn.execute("SET azure_transport_option_type='curl'")
            conn.execute("CREATE SECRET overture_azure (TYPE azure, PROVIDER config, ACCOUNT_NAME 'overturemapswestus2')")

        query = (
            "COPY ("
            " SELECT *"
            f" FROM read_parquet({sql_literal(source)}, filename=true, hive_partitioning=1)"
            f" WHERE bbox.xmin < {max_lon}"
            f"   AND bbox.xmax > {min_lon}"
            f"   AND bbox.ymin < {max_lat}"
            f"   AND bbox.ymax > {min_lat}"
            f") TO {sql_literal(str(target))} (FORMAT PARQUET)"
        )
        logger.info("Overture: DuckDB %s → %s", provider, source)
        started = time.monotonic()
        conn.execute(query)
        elapsed = max(time.monotonic() - started, 1e-9)
        if not target.exists() or target.stat().st_size <= 0:
            return None
        size_mb = target.stat().st_size / (1024 * 1024)
        logger.info(
            "Overture: DuckDB %s: %.1f МБ за %.1f с (%.2f МБ/с)",
            provider,
            size_mb,
            elapsed,
            size_mb / elapsed,
        )
        gdf = gpd.read_parquet(target)
        return gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    finally:
        conn.close()
        with contextlib.suppress(OSError):
            target.unlink()


def _http_download_overture_place(
    theme: str,
    bbox: tuple[float, float, float, float],
    release: str | None,
    cache_dir: str | Path,
    retries: int,
    retry_delay: float,
) -> Any:
    """Скачивает тему Overture по HTTP в локальный кэш и читает через pyarrow."""
    import geopandas as gpd

    if theme != "place":
        raise ValueError(f"HTTP-загрузка поддерживает только тему 'place', получено {theme!r}")
    if release is None:
        raise ValueError("effective release должен быть разрешён до HTTP-загрузки")

    keys = _http_resolve_stac_part_files(
        release,
        "places",
        "place",
        bbox,
        retries=retries,
        retry_delay=retry_delay,
    )
    if not keys:
        logger.warning("Overture: STAC не нашёл файлов для bbox %s, release %s", bbox, release)
        return None

    _download_overture_parts(keys, cache_dir, retries, retry_delay)

    local_files = [
        str(_part_local_path(k, cache_dir))
        for k in keys
        if _part_local_path(k, cache_dir).exists()
    ]

    bbox_filter = (
        min(bbox[1], bbox[3]),
        min(bbox[0], bbox[2]),
        max(bbox[1], bbox[3]),
        max(bbox[0], bbox[2]),
    )
    return _read_overture_parts(local_files, bbox_filter, gpd)


def _fetch_and_write_auto(
    spec: _AutoDownloadSpec,
    retries: int,
    retry_delay: float,
    backend: str,
) -> str | None:
    """Скачивает тему выбранным транспортом, пишет атомарный локальный кэш."""
    spec.cache_file.parent.mkdir(parents=True, exist_ok=True)
    backends = _download_backend_order(backend)
    last_exc: Exception | None = None

    for current in backends:
        try:
            if current in {"duckdb_s3", "duckdb_azure"}:
                gdf = _duckdb_download_overture_place(
                    spec.theme,
                    bbox=spec.bbox,
                    release=spec.effective_release or spec.release_key,
                    output_dir=spec.cache_file.parent,
                    provider=current,
                )
            elif current == "http":
                gdf = _http_download_overture_place(
                    spec.theme,
                    bbox=spec.bbox,
                    release=spec.effective_release,
                    cache_dir=spec.cache_file.parent,
                    retries=retries,
                    retry_delay=retry_delay,
                )
            else:
                raise ValueError(f"Неизвестный Overture download backend: {backend!r}")

            if gdf is not None:
                if len(gdf) == 0:
                    return None
                _write_geoparquet_atomic(gdf, spec.cache_file)
                logger.info("Overture: %d объектов → %s", len(gdf), spec.cache_file)
                return str(spec.cache_file)
        except ImportError as exc:
            last_exc = exc
            logger.warning("Overture: транспорт %s недоступен: %s", current, exc)
        except Exception as exc:
            last_exc = exc
            logger.warning("Overture: транспорт %s не сработал: %s", current, exc)
            if backend != "auto":
                raise

    if last_exc is not None and backend != "auto":
        raise last_exc
    return None


def auto_download_overture(
    bbox: tuple[float, float, float, float],
    cache_dir: str | Path,
    theme: str = "place",
    release: str | None = None,
    retries: int = 0,
    retry_delay: float = 2.0,
    backend: str | None = None,
) -> str | None:
    """Автоматически скачивает тему Overture в локальный кэш (или читает кэш)."""
    spec = _prepare_auto_download(bbox, theme, release, cache_dir)
    if spec is None:
        return None

    retries = max(retries, 0)
    backend = (backend or os.getenv("OVERTURE_DOWNLOAD_BACKEND", "auto")).strip().lower()
    if backend not in {"auto", "duckdb_s3", "duckdb_azure", "http"}:
        logger.warning("Overture: неизвестный download backend %r; используем auto", backend)
        backend = "auto"

    if spec.cache_file.exists():
        logger.info("Overture: кэш найден: %s", spec.cache_file)
        return str(spec.cache_file)

    logger.info(
        "Overture: загрузка '%s' (%.6f, %.6f) — (%.6f, %.6f), release=%s...",
        spec.theme,
        *spec.bbox,
        spec.release_key,
    )

    try:
        return _fetch_and_write_auto(spec, retries, retry_delay, backend)
    except KeyError:
        logger.warning("Overture: неизвестная тема '%s'", theme)
        return None
    except Exception as exc:
        logger.warning("Overture: ошибка автозагрузки: %s", exc)
        logger.exception("Overture auto-download failed")
        return None


__all__ = [
    "_SAFE_COMPONENT_RE",
    "_AutoDownloadSpec",
    "_fetch_and_write_auto",
    "_http_download_overture_place",
    "_prepare_auto_download",
    "_safe_cache_component",
    "_sanitize_package_version",
    "_write_geoparquet_atomic",
    "auto_download_overture",
    "build_overture_cache_name",
    "resolve_poi_place_file",
]