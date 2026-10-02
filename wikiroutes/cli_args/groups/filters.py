"""Фильтры маршрутов: геометрия, граница города, маршруты из OSM."""
from __future__ import annotations

import argparse

def add_filter_args(parser: argparse.ArgumentParser) -> None:
    """Ограничения по непрямолинейности, длине, радиусу и границе города."""
    # Фильтры
    parser.add_argument(
        "--curv",
        type=float,
        default=0.0,
        help="Максимальный коэффициент непрямолинейности маршрута; 0 отключает ограничение.",
    )
    parser.add_argument(
        "--minlen",
        type=float,
        default=None,
        help="Минимальная длина маршрута в километрах; 0 отключает ограничение.",
    )
    parser.add_argument(
        "--maxlen",
        type=float,
        default=None,
        help="Максимальная длина маршрута в километрах; 0 отключает ограничение.",
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=None,
        help="Максимальное расстояние маршрута от заданного центра в километрах; 0 отключает ограничение.",
    )
    parser.add_argument(
        "--center-lat",
        type=float,
        default=None,
        help="Широта центра для фильтра --radius.",
    )
    parser.add_argument(
        "--center-lon",
        type=float,
        default=None,
        help="Долгота центра для фильтра --radius.",
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="Оставить только активные маршруты.",
    )
    parser.add_argument(
        "--no-boundary",
        dest="boundary",
        action="store_false",
        default=True,
        help="Отключить автоматическое получение границы города из OSM и фильтр маршрутов по ней.",
    )
    parser.add_argument(
        "--boundary-buffer",
        type=float,
        default=500.0,
        help="Расширение границы города в метрах при фильтрации (допуск для маршрутов у края).",
    )
    parser.add_argument(
        "--boundary-country",
        dest="boundary_country",
        type=str,
        default="ru",
        help="ISO-код страны (по умолчанию 'ru') для разрешения омонимов при поиске границы "
        "города в OSM (напр. 'dzerzhynsk' → российский Дзержинск). При отсутствии результата "
        "по стране делается глобальный запрос (это спасает Крым, помеченный в OSM как Украина).",
    )
    parser.add_argument(
        "--boundary-extra",
        dest="boundary_extra",
        type=str,
        default="",
        help="Дополнительные relation-id границ OSM (через запятую) для склейки с границей "
        "города (объединение unary_union). Напр. '--boundary-extra 12345,67890' — муниципалитет "
        "и т.п. Загружаются из Overpass.",
    )
    parser.add_argument(
        "--boundary-geojson",
        dest="boundary_geojson",
        action="store_true",
        help="Экспортировать использованную границу города в отдельный GeoJSON-файл.",
    )
    parser.add_argument(
        "--boundary-buildings",
        dest="boundary_buildings",
        type=str,
        nargs="?",
        const="auto",
        default=None,
        help=(
            "Построить границу города как «городскую территорию» по площади "
            "зданий Overture вокруг центра из OSM (US Census Bureau: ядро 10%%, "
            "раствор 5%%, мин. 25 км², смыкание при разрыве <800 м, анклавы "
            "до 6 миль). Значение — файл/каталог "
            "зданий Overture (parquet/geojson/geoparquet) или URL; без значения "
            "тема buildings/building автозагружается (требуется сеть). "
            "Без флага используется граница OSM."
        ),
    )
    parser.add_argument(
        "--max-route-number",
        type=int,
        default=0,
        help="Максимальный номер маршрута и правило отсева маршрутов без номера или со скобками; 0 отключает фильтр.",
    )

    # Маршруты из OSM
    parser.add_argument(
        "--osm-routes",
        action="store_true",
        help="Добавить маршруты общественного транспорта из OSM (Overpass) к общему "
        "списку. Отношения route становятся направлениями и группируются в маршруты "
        "по номеру (ref) и типу транспорта; запрос строится по bbox границы города "
        "(кэшируется в каталоге кэша).",
    )
