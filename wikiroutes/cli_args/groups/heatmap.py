"""Аргументы тепловой карты маршрутов."""
from __future__ import annotations

import argparse


def add_heatmap_args(parser: argparse.ArgumentParser) -> None:
    """Параметры тепловой карты."""
    # Тепловая карта
    parser.add_argument(
        "--heatmap",
        action="store_true",
        help="Создать тепловую карту маршрутов.",
    )
    parser.add_argument(
        "--heat-cell",
        type=float,
        default=0.1,
        help="Размер ячейки тепловой карты в километрах.",
    )
    parser.add_argument(
        "--heat-alpha",
        default="ff",
        help="Прозрачность тепловой карты в шестнадцатеричном виде.",
    )
    parser.add_argument(
        "--heat-gamma",
        type=float,
        default=0.6,
        help="Показатель усиления плотности тепловой карты.",
    )
    parser.add_argument(
        "--heat-top",
        type=float,
        default=35.0,
        help="Верхний порог высоты или интенсивности тепловой карты.",
    )
    parser.add_argument(
        "--heat-max-height",
        type=float,
        default=400.0,
        help="Максимальная высота объектов тепловой карты.",
    )
    parser.add_argument(
        "--heat-flat",
        action="store_true",
        help="Отключить объёмное представление тепловой карты.",
    )
    parser.add_argument(
        "--heat-smooth",
        action="store_true",
        help="Сгладить тепловую карту после расчёта.",
    )
