"""Отпечаток сети и ключ кэша скимов T1 (§32.4).

Документ требует инвалидацию кэша **по содержимому** сети, а не по флагу
«грязно»: флаг теряется при расхождении вкладок **[T]** (§32.4). Поэтому
отпечаток считается по структуре сети, и любое изменение линии, пути,
интервала, остановки или пешего перехода даёт новое значение.

Отпечаток не входит в расчёт T0 — это замер из §32.7: 21 мс против 2,1 мс
получилось именно потому, что длинную промежуточную строку и BigInt убрали.
Здесь тот же приём: CRC-32 накапливается по частям, без сборки строки
целиком.

Порядок обхода — канонический (§32.5). Это не педантизм: ``stop_routes``
хранится множеством, а порядок обхода множества строк меняется между
процессами вместе с ``PYTHONHASHSEED``. Отпечаток, посчитанный по такому
порядку, был бы разным при одном и том же городе — и кэш T1 выдавал бы
скимы одной сети как разные.

Персистентность (§32.4): сохраняется только T0. T1–T3 пересчитываются,
поэтому хранилище ниже намеренно живёт только в памяти и не пишет на диск.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import struct
from typing import Any, Generic, TypeVar
from zlib import crc32

from .params import ROUTING_WINDOWS

from .network import _Network


#: Сколько наборов скимов T1 держим на текущей сети (§32.4: «не более
#: 4 сетов T1, прошлые вытесняются по редкоте использования»). Число равно
#: числу окон маршрутизации, поэтому полезного набора хватает ровно на один
#: полный прогон T1.
T1_CACHE_CAPACITY: int = len(ROUTING_WINDOWS)

#: Индексы окон маршрутизации (§32.4: ``windowIndex`` — индекс одного из
#: четырёх окон маршрутизации, а не части суток Takt).
WINDOW_INDEX: dict[str, int] = {
    code: index for index, (code, _start, _end) in enumerate(ROUTING_WINDOWS)
}


def _stable_id(value: Any) -> str:
    """Текстовое имя объекта, устойчивое между процессами.

    Идентификаторы маршрутов — int или str, но тип входит в ключ: иначе
    маршрут ``1`` и маршрут ``"1"`` дали бы одинаковый вклад в отпечаток.
    """
    return f"{type(value).__name__}:{value}"


def _pack(value: float) -> bytes:
    """Упаковка числа в 8 байт с фиксированным порядком.

    ``repr`` числа зависит от его точности и версии интерпретатора, а
    ``pack`` — нет: одинаковое значение даёт одинаковые байты везде.
    """
    return struct.pack("<d", float(value))


def network_fingerprint(net: _Network) -> str:
    """Отпечаток сети игрока — часть ключа кэша T1 (§32.4).

    В отпечаток входит всё, что меняет результат маршрутизации: геометрия
    остановок и их радиусы, пешие переходы, режимы линий, интервалы **по
    каждому окну** (интервал вне окна не влияет на этот прогон) и сами
    направления. Любое изменение сети даёт новое значение, что и требуется:
    инвалидация по содержимому, а не по флагу.
    """
    crc = 0

    def feed(*parts: bytes) -> None:
        nonlocal crc
        for part in parts:
            crc = crc32(part, crc)

    # Остановки. Порядок канонический — по ключу, а не по порядку вставки
    # (§32.5): словари с ключами-строками, без плавающего порядка обхода.
    ordered_keys = sorted(range(len(net.keys)), key=lambda i: net.keys[i])
    feed(struct.pack("<I", len(net.keys)))
    for index in ordered_keys:
        feed(
            net.keys[index].encode("utf-8"),
            b"\x1f",
            _pack(net.lats[index]),
            _pack(net.lons[index]),
            _pack(net.radii[index]),
            b"\x1e",
        )
        # stop_routes — множество: порядок обхода не задан, сортируем.
        routes = sorted(net.stop_routes[index], key=_stable_id)
        feed(struct.pack("<I", len(routes)))
        for route_id in routes:
            feed(_stable_id(route_id).encode("utf-8"), b"\x1f")

    # Пешие переходы. Сверхние узлы пишутся **ключом**, а не индексом: индексы
    # узлов назначаются в порядке построения и зависят от порядка линий во
    # входе, а сеть от перестановки входа не меняется.
    for index in ordered_keys:
        walk_edges = sorted(
            ((net.keys[to_idx], walk_s) for to_idx, walk_s in net.walk_from[index]),
            key=lambda item: item[0],
        )
        feed(struct.pack("<I", len(walk_edges)))
        for to_key, walk_s in walk_edges:
            feed(to_key.encode("utf-8"), b"\x1f", _pack(walk_s))

    # Линии: режим, интервалы по окнам, направления и время езды по секциям.
    route_ids = sorted(net.route_schedule, key=_stable_id)
    feed(struct.pack("<I", len(route_ids)))
    for route_id in route_ids:
        schedule = net.route_schedule[route_id]
        feed(_stable_id(route_id).encode("utf-8"))
        feed(net.route_mode.get(route_id, "").encode("utf-8"), b"\x1f")
        for code, _start, _end in ROUTING_WINDOWS:
            minutes = schedule.minutes_for(code)
            # None и 0.0 — разные значения одного признака (§35.1):
            # интервал, отсутствующий в окне, не равен нулевому интервалу.
            feed(b"\x00" if minutes is None else b"\x01")
            if minutes is not None:
                feed(_pack(minutes))
        feed(b"\x1e")

    for route_id, dir_idx, order_nodes in sorted(
        net.directions, key=lambda item: (_stable_id(item[0]), item[1])
    ):
        feed(_stable_id(route_id).encode("utf-8"), b"\x1f")
        feed(struct.pack("<i", dir_idx), struct.pack("<I", len(order_nodes)))
        previous: int | None = None
        for position, node_index in enumerate(order_nodes):
            feed(net.keys[node_index].encode("utf-8"), b"\x1f")
            if previous is not None:
                ride_s = 0.0
                for _src, _d, seg_idx, dst, seconds in net.ride_from[previous]:
                    if dst == node_index and seg_idx == position - 1:
                        ride_s = seconds
                        break
                feed(_pack(ride_s))
            previous = node_index
        feed(b"\x1e")

    # Шаг сети и режимы, которые на скимы не влияют, но при смене версии
    # движка обязаны инвалидировать кэш: иначе старый расчёт переживёт
    # изменение модели.
    feed(b"passenger-t1-v1")

    return f"{crc & 0xFFFFFFFF:08x}"


@dataclass(frozen=True, slots=True)
class T1CacheKey:
    """Ключ кэша T1: ``(cityId, cityDataVersion, networkHash, windowIndex)``.

    ``windowIndex`` — индекс одного из **четырёх окон маршрутизации**
    (§5.6a), а не части суток Takt: расписание окна собрано из столбцов
    Takt, с которыми оно пересекается, и одна часть суток может соответствовать
    нескольким окнам.
    """

    city_id: str
    city_data_version: str
    network_hash: str
    window_index: int

    def __str__(self) -> str:
        return (
            f"{self.city_id}@{self.city_data_version}"
            f"/{self.network_hash}/w{self.window_index}"
        )


def t1_cache_key(
    city_id: str,
    city_data_version: str,
    network_hash: str,
    window: str,
) -> T1CacheKey:
    """Собирает ключ по имени окна маршрутизации."""
    index = WINDOW_INDEX.get(window)
    if index is None:
        raise KeyError(f"окно не является окном маршрутизации: {window!r}")
    return T1CacheKey(
        city_id=str(city_id),
        city_data_version=str(city_data_version),
        network_hash=str(network_hash),
        window_index=index,
    )


_T = TypeVar("_T")


class T1SkimStore(Generic[_T]):
    """Хранилище скимов T1: не более ``T1_CACHE_CAPACITY`` наборов (§32.4).

    Вытеснение — по редкоте использования. Хранилище только в памяти:
    персистентен лишь T0 (§32.4), а сериализация скимов дорога при малом
    выигрыше — сеть меняется каждый ход.
    """

    __slots__ = ("_items", "_capacity", "hits", "misses", "evictions")

    def __init__(self, capacity: int = T1_CACHE_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("ёмкость кэша T1 должна быть не меньше 1")
        self._capacity = int(capacity)
        self._items: OrderedDict[T1CacheKey, _T] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: T1CacheKey) -> _T | None:
        """Возвращает скимы по ключу, обновляя позицию в порядке использования."""
        value = self._items.get(key)
        if value is None:
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: T1CacheKey, value: _T) -> None:
        """Кладёт скимы и вытесняет самый давно не использованный набор."""
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
            self.evictions += 1

    def keys_in_canonical_order(self) -> tuple[T1CacheKey, ...]:
        """Ключи в каноническом порядке: окно ↑ (§32.5, §32.6).

        Порядок обхода ``OrderedDict`` — порядок использования, то есть он
        зависит от того, что игрок открывал раньше. Для отчёта и для
        сравнения A/B нужен порядок, который от поведения не зависит.
        """
        return tuple(
            sorted(self._items, key=lambda item: item.window_index)
        )

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items