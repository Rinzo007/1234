"""Расчёт пассажиропотока по маршрутам пайплайна.

Пассажирский расчёт живёт в конвейере, а не как ручная операция: тот же
пайплайн, тот же город, тот же кэш.

Что здесь принципиально:

* **Интервалы помечены по происхождению.** В каталоге перевозчиков они
  практически отсутствуют, и без пометки число выглядит измеренным, будучи
  построенным на предположении (§17.3).
* **Рабочие места — оценка, а не измерение.** Слой населения их не содержит;
  без слоя застройки OD-матрицу построить не из чего (§5.5).
* **Провал расчёта не молчит.** Если данных не хватило, вызывающий получает
  ``reason``, а не пустой отчёт, похожий на «город без пассажиров».
* **Остановки маршрутов должны быть упорядочены.** Без порядка остановок
  маршрутизатор строит сеть, в которой ехать некуда, и считает это городом.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import CliConfig
from .report import Reporter

#: Стандартные пути к растрам GHSL, если они не заданы явно.
DEFAULT_POP_RELATIVE = Path("GHS") / "rus_pop_2030_CN_100m_R2025A_v1.tif"
DEFAULT_BUILT_RELATIVE = (
    Path("GHS") / "GHS_BUILT_S_E2030_GLOBE_R2023A_54009_100_V1_0.tif"
)

#: Каталог с растрами GHSL по умолчанию. Рабочий каталог уезжает туда, где
#: запустили команду, а данные лежат в проекте; искать только в текущем
#: каталоге значит наказывать за то, откуда запустили.
_PROJECT_GHS_DIR = Path(__file__).resolve().parent.parent / "GHS"


@dataclass
class PassengerFlowResult:
    """Результат расчёта пассажиропотока для XLSX и отчёта.

    ``result`` равно ``None``, если расчёт не состоялся, и тогда ``reason``
    объясняет почему. Молчаливый пустой результат читался бы как «в городе
    никто не ездит», а это противоположно смыслу расчёта.
    """

    result: Any = None
    #: Причина неудачи. По умолчанию заполнена, а не ``None``: результат без
    #: результата и без причины неотличим от «город, где никто не ездит».
    reason: str | None = "расчёт не запускался"
    network_report: Mapping[str, Any] | None = None
    reading_summary: str | None = None
    jobs_summary: str | None = None
    demand_points: int = 0

    @property
    def ok(self) -> bool:
        return self.result is not None


def _resolve_raster(explicit: str | None, env: str, relative: Path) -> Path | None:
    """Ищет **существующий** растр: явный путь, переменная окружения, стандартный.

    Порядок не случаен: явный аргумент — намерение человека, переменная
    окружения — настройка машины, стандартный путь — соглашение о том, где
    лежат данные.

    Существование проверяется и для явно заданного пути тоже. Иначе опечатка в
    ``--passenger-pop`` дошла бы до ``rasterio``, и расчёт упал бы с
    ``RasterioIOError`` из глубины чужой библиотеки — читатель не понял бы,
    что не так: не найден файл или сломан сам расчёт.
    """
    for candidate in _raster_candidates(explicit, env, relative):
        if candidate.is_file():
            return candidate
    return None


def _raster_candidates(
    explicit: str | None, env: str, relative: Path
) -> list[Path]:
    """Возможные пути к растру в порядке приоритета."""
    if explicit:
        return [Path(explicit)]
    from_env = os.getenv(env, "").strip()
    if from_env:
        return [Path(from_env)]
    return [Path.cwd() / relative, _PROJECT_GHS_DIR / relative.name]


def _as_latlon_bbox(bbox: Any) -> Any:
    """Приводит границу города к ``LatLonBBox``.

    Пайплайн отдаёт ``BBox`` — именованный объект с полями ``min_lat`` и
    прочими, а не кортеж. Порядок полей здесь важен дважды: у ``BBox`` это
    (широта, долгота), у ``from_xy`` ожидается (x, y), то есть (долгота,
    широта). Перепутать — значит направить выборку растра в Саудовскую Аравию,
    а это уже не ошибка формата, а тихая порча результата.
    """
    from overture.city import LatLonBBox

    if bbox is None:
        raise ValueError("у города нет границы: считать пассажиров не по чему")
    if isinstance(bbox, LatLonBBox):
        return bbox
    if hasattr(bbox, "min_lat") and hasattr(bbox, "min_lon"):
        return LatLonBBox(
            min_lat=float(bbox.min_lat),
            min_lon=float(bbox.min_lon),
            max_lat=float(bbox.max_lat),
            max_lon=float(bbox.max_lon),
        )
    # Кортеж: порядок (долгота, широта), как его отдаёт bbox_as_tuple.
    return LatLonBBox.from_xy(bbox)


def _ordered_stops_problem(routes: Sequence[Any]) -> str | None:
    """Находит маршрут без упорядоченных остановок.

    Одна остановка в направлении — не линия, а точка: по ней нельзя построить
    ребро, и пассажир не может ехать. Молча пропустить такой маршрут значило
    бы вычесть из города линию, которой там нет.
    """
    for route in routes:
        for index, direction in enumerate(getattr(route, "directions", ()) or ()):
            if len(direction.stops) < 2:
                name = getattr(route, "name", "?")
                return (
                    f"маршрут {name}, направление {index + 1}: "
                    f"{len(direction.stops)} остановок — линии нужно минимум две, "
                    "иначе ехать некуда"
                )
    return None


def measured_intervals_for(
    routes: Sequence[Any],
    sessions: Any,
    cache: Any,
    *,
    city_slug: str,
) -> dict[Any, Any]:
    """Читает измеренные интервалы из кэша каталога перевозчиков.

    Маршруты пайплайна уже загружены, и их сырые ответы лежат в кэше — повторно
    ходить в сеть не нужно. Кэшированный ответ здесь и есть источник измерения;
    если записи нет, маршрут остаётся без измеренного интервала, и это
    отражается в отчёте, а не замалчивается.
    """
    from city.network import parse_catalog_intervals

    measured: dict[Any, Any] = {}
    for route in routes:
        route_id = getattr(route, "route_id", None)
        if route_id is None or cache is None:
            continue
        try:
            payload = cache.get("route", f"{city_slug}:{route_id}")
        except Exception:  # noqa: BLE001 - кэш не должен ронять расчёт
            payload = None
        if payload is None:
            continue
        values = parse_catalog_intervals(payload)
        if values:
            measured[route_id] = min(values)
    return measured


def run_passenger_flow(
    config: CliConfig,
    routes: Sequence[Any],
    *,
    bbox: Any,
    sessions: Any,
    cache: Any,
    city_slug: str = "",
    reporter: Reporter | None = None,
    root: Path | None = None,
) -> PassengerFlowResult:
    """Считает пассажиропоток по маршрутам, уже загруженным пайплайном.

    :param routes: маршруты из ``PipelineResult.ok_routes``.
    :param bbox: граница города — ``BBox`` пайплайна или кортеж.
    :param city_slug: город для ключа кэша; нужен, чтобы достать интервалы.
    :param root: корень проекта — ищется, если рабочий каталог другой.
    """
    from city.network import (
        INTERVAL_FROM_CATALOG,
        RouteInterval,
        interval_for,
    )
    from overture.city import LatLonBBox
    from passenger.estimate import BuiltSurfaceRaster
    from passenger.flow import calculate_passenger_flow
    from passenger.network import passenger_mode_key
    from passenger.params import PassengerOptions
    from passenger.population import PopulationRaster, population_points

    def say(text: str) -> None:
        if reporter is not None:
            reporter.line(text)

    if not routes:
        return PassengerFlowResult(reason="в городе нет ни одного маршрута")

    problem = _ordered_stops_problem(routes)
    if problem is not None:
        return PassengerFlowResult(
            reason=f"остановки не упорядочены: {problem}"
        )

    # Интервалы: сначала измеренные (из кэша каталога), при их отсутствии —
    # предположение по режиму, помеченное как mode_default.
    measured = measured_intervals_for(
        routes, sessions, cache, city_slug=city_slug
    )
    intervals: dict[Any, RouteInterval] = {}
    for route in routes:
        mode = passenger_mode_key(getattr(route, "route_type", None))
        measured_value = measured.get(route.route_id)
        if measured_value is not None:
            intervals[route.route_id] = RouteInterval(
                minutes=float(measured_value),
                source=INTERVAL_FROM_CATALOG,
            )
        else:
            intervals[route.route_id] = interval_for(None, mode)
    if measured:
        say(f"  Измеренных интервалов из каталога: {len(measured)}")

    headways = {
        route_id: value.minutes
        for route_id, value in intervals.items()
        if value.minutes is not None
    }

    pop_path = _resolve_raster(
        config.passenger_pop, "WIKIROUTES_GHS_POP", DEFAULT_POP_RELATIVE
    )
    built_path = _resolve_raster(
        config.passenger_built, "WIKIROUTES_GHS_BUILT", DEFAULT_BUILT_RELATIVE
    )
    if pop_path is None:
        return PassengerFlowResult(
            reason=(
                "не найден растр населения GHS_POP: задайте --passenger-pop "
                "или переменную WIKIROUTES_GHS_POP"
            )
        )
    if built_path is None:
        return PassengerFlowResult(
            reason=(
                "не найден растр застройки GHS_BUILT_S: без него слой населения "
                "не даёт рабочих мест, и OD-матрицу построить не из чего (§5.5); "
                "задайте --passenger-built или WIKIROUTES_GHS_BUILT"
            )
        )

    box = _as_latlon_bbox(bbox)
    points_per_km2 = float(config.passenger_points_per_km2)
    say(f"  Растр населения: {pop_path.name}")
    say(f"  Растр застройки: {built_path.name}")
    say(f"  Точек спроса на км²: {points_per_km2:g}")

    with PopulationRaster(pop_path) as pop, BuiltSurfaceRaster(built_path) as built:
        points, reading, jobs = population_points(
            pop, box, points_per_km2=points_per_km2, built=built
        )
    say(f"  {reading.summary()}")
    if jobs is not None:
        say(f"  {jobs.summary()}")

    if not points:
        return PassengerFlowResult(
            reason="в границе города не нашлось ни одной точки с населением"
        )

    say(f"  Маршрутов в расчёте: {len(routes)}")
    top_k = int(config.passenger_top_k_destinations) or None
    if top_k:
        say(f"  Концов у каждого начала: {top_k}")
    result = calculate_passenger_flow(
        routes,
        points=points,
        headways=headways,
        options=PassengerOptions(top_k_destinations=top_k),
    )

    report_data = network_report_from(intervals, len(routes), routes_failed=0)
    say(
        f"  Спрос {result.total_demand:,.0f}, транспорт "
        f"{result.transit_trips:,.0f}, удовлетворённость "
        f"{result.satisfaction:.2f} %"
    )

    return PassengerFlowResult(
        result=result,
        network_report=report_data,
        reading_summary=reading.summary(),
        jobs_summary=jobs.summary() if jobs is not None else None,
        demand_points=len(points),
    )


def network_report_from(
    intervals: Mapping[Any, RouteInterval],
    routes_loaded: int,
    *,
    routes_failed: int = 0,
) -> dict[str, Any]:
    """Отчёт о происхождении данных по уже посчитанным интервалам."""
    measured = sum(1 for value in intervals.values() if value.measured)
    assumed = sum(
        1 for value in intervals.values()
        if value.source == "mode_default"
    )
    without = len(intervals) - measured - assumed
    share = measured / len(intervals) if intervals else 0.0
    return {
        "slug": "",
        "routes_loaded": routes_loaded,
        "routes_failed": routes_failed,
        "intervals": {
            "measured": measured,
            "assumed_from_mode_default": assumed,
            "absent": without,
            "measured_share": round(share, 6),
        },
        "note": (
            "интервалы в каталоге перевозчиков практически отсутствуют; "
            "интервалы «предположены по режиму» — это допущение (§8.5), "
            "а не измерение, и оно помечено как mode_default"
        ),
    }


__all__ = ["PassengerFlowResult", "network_report_from", "run_passenger_flow"]
