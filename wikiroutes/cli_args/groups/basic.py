"""Базовые аргументы CLI: город, параллелизм, формат вывода, пакетный режим и кэш."""
from __future__ import annotations

import argparse

from ...constants import MAX_WORKERS
from ...config import DEFAULT_CACHE_DIR


def add_basic_args(parser: argparse.ArgumentParser) -> None:
    """Город, сеть, форматы вывода и параллелизм."""
    # Основные
    parser.add_argument(
        "city_arg",
        nargs="?",
        default=None,
        help="Город, его идентификатор или URL каталога.",
    )
    parser.add_argument(
        "--city",
        dest="city_flag",
        default=None,
        help="Город или идентификатор города; имеет приоритет над позиционным аргументом.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Пользовательский URL каталога маршрутов вместо автоматически построенного.",
    )
    parser.add_argument(
        "--type",
        action="append",
        default=None,
        help="Оставить только указанные типы маршрутов; можно задавать несколько раз или через запятую.",
    )
    parser.add_argument(
        "--no-type",
        action="append",
        default=None,
        help="Исключить тип маршрута; можно задавать несколько раз или через запятую.",
    )
    parser.add_argument(
        "--route",
        type=str,
        default=None,
        help="Обработать только маршрут с указанным названием или идентификатором.",
    )
    parser.add_argument(
        "--map-type",
        dest="map_type",
        type=str,
        default=None,
        help="Тип направления для отдельной карты (напр. 'tram'). На карте показываются "
        "выжившие направления этого типа и направления, которые их заменили при "
        "дедупликации (дубликаты).",
    )
    parser.add_argument(
        "--stops",
        action="store_true",
        help="Включить данные об остановках в результирующий экспорт.",
    )
    parser.add_argument(
        "--format",
        default="xlsx,kml",
        help="Форматы экспорта через запятую; по умолчанию xlsx,kml.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Базовое имя или путь для выходных файлов.",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=MAX_WORKERS,
        help="Количество потоков для параллельной загрузки маршрутов.",
    )


def add_misc_args(parser: argparse.ArgumentParser) -> None:
    """Пакетный режим и кэш."""
    # Пакетный режим
    parser.add_argument(
        "--batch",
        default=None,
        help="Файл с командами для последовательного запуска; пустые строки, заголовки и строки '#' пропускаются.",
    )
    parser.add_argument(
        "--batch-reset",
        action="store_true",
        help="Пакетный режим: сбросить сохранённое состояние и выполнить все команды заново.",
    )

    # Кэш
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Не использовать кэш при загрузке данных.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Принудительно обновить данные в кэше.",
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help="Каталог локального кэша; по умолчанию D:\\Programs\\Cities2\\wikiroutes_cache (можно переопределить WIKIROUTES_CACHE_DIR).",
    )
