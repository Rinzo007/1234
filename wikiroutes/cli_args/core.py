"""Оркестрация разбора аргументов командной строки CLI.

Группы аргументов распределены по подмодулям ``cli_args``:
    ``basic`` (город/формат/пакет/кэш), ``filters`` (фильтры и граница),
    ``heatmap``, ``dedup``, ``ideas``, ``overture``, ``passenger``.
"""

from __future__ import annotations

import argparse

from .groups import (
    add_basic_args,
    add_dedup_args,
    add_filter_args,
    add_heatmap_args,
    add_ideas_args,
    add_misc_args,
    add_overture_args,
    add_passenger_args,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Экспорт сети общественного транспорта (XLSX + KML)."
    )
    add_basic_args(parser)
    add_filter_args(parser)
    add_heatmap_args(parser)
    add_dedup_args(parser)
    add_ideas_args(parser)
    add_overture_args(parser)
    add_passenger_args(parser)
    add_misc_args(parser)
    args = parser.parse_args(argv)
    for action in parser._actions:
        action.default = argparse.SUPPRESS
    return args