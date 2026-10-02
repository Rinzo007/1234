"""Единая стадия построения задач загрузки маршрутов."""

from __future__ import annotations

from typing import Any

from ..cache import JsonCache
from ..config import CliConfig
from ..http_client import SessionProvider
from ..models import Catalog, RouteData, RouteTask
from ..report import Reporter
from ..support import route_passes_number_filter


def build_route_tasks(
    catalog: Catalog,
    city_slug: str,
    config: CliConfig,
) -> list[RouteTask]:
    """Строит задачи загрузки для всех разрешённых типов маршрутов.

    Никакого деления на базовые/вторичные типы здесь нет: все секции
    обрабатываются одинаково, а фильтрация задаётся только конфигурацией.
    """
    route_filter = config.route_filter.strip().lower() if config.route_filter else None
    tasks: list[RouteTask] = []

    for section in catalog.sections:
        if section.route_type in config.disabled_types:
            continue
        if config.type_filter and section.route_type not in config.type_filter:
            continue

        for link in section.links:
            if route_filter and not (
                link.name.strip().lower() == route_filter
                or str(link.route_id) == route_filter
            ):
                continue

            if not route_passes_number_filter(link.name, config.max_route_number):
                continue

            tasks.append(
                RouteTask(
                    city=city_slug,
                    route_type=section.route_type,
                    name=link.name,
                    route_id=link.route_id,
                    section_title=section.title,
                )
            )

    return tasks


def load_idea_routes(
    config: CliConfig,
    *,
    city_slug: str,
    cache: JsonCache,
    session_provider: SessionProvider,
    reporter: Reporter,
) -> list[RouteData]:
    """Загружает идеи пассажиров и преобразует их в маршруты."""
    if not config.ideas:
        return []

    reporter.line("\n[2.1/4] Загрузка идей пассажиров...")
    try:
        from ..ideas import ideas_to_routes, load_ideas
    except ModuleNotFoundError as exc:
        if exc.name in {"wikiroutes.ideas", "ideas"}:
            reporter.line(
                "  ⚠ Модуль идей пассажиров отсутствует в текущем пакете; "
                "идеи пропущены."
            )
            return []

        raise

    ideas_data: list[dict[str, Any]] = []
    try:
        ideas_data = load_ideas(
            city_slug=city_slug,
            cache=cache,
            session_provider=session_provider,
            search=config.ideas_search,
            sort=config.ideas_sort,
            max_pages=config.ideas_max_pages,
        )
    except Exception as exc:  # noqa: BLE001
        reporter.line(f"  ⚠ Ошибка загрузки идей: {exc}")
        return []

    idea_routes = ideas_to_routes(
        ideas_data,
        cache,
        session_provider,
    )
    reporter.line(f"  Добавлено {len(idea_routes)} идей к общему списку")
    return idea_routes


__all__ = ["build_route_tasks", "load_idea_routes"]
