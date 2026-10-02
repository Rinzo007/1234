"""Команда пережатия кэша: сжимает несжатые записи, написанные старым кодом.

До включения gzip-сжатия в ``JsonCache`` все записи хранились обычным JSON.
Эта команда проходит по файлам ``*.json`` в каталоге кэша и пережимает gzip
несжатые записи не меньше порога — остальные (уже сжатые, мелкие, не-JSON)
не трогаются. Формат совместим: ``JsonCache.get`` прозрачно читает и сжатые,
и несжатые записи, поэтому миграция безопасна пошагово.
"""

from __future__ import annotations

import argparse
import sys

from .cache import DEFAULT_COMPRESS_MIN_BYTES, compact_cache

__all__ = ["cache_compact_main"]

CACHE_KINDS = [
    "catalog",
    "idea_geo",
    "idea_list",
    "osm_boundary",
    "osm_routes",
    "overture_auto",
    "parts",
    "poi",
    "poi_stops",
    "route",
    "route_token",
]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m wikiroutes.cache_compact",
        description=(
            "Пережать несжатые записи файлового кэша gzip "
            "(миграция после включения сжатия в JsonCache)."
        ),
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default="wikiroutes_cache",
        help="Каталог кэша (по умолчанию: wikiroutes_cache).",
    )
    parser.add_argument(
        "--kinds",
        default=None,
        help=(
            "Ограничить пережатие только указанными видами через запятую. "
            f"Доступные виды: {', '.join(CACHE_KINDS)}."
        ),
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=DEFAULT_COMPRESS_MIN_BYTES,
        help=(
            "Минимальный размер записи (в байтах) для сжатия; "
            f"по умолчанию {DEFAULT_COMPRESS_MIN_BYTES}."
        ),
    )
    return parser.parse_args(argv)


def cache_compact_main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(argv)
    kinds = None
    if args.kinds:
        kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
        unknown = kinds - set(CACHE_KINDS)
        if unknown:
            print(
                f"Ошибка: неизвестные виды кэша: {', '.join(sorted(unknown))}\n"
                f"Доступные: {', '.join(CACHE_KINDS)}",
                file=sys.stderr,
            )
            return 1

    try:
        stats = compact_cache(args.directory, compress_min_bytes=args.min_bytes, kinds=kinds)
    except OSError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    scope = ", ".join(sorted(kinds)) if kinds else "все"
    print(
        f"Кэш: {args.directory} (виды: {scope})\n"
        f"  просмотрено: {stats['scanned']}"
        f"  | пережато gzip: {stats['compressed']}"
        f"  | уже сжато: {stats['already']}"
        f"  | меньше порога: {stats['too_small']}"
        f"  | пропущено: {stats['skipped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(cache_compact_main())