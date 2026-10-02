"""Лист XLSX с результатами дедупликации."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..support import format_dedup_id, source_label
from .helpers import finish_sheet, safe_append


def write_dedup_sheet(
    wb: Any,
    dedup_removed: Sequence[Mapping[str, Any]],
) -> None:
    """Лист «Удаление дублирования»."""
    ws = wb.create_sheet("Удаление дублирования")
    headers = [
        "Шаг",
        "Направление (ID)",
        "Тип",
        "Название",
        "Источник",
        "Закрывает пар",
        "Партнёры (Kmax)",
        "Из-за маршрута №",
        "Из-за маршрута тип",
        "Из-за направления (ID)",
        "Причина",
        "Длина, км",
    ]
    safe_append(ws, headers)

    for record in dedup_removed:
        blocker = record.get("представитель")
        safe_append(
            ws,
            [
                record["шаг"],
                format_dedup_id(record["маршрут"]),
                record["тип"],
                record["название"],
                source_label(record.get("источник", "")),
                record["закрывает пар"],
                record["партнёры (Kmax)"],
                record.get("представитель_название", ""),
                record.get("представитель_тип", ""),
                format_dedup_id(blocker) if blocker is not None else "",
                record["причина"],
                record["длина, км"],
            ],
        )

    finish_sheet(
        ws,
        headers,
        num_fmt={12: "0.00"},
        wrap={7, 11},
        fixed_widths=[8, 18, 10, 20, 12, 15, 20, 12, 12, 18, 40, 10],
    )
