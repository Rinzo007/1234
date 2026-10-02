import math
from typing import Any

from .constants import LAT_SHIFT, LON_SHIFT, XOR_KEY
from .type_defs import Coordinate


def decode_xor(value: str) -> str:
    """Декодирует строку WikiRoutes простым позиционным XOR-ключом."""
    return "".join(
        chr((ord(char) ^ XOR_KEY[index & 15]) & 0xFFFF)
        for index, char in enumerate(value)
    )


def decode_obj(obj: Any) -> Any:
    """Рекурсивно декодирует строки во вложенном JSON-подобном объекте.

    Списки и словари обходятся рекурсивно; остальные значения возвращаются
    без изменений. Имена ключей словарей намеренно не декодируются.
    """
    if isinstance(obj, str):
        return decode_xor(obj)

    if isinstance(obj, list):
        return [decode_obj(item) for item in obj]

    if isinstance(obj, dict):
        return {key: decode_obj(value) for key, value in obj.items()}

    return obj


def _to_float(value: Any) -> float | None:
    """Преобразует значение к конечному ``float`` или возвращает ``None``."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None

    return result if math.isfinite(result) else None


def unshift_coords(raw: Any) -> tuple[Coordinate, ...]:
    """Декодирует и проверяет список координат WikiRoutes.

    Из каждой записи берутся ``latitude`` и ``longitude``, после чего удаляется
    позиционный сдвиг API. Координаты за пределами географических диапазонов
    отбрасываются.
    """
    if not isinstance(raw, list):
        return ()

    coords: list[Coordinate] = []

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue

        lat = _to_float(item.get("latitude"))
        lon = _to_float(item.get("longitude"))

        if lat is None or lon is None:
            continue

        lat = round(1e7 * (lat - LAT_SHIFT[index & 7])) / 1e7
        lon = round(1e7 * (lon - LON_SHIFT[index & 7])) / 1e7

        if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
            coords.append((lat, lon))

    return tuple(coords)
