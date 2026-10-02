"""Настройки Overture и обратная совместимость со старым API."""

from .config import OvertureConfig

# Один объект-конфигурация является источником истины для нового кода.
DEFAULT_CONFIG = OvertureConfig.from_env()

# Старые имена оставлены как совместимые алиасы. Внутри pipeline они больше
# не используются: фактическая конфигурация приходит через OvertureConfig.
OVERTURE_CACHE_VERSION = DEFAULT_CONFIG.cache_version

OVERTURE_THEME_ALIASES = {
    "places": "place",
    "buildings": "building",
    "transportation": "segment",
    "address": "address",
    "infrastructure": "infrastructure",
    "division": "division",
    "division_area": "division_area",
    "division_boundary": "division_boundary",
    "connector": "connector",
}
