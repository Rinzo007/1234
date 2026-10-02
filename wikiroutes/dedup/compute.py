"""Жадное удержание направлений после spatial-анализа.

Инвариант маршрута: направлений остаётся не более двух — пара «туда»
(``di == 0``) и «обратно» (``di == 1``) неразделимы (живут или удаляются
вместе), а лишние направления (``di >= 2``) удаляются.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..support import format_dedup_id
from ..support import safe_float as _safe_float
from .policy import (
    DirectionId,
    _id_sort_key,
    _pair_indices,
    _route_key,
)


def _metric_unit(metric: str) -> tuple[str, str]:
    """Заголовок и единица измерения отчётного значения для метрики."""
    if metric == "poi":
        return "POI вдоль маршрута", "шт."
    return "POI у остановок", "шт."


def _metric_value(metric: str, raw: float) -> float:
    """Форматирует отчётное значение для метрики."""
    del metric
    return float(round(max(0.0, _safe_float(raw))))


def _metric_subject(metric: str) -> str:
    """Прилагательное «более …» маршрута в причине."""
    del metric
    return "насыщенного POI"


def _metric_short_label(metric: str) -> str:
    """Короткая подпись значения в причине."""
    del metric
    return "POI"


def _metric_display(metric: str, raw: float) -> str:
    """Строковое представление значения для причины."""
    return str(int(_metric_value(metric, raw)))


def _coerce_id(item: Any) -> DirectionId | None:
    """Направление как int или пара (route_id, direction_index) из значения."""
    if isinstance(item, tuple) and len(item) == 2:
        try:
            return (int(item[0]), int(item[1]))
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        return int(item)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_ids(raw_ids: Any) -> list[DirectionId]:
    """Нормализует список идентификаторов направлений в `(route_id, di)`/int."""
    ids: list[DirectionId] = []
    for item in raw_ids:
        parsed = _coerce_id(item)
        if parsed is not None:
            ids.append(parsed)
    return ids


def _has_kmax(analysis: dict[str, Any]) -> bool:
    """Есть ли в анализе пригодные пары направлений с колонкой ``Kmax``."""
    base_pairs = analysis.get("pairs")
    if base_pairs is None or getattr(base_pairs, "empty", True):
        return False
    return "Kmax" in getattr(base_pairs, "columns", [])


@dataclass(frozen=True)
class _PairTable:
    """Индексы пар, Kmax и карта перевода идентификаторов направления."""

    x_idx: np.ndarray
    y_idx: np.ndarray
    kmax: np.ndarray
    id_to_idx: dict[DirectionId, int]
    ids: list[DirectionId]
    n_ids: int


def _pair_table(base_pairs: Any, ids: list[DirectionId]) -> _PairTable:
    """Собирает ``_PairTable`` из листа пар анализа.

    ``kmax`` — максимум K2_xy/K2_yx при наличии колонок K2, иначе просто Kmax.
    """
    x_idx, y_idx, id_to_idx = _pair_indices(base_pairs, ids)
    raw_kmax = np.asarray(base_pairs["Kmax"], dtype=float).ravel()
    if "K2_xy" in base_pairs.columns and "K2_yx" in base_pairs.columns:
        kmax = np.maximum(
            np.asarray(base_pairs["K2_xy"], dtype=float),
            np.asarray(base_pairs["K2_yx"], dtype=float),
        ).ravel()
    else:
        kmax = raw_kmax
    return _PairTable(
        x_idx=x_idx,
        y_idx=y_idx,
        kmax=kmax,
        id_to_idx=id_to_idx,
        ids=ids,
        n_ids=len(ids),
    )


def _active_index_array(
    active: set[DirectionId], id_to_idx: dict[DirectionId, int]
) -> np.ndarray:
    active_present = [r for r in active if r in id_to_idx]
    return np.fromiter(
        (id_to_idx[r] for r in active_present),
        dtype=np.int64,
        count=len(active_present),
    )


def _collect_partner_graph(
    table: _PairTable,
    candidates: np.ndarray,
    extra: np.ndarray,
) -> dict[DirectionId, dict[DirectionId, float]]:
    """Симметричный граф пар: направление → его партнёры c их K."""
    valid = (
        (table.x_idx != table.y_idx)
        & (table.x_idx >= 0)
        & (table.y_idx >= 0)
        & (table.x_idx < table.n_ids)
        & (table.y_idx < table.n_ids)
        & np.isin(table.x_idx, candidates)
        & np.isin(table.y_idx, candidates)
        & extra
    )
    partners: dict[DirectionId, dict[DirectionId, float]] = {}
    for li in np.nonzero(valid)[0]:
        xi = int(table.x_idx[li])
        yi = int(table.y_idx[li])
        x_id = table.ids[xi]
        y_id = table.ids[yi]
        k_value = float(table.kmax[li])
        partners.setdefault(x_id, {})[y_id] = k_value
        partners.setdefault(y_id, {})[x_id] = k_value
    return partners


def _route_member_representative(
    sid: DirectionId,
    active: set[DirectionId],
) -> DirectionId:
    """Живое направление того же маршрута для отчёта о лишнем направлении."""
    route_id, _ = _route_key(sid)
    siblings = sorted(
        (rid for rid in active if _route_key(rid)[0] == route_id and rid != sid),
        key=_id_sort_key,
    )
    return siblings[0] if siblings else sid


def _enforce_route_limits(
    removed: list[dict[str, Any]],
    active: set[DirectionId],
    universe: set[DirectionId],
    ctx: _RemovalContext,
) -> tuple[list[dict[str, Any]], set[DirectionId]]:
    """Инвариант маршрута: не более двух направлений, пара «туда/обратно» — вместе.

    * пара ``di == 0``/``di == 1``: если удалено одно направление пары, а
      парное живо, удаление откатывается (маршрут сохраняется целиком);
    * лишние направления (``di >= 2``) удаляются — у маршрута остаётся не
      более двух направлений.
    """
    removed_ids = {rec["маршрут"] for rec in removed}

    members_by_route: dict[int, set[DirectionId]] = {}
    for rid in universe:
        members_by_route.setdefault(_route_key(rid)[0], set()).add(rid)

    restored: set[DirectionId] = set()
    extras_to_drop: set[DirectionId] = set()
    for members in members_by_route.values():
        pair = {rid for rid in members if _route_key(rid)[1] in (0, 1)}
        extras = members - pair
        if pair:
            if pair & active:
                # Пара жива — возвращаем удалённые направления пары, лишние убираем.
                restored |= pair & removed_ids
                extras_to_drop |= extras & active
            else:
                # Пара удалена целиком — маршрут исчезает целиком.
                extras_to_drop |= extras & active
        elif len(members) > 2:
            # Без пары оставляем первые два направления по стабильному ID.
            extras_to_drop |= set(sorted(members, key=_id_sort_key)[2:])

    new_active = (active | restored) - extras_to_drop
    if restored:
        removed = [rec for rec in removed if rec["маршрут"] not in restored]
    for extra in sorted(extras_to_drop, key=_id_sort_key):
        removed.append(
            _removal_record(
                ctx,
                len(removed) + 1,
                extra,
                _VictimPick(
                    _route_member_representative(extra, new_active), {}, 0.0
                ),
                "лишнее направление: у маршрута остаётся не более двух "
                "(«туда»/«обратно»)",
            )
        )
    return removed, new_active


@dataclass(frozen=True)
class _RemovalContext:
    """Общий контекст построения отчёта о дедупликации."""

    metric: str
    meta: dict[Any, Any]
    lengths: dict[Any, float]

    @property
    def metric_title(self) -> str:
        """Заголовок колонки значения в отчёте."""
        return _metric_unit(self.metric)[0]


@dataclass(frozen=True)
class _VictimPick:
    """Параметры записи о выборе представителя для удаления."""

    main_partner_id: DirectionId
    partners: dict[DirectionId, float]
    victim_val: float


def _removal_record(
    ctx: _RemovalContext,
    step: int,
    victim: DirectionId,
    pick: _VictimPick,
    reason: str,
) -> dict[str, Any]:
    """Запись о удалении направления (для листа удалений)."""
    ordered_partners = sorted(
        pick.partners.items(),
        key=lambda item: (-item[1], _id_sort_key(item[0])),
    )
    return {
        "шаг": step,
        "маршрут": victim,
        "представитель": pick.main_partner_id,
        "представитель_название": ctx.meta.get(pick.main_partner_id, {}).get("name", ""),
        "представитель_тип": ctx.meta.get(pick.main_partner_id, {}).get("type", ""),
        "тип": ctx.meta.get(victim, {}).get("type", ""),
        "название": ctx.meta.get(victim, {}).get("name", ""),
        "закрывает пар": len(pick.partners),
        "партнёры (Kmax)": ", ".join(
            f"{format_dedup_id(partner_id)} ({kmax:.3f})"
            for partner_id, kmax in ordered_partners
        ),
        "причина": reason,
        "длина, км": round(_safe_float(ctx.lengths.get(victim)) / 1000.0, 2),
        ctx.metric_title: _metric_value(ctx.metric, pick.victim_val),
    }


def _run_greedy_additions(
    candidate: set[DirectionId],
    partner_pairs: dict[DirectionId, dict[DirectionId, float]],
    values: dict[DirectionId, float],
    ctx: _RemovalContext,
) -> tuple[set[DirectionId], list[dict[str, Any]]]:
    """Жадный проход: удерживает направление, пока нет конфликта с удержанным."""
    removed: list[dict[str, Any]] = []
    kept: set[DirectionId] = set()

    order = sorted(
        candidate,
        key=lambda rid: (
            -values.get(rid, 0.0),
            tuple(-part for part in _id_sort_key(rid)),
        ),
    )

    for rid in order:
        partners = partner_pairs.get(rid)
        conflicting = {p: k for p, k in (partners or {}).items() if p in kept}
        if not conflicting:
            kept.add(rid)
            continue

        rep_id, rep_k = max(
            conflicting.items(),
            key=lambda item: (item[1], _id_sort_key(item[0])),
        )
        reason = (
            f"дубликат более {_metric_subject(ctx.metric)} маршрута при K={rep_k:.3f}; "
            f"{_metric_short_label(ctx.metric)}={_metric_display(ctx.metric, values.get(rid, 0.0))}"
        )
        removed.append(
            _removal_record(
                ctx,
                len(removed) + 1,
                rid,
                _VictimPick(
                    rep_id, conflicting, values.get(rid, 0.0)
                ),
                reason,
            )
        )

    return kept, removed


def dedup_compute_additions(
    analysis: dict[str, Any],
    k_del: float,
    weights: dict[DirectionId, float],
    initial_active: set[DirectionId] | None = None,
    metric: str = "poi_stops",
) -> tuple[list[dict[str, Any]], set[DirectionId], list[dict[str, Any]]]:
    """Жадно ДОБАВЛЯЕТ направления по убыванию ценности (веса POI), оставляя
    неизбыточные."""
    if not weights:
        raise ValueError(
            "weights обязателен: дедупликация требует POI (poi_stops)"
        )
    ids = _parse_ids(analysis.get("ids", ()))
    candidate = set(ids)
    if initial_active is not None:
        candidate &= initial_active

    lengths = analysis.get("lengths", {})
    meta = analysis.get("meta", {})
    ctx = _RemovalContext(metric, meta, lengths)

    if not _has_kmax(analysis):
        removed, kept = _enforce_route_limits([], set(candidate), set(candidate), ctx)
        return removed, kept, []

    k_del_f = _safe_float(k_del, default=float("nan"))
    if not np.isfinite(k_del_f):
        removed, kept = _enforce_route_limits([], set(candidate), set(candidate), ctx)
        return removed, kept, []
    if not 0.0 <= k_del_f <= 1.0:
        raise ValueError("k_del должен быть числом в диапазоне [0, 1]")

    values = {
        rid: max(0.0, _safe_float(weights.get(rid))) for rid in candidate
    }

    table = _pair_table(analysis["pairs"], ids)
    cand_idx_arr = _active_index_array(candidate, table.id_to_idx)
    extra = np.isfinite(table.kmax) & (table.kmax > k_del_f)
    partner_pairs = _collect_partner_graph(table, cand_idx_arr, extra)

    kept, removed = _run_greedy_additions(
        candidate,
        partner_pairs,
        values,
        ctx,
    )
    removed, kept = _enforce_route_limits(removed, kept, set(candidate), ctx)
    return removed, kept, []


def enforce_route_limits(
    removed: list[dict[str, Any]],
    active: set[DirectionId],
    analysis: dict[str, Any],
    metric: str = "poi_stops",
) -> tuple[list[dict[str, Any]], set[DirectionId]]:
    """Публичная обёртка инварианта маршрута «не более двух направлений».

    Применяется оркестратором после выбора удаляемых направлений.
    """
    universe = set(_parse_ids(analysis.get("ids", ())))
    ctx = _RemovalContext(metric, analysis.get("meta", {}), analysis.get("lengths", {}))
    return _enforce_route_limits(removed, active, universe, ctx)