"""Сеть города для пассажирского расчёта: маршруты, остановки, интервалы.

## Что здесь и почему не в Overture

Маршрутов общественного транспорта в Overture нет. Поле ``segment.routes[]``
содержит обозначения **автомобильных** дорог — в проверенном bbox Воронежа
``М-4 «Дон»``, ``Р-22 «Каспий»``, ``European route E38``, ``Asian Highway AH61``.
Линии ОТ с упорядоченными остановками приходят из каталога перевозчиков, и
именно этот путь здесь используется.

## Что найдено в источнике на реальном городе

| Величина | Значение для Воронежа |
|---|---|
| Маршрутов в каталоге | 847 (555 автобус, 151 маршрутка, 29 троллейбус, 33 трамвай, 5 водный, 74 поезд) |
| Остановки по направлению | приходят, упорядочены, с координатами (проверено: 38 и 42 у маршрута 1) |
| **Интервалы** | **2 из 24 проверенных маршрутов**, и те «более 20 мин.» |

Интервалы в источнике практически отсутствуют, а без них транспорт не имеет
привлекательности во времени (§6.3). §27.1, правило 8 требует не выдумывать
измерение там, где его не было. Поэтому интервал у каждого маршрута имеет
**происхождение**, и оно записывается в сеть:

| Источник | Что означает |
|---|---|
| ``catalog`` | интервал измерен и опубликован |
| ``mode_default`` | интервал **предположен** по режиму из §8.5 — это допущение, а не измерение |
| ``none`` | интервала нет нигде; линия в расчёте не ходит |

Город, где у большинства маршрутов ``mode_default``, считается — но считается
на предположении, и это обязано быть видно в отчёте, а не спрятано в
значении по умолчанию.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Sequence

from passenger.params import MODE_PARAMS, PassengerOptions


#: Происхождение интервала маршрута.
INTERVAL_FROM_CATALOG: str = "catalog"
INTERVAL_FROM_MODE_DEFAULT: str = "mode_default"
INTERVAL_NONE: str = "none"


@dataclass(frozen=True, slots=True)
class RouteInterval:
    """Интервал движения линии с его происхождением."""

    #: Интервал в минутах. ``None`` — линия не ходит.
    minutes: float | None
    source: str

    @property
    def measured(self) -> bool:
        return self.source == INTERVAL_FROM_CATALOG


@dataclass(frozen=True, slots=True)
class TransitNetwork:
    """Сеть города, готовая к пассажирскому расчёту.

    Содержит и измеренное, и предположенное, и разделение между ними видно по
    каждому маршруту.
    """

    slug: str
    #: Маршруты в формате, который принимает ``calculate_passenger_flow``.
    routes: tuple[Any, ...]
    #: ``route_id → RouteInterval``.
    intervals: dict[Any, RouteInterval] = field(default_factory=dict)
    #: Сколько маршрутов в каталоге, но не загружено.
    failed: int = 0

    @property
    def route_count(self) -> int:
        return len(self.routes)

    @property
    def measured_intervals(self) -> int:
        return sum(1 for value in self.intervals.values() if value.measured)

    @property
    def assumed_intervals(self) -> int:
        return sum(
            1 for value in self.intervals.values()
            if value.source == INTERVAL_FROM_MODE_DEFAULT
        )

    @property
    def headway_coverage(self) -> float:
        """Доля маршрутов с измеренным интервалом.

        Это не метрика качества модели, а **прямота расчёта**: пока она низкая,
        транспортная привлекательность взята из предположения.
        """
        if not self.intervals:
            return 0.0
        return self.measured_intervals / len(self.intervals)

    def summary(self) -> str:
        return (
            f"{self.slug}: маршрутов {self.route_count}"
            + (f", не загружено {self.failed}" if self.failed else "")
            + f"; интервалы измерены у {self.measured_intervals}, "
            f"предположены у {self.assumed_intervals} "
            f"({self.headway_coverage * 100:.0f} % измерено)"
        )


def parse_catalog_intervals(payload: Any) -> list[float]:
    """Извлекает интервалы из payload каталога.

    Источник отдаёт их в двух местах, и оба надо посмотреть: ``workInterval`` —
    человекочитаемая строка вроде «более 20 мин.», ``departureIntervals`` —
    массив с ``intervalValue`` и интервалом времени суток. Строка разбирается
    осторожно: «более 20 мин.» — это **не** 20 минут, а нижняя оценка, и
    подставить её как точный интервал значит завысить частоту.
    """
    if not isinstance(payload, dict):
        return []

    values: list[float] = []
    for trip in payload.get("trips") or ():
        if not isinstance(trip, dict):
            continue
        for schedule in trip.get("schedules") or ():
            if not isinstance(schedule, dict):
                continue
            for entry in schedule.get("departureIntervals") or ():
                if not isinstance(entry, dict):
                    continue
                value = entry.get("intervalValue")
                try:
                    minutes = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(minutes) and minutes > 0.0:
                    values.append(minutes)

    text = str(payload.get("workInterval") or "").strip().lower()
    if not values and text and "более" not in text:
        digits = "".join(
            character if character.isdigit() or character == "." else " "
            for character in text
        )
        for token in digits.split():
            try:
                minutes = float(token)
            except ValueError:
                continue
            if math.isfinite(minutes) and minutes > 0.0:
                values.append(minutes)
                break
    return values


def interval_for(
    payload: Any,
    mode: str,
    options: PassengerOptions | None = None,
) -> RouteInterval:
    """Интервал линии с происхождением.

    Порядок: сначала измеренный интервал из источника; при его отсутствии —
    **предположение** по режиму (§8.5), и оно помечается как
    ``mode_default``. Молчаливый дефолт без пометки означал бы, что расчёт
    выглядит измеренным, будучи построенным на допущении (§17.3).
    """
    measured = parse_catalog_intervals(payload)
    if measured:
        # Несколько интервалов за день — берём самый частый: он определяет
        # ожидание в пик, а §5.6a различает окна, но лучший измеренный
        # интервал всё равно грубее окна, и это объявляется источником.
        return RouteInterval(minutes=min(measured), source=INTERVAL_FROM_CATALOG)

    options = options or PassengerOptions()
    params = MODE_PARAMS.get(mode, MODE_PARAMS["bus"])
    default = params.get("default_headway_min")
    try:
        minutes = float(default)
    except (TypeError, ValueError):
        return RouteInterval(minutes=None, source=INTERVAL_NONE)
    if not math.isfinite(minutes) or minutes <= 0.0:
        return RouteInterval(minutes=None, source=INTERVAL_NONE)
    return RouteInterval(minutes=minutes, source=INTERVAL_FROM_MODE_DEFAULT)


def _project_module(name: str) -> Any:
    """Импортирует модуль проекта независимо от того, как настроен ``sys.path``.

    В проекте сосуществуют два соглашения, и это не небрежность, а следствие
    границы: ``wikiroutes/`` добавляется в ``sys.path``, поэтому внутренние
    модули пакета ``overture`` импортируются плоско (``from cache import ...``).
    Но ``routes.py`` и ``catalog.py`` используют **относительные** импорты и
    обязаны импортироваться как ``wikiroutes.routes`` — иначе Python не находит
    родительский пакет.

    Поэтому сначала пробуем полный путь, затем плоский. Ошибка импорта
    одинакова при обоих соглашениях, а различается только форма записи.
    """
    import importlib

    try:
        return importlib.import_module(f"wikiroutes.{name}")
    except ImportError:
        return importlib.import_module(name)


def load_transit_network(
    slug: str,
    *,
    sessions: Any,
    cache: Any,
    section_limit: int | None = None,
    route_limit: int | None = None,
    options: PassengerOptions | None = None,
    catalog: Any = None,
) -> TransitNetwork:
    """Загружает сеть города из каталога перевозчиков.

    :param sessions: ``SessionProvider`` проекта — переиспользуется, чтобы не
        открывать сессию на каждый маршрут.
    :param cache: ``JsonCache`` проекта. Сырые ответы кладутся в кэш самим
        путём загрузки, поэтому повторный запуск города не ходит в сеть.
    :param route_limit: ограничить число маршрутов — для проверки на малом
        городе; на полном городе ограничение не задаётся.

    Маршруты, которые не удалось загрузить, считаются и попадают в
    :attr:`TransitNetwork.failed`: молчаливый пропуск линии выглядел бы как
    город без неё.
    """
    from passenger.network import passenger_mode_key

    routes_module = _project_module("routes")
    catalog_module = _project_module("catalog")

    if catalog is None:
        session = sessions.get()
        catalog = catalog_module.load_catalog(
            catalog_module.make_catalog_url(slug, None), slug, session, cache
        )

    city_slug = getattr(catalog, "city_slug", "") or slug
    sections = list(getattr(catalog, "sections", ()) or ())
    if section_limit is not None:
        sections = sections[:section_limit]

    loaded: list[Any] = []
    intervals: dict[Any, RouteInterval] = {}
    failed = 0

    for section in sections:
        links = list(getattr(section, "links", ()) or ())
        if route_limit is not None:
            links = links[:route_limit]
        for entry in links:
            route = routes_module.fetch_route_network(
                city=city_slug,
                route_type=str(getattr(section, "route_type", "bus")),
                name=entry.name,
                route_id=entry.route_id,
                sessions=sessions,
                cache=cache,
                section_title=getattr(section, "title", ""),
            )
            if route is None or route.error or not route.directions:
                failed += 1
                continue
            payload = cache.get("route", f"{city_slug}:{entry.route_id}")
            mode = passenger_mode_key(getattr(section, "route_type", None))
            intervals[entry.route_id] = interval_for(payload, mode, options)
            loaded.append(route)

    return TransitNetwork(
        slug=slug,
        routes=tuple(loaded),
        intervals=intervals,
        failed=failed,
    )


def headways_for(network: TransitNetwork) -> dict[Any, float]:
    """Словарь ``route_id → интервал_мин`` для пассажирского расчёта.

    Маршруты без интервала **не попадают** в словарь. ``calculate_passenger_flow``
    трактует отсутствие интервала как «линия стоит» (§8.5), и это правильно:
    линия с неизвестной частотой не должна ездить с выдуманной частотой.
    """
    return {
        route_id: float(value.minutes)
        for route_id, value in network.intervals.items()
        if value.minutes is not None
    }


def network_report(network: TransitNetwork) -> dict[str, Any]:
    """Отчёт о происхождении данных сети — «чем измеряли» (§17.3)."""
    measured = network.measured_intervals
    assumed = network.assumed_intervals
    without = len(network.intervals) - measured - assumed
    return {
        "slug": network.slug,
        "routes_loaded": network.route_count,
        "routes_failed": network.failed,
        "intervals": {
            "measured": measured,
            "assumed_from_mode_default": assumed,
            "absent": without,
            "measured_share": round(network.headway_coverage, 6),
        },
        "note": (
            "интервалы в каталоге перевозчиков практически отсутствуют; "
            "предположенные по режиму интервалы — это допущение (§8.5), "
            "а не измерение, и оно помечено как mode_default"
        ),
    }


__all__ = [
    "INTERVAL_FROM_CATALOG",
    "INTERVAL_FROM_MODE_DEFAULT",
    "INTERVAL_NONE",
    "RouteInterval",
    "TransitNetwork",
    "headways_for",
    "interval_for",
    "load_transit_network",
    "network_report",
    "parse_catalog_intervals",
]
