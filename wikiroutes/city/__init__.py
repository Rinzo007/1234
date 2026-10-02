"""Модуль города: пакет данных спроса, файлы выпуска и проверки приёма.

Отдельно от ``overture``, потому что город — не то же самое, что источник его
данных. Overture даёт здания, места и дорожную сеть; GHSL даёт население; а
город — это то, что из них собрано, с инвариантами и проверками приёма (§33.1:
``city`` отвечает за «загрузку пакета, санитацию, инварианты, зонирование и
рубрику качества»).

## Что проверяется до попадания города в список

Десять проверок §18.5. Из них четыре — про согласованность спроса, а не про
наличие файлов: город может содержать все файлы и при этом считаться неверно.

## Границы модуля

``city`` не знает о расчёте пассажиров и не знает о сети игрока. Он читает и
пишет данные города и проверяет их. Расчёт читает то, что этот модуль собрал, —
но в обратную сторону зависимости нет: город не зависит от того, кто его читает.
"""

from __future__ import annotations

from .checks import (
    CHECK_NAMES,
    CheckReport,
    CheckResult,
    check_config,
    check_demand_point_spacing,
    check_file,
    check_phantom_points,
    check_residents_match,
    check_version_matches_tag,
    run_checks,
)
from .demand import (
    DEMAND_DATA_FILENAME,
    DemandData,
    DemandRecord,
    Pop,
    build_pops,
    to_demand_points,
)
from .distance import (
    EARTH_RADIUS_M,
    haversine_m,
    nearest_neighbour_distances_m,
)
from .network import (
    INTERVAL_FROM_CATALOG,
    INTERVAL_FROM_MODE_DEFAULT,
    INTERVAL_NONE,
    RouteInterval,
    TransitNetwork,
    headways_for,
    interval_for,
    load_transit_network,
    network_report,
    parse_catalog_intervals,
)
from .package import (
    BUILDINGS_INDEX_FILENAME,
    CITY_SCHEMA_VERSION,
    CONFIG_FILENAME,
    DEMAND_SCHEMA_VERSION,
    RELEASE_EXTENSIONS,
    ROADS_FILENAME,
    RUBRIC_FILENAME,
    RUNWAYS_FILENAME,
    SLUG_PATTERN,
    VERSION_PATTERN,
    CityConfig,
    buildings_index,
    package_filename,
    roads_geojson,
    rubric_document,
    validate_slug,
    validate_version,
    write_release,
)

__all__ = [
    "BUILDINGS_INDEX_FILENAME",
    "CHECK_NAMES",
    "CITY_SCHEMA_VERSION",
    "CONFIG_FILENAME",
    "DEMAND_DATA_FILENAME",
    "DEMAND_SCHEMA_VERSION",
    "EARTH_RADIUS_M",
    "INTERVAL_FROM_CATALOG",
    "INTERVAL_FROM_MODE_DEFAULT",
    "INTERVAL_NONE",
    "RELEASE_EXTENSIONS",
    "ROADS_FILENAME",
    "RUBRIC_FILENAME",
    "RUNWAYS_FILENAME",
    "SLUG_PATTERN",
    "VERSION_PATTERN",
    "CheckReport",
    "CheckResult",
    "CityConfig",
    "DemandData",
    "DemandRecord",
    "Pop",
    "RouteInterval",
    "TransitNetwork",
    "build_pops",
    "buildings_index",
    "check_config",
    "check_demand_point_spacing",
    "check_file",
    "check_phantom_points",
    "check_residents_match",
    "check_unreachable_destinations",
    "check_version_matches_tag",
    "haversine_m",
    "headways_for",
    "interval_for",
    "load_transit_network",
    "nearest_neighbour_distances_m",
    "network_report",
    "package_filename",
    "parse_catalog_intervals",
    "roads_geojson",
    "rubric_document",
    "run_checks",
    "to_demand_points",
    "validate_slug",
    "validate_version",
    "write_release",
]
