"""Публичный API Overture без тяжёлых импортов на import-time."""

from __future__ import annotations

__all__ = [
    "OvertureConfig",
    "OvertureResult",
    "OvertureStatus",
    "compute_overture",
    "compute_overture_result",
]


def __getattr__(name: str):
    if name == "OvertureConfig":
        from .config import OvertureConfig
        return OvertureConfig
    if name in {"OvertureResult", "OvertureStatus"}:
        from .result import OvertureResult, OvertureStatus
        return {"OvertureResult": OvertureResult, "OvertureStatus": OvertureStatus}[name]
    if name in {"compute_overture", "compute_overture_result"}:
        from .core import compute_overture, compute_overture_result
        return {
            "compute_overture": compute_overture,
            "compute_overture_result": compute_overture_result,
        }[name]
    raise AttributeError(name)
