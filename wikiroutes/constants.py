"""Централизованные константы пакета wikiroutes."""

import os

__all__ = [
    "API_HEADERS_EXTRA",
    "BASE_URL",
    "CITY_ALIASES",
    "DEFAULT_CITY",
    "EARTH_DIAMETER_KM",
    "HEADERS",
    "LAT_SHIFT",
    "LON_SHIFT",
    "MAX_RETRIES",
    "MAX_WORKERS",
    "NOMINATIM_URL",
    "REQUEST_TIMEOUT",
    "ROUTE_CATALOG_BINDINGS",
    "STRAIGHT_EPS",
    "XOR_KEY",
]

# ── OSM (Nominatim) ───────────────────────────────────────────────────────
# Эндпоинт геокодирования для получения административной границы города.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# ── URL и город по умолчанию ─────────────────────────────────────────────
BASE_URL = "https://ru.wikiroutes.info"
DEFAULT_CITY = "voronezh"

# ── HTTP ─────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT = 60.0
MAX_RETRIES = 2
# Автонастройка workers по количеству CPU, можно переопределить через MAX_WORKERS env
MAX_WORKERS = int(os.getenv("MAX_WORKERS", os.cpu_count() or 4))

# ── Алиасы городов ───────────────────────────────────────────────────────
CITY_ALIASES: dict[str, str] = {
    "kyiv": "kiev",
    "київ": "kiev",
    "киев": "kiev",
}

# ── Привязка каталога маршрутов ─────────────────────────────────────────
# {город запуска: город-донор}: маршруты загружаются из каталога города-донора,
# а граница/ГИС/наименование остаются у города запуска. Пусто — без привязки.
# Можно переопределить на прогон через env WIKIROUTES_ROUTE_CATALOG.
ROUTE_CATALOG_BINDINGS: dict[str, str] = {}

# ── Заголовки HTTP ───────────────────────────────────────────────────────
# HEADERS применяются ко ВСЕМ хостам (wikiroutes, OSM, Overture).
# Не содержат секретов — только User-Agent.
HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# ИСПРАВЛЕНО (H-01): убраны лишние пробелы в ключах.
# ИСПРАВЛЕНО (L-01): восстановлено значение Accept с */*.
# ВНИМАНИЕ: если в реальном файле пробелы отсутствуют — это был артефакт вставки.
API_HEADERS_EXTRA: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}

# ── Ключи дешифровки координат (обфускация API, НЕ криптография) ─────────
XOR_KEY: tuple[int, ...] = (
    41,
    17,
    83,
    29,
    61,
    7,
    53,
    97,
    13,
    71,
    37,
    89,
    23,
    67,
    11,
    79,
)

LAT_SHIFT: tuple[float, ...] = (
    5.331,
    6.812,
    9.315,
    0.233,
    5.187,
    4.778,
    4.163,
    5.886,
)

LON_SHIFT: tuple[float, ...] = (
    7.882,
    7.05,
    3.84,
    1.77,
    0.21,
    4.41,
    4.012,
    1.366,
)

# ── Геодезические константы ──────────────────────────────────────────────
# Средний диаметр Земли в км. Используется в geometry.py:
# haversine_km возвращает D * asin(sqrt(a)), что эквивалентно 2*R*asin(sqrt(a)).
EARTH_DIAMETER_KM = 12742.0

# Порог для определения вырожденного отрезка (км).
STRAIGHT_EPS = 1e-6
