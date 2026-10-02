"""Оркестрация дедупликации pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from ..config import CliConfig
from ..dedup import dedup_analyze, dedup_network_after
from ..dedup.compute import (
    dedup_compute_additions,
    enforce_route_limits,
)
from ..dedup.policy import (
    count_self_redundant_pairs,
)
from ..metrics import PoiStats
from ..models import RouteData
from ..report import Reporter
from ..type_defs import DirectionKey
from .dedup import apply_dedup_removals

DedupId: TypeAlias = int | DirectionKey


@dataclass(frozen=True, slots=True)
class DedupPassResult:
    """Результат одного прохода дедупликации."""

    removed: list[dict[str, Any]]
    active: set[DedupId] | None
    residual: list[dict[str, Any]]
    routes: list[RouteData]
    fully_removed: int
    shortened_routes: int
    km_after: float | None
    unit_count: int
    # Соответствие (route_id, di_после_прохода) -> di_до_прохода.
    # None означает тождество (проход ничего не вырезал).
    dir_orig: dict[tuple[int, int], int] | None = None


@dataclass(frozen=True, slots=True)
class DedupStageResult:
    """Результат полного stage дедупликации."""

    removed: list[dict[str, Any]]
    routes: list[RouteData]
    analysis: dict[str, Any]
    net_metrics: dict[str, Any]
    # Сквозное соответствие (route_id, di_в_routes) -> исходный di
    # (индекс направления на входе стадии). Композиция отображений
    # всех проходов; None — тождество (ничего не вырезалось).
    dir_orig_index: dict[tuple[int, int], int] | None = None


def _compose_dir_orig(
    base: dict[tuple[int, int], int] | None,
    local: dict[tuple[int, int], int] | None,
) -> dict[tuple[int, int], int] | None:
    """Композиция соответствий индексов: base ∘ local.

    ``local`` отображает выходные индексы прохода во входные,
    ``base`` — входные в исходные (накопленные прошлыми проходами).
    """
    if not local:
        return base
    if not base:
        return dict(local)
    return {
        key: base.get((key[0], input_di), input_di)
        for key, input_di in local.items()
    }


def _direction_source_lookup(
    routes: list[RouteData],
) -> dict[tuple[int, int], str]:
    """Словарь ``(route_id, di) → источник данных`` по всем направлениям."""
    lookup: dict[tuple[int, int], str] = {}
    for route in routes:
        for di in range(len(route.directions)):
            lookup[(route.route_id, di)] = route.source
    return lookup


def _stamp_direction_sources(
    removed: list[dict[str, Any]],
    source_lookup: dict[tuple[int, int], str],
) -> None:
    """Проставляет «источник» в записи об удалённых направлениях."""
    for rec in removed:
        key = rec.get("маршрут")
        if isinstance(key, tuple) and len(key) == 2:
            rec["источник"] = source_lookup.get(key, "")


def _attach_direction_coords(
    removed: list[dict[str, Any]], routes: list[RouteData]
) -> list[dict[str, Any]]:
    """Добавляет в записи дедупликации геометрию удалённых и заменивших
    направлений.

    Ключи ``(route_id, di)`` в записях соответствуют индексам в ``routes``
    (тот же список, по которому работал проход), поэтому индекс на этом
    этапе ещё согласован — в отличие от ``all_routes`` после фильтрации.
    """
    lookup: dict[tuple[int, int], tuple[Any, Any, Any, Any]] = {}
    for route in routes:
        rt = getattr(
            route.route_type, "value", getattr(route.route_type, "name", str(route.route_type))
        )
        for di, d in enumerate(route.directions):
            if d is not None and getattr(d, "coords", None):
                lookup[(route.route_id, di)] = (rt, d.coords, d.name, route.name)
    _stamp_direction_sources(removed, _direction_source_lookup(routes))
    for rec in removed:
        for keyfield, outfield in (
            ("маршрут", "маршрут_координаты"),
            ("представитель", "представитель_координаты"),
        ):
            key = rec.get(keyfield)
            if not (isinstance(key, tuple) and len(key) == 2):
                continue
            info = lookup.get(key)
            if info is not None:
                rec[outfield] = {
                    "тип": info[0],
                    "координаты": info[1],
                    "название": info[2],
                    "маршрут": info[3],
                }
    return removed


def _normalized_pair(key: Any) -> tuple[int, int] | None:
    """Нормализует ключ статов в кортеж (route_id, di) с int."""
    if isinstance(key, tuple) and len(key) == 2:
        try:
            return (int(key[0]), int(key[1]))
        except (TypeError, ValueError):
            return None
    try:
        return (int(key), 0)
    except (TypeError, ValueError):
        return None


def _lookup_stat(
    idx: DedupId,
    stats_direct: dict[Any, Any],
    stats_normalized: dict[tuple[int, int], Any],
    stat_field: str,
) -> float | None:
    """Значение статистики для id, перебирая прямые и нормализованные ключи."""
    if idx in stats_direct:
        return getattr(stats_direct[idx], stat_field, 0.0)
    norm = _normalized_pair(idx)
    if norm is None:
        return None
    if norm in stats_normalized:
        return getattr(stats_normalized[norm], stat_field, 0.0)
    norm0 = (norm[0], 0)
    if norm0 in stats_normalized:
        return getattr(stats_normalized[norm0], stat_field, 0.0)
    return None


def _build_stat_weights(
    ids: list[DedupId],
    stats: dict[Any, Any],
    stat_field: str = "count",
) -> dict[DedupId, float]:
    """
    Сопоставляет идентификаторы из анализа со статистикой, перебирая все
    возможные варианты ключей (скаляр, кортеж, с приведением типов).
    """
    stats_normalized: dict[tuple[int, int], Any] = {}
    for key, stat in stats.items():
        norm = _normalized_pair(key)
        if norm is not None:
            stats_normalized[norm] = stat

    result: dict[DedupId, float] = {}
    for idx in ids:
        value = _lookup_stat(idx, stats, stats_normalized, stat_field)
        if value is not None:
            result[idx] = float(value)
        # else: оставляем без веса
    return result


def _skip_pass_result(
    routes: list[RouteData],
    analysis: dict[str, Any],
    active_set: set[DedupId] | None,
    unit_count: int,
    *,
    residual: list[dict[str, Any]] | None = None,
    dir_orig: dict[tuple[int, int], int] | None = None,
) -> DedupPassResult:
    """Пустой результат прохода (нечего убирать / веса не найдены)."""
    active = active_set
    km_after, _, _ = dedup_network_after(analysis, active)
    return DedupPassResult(
        removed=[],
        active=active,
        residual=residual or [],
        routes=routes,
        fully_removed=0,
        shortened_routes=0,
        km_after=km_after,
        unit_count=unit_count,
        dir_orig=dir_orig,
    )


def _report_weights(
    reporter: Reporter | None,
    ids: list[DedupId],
    volumes: dict[DedupId, float],
    metric_label: str,
) -> None:
    """Пишет в отчёт число найденных весов и примеры id без весов."""
    if reporter is None:
        return
    reporter.line(
        f"  Найдено весов {metric_label}: {len(volumes)} из {len(ids)} направлений"
    )
    if len(volumes) >= len(ids):
        return
    missing = [str(id) for id in ids[:10] if id not in volumes]
    if missing:
        reporter.line(f"    Примеры ID без весов: {missing[:5]}...")


def _build_pass_weights(
    ids: list[DedupId],
    config: CliConfig,
    poi_stops_dir_stats: dict[tuple[int, int], PoiStats] | None,
    reporter: Reporter | None,
) -> tuple[dict[DedupId, float], str]:
    """Строит словарь весов и выбирает фактическую метрику.

    Веса — только POI у остановок; иначе пустой словарь.
    """
    metric_choice = config.dedup_metric  # "poi", "poi_stops" или None (авто)

    if config.poi_stops and poi_stops_dir_stats:
        volumes = _build_stat_weights(ids, poi_stops_dir_stats, "count")
        _report_weights(reporter, ids, volumes, "POI")
        return volumes, metric_choice or "poi_stops"

    return {}, metric_choice or "poi_stops"


def _skip_without_weights(
    config: CliConfig,
    routes: list[RouteData],
    analysis: dict[str, Any],
    active_set: set[DedupId] | None,
    unit_count: int,
    reporter: Reporter | None,
) -> DedupPassResult:
    """Пропуск прохода при отсутствии весов.

    Направления удерживаются жадно по убыванию весов POI, поэтому без
    весов работать нечем: при явно запрошенной метрике останавливается
    ошибкой, иначе пишет предупреждение и возвращает пустой результат.
    """
    if config.dedup_metric is not None:
        raise ValueError(
            f"веса для метрики '{config.dedup_metric}' не найдены — "
            "дедупликация требует POI (poi_stops)"
        )
    if reporter is not None:
        reporter.line(
            "  Внимание: для дедупликации требуются веса, но они не найдены. "
            "Пропускаем дедупликацию."
        )
    return _skip_pass_result(routes, analysis, active_set, unit_count)


def _compute_dedup(
    analysis: dict[str, Any],
    config: CliConfig,
    weights: dict[DedupId, float],
    effective_metric: str,
    active_set: set[DedupId] | None,
) -> tuple[list[dict[str, Any]], set[DedupId], list[dict[str, Any]]]:
    """Жадное удержание направлений по убыванию весов + инвариант маршрута
    «не более двух направлений»."""
    removed, active, residual = dedup_compute_additions(
        analysis, config.dedup_threshold, weights=weights,
        initial_active=active_set, metric=effective_metric,
    )
    removed, active = enforce_route_limits(
        removed, active, analysis, metric=effective_metric
    )
    return removed, active, residual


def _report_pass_progress(
    reporter: Reporter | None,
    pass_no: int,
    max_passes: int,
    removed: list[dict[str, Any]],
    fully_removed: int,
) -> None:
    if reporter is None:
        return
    reporter.line(
        f"  Проход {pass_no}/{max_passes}: "
        f"удалено направлений {len(removed)} "
        f"(маршрутов целиком: {fully_removed})"
    )


def run_dedup_pass(
    routes: list[RouteData],
    analysis: dict[str, Any],
    config: CliConfig,
    *,
    poi_stops_dir_stats: dict[tuple[int, int], PoiStats] | None = None,
    active_set: set[DedupId] | None = None,
    reporter: Reporter | None = None,
    pass_no: int = 1,
    max_passes: int = 1,
) -> DedupPassResult:
    """Выполняет один логический проход дедупликации."""
    unit_count = len(analysis.get("ids", ()))
    if unit_count < 2:
        return _skip_pass_result(routes, analysis, active_set, unit_count)

    # --- Построение словаря весов ---
    weights, effective_metric = _build_pass_weights(
        analysis["ids"],
        config,
        poi_stops_dir_stats,
        reporter,
    )

    # Без весов работать нечем: направления удерживаются жадно по убыванию
    # весов. При явно запрошенной метрике прекращаем с ошибкой, иначе
    # пропускаем проход.
    if not weights:
        return _skip_without_weights(
            config, routes, analysis, active_set, unit_count, reporter
        )

    removed, active, residual = _compute_dedup(
        analysis, config, weights, effective_metric, active_set
    )

    removed = _attach_direction_coords(removed, routes)

    if not removed:
        return _skip_pass_result(
            routes, analysis, active or set(), unit_count, residual=residual
        )

    removed_uids = {rec["маршрут"] for rec in removed}
    new_routes, fully_removed, shortened_routes, dir_orig = apply_dedup_removals(
        routes,
        analysis["meta"],
        removed_uids,
    )

    active = active or set()
    km_after, _, _ = dedup_network_after(analysis, active)

    _report_pass_progress(
        reporter, pass_no, max_passes, removed, fully_removed
    )

    return DedupPassResult(
        removed=removed,
        active=active,
        residual=residual,
        routes=new_routes,
        fully_removed=fully_removed,
        shortened_routes=shortened_routes,
        km_after=km_after,
        unit_count=unit_count,
        dir_orig=dir_orig,
    )


def _analyze_current(
    config: CliConfig,
    routes: list[RouteData],
) -> dict[str, Any] | None:
    """Запускает анализ дедупликации для текущего списка маршрутов."""
    return dedup_analyze(
        routes,
        config.dedup_buffer,
        thr=config.dedup_threshold,
        per_direction=True,
        compute_unique_segments=bool(config.dedup_unique_km > 0),
        cache=config.dedup_cache,
        cache_dir=config.dedup_cache_dir,
        approximate=config.dedup_approx,
        approx_step=config.dedup_approx_step,
        exact_margin=config.dedup_approx_margin,
        profile=config.dedup_profile,
        compute_unique_net=config.dedup_unique_net,
    )


def _build_net_metrics(
    final_analysis: dict[str, Any],
    config: CliConfig,
    before_km: float | None,
    after_km: float | None,
    passes_executed: int,
    total_fully_removed: int,
    total_shortened: int,
    active_set: set[DedupId] | None,
) -> dict[str, Any]:
    """Собирает итоговую статистику сети после дедупликации."""
    net_metrics: dict[str, Any] = {
        "before_km": before_km,
        "after_km": after_km,
        "passes": passes_executed,
        "fully_removed": total_fully_removed,
        "shortened_routes": total_shortened,
    }
    if not final_analysis:
        return net_metrics
    active = active_set or set()
    total_km_after, unique_km_after, km_coef_after = dedup_network_after(
        final_analysis, active
    )
    net_metrics["total_km"] = total_km_after
    net_metrics["unique_km"] = unique_km_after
    net_metrics["km_coef"] = km_coef_after
    net_metrics["self_redundant_pairs"] = count_self_redundant_pairs(
        final_analysis, config.dedup_threshold
    )
    return net_metrics


def run_dedup_stage(
    routes: list[RouteData],
    config: CliConfig,
    *,
    poi_stops_dir_stats: dict[tuple[int, int], PoiStats] | None = None,
    reporter: Reporter | None = None,
) -> DedupStageResult:
    """Выполняет все заданные проходы дедупликации."""
    current_routes = routes
    all_removed: list[dict[str, Any]] = []
    active_set: set[DedupId] | None = None
    dir_orig_index: dict[tuple[int, int], int] | None = None
    final_analysis: dict[str, Any] | None = None
    total_fully_removed = 0
    total_shortened = 0
    before_km: float | None = None
    after_km: float | None = None
    passes_executed = 0

    max_passes = max(1, config.dedup_passes)

    for pass_no in range(1, max_passes + 1):
        analysis = _analyze_current(config, current_routes)
        if analysis is None:
            break

        final_analysis = analysis
        if before_km is None:
            before_km = analysis.get("total_km")

        result = run_dedup_pass(
            current_routes,
            analysis,
            config,
            poi_stops_dir_stats=poi_stops_dir_stats,
            active_set=active_set,
            reporter=reporter,
            pass_no=pass_no,
            max_passes=max_passes,
        )

        passes_executed += 1
        if result.removed:
            all_removed.extend(result.removed)
        after_km = result.km_after
        active_set = result.active
        dir_orig_index = _compose_dir_orig(dir_orig_index, result.dir_orig)
        total_fully_removed += result.fully_removed
        total_shortened += result.shortened_routes
        current_routes = result.routes

        if not result.removed or pass_no == max_passes:
            break

    if final_analysis is None:
        final_analysis = {}

    if after_km is None:
        after_km = before_km

    net_metrics = _build_net_metrics(
        final_analysis,
        config,
        before_km,
        after_km,
        passes_executed,
        total_fully_removed,
        total_shortened,
        active_set,
    )

    return DedupStageResult(
        removed=all_removed,
        routes=current_routes,
        analysis=final_analysis,
        net_metrics=net_metrics,
        dir_orig_index=dir_orig_index,
    )


__all__ = [
    "DedupPassResult",
    "DedupStageResult",
    "run_dedup_pass",
    "run_dedup_stage",
]