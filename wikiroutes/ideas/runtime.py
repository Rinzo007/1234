"""Идеи: загрузка и преобразование идей в маршруты (высокоуровневый API)."""
import concurrent.futures
import re
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from ..cache import JsonCache
from ..constants import BASE_URL
from ..enums import RouteType
from ..http_client import SessionProvider
from ..models import Direction, RouteData, Stop
from .parse import (
    IDEA_GEO_CACHE_VER,
    _extract_idea_description,
    _extract_json_array,
    _fetch_idea_geometry_worker,
    _parse_lng_lat_pairs,
    fetch_idea_geometry,
    logger,
    parse_idea_rating,
)

__all__ = [
    "IDEA_GEO_CACHE_VER",
    "_extract_idea_description",
    "_extract_json_array",
    "_fetch_idea_geometry_worker",
    "_parse_lng_lat_pairs",
    "fetch_idea_geometry",
    "ideas_to_routes",
    "load_ideas",
    "logger",
    "parse_idea_rating",
]

def load_ideas(
    city_slug: str,
    cache: JsonCache,
    session_provider: SessionProvider,
    search: str | None = None,
    sort: str | None = None,
    max_pages: int | None = None,
    delay: float = 1.5,
) -> list[dict[str, Any]]:
    if not city_slug:
        logger.warning("Не указан slug города для загрузки идей")
        return []

    session = session_provider.get()
    terms = [term.strip() for term in str(search or "").split(",") if term.strip()]
    queries: list[str | None] = [term for term in terms]
    if not queries:
        queries = [None]

    all_ideas: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    def load_one(query: str | None) -> None:
        page = 1
        empty_count = 0
        cached_pages = 0
        stale_pages = 0
        # Лимит подряд идущих сетевых неудач на одной странице:
        # без него при стабильной ошибке сервера цикл page > 1
        # не увеличивает page и работает вечно.
        max_page_failures = 3
        page_failures = 0

        while True:
            if max_pages and page > max_pages:
                logger.info("Достигнут лимит %d страниц", max_pages)
                break

            page_key = f"{city_slug}_s={query or ''}_o={sort or ''}_p={page}"
            page_ideas: list[dict[str, Any]] | None = None
            from_cache = False

            cached_page = cache.get("idea_list", page_key)
            if cached_page is not None:
                if (cached_page.get("search") or None) == (query or None) and (
                    cached_page.get("sort") or None
                ) == (sort or None):
                    page_ideas = cached_page.get("ideas", [])
                    from_cache = True
                    cached_pages += 1
                else:
                    stale_pages += 1

            if page_ideas is None:
                params = {"page": str(page)}
                if query:
                    params["search"] = query
                if sort:
                    params["sort"] = sort

                try:
                    response = session.get(
                        f"{BASE_URL}/{city_slug}/idea",
                        params=params,
                        timeout=40,
                    )
                    if response.status_code == 404:
                        break
                    response.raise_for_status()

                    soup = BeautifulSoup(response.text, "html.parser")
                    page_ideas = []

                    for table in soup.find_all("table"):
                        for tr in table.find_all("tr"):
                            tds = tr.find_all("td")
                            if len(tds) < 6:
                                continue

                            match = re.search(
                                r"(\d+)", tds[0].get_text(" ", strip=True)
                            )
                            idea_id = int(match.group(1)) if match else None

                            anchor = tds[1].find("a", href=True)
                            href = anchor["href"] if anchor else None
                            if isinstance(href, str) and not href.startswith("http"):
                                href = urljoin(BASE_URL, href)

                            # Ссылка на идею должна вести на /idea/<id>. Если вместо
                            # этого указан обычный маршрут (?routes=<id>), это не
                            # идея, и собирать её как идею нельзя (иначе в выводе
                            # появляется «Идея» со ссылкой на обычный маршрут).
                            is_idea_link = bool(
                                href
                                and "/idea" in urlparse(href).path
                                and "routes" not in parse_qs(urlparse(href).query)
                            )

                            rating, voters = parse_idea_rating(
                                tds[5].get_text(" ", strip=True)
                            )

                            page_ideas.append(
                                {
                                    "id": idea_id,
                                    "title": tds[1].get_text(" ", strip=True),
                                    "url": href
                                    or (
                                        f"{BASE_URL}/idea/{idea_id}"
                                        if idea_id
                                        else None
                                    ),
                                    "city": tds[2].get_text(" ", strip=True),
                                    "author": tds[3].get_text(" ", strip=True),
                                    "date": tds[4].get_text(" ", strip=True),
                                    "rating": rating,
                                    "voters": voters,
                                    "is_idea_link": is_idea_link,
                                }
                            )

                    cache.put(
                        "idea_list",
                        page_key,
                        {
                            "search": query,
                            "sort": sort,
                            "ideas": page_ideas,
                        },
                    )

                except Exception as exc:  # noqa: BLE001 — устойчивость пагинации к сетевым/парсинговым сбоям
                    logger.warning("Ошибка на странице %d: %s", page, exc)
                    if page > 1:
                        page_failures += 1
                        if page_failures >= max_page_failures:
                            logger.error(
                                "Страница %d недоступна после %d попыток — "
                                "пагинация остановлена",
                                page,
                                page_failures,
                            )
                            break
                        time.sleep(delay * 2)
                        continue
                    page_ideas = []

            if not page_ideas:
                empty_count += 1
                if empty_count >= 3:
                    break
            else:
                empty_count = 0
                page_failures = 0

            for idea in page_ideas:
                idea_id = idea.get("id")
                if idea_id is None:
                    continue
                if idea_id not in seen_ids:
                    seen_ids.add(idea_id)
                    all_ideas.append(idea)

            logger.info(
                "[%s] стр. %d: %d идей, всего: %d%s",
                query or "все",
                page,
                len(page_ideas),
                len(all_ideas),
                " (кэш)" if from_cache else "",
            )

            page += 1
            if not from_cache:
                time.sleep(delay)

        if stale_pages:
            logger.info(
                "Сброшен кэш: %d стр. (изменился запрос/формат)",
                stale_pages,
            )
        if cached_pages:
            logger.info("Страниц из кэша: %d", cached_pages)

    if len(queries) > 1:
        logger.info(
            "Поиск по %d словам: %s",
            len(queries),
            ", ".join(q for q in queries if q is not None),
        )

    for query in queries:
        logger.info(
            "Загрузка идей для %s%s...",
            city_slug,
            f" (поиск: {query})" if query else "",
        )
        load_one(query)

    if not all_ideas:
        logger.warning("Идеи не найдены")
    else:
        logger.info("Загружено %d идей (без дублей)", len(all_ideas))

    return all_ideas

