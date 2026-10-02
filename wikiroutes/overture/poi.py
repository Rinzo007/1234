"""POI-специфичное разрешение файла Overture place."""

from collections.abc import Callable
from pathlib import Path


def resolve_poi_place_file(
    override: str | None,
    configured: str | None,
    bbox: tuple[float, float, float, float] | None,
    cache_dir: str | Path,
    release: str | None,
    retries: int,
    warn: Callable[[str], None],
) -> str | None:
    """Разрешает файл POI: переопределение → конфиг → автозагрузка Overture."""
    if override is not None:
        return override
    if configured is not None:
        return configured
    if bbox is None:
        warn("  ⚠ POI-stops: bbox не определён — расчёт пропущен")
        return None

    # Импорт внутри функции: ``overture.download`` реэкспортирует эту же
    # функцию, и импорт на уровне модуля замкнул бы цикл
    # download → poi → download. Ленивый импорт разрывает цикл без прокси.
    from .download import auto_download_overture

    warn("  POI по остановкам: автозагрузка Overture place...")
    poi_stops_file = auto_download_overture(
        bbox,
        cache_dir,
        theme="place",
        release=release,
        retries=retries,
    )
    if poi_stops_file is None:
        warn("  ⚠ POI-stops: не удалось загрузить Overture place — пропущено")
    return poi_stops_file


__all__ = ["resolve_poi_place_file"]
