"""Аргументы расчёта пассажиропотока."""
from __future__ import annotations

import argparse


def add_passenger_args(parser: argparse.ArgumentParser) -> None:
    """Расчёт пассажиропотока и вывод его в XLSX."""
    parser.add_argument(
        "--passenger",
        action="store_true",
        help="Рассчитать пассажиропоток и добавить лист «Пассажиропоток» в XLSX.",
    )
    parser.add_argument(
        "--passenger-pop",
        default=None,
        help="Растр GHSL GHS_POP для расчёта населения; по умолчанию берётся из "
             "переменной WIKIROUTES_GHS_POP или стандартного пути.",
    )
    parser.add_argument(
        "--passenger-built",
        default=None,
        help="Растр GHSL GHS_BUILT_S для оценки рабочих мест по застройке. "
             "Без него слой населения не даёт рабочих мест, и OD-матрицу "
             "построить не из чего (§5.5).",
    )
    parser.add_argument(
        "--passenger-points-per-km2",
        type=float,
        default=10.0,
        help="Плотность точек спроса на км². Больше — значит дробление ячеек "
             "растра точнее, чем сами данные.",
    )
    parser.add_argument(
        "--passenger-top-k-destinations",
        type=int,
        default=0,
        help="Оставить у каждого начала только N самых вероятных концов (0 — без "
             "ограничения). Пары OD растут как произведение, поэтому на большом "
             "городе это единственный способ уложиться в потолок, не теряя "
             "пространственное разрешение точек спроса.",
    )
    parser.add_argument(
        "--passenger-route-limit",
        type=int,
        default=0,
        help="Ограничить число маршрутов в расчёте (0 — без ограничения). "
             "Нужен для быстрой проверки на большом городе.",
    )
