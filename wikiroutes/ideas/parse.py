"""Идеи: парсинг рейтингов и загрузка геометрии идей (низкий уровень)."""
import json
import logging
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

from ..cache import JsonCache
from ..constants import BASE_URL
from ..http_client import SessionProvider

logger = logging.getLogger("wikiroutes.ideas")

IDEA_GEO_CACHE_VER = "v3"


def parse_idea_rating(text: str | None) -> tuple[float | None, int | None]:
    text = (text or "").strip()

    if not text or text == "—":
        return None, None

    match = re.match(r"(-?\d+(?:[.,]\d+)?)\s*(?:\((\d+)\))?", text)
    if not match:
        return None, None

    rating_str = match.group(1).replace(",", ".")
    return float(rating_str), (int(match.group(2)) if match.group(2) else None)

def _extract_json_array(html: str, start: int, max_len: int = 400000) -> str | None:
    if start >= len(html) or html[start] != "[":
        return None

    depth = 0
    in_str = False
    esc = False
    last = min(len(html), start + max_len)

    for i in range(start, last):
        ch = html[i]

        if in_str:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]

    return None


def _parse_lng_lat_pairs(arr_text: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []

    for lng, lat in re.findall(
        r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]",
        arr_text,
    ):
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except ValueError:
            continue

        if -90 <= lat_f <= 90 and -180 <= lng_f <= 180:
            points.append((lat_f, lng_f))

    return points


def _extract_idea_description(soup: BeautifulSoup) -> str:
    """Извлекает описание идеи из формы редактирования или отображаемого блока."""
    # В режиме редактирования WikiRoutes описание обычно находится в textarea.
    for textarea in soup.find_all("textarea"):
        attrs = " ".join(
            str(textarea.get(attr, ""))
            for attr in ("id", "name", "class", "data-name")
        ).lower()
        if "description" in attrs or "описан" in attrs:
            text = textarea.get_text(" ", strip=True)
            if text:
                return text

    # Запасной вариант: берём textarea с содержимым, если он единственный.
    textareas = [
        textarea.get_text(" ", strip=True)
        for textarea in soup.find_all("textarea")
    ]
    textareas = [text for text in textareas if text]
    if len(textareas) == 1:
        return textareas[0]

    # В обычной версии страницы описание находится после заголовка «Описание».
    for heading in soup.find_all(
        ["h2", "h3", "h4", "div", "span", "label"]
    ):
        if heading.get_text(" ", strip=True).lower() != "описание":
            continue

        for sibling in heading.find_all_next(limit=5):
            text = sibling.get_text(" ", strip=True)
            if text and text.lower() != "описание":
                return text

    return ""


def _parse_idea_coords(html: str) -> list[tuple[float, float]]:
    """Координаты идеи: наибольший блок "coordinates" + дедупликация вершин."""
    coords: list[tuple[float, float]] = []
    for match in re.finditer(r'"coordinates"\s*:\s*\[', html):
        arr = _extract_json_array(html, match.end() - 1)
        if not arr:
            continue

        points = _parse_lng_lat_pairs(arr)
        if len(points) > len(coords):
            coords = points

    if len(coords) > 2 and coords[0] == coords[-1]:
        coords = coords[:-1]

    deduped: list[tuple[float, float]] = []
    for point in coords:
        if not deduped or deduped[-1] != point:
            deduped.append(point)
    return deduped


def _stop_from_obj(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None

    try:
        raw_lat = obj.get("lat")
        raw_lon = obj.get("lng")
        if raw_lat is None or raw_lon is None:
            return None
        lat = float(raw_lat)
        lon = float(raw_lon)
    except (ValueError, TypeError):
        return None

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    return {
        "id": obj.get("id"),
        "latitude": lat,
        "longitude": lon,
        "name": str(obj.get("name", "") or ""),
    }


def _parse_idea_stops(html: str) -> list[dict[str, Any]]:
    stops: list[dict[str, Any]] = []
    stops_match = re.search(r'"stops"\s*:\s*\[', html)

    if not stops_match:
        return stops

    arr = _extract_json_array(html, stops_match.end() - 1)
    if not arr:
        return stops

    try:
        parsed = json.loads(arr)
    except (ValueError, TypeError):
        return stops

    if not isinstance(parsed, list):
        return stops

    for obj in parsed:
        stop = _stop_from_obj(obj)
        if stop is not None:
            stops.append(stop)
    return stops


def fetch_idea_geometry(
    idea_id: int,
    session: requests.Session,
    cache: JsonCache,
) -> tuple[
    list[tuple[float, float]],
    str,
    list[dict[str, Any]],
    bool,
]:
    """Загружает геометрию, название, описание и остановки идеи."""
    cache_key = f"{IDEA_GEO_CACHE_VER}_{idea_id}"
    cached = cache.get("idea_geo", cache_key)

    if cached is not None:
        return (
            cached.get("coords", []),
            cached.get("description", ""),
            cached.get("stops", []),
            True,  # from_cache
        )

    url = f"{BASE_URL}/idea/{idea_id}"

    response = session.get(url, timeout=30)
    response.raise_for_status()

    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    description = _extract_idea_description(soup)

    # Парсинг геометрии и остановок из HTML. Сетевых запросов здесь нет,
    # поэтому requests.RequestException не отлавливаем.
    try:
        coords = _parse_idea_coords(html)
        stops = _parse_idea_stops(html)

        cache.put(
            "idea_geo",
            cache_key,
            {
                "coords": coords,
                "description": description,
                "stops": stops,
            },
        )

    except (ValueError, TypeError, KeyError) as exc:
        logger.warning(
            "Не удалось распарсить данные для идеи %s: %s",
            idea_id,
            exc,
        )
        return [], "", [], False

    return coords, description, stops, False


def _fetch_idea_geometry_worker(
    idea: dict[str, Any],
    cache: JsonCache,
    session_provider: SessionProvider,
) -> (
    tuple[
        dict[str, Any],
        int,
        list[tuple[float, float]],
        str,
        str,
        list[dict[str, Any]],
        bool,
    ]
    | None
):
    """Загружает данные одной идеи в пуле потоков."""
    idea_id = idea.get("id")
    if idea_id is None:
        return None

    session = session_provider.get()
    try:
        coords, description, raw_stops, from_cache = fetch_idea_geometry(
            int(idea_id),
            session,
            cache,
        )
    except Exception as exc:  # noqa: BLE001 — одна упавшая идея не должна ронять весь прогон
        logger.warning(
            "Пропускаем идею %s из-за ошибки загрузки: %s", idea_id, exc
        )
        return None

    return (
        idea,
        int(idea_id),
        coords,
        description,
        raw_stops,
        from_cache,
    )