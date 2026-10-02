"""Аргументы дедупликации перекрывающихся направлений."""
from __future__ import annotations

import argparse


def add_dedup_args(parser: argparse.ArgumentParser) -> None:
    """Дедупликация направлений (буфер, порог, профиль)."""
    # Дедупликация
    parser.add_argument(
        "--dedup",
        action="store_true",
        help="Включить дедупликацию перекрывающихся направлений.",
    )
    parser.add_argument(
        "--dedup-buffer",
        type=float,
        default=500.0,
        help="Ширина пространственного буфера для поиска перекрытий в метрах.",
    )
    parser.add_argument(
        "--dedup-threshold",
        type=float,
        default=0.70,
        help="Порог перекрытия K для признания направления избыточным.",
    )
    parser.add_argument(
        "--dedup-passes",
        type=int,
        default=1,
        help="Количество последовательных проходов дедупликации; 1 — один проход.",
    )
    parser.add_argument(
        "--dedup-unique-km",
        dest="dedup_unique_km",
        type=float,
        default=0.0,
        help="Минимальная длина уникального (неперекрытого) участка в км: "
        "направления с таким участком защищены от удаления при дедупликации "
        "(0 — защита отключена).",
    )
    parser.add_argument(
        "--no-dedup-cache",
        dest="dedup_cache",
        action="store_false",
        help="Не кэшировать результат dedup_analyze на диск (pickle). По умолчанию "
        "кэш включён и ускоряет повторные прогоны того же набора маршрутов.",
    )
    parser.add_argument(
        "--dedup-cache-dir",
        dest="dedup_cache_dir",
        type=str,
        default=None,
        help="Каталог кэша dedup_analyze (по умолчанию: ./.wikiroutes_dedup_cache).",
    )
    parser.add_argument(
        "--dedup-approx",
        dest="dedup_approx",
        action="store_true",
        help="Черновой проход покрытия точечным сэмплированием вместо точных "
        "пересечений (быстрее, но приближённо). Точность восстанавливается "
        "для спорных пар через --dedup-approx-margin.",
    )
    parser.add_argument(
        "--dedup-approx-step",
        dest="dedup_approx_step",
        type=float,
        default=None,
        help="Шаг сэмплирования в метрах (по умолчанию: buffer_r/4). Меньше — точнее.",
    )
    parser.add_argument(
        "--dedup-approx-margin",
        dest="dedup_approx_margin",
        type=float,
        default=0.0,
        help="Двухпроходный режим: пары с K2 в пределах порога ±margin "
        "пересчитываются точно (0 — без уточнения).",
    )
    parser.add_argument(
        "--dedup-profile",
        dest="dedup_profile",
        choices=["exact", "fast"],
        default="exact",
        help="Профиль геометрии dedup (Tier 2): 'exact' — текущая точность "
        "(по умолчанию); 'fast' — понижение точности (simplify/set_precision/"
        "плоский буфер/grid_size в union) для ускорения, слегка меняет покрытие.",
    )
    parser.add_argument(
        "--no-dedup-unique-net",
        dest="dedup_unique_net",
        action="store_false",
        help="Не считать глобальный unique_net (дорогой union всех направлений). "
        "Тогда unique_km/km_coef в отчёте dedup_analyze принимаются равными "
        "total (коэффициент 1.0); итоговые метрики сети считаются отдельно.",
    )