def ideas_to_routes(
    ideas: list[dict[str, Any]],
    cache: JsonCache,
    session_provider: SessionProvider,
) -> list[RouteData]:
    routes: list[RouteData] = []
    cached_count = 0

    logger.info("Загрузка геометрии для %d идей...", len(ideas))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for i, result in enumerate(
            pool.map(
                lambda idea: _fetch_idea_geometry_worker(
                    idea,
                    cache,
                    session_provider,
                ),
                ideas,
            ),
            start=1,
        ):
            if result is None:
                continue

            (
                idea,
                idea_id,
                coords,
                description,
                raw_stops,
                from_cache,
            ) = result

            if from_cache:
                cached_count += 1

            # Ссылка на идею отсутствует или указывает на обычный маршрут —
            # такой элемент из списка идей не превращаем в маршрут.
            # Для ранее закэшированных страниц признак отсутствует — считаем
            # их настоящими идеями (is_idea_link=True), чтобы не терять данные.
            if not idea.get("is_idea_link", True):
                logger.warning(
                    "Пропускаем элемент %s — ссылка не на идею", idea_id
                )
                continue

            # Сохраняем описание в исходной записи идеи, чтобы оно было
            # доступно вызывающему коду даже несмотря на отсутствие поля
            # description в RouteData.
            idea["description"] = description

            # У идеи нет достоверного типа транспорта — используем отдельный
            # тип «Идея», чтобы не путать предложения с реальными маршрутами.
            route_type = RouteType.IDEA

            stops: list[Stop] = []
            for raw_stop in raw_stops:
                stop = Stop.from_api(raw_stop)
                if stop is not None:
                    stops.append(stop)

            directions: list[Direction] = []
            if coords:
                directions.append(
                    Direction(
                        coords=tuple(coords),
                        stops=tuple(stops),
                        name=str(idea.get("title", "")),
                    )
                )

            rating = idea.get("rating")
            voters = idea.get("voters")

            route = RouteData(
                name=str(idea.get("title", "")),
                route_type=route_type,
                route_id=idea_id,
                url=str(idea.get("url") or ""),
                directions=tuple(directions),
                error=None,
                active=True,
                is_idea=True,
                source="idea",
                company=str(idea.get("author", "")),
                price=str(idea.get("city", "")),
                transport_class=(
                    f"{'' if rating is None else rating}"
                    f"({'' if voters is None else voters})"
                ),
            )

            routes.append(route)

            status = (
                f"✅ {len(coords)} точек, {len(stops)} ост."
                if coords
                else "⚠ нет геометрии"
            )
            if from_cache:
                status += " (кэш)"

            logger.info("[%d/%d] %s", i, len(ideas), status)

    geo_count = sum(1 for route in routes if route.directions)
    logger.info(
        "Загружено геометрии для %d/%d идей (из кэша: %d)",
        geo_count,
        len(routes),
        cached_count,
    )

    return routes

