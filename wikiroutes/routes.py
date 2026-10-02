import contextlib
import logging
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypeVar

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from .cache import JsonCache
from .constants import API_HEADERS_EXTRA, BASE_URL, MAX_RETRIES, REQUEST_TIMEOUT
from .decoding import decode_obj
from .enums import RouteType, parse_route_type, type_label
from .http_client import SessionProvider
from .models import Direction, RouteData, RouteTask
from .parsers import parse_route_payload
from .route_meta import extract_metadata

logger = logging.getLogger("wikiroutes.routes")

# Транзитные сетевые сбои, при которых имеет смысл повторить запрос:
# обрыв соединения (в т.ч. неполное чтение тела — IncompleteRead) и таймаут.
_TRANSIENT_ERRORS = (RequestsConnectionError, RequestsTimeout)


def _is_bus_section(title: str) -> bool:
    """Истинно для заголовков секций каталога, обозначающих автобусы
    (включая «Автобусы»), но не маршрутки/электрички и прочее."""
    text = title.lower()
    return "автобус" in text or "bus" in text


def _electrobus_route_type(task: RouteTask, is_electrobus: bool) -> RouteType:
    """Возвращает тип маршрута с учётом электробуса.

    Электробус получает отдельный тип (и, значит, отдельный цвет в KML) только
    когда пришёл из раздела автобусов; в остальных разделах каталога он
    остаётся обычным автобусом, чтобы не «размазывался» по другим видам
    транспорта.
    """
    if is_electrobus and task.route_type == RouteType.BUS and _is_bus_section(task.section_title):
        return RouteType.ELECTROBUS
    return task.route_type

_T = TypeVar("_T")


def _retry_or_raise(attempt: int, exc: Exception, label: str) -> None:
    """На последней попытке пробрасывает сбой, иначе ждёт перед повтором."""
    if attempt == MAX_RETRIES:
        raise exc
    delay = 0.5 * (2 ** attempt)
    logger.warning(
        "Повтор %s (попытка %d/%d): %s",
        label,
        attempt + 1,
        MAX_RETRIES + 1,
        exc,
    )
    time.sleep(delay)


