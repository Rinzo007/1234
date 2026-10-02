"""Единый контракт результата расчёта Overture."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from .ports import StatsPort


class OvertureStatus(str, Enum):
    """Итоговый статус расчёта Overture."""

    SUCCESS = "success"
    NO_DATA = "no_data"
    INVALID_INPUT = "invalid_input"
    SKIPPED = "skipped"
    ERROR = "error"
    PARTIAL = "partial"


StatsT = TypeVar("StatsT")


@dataclass(frozen=True, slots=True)
class OvertureResult(Generic[StatsT]):
    """Результат расчёта с явным статусом вместо перегруженного tuple."""

    stats: StatsT
    meta: dict[str, Any] | None
    direction_stats: dict[tuple[int, int], StatsPort]
    status: OvertureStatus
    reason: str | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status in {OvertureStatus.SUCCESS, OvertureStatus.PARTIAL}

    def as_legacy_tuple(
        self,
    ) -> tuple[
        StatsT,
        dict[str, Any] | None,
        dict[tuple[int, int], StatsPort],
    ]:
        """Возвращает старый tuple-API для существующих вызывающих кодов."""
        return self.stats, self.meta, self.direction_stats

    @classmethod
    def success(
        cls,
        stats: StatsT,
        meta: dict[str, Any],
        direction_stats: dict[tuple[int, int], StatsPort],
        *,
        warnings: tuple[str, ...] = (),
    ) -> OvertureResult[StatsT]:
        return cls(
            stats=stats,
            meta=meta,
            direction_stats=direction_stats,
            status=OvertureStatus.SUCCESS,
            warnings=warnings,
        )

    @classmethod
    def partial(
        cls,
        stats: StatsT,
        meta: dict[str, Any],
        direction_stats: dict[tuple[int, int], StatsPort],
        *,
        warnings: tuple[str, ...] = (),
    ) -> OvertureResult[StatsT]:
        return cls(
            stats=stats,
            meta=meta,
            direction_stats=direction_stats,
            status=OvertureStatus.PARTIAL,
            warnings=warnings,
        )

    @classmethod
    def no_data(
        cls,
        *,
        reason: str = "no_data",
        meta: dict[str, Any] | None = None,
    ) -> OvertureResult[dict[int, StatsPort]]:
        # ``meta["status"]`` присутствует и здесь, а не только в успешном пути:
        # legacy tuple-API возвращает ``meta`` вызывающему коду, который
        # различает состояния расчёта именно по нему. Пустой ``meta``
        # превращал бы «данных нет» в неотличимое от «метаданных нет».
        resolved = dict(meta or {})
        resolved.setdefault("status", OvertureStatus.NO_DATA.value)
        resolved.setdefault("reason", reason)
        return cls(
            stats={},
            meta=resolved,
            direction_stats={},
            status=OvertureStatus.NO_DATA,
            reason=reason,
        )

    @classmethod
    def invalid_input(
        cls, *, reason: str
    ) -> OvertureResult[dict[int, StatsPort]]:
        return cls(
            stats={},
            meta={"status": OvertureStatus.INVALID_INPUT.value},
            direction_stats={},
            status=OvertureStatus.INVALID_INPUT,
            reason=reason,
        )

    @classmethod
    def skipped(
        cls, *, reason: str
    ) -> OvertureResult[dict[int, StatsPort]]:
        return cls(
            stats={},
            meta={"status": OvertureStatus.SKIPPED.value},
            direction_stats={},
            status=OvertureStatus.SKIPPED,
            reason=reason,
        )

    @classmethod
    def error_result(
        cls,
        exc: BaseException,
        *,
        cache_signature: str | None = None,
    ) -> OvertureResult[dict[int, StatsPort]]:
        return cls(
            stats={},
            meta={
                "status": OvertureStatus.ERROR.value,
                "error": str(exc),
                "cache_signature": cache_signature,
            },
            direction_stats={},
            status=OvertureStatus.ERROR,
            error=str(exc),
        )


__all__ = ["OvertureResult", "OvertureStatus"]
