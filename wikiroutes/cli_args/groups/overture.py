"""Аргументы Overture Maps: показатели и POI."""
from __future__ import annotations

import argparse


def add_overture_args(parser: argparse.ArgumentParser) -> None:
    """Показатели по Overture Maps и POI."""
    # Overture Maps
    parser.add_argument(
        "--overture",
        action="store_true",
        help="Рассчитать показатели по данным Overture Maps.",
    )
    parser.add_argument(
        "--overture-file",
        default=None,
        help="Локальный файл Overture Maps вместо автоматической загрузки.",
    )
    parser.add_argument(
        "--overture-buffer",
        type=float,
        default=500.0,
        help="Радиус буфера Overture Maps вокруг маршрута в метрах.",
    )
    parser.add_argument(
        "--overture-theme",
        default="place",
        help="Тема Overture для автозагрузки: place (POI), building, segment и т.д.",
    )
    parser.add_argument(
        "--overture-release",
        default=None,
        help="Версия выпуска Overture Maps; по умолчанию используется OVERTURE_RELEASE или актуальный выпуск.",
    )
    parser.add_argument(
        "--overture-download-retries",
        type=int,
        default=3,
        help="Число повторов автоматической загрузки Overture при сетевых сбоях (по умолчанию 3).",
    )

    # POI по остановкам (Overture place)
    parser.add_argument(
        "--poi",
        action="store_true",
        help="Включить маршрутизацию с учётом POI (Overture place).",
    )
    parser.add_argument(
        "--poi-stops",
        action="store_true",
        help="Подсчёт POI в радиусе остановок по данным Overture Maps (place).",
    )
    parser.add_argument(
        "--poi-stops-file",
        default=None,
        help="Файл Overture place (geoparquet) для подсчёта POI по остановкам.",
    )
    parser.add_argument(
        "--poi-stops-buffer",
        type=float,
        default=500.0,
        help="Радиус буфера POI вокруг остановок в метрах (по умолчанию 500).",
    )
    parser.add_argument(
        "--poi-buffer",
        type=float,
        default=None,
        help="Радиус буфера POI (только для совместимости с --poi).",
    )
