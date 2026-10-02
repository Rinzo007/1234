"""Вспомогательные операции pipeline, связанные с дедупликацией."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, TypeAlias

from ..models import RouteData
from ..type_defs import DirectionKey

DedupId: TypeAlias = int | DirectionKey


def apply_dedup_removals(
    routes: list[RouteData],
    meta: dict[DedupId, dict[str, Any]],
    removed_uids: set[DedupId],
) -> tuple[list[RouteData], int, int, dict[tuple[int, int], int]]:
    """Вырезает удалённые направления из маршрутов.

    Возвращает ``(новые_маршруты, удалено_целиком, сокращено, orig_index)``,
    где ``orig_index[(route_id, новый_di)]`` — исходный ``di`` направления
    до фильтрации. Индексы смещаются при удалении (кортеж направлений
    пересобирается), а записи дедупликации ссылаются на исходные ключи —
    без ``orig_index`` экспорт (KML) и сопоставление статистик путают
    выжившие направления с удалёнными.
    """
    removed_dirs_by_route: dict[int, set[int]] = {}
    for uid in removed_uids:
        rec = meta.get(uid)
        if not rec:
            continue
        route_id = rec.get("route_id")
        di = rec.get("di")
        if route_id is None or di is None:
            continue
        removed_dirs_by_route.setdefault(int(route_id), set()).add(int(di))

    new_routes: list[RouteData] = []
    orig_index: dict[tuple[int, int], int] = {}
    fully_removed = 0
    shortened_routes = 0
    for route in routes:
        removed_dis = removed_dirs_by_route.get(route.route_id)
        if not removed_dis:
            for new_di, _direction in enumerate(route.directions):
                orig_index[(route.route_id, new_di)] = new_di
            new_routes.append(route)
            continue
        directions = route.directions
        kept: list[tuple[int, Any]] = [
            (di, direction)
            for di, direction in enumerate(directions)
            if di not in removed_dis and len(direction.coords) >= 2
        ]
        kept_directions = tuple(direction for _di, direction in kept)
        if not kept_directions:
            fully_removed += 1
            continue
        if len(kept_directions) < len(directions):
            shortened_routes += 1
        for new_di, (orig_di, _direction) in enumerate(kept):
            orig_index[(route.route_id, new_di)] = orig_di
        new_routes.append(replace(route, directions=kept_directions))
    return new_routes, fully_removed, shortened_routes, orig_index


def dir_stat_volume_map(
    unit_ids: list[DedupId],
    meta: dict[DedupId, dict[str, Any]],
    dir_stats: dict[tuple[int, int], Any],
    attr: str,
) -> dict[DedupId, float]:
    result: dict[DedupId, float] = {}
    for uid in unit_ids:
        rec = meta.get(uid)
        if not rec:
            continue
        route_id = rec.get("route_id")
        di = rec.get("di")
        if route_id is None or di is None:
            continue
        stat = dir_stats.get((int(route_id), int(di)))
        if stat is not None:
            result[uid] = float(getattr(stat, attr))
    return result


__all__ = ["apply_dedup_removals", "dir_stat_volume_map"]
