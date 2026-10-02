"""Конфигурация CLI и runtime приложения WikiRoutes.

Единая точка сборки настроек между argparse и pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import make_catalog_url
from .constants import (
    BASE_URL,
    DEFAULT_CITY,
    MAX_WORKERS,
    ROUTE_CATALOG_BINDINGS,
)
from .enums import ExportFormat, RouteType, parse_route_type


DEFAULT_DATA_DIR = Path(
    os.getenv("WIKIROUTES_DATA_DIR", r"D:\Programs\Cities2")
)
DEFAULT_CACHE_DIR = os.getenv(
    "WIKIROUTES_CACHE_DIR",
    str(DEFAULT_DATA_DIR / "wikiroutes_cache"),
)
DEFAULT_ORCHESTRATOR_DIR = Path(
    os.getenv(
        "WIKIROUTES_ORCHESTRATOR_DIR",
        r"D:\Programs\found_scripts\scripts\orchestrator",
    )
)


class CliConfigError(ValueError):
    """Некорректная комбинация параметров CLI."""


def _split_values(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [item.strip() for item in values.split(",") if item.strip()]
    result: list[str] = []
    for value in values:
        result.extend(item.strip() for item in str(value).split(",") if item.strip())
    return result


def _route_types(values: Any) -> frozenset[RouteType]:
    try:
        return frozenset(parse_route_type(value) for value in _split_values(values))
    except ValueError as exc:
        raise CliConfigError(f"Неизвестный тип маршрута: {exc}") from exc


def _output_formats(value: str | None) -> frozenset[str]:
    values = {item.strip().lower() for item in _split_values(value)}
    allowed = {item.value for item in ExportFormat}
    unknown = values - allowed
    if unknown:
        raise CliConfigError(
            "Неизвестный формат вывода: " + ", ".join(sorted(unknown))
        )
    return frozenset(values or {ExportFormat.XLSX.value, ExportFormat.KML.value})


def _map_type(value: Any) -> RouteType | None:
    if not value:
        return None
    try:
        return parse_route_type(value)
    except ValueError as exc:
        raise CliConfigError(f"Неизвестный тип для --map-type: {value}") from exc


def _extra_relations(value: Any) -> tuple[int, ...]:
    if not value:
        return ()
    result: list[int] = []
    for item in _split_values(value):
        try:
            result.append(int(item))
        except ValueError as exc:
            raise CliConfigError(f"Некорректный --boundary-extra: {item}") from exc
    return tuple(result)


@dataclass
class CliConfig:
    # Application / output
    city_input: str = DEFAULT_CITY
    catalog_url: str = f"{BASE_URL}/{DEFAULT_CITY}/catalog"
    route_catalog_city: str = ""
    output_formats: frozenset[str] = field(
        default_factory=lambda: frozenset({"xlsx", "kml"})
    )
    output: str | None = None
    workers: int = MAX_WORKERS
    stops: bool = False
    terminals_geojson: bool = False
    boundary_geojson: bool = False
    map_type: RouteType | None = None


    # Route filters
    type_filter: frozenset[RouteType] = field(default_factory=frozenset)
    disabled_types: frozenset[RouteType] = field(default_factory=frozenset)
    route_filter: str | None = None
    max_route_number: int = 0
    curv: float = 0.0
    minlen: float | None = None
    maxlen: float | None = None
    radius: float | None = None
    center_lat: float | None = None
    center_lon: float | None = None
    active_only: bool = False
    boundary: bool = True
    boundary_buffer: float = 500.0
    boundary_country: str = "ru"
    boundary_extra: tuple[int, ...] = ()
    boundary_buildings: str | None = None

    # Enrichment / spatial
    overture: bool = False
    overture_file: str | None = None
    overture_buffer: float = 500.0
    overture_theme: str = "place"
    overture_release: str | None = None
    overture_download_retries: int = 3
    poi: bool = False
    poi_buffer: float | None = None
    poi_stops: bool = False
    poi_stops_file: str | None = None
    poi_stops_buffer: float = 500.0

    # Passenger flow
    passenger: bool = False
    passenger_pop: str | None = None
    passenger_built: str | None = None
    passenger_points_per_km2: float = 10.0
    passenger_top_k_destinations: int = 0
    passenger_route_limit: int = 0

    # Dedup
    dedup: bool = False
    dedup_metric: str | None = None
    dedup_buffer: float = 500.0
    dedup_threshold: float = 0.70
    dedup_passes: int = 1
    dedup_unique_km: float = 0.0
    dedup_cache: bool = True
    dedup_cache_dir: str | None = None
    dedup_approx: bool = False
    dedup_approx_step: float | None = None
    dedup_approx_margin: float = 0.0
    dedup_profile: str = "exact"
    dedup_unique_net: bool = True

    # Heatmap / ideas / OSM
    heatmap: bool = False
    heat_cell: float = 0.1
    heat_alpha: str = "ff"
    heat_gamma: float = 0.6
    heat_top: float = 35.0
    heat_max_height: float = 400.0
    heat_flat: bool = False
    heat_smooth: bool = False
    ideas: bool = False
    ideas_max_pages: int | None = None
    ideas_search: str | None = None
    ideas_sort: str | None = None
    osm_routes: bool = False

    # Runtime flags
    no_cache: bool = False
    refresh: bool = False
    cache_dir: str = DEFAULT_CACHE_DIR


def build_cli_config(args: Any) -> CliConfig:
    """Собирает типизированную конфигурацию из argparse Namespace."""
    city = str(getattr(args, "city_flag", None) or getattr(args, "city_arg", None) or DEFAULT_CITY).strip()
    city_url = make_catalog_url(city, getattr(args, "url", None))
    catalog_city = city.strip("/").split("/")[0].lower() if "/" in city else city.lower()

    env_binding = os.getenv("WIKIROUTES_ROUTE_CATALOG", "").strip()
    route_catalog_city = env_binding or ROUTE_CATALOG_BINDINGS.get(catalog_city, "")

    config = CliConfig(
        city_input=catalog_city,
        catalog_url=city_url,
        route_catalog_city=route_catalog_city,
        output_formats=_output_formats(getattr(args, "format", None)),
        output=getattr(args, "output", None),
        workers=max(1, int(getattr(args, "workers", MAX_WORKERS))),
        stops=bool(getattr(args, "stops", False)),
        terminals_geojson=bool(getattr(args, "terminals_geojson", False)),
        boundary_geojson=bool(getattr(args, "boundary_geojson", False)),
        map_type=_map_type(getattr(args, "map_type", None)),
        type_filter=_route_types(getattr(args, "type", None)),
        disabled_types=_route_types(getattr(args, "no_type", None)),
        route_filter=getattr(args, "route", None),
        max_route_number=max(0, int(getattr(args, "max_route_number", 0))),
        curv=float(getattr(args, "curv", 0.0) or 0.0),
        minlen=getattr(args, "minlen", None),
        maxlen=getattr(args, "maxlen", None),
        radius=getattr(args, "radius", None),
        center_lat=getattr(args, "center_lat", None),
        center_lon=getattr(args, "center_lon", None),
        active_only=bool(getattr(args, "active_only", False)),
        boundary=bool(getattr(args, "boundary", True)),
        boundary_buffer=float(getattr(args, "boundary_buffer", 500.0) or 0.0),
        boundary_country=getattr(args, "boundary_country", "ru") or "ru",
        boundary_extra=_extra_relations(getattr(args, "boundary_extra", "")),
        boundary_buildings=getattr(args, "boundary_buildings", None),
        overture=bool(getattr(args, "overture", False)),
        overture_file=getattr(args, "overture_file", None),
        overture_buffer=float(getattr(args, "overture_buffer", 500.0) or 0.0),
        overture_theme=getattr(args, "overture_theme", "place") or "place",
        overture_release=getattr(args, "overture_release", None),
        overture_download_retries=max(0, int(getattr(args, "overture_download_retries", 3))),
        poi=bool(getattr(args, "poi", False)),
        poi_buffer=getattr(args, "poi_buffer", None),
        poi_stops=bool(getattr(args, "poi_stops", False)),
        poi_stops_file=getattr(args, "poi_stops_file", None),
        poi_stops_buffer=float(getattr(args, "poi_stops_buffer", 500.0) or 0.0),
        passenger=bool(getattr(args, "passenger", False)),
        passenger_pop=getattr(args, "passenger_pop", None),
        passenger_built=getattr(args, "passenger_built", None),
        passenger_points_per_km2=max(
            0.1, float(getattr(args, "passenger_points_per_km2", 10.0) or 10.0)
        ),
        passenger_route_limit=max(
            0, int(getattr(args, "passenger_route_limit", 0) or 0)
        ),
        passenger_top_k_destinations=max(
            0, int(getattr(args, "passenger_top_k_destinations", 0) or 0)
        ),
        dedup=bool(getattr(args, "dedup", False)),
        dedup_buffer=float(getattr(args, "dedup_buffer", 500.0) or 0.0),
        dedup_threshold=float(getattr(args, "dedup_threshold", 0.70) or 0.0),
        dedup_passes=max(1, int(getattr(args, "dedup_passes", 1))),
        dedup_unique_km=max(0.0, float(getattr(args, "dedup_unique_km", 0.0) or 0.0)),
        dedup_cache=bool(getattr(args, "dedup_cache", True)),
        dedup_cache_dir=getattr(args, "dedup_cache_dir", None),
        dedup_approx=bool(getattr(args, "dedup_approx", False)),
        dedup_approx_step=getattr(args, "dedup_approx_step", None),
        dedup_approx_margin=max(0.0, float(getattr(args, "dedup_approx_margin", 0.0) or 0.0)),
        dedup_profile=getattr(args, "dedup_profile", "exact") or "exact",
        dedup_unique_net=bool(getattr(args, "dedup_unique_net", True)),
        heatmap=bool(getattr(args, "heatmap", False)),
        heat_cell=float(getattr(args, "heat_cell", 0.1) or 0.0),
        heat_alpha=getattr(args, "heat_alpha", "ff") or "ff",
        heat_gamma=float(getattr(args, "heat_gamma", 0.6) or 0.0),
        heat_top=float(getattr(args, "heat_top", 35.0) or 0.0),
        heat_max_height=float(getattr(args, "heat_max_height", 400.0) or 0.0),
        heat_flat=bool(getattr(args, "heat_flat", False)),
        heat_smooth=bool(getattr(args, "heat_smooth", False)),
        ideas=bool(getattr(args, "ideas", False)),
        ideas_max_pages=getattr(args, "ideas_max_pages", None),
        ideas_search=getattr(args, "ideas_search", None),
        ideas_sort=getattr(args, "ideas_sort", None),
        osm_routes=bool(getattr(args, "osm_routes", False)),
        no_cache=bool(getattr(args, "no_cache", False)),
        refresh=bool(getattr(args, "refresh", False)),
        cache_dir=str(getattr(args, "cache_dir", DEFAULT_CACHE_DIR) or DEFAULT_CACHE_DIR),
    )

    if config.no_cache and config.refresh:
        raise CliConfigError("--no-cache и --refresh нельзя использовать вместе")
    if config.map_type is not None and config.map_type not in set(RouteType):
        raise CliConfigError("Некорректный --map-type")
    if config.dedup_profile not in {"exact", "fast"}:
        raise CliConfigError("--dedup-profile должен быть exact или fast")
    return config


__all__ = [
    "CliConfig",
    "CliConfigError",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_DATA_DIR",
    "DEFAULT_ORCHESTRATOR_DIR",
    "build_cli_config",
]
