"""Аргументы идей пассажиров и экспорта GeoJSON."""
from __future__ import annotations

import argparse


def add_ideas_args(parser: argparse.ArgumentParser) -> None:
    """Идеи пассажиров и вспомогательные GeoJSON-экспорты."""
    # Идеи пассажиров
    parser.add_argument(
        "--ideas",
        action="store_true",
        help="Загрузить и добавить в расчёт идеи пассажиров.",
    )
    parser.add_argument(
        "--ideas-max-pages",
        type=int,
        default=None,
        help="Максимальное количество страниц при загрузке идей пассажиров.",
    )
    parser.add_argument(
        "--ideas-search",
        type=str,
        default=None,
        help="Фильтр идей по заголовку; несколько поисковых терминов разделяются запятыми.",
    )
    parser.add_argument(
        "--ideas-sort",
        type=str,
        default=None,
        help="Параметр сортировки при загрузке идей пассажиров.",
    )
    parser.add_argument(
        "--terminals-geojson",
        action="store_true",
        help="Экспортировать конечные остановки отдельным GeoJSON-файлом "
        "(FeatureCollection точек WGS-84: [lon, lat], название из properties.name).",
    )
