"""Основной runtime CLI: разбор команд и запуск pipeline."""

from __future__ import annotations

import contextlib
import sys

from .cache import JsonCache
from .cli_args import parse_args
from .cli_batch import run_batch as _run_batch
from .cli_export import export_outputs
from .config import CliConfig, CliConfigError, build_cli_config
from .errors import CatalogLoadError
from .http_client import SessionProvider
from .osm.routes import OSM_CACHE_ENTRY_BYTES
from .pipeline import PipelineContext, run_pipeline
from .report import Reporter

__all__ = ["main", "parse_args", "run_batch_cli"]


def run_batch_cli(batch_file: str, *, reset: bool = False) -> int:
    """Выполняет команды из batch-файла через специализированный модуль."""
    return _run_batch(batch_file, main, reset=reset)


def _run_application(config: CliConfig) -> int:
    """Создаёт runtime-зависимости, запускает pipeline и экспортирует результат."""
    read_cache = not config.no_cache and not config.refresh
    write_cache = not config.no_cache
    cache = JsonCache(
        config.cache_dir,
        read=read_cache,
        write=write_cache,
        kind_limits={
            "osm_routes": OSM_CACHE_ENTRY_BYTES,
        },
    )

    print("=" * 65)
    print("  Экспорт сети: XLSX (полный) + KML (все отфильтрованные)")
    print("  Источник: ru.wikiroutes.info")
    print("=" * 65)
    print(
        f"\nГород: {config.city_input}\n"
        f"Каталог: {config.catalog_url}\n"
        f"Форматы: {', '.join(sorted(config.output_formats))}"
    )
    if config.route_catalog_city:
        print(f"Маршруты привязаны из каталога города: {config.route_catalog_city}")
    print("\n[1/4] Загрузка каталога...")

    with SessionProvider() as sessions:
        reporter = Reporter(echo=True)
        try:
            result = run_pipeline(
                config,
                PipelineContext(cache=cache, sessions=sessions, reporter=reporter),
            )
        except CatalogLoadError as exc:
            raise RuntimeError(f"ОШИБКА загрузки каталога: {exc}") from exc
        except (CliConfigError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc

        export_outputs(config, result)

    print("\nГотово.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Запускает CLI в обычном или пакетном режиме."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError):
                reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv)

    try:
        if args.batch:
            return run_batch_cli(args.batch, reset=args.batch_reset)

        config = build_cli_config(args)
        return _run_application(config)
    except KeyboardInterrupt:
        print("\nПрервано пользователем", file=sys.stderr)
        return 1
    except (RuntimeError, CliConfigError, ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