def _with_retry(operation: Callable[[], _T], *, label: str) -> _T:
    """Выполняет ``operation`` с повторами при транзитных сетевых сбоях.

    Повторяет вызов до ``MAX_RETRIES`` раз при ``ConnectionError``/``Timeout``
    (например, ``IncompleteRead`` при обрыве ответа сервера) с экспоненциальной
    задержкой. Прочие исключения пробрасываются сразу.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            return operation()
        except _TRANSIENT_ERRORS as exc:
            _retry_or_raise(attempt, exc, label)


def extract_token(html: str) -> str | None:
    """Извлекает токен WikiRoutes из HTML страницы каталога маршрута."""
    match = re.search(r"tk:\s*['\"]([a-f0-9-]+)['\"]", html)
    return match.group(1) if match else None


def _cached_token(cache: JsonCache, cache_key: str) -> str | None:
    """Достаёт сохранённый токен маршрута (одно сетевое обращение меньше)."""
    entry = cache.get("route_token", cache_key)

    if isinstance(entry, dict):
        token = entry.get("token")

        if isinstance(token, str) and token:
            return token

    return None


@dataclass(slots=True)
class _FetchContext:
    """Состояние сетевой загрузки одного маршрута."""

    task: RouteTask
    session: Any
    cache: JsonCache


def _route_url(task: RouteTask) -> str:
    """URL страницы каталога маршрута."""
    return f"{BASE_URL}/{task.city}?routes={task.route_id}"


def _task_cache_key(task: RouteTask) -> str:
    """Ключ кэша маршрута/токена."""
    return f"{task.city}:{task.route_id}"


def _api_headers(token: str | None, url: str) -> dict[str, str]:
    """Заголовки API-запроса маршрута."""
    return API_HEADERS_EXTRA | {
        "X-WR-T": token,
        "Referer": url,
    }


def _fetch_api_payload(ctx: _FetchContext, token: str) -> Any:
    """Выполняет API-запрос; бросает ``HTTPError`` при неуспехе.

    Транзитные сетевые сбои (обрыв соединения, ``IncompleteRead``) повторяются.
    """
    headers = _api_headers(token, _route_url(ctx.task))

    def _call() -> Any:
        api_response = ctx.session.get(
            f"{BASE_URL}/api/wr/route/{ctx.task.route_id}",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if api_response.status_code != 200:
            body = (api_response.text or "")[:150].replace("\n", " ")
            raise requests.HTTPError(f"API {api_response.status_code}: {body}")
        # Чтение тела здесь: IncompleteRead проявляется при декодировании JSON.
        return decode_obj(api_response.json())

    return _with_retry(_call, label=f"route {ctx.task.route_id} api")


def _fetch_page_text(ctx: _FetchContext) -> str:
    """Загружает страницу маршрута.

    Транзитные сетевые сбои повторяются.
    """

    def _call() -> str:
        page = ctx.session.get(_route_url(ctx.task), timeout=REQUEST_TIMEOUT)
        page.raise_for_status()
        return page.text

    return _with_retry(_call, label=f"route {ctx.task.route_id} page")


def _fresh_token_from_page(ctx: _FetchContext) -> str | None:
    """Загружает страницу маршрута и извлекает свежий токен."""
    return extract_token(_fetch_page_text(ctx))


def _fetch_payload_with_token_refresh(
    ctx: _FetchContext, token: str
) -> Any:
    """Запрашивает payload API.

    При 403 ``permission.denied`` кэшированный токен мог устареть —
    обновляет токен со страницы маршрута и повторяет запрос один раз.
    """
    try:
        return _fetch_api_payload(ctx, token)
    except requests.HTTPError as exc:
        text = str(exc).lower()
        if "403" not in text and "permission" not in text:
            raise
        fresh = _fresh_token_from_page(ctx)
        if not fresh or fresh == token:
            raise
        ctx.cache.put("route_token", _task_cache_key(ctx.task), {"token": fresh})
        return _fetch_api_payload(ctx, fresh)


def _token_with_page(ctx: _FetchContext) -> tuple[str | None, str | None]:
    """Возвращает ``(токен, page_text)``; ``page_text=None`` при токене из кэша."""
    token = _cached_token(ctx.cache, _task_cache_key(ctx.task))
    if token:
        return token, None
    page_text = _fetch_page_text(ctx)
    token = extract_token(page_text)
    if token:
        ctx.cache.put("route_token", _task_cache_key(ctx.task), {"token": token})
    return token, page_text


def _route_base(task: RouteTask) -> dict[str, Any]:
    """Базовые поля RouteData из задачи."""
    return {
        "name": task.name,
        "route_type": task.route_type,
        "route_id": task.route_id,
        "url": f"{BASE_URL}/{task.city}?routes={task.route_id}",
    }


def _route_from_cached_payload(
    cache: JsonCache,
    task: RouteTask,
) -> RouteData | None:
    """Собирает RouteData из кэша; ``None``, если записи нет или она битая."""
    cached = cache.get("route", f"{task.city}:{task.route_id}")

    if cached is None:
        return None

    parsed = parse_route_payload(cached)

    if parsed is None:
        return None

    base = _route_base(task)

    if extract_metadata(cached).is_electrobus:
        base["route_type"] = _electrobus_route_type(task, True)

    return RouteData(
        **base,
        directions=parsed.directions,
        cached=True,
        price=parsed.price,
        company=parsed.company,
        active=parsed.active,
        transport_class=parsed.transport_class,
    )


def _build_route_data(
    ctx: _FetchContext,
    payload: Any,
    page_text: str | None,
) -> RouteData:
    """Собирает RouteData из payload (метаданные, кэш, тип электробуса)."""
    base = _route_base(ctx.task)
    if not isinstance(payload, dict):
        return RouteData(**base, error="no geometry")

    meta = extract_metadata(payload, page_text)
    if not meta.transport_class and page_text is None:
        with contextlib.suppress(requests.RequestException, ValueError):
            meta = extract_metadata(payload, _fetch_page_text(ctx))

    payload["transport_class"] = meta.transport_class
    payload["is_electrobus"] = meta.is_electrobus

    parsed = parse_route_payload(payload)
    if parsed is None:
        return RouteData(**base, error="no geometry")

    ctx.cache.put("route", _task_cache_key(ctx.task), payload)

    if meta.is_electrobus:
        base["route_type"] = _electrobus_route_type(ctx.task, True)

    return RouteData(
        **base,
        directions=parsed.directions,
        price=parsed.price,
        company=parsed.company,
        active=parsed.active,
        transport_class=parsed.transport_class,
    )


def fetch_route_network(
    city: str,
    route_type: str,
    name: str,
    route_id: int,
    sessions: SessionProvider,
    cache: JsonCache,
    section_title: str = "",
) -> RouteData:
    """Загружает маршрут из сети с кэшированием токена и payload.

    Ошибки HTTP, декодирования и разбора возвращаются в ``RouteData.error``.
    """
    task = RouteTask(
        city=city,
        route_type=parse_route_type(route_type),
        name=name,
        route_id=route_id,
        section_title=section_title,
    )
    base = _route_base(task)
    ctx = _FetchContext(task=task, session=sessions.get(), cache=cache)

    try:
        token, page_text = _token_with_page(ctx)
        if not token:
            return RouteData(**base, error="token not found")

        payload = _fetch_payload_with_token_refresh(ctx, token)

        if payload is None:
            # Пустой ответ (например, устаревший токен) — пробуем ещё раз
            # со свежей страницей и свежим токеном.
            page_text = _fetch_page_text(ctx)
            token = extract_token(page_text)
            if not token:
                return RouteData(**base, error="token not found")
            ctx.cache.put("route_token", _task_cache_key(task), {"token": token})
            payload = _fetch_payload_with_token_refresh(ctx, token)

        return _build_route_data(ctx, payload, page_text)

    except (requests.RequestException, ValueError) as exc:
        logger.warning("Route %s failed: %s", route_id, exc)
        return RouteData(**base, error=str(exc))


class RouteFetcher(Protocol):
    """Интерфейс загрузчика маршрута: кэш и/или сеть.

    ``is_cached`` позволяет вызывающему коду выбирать стратегию
    параллельности без сетевых запросов.
    """

    def is_cached(self, task: RouteTask) -> bool:
        """True, если маршрут доступен целиком из кэша."""
        ...

    def fetch(self, task: RouteTask) -> RouteData:
        """Возвращает ``RouteData`` по задаче, включая записи с ошибками."""
        ...


class CachedNetworkRouteFetcher:
    """Реализация ``RouteFetcher``: кэш → сеть."""

    def __init__(self, cache: JsonCache, sessions: SessionProvider) -> None:
        self._cache = cache
        self._sessions = sessions

    def is_cached(self, task: RouteTask) -> bool:
        """True, если маршрут доступен целиком из кэша."""
        return (
            self._cache.get("route", f"{task.city}:{task.route_id}") is not None
        )

    def fetch(self, task: RouteTask) -> RouteData:
        """Возвращает маршрут из кэша или загружает из сети."""
        cached = _route_from_cached_payload(self._cache, task)

        if cached is not None:
            return cached

        return fetch_route_network(
            task.city,
            task.route_type.value,
            task.name,
            task.route_id,
            self._sessions,
            self._cache,
            section_title=task.section_title,
        )


def _unique_tasks(tasks: Sequence[RouteTask]) -> list[RouteTask]:
    """Возвращает задачи без дубликатов по (город, тип, ID)."""
    seen: set[tuple[str, object, int]] = set()
    unique: list[RouteTask] = []
    for task in tasks:
        key = (task.city, task.route_type, int(task.route_id))
        if key in seen:
            continue
        seen.add(key)
        unique.append(task)
    return unique


def _log_route_progress(
    done: int, total: int, task: RouteTask, route: RouteData
) -> None:
    """Пишет прогресс загрузки одного маршрута."""
    status = route.error if route.error else f"{route.min_km:.1f} km"
    logger.info(
        "[%d/%d] %s %s: %s",
        done,
        total,
        type_label(task.route_type),
        task.name,
        status,
    )


def download_routes(
    tasks: Sequence[RouteTask],
    workers: int,
    fetcher: RouteFetcher,
) -> list[RouteData]:
    """Параллельно загружает маршруты из набора задач через ``fetcher``.

    Возвращает один ``RouteData`` на каждый уникальный маршрут, включая записи
    с ошибками загрузки. Повторяющиеся задачи с тем же городом, типом и ID
    игнорируются. ``workers`` ограничивает число рабочих потоков.
    """
    routes: list[RouteData] = []

    if not tasks:
        return routes

    # Один маршрут может встретиться в каталоге несколько раз. Убираем
    # дубликаты до создания futures, чтобы не выполнять повторную загрузку
    # и не добавлять одинаковый RouteData в результат.
    unique_tasks = _unique_tasks(tasks)
    total = len(unique_tasks)

    # Если весь кэш уже прогрет, сетевой конкуренции нет — пул потоков
    # только добавляет оверхед на создание потоков.
    if all(fetcher.is_cached(task) for task in unique_tasks):
        for task in unique_tasks:
            route = fetcher.fetch(task)
            routes.append(route)
            _log_route_progress(len(routes), total, task, route)
        return routes

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(fetcher.fetch, task): task for task in unique_tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                route = future.result()
            except Exception as exc:
                logger.exception("Unexpected error while fetching %s", task.name)
                route = RouteData(
                    name=task.name,
                    route_type=task.route_type,
                    route_id=task.route_id,
                    url=_route_url(task),
                    error=str(exc),
                )
            routes.append(route)
            _log_route_progress(len(routes), total, task, route)

    return routes
