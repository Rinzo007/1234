import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from .cache import JsonCache
from .constants import BASE_URL
from .enums import RouteType
from .errors import CatalogLoadError
from .models import Catalog, CatalogSection, RouteLink

logger = logging.getLogger("wikiroutes.catalog")


def city_from_url(url: str) -> str:
    try:
        parts = [part for part in unquote(urlparse(url).path).split("/") if part]

        if not parts or parts[0].lower() == "catalog":
            return ""

        return parts[0].lower()
    except Exception:
        logger.debug("city_from_url failed for %s", url, exc_info=True)
        return ""


def make_catalog_url(city: str, url: str | None = None) -> str:
    if url:
        url = url.strip()

        if not url.lower().startswith(("http://", "https://")):
            url = f"{BASE_URL}/{url.strip('/')}"

        return url if "/catalog" in url else url.rstrip("/") + "/catalog"

    return f"{BASE_URL}/{city.strip('/')}/catalog"


# Порядок правил значим: более специфичные типы проверяются раньше общих
# (например, «электробус» до «автобус», «троллейбус» до «трамвай»).
_SECTION_MARKERS: tuple[tuple[tuple[str, ...], RouteType], ...] = (
    (("метро", "metro", "subway", "underground"), RouteType.METRO),
    (("фуникул", "funicular"), RouteType.FUNICULAR),
    (
        ("канат", "cable", "ropeway", "гондол", "подвесн", "кресельн"),
        RouteType.CABLE,
    ),
    # До TRAIN: «monorail» содержит подстроку «rail» и иначе попал бы в поезда.
    (("монорельс", "monorail"), RouteType.MONORAIL),
    (("поезд", "электрич", "ж/д", "rail", "train", "пригород"), RouteType.TRAIN),
    (("троллейбус", "trolleybus"), RouteType.TROLLEYBUS),
    (("трамва", "tram"), RouteType.TRAM),
)

_WATER_MARKERS: tuple[str, ...] = (
    "водн",
    "катер",
    "теплоход",
    "речн",
    "паром",
    "причал",
    "water",
    "ferry",
    "boat",
    "ship",
)


def classify_section(title: str) -> RouteType | None:
    """Сопоставляет заголовок секции каталога типу транспорта."""
    text = title.lower()

    for markers, route_type in _SECTION_MARKERS:
        if any(marker in text for marker in markers):
            return route_type

    if any(marker in text for marker in _WATER_MARKERS):
        return RouteType.WATER

    if "автобус" in text or ("bus" in text and "trolley" not in text):
        return RouteType.BUS

    if any(marker in text for marker in ("маршрут", "minibus", "marshrut")):
        return RouteType.BUS

    return None


@dataclass(frozen=True, slots=True)
class _SectionScan:
    """Результат разбора секций каталога до фильтрации по городу."""

    entries: dict[tuple[str, RouteType], list[tuple[str, int, str]]]
    slug_counts: dict[str, int]
    unrecognized: tuple[str, ...]


def _load_catalog_html(
    catalog_url: str,
    session: requests.Session,
    cache: JsonCache,
) -> tuple[str, str]:
    """HTML каталога и финальный URL; кэш → сеть."""
    cached = cache.get("catalog", catalog_url)

    if isinstance(cached, dict) and isinstance(cached.get("html"), str):
        return cached["html"], str(cached.get("url") or catalog_url)

    try:
        response = session.get(catalog_url, timeout=60.0)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CatalogLoadError(f"Cannot load catalog: {exc}") from exc

    html = response.text
    final_url = response.url

    cache.put(
        "catalog",
        catalog_url,
        {
            "url": final_url,
            "html": html,
        },
    )

    return html, final_url


def _extract_city_title(
    soup: BeautifulSoup,
    actual_city: str,
    fallback_city: str,
) -> str:
    """Заголовок города: h1 → title → город из URL → fallback."""
    city_title = actual_city or fallback_city

    h1 = soup.find("h1")

    if h1 and h1.get_text(strip=True):
        city_title = h1.get_text(strip=True)
    elif soup.title and soup.title.get_text(strip=True):
        city_title = soup.title.get_text(strip=True)

    return re.sub(r"\s+", " ", city_title).strip()


def _link_city(parts: list[str], default: str) -> str:
    """Город из пути ссылки каталога (первый сегмент, кроме 'catalog')."""
    for part in parts:
        if part.lower() != "catalog":
            return part.lower()

    return default


def _parse_section_href(href: str, actual_city: str) -> tuple[str, int] | None:
    """Разбирает ссылку маршрута в ``(город, id)``; ``None``, если не подходит."""
    parsed = urlparse(href)
    query = parse_qs(parsed.query)

    if "routes" not in query:
        return None

    route_id = int(query["routes"][0])
    parts = [part for part in unquote(parsed.path).split("/") if part]

    return _link_city(parts, actual_city), route_id


def _scan_sections(soup: BeautifulSoup, actual_city: str) -> _SectionScan:
    """Собирает записи секций, счётчики городов и нераспознанные заголовки."""
    entries: dict[tuple[str, RouteType], list[tuple[str, int, str]]] = {}
    slug_counts: dict[str, int] = {}
    unrecognized: list[str] = []

    for header in soup.find_all("div", class_="typeHeader"):
        name_span = header.find("span", class_="typeHeader-name")

        if not name_span:
            continue

        title = name_span.get_text(strip=True)
        route_type = classify_section(title)

        if route_type is None:
            unrecognized.append(title)
            continue

        block = header.find_next_sibling("div", class_="wikiCatalogFilterListItem")

        if not block:
            continue

        for anchor in block.find_all("a", href=True):
            try:
                link = _parse_section_href(str(anchor["href"]), actual_city)

                if link is None:
                    continue

                link_city, route_id = link
                name = anchor.get_text(strip=True)

                entries.setdefault((title, route_type), []).append(
                    (link_city, route_id, name)
                )
                slug_counts[link_city] = slug_counts.get(link_city, 0) + 1
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping catalog link: %s", exc)

    return _SectionScan(entries, slug_counts, tuple(unrecognized))


def _dominant_city(actual_city: str, slug_counts: dict[str, int]) -> str:
    """Город каталога: если фактический не встречается в ссылках — частотный лидер."""
    if actual_city in slug_counts or not slug_counts:
        return actual_city

    return max(slug_counts.items(), key=lambda item: item[1])[0]


def _build_sections(
    entries: dict[tuple[str, RouteType], list[tuple[str, int, str]]],
    city_slug: str,
) -> tuple[CatalogSection, ...]:
    """Фильтрует записи по городу и собирает секции каталога."""
    sections: list[CatalogSection] = []

    for (title, route_type), section_entries in entries.items():
        links = tuple(
            RouteLink(name=name, route_id=route_id)
            for slug, route_id, name in section_entries
            if slug == city_slug
        )

        if links:
            sections.append(CatalogSection(title=title, route_type=route_type, links=links))

    return tuple(sections)


def load_catalog(
    catalog_url: str,
    fallback_city: str,
    session: requests.Session,
    cache: JsonCache,
) -> Catalog:
    html, final_url = _load_catalog_html(catalog_url, session, cache)

    actual_city = city_from_url(final_url) or fallback_city
    soup = BeautifulSoup(html, "html.parser")

    city_title = _extract_city_title(soup, actual_city, fallback_city)
    scan = _scan_sections(soup, actual_city)
    dominant_city = _dominant_city(actual_city, scan.slug_counts)

    return Catalog(
        sections=_build_sections(scan.entries, dominant_city),
        city_title=city_title,
        city_slug=dominant_city,
        unrecognized=scan.unrecognized,
    )
