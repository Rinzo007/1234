"""Полная загрузка города из Overture: граница, здания, места, дорожная сеть.

Модуль собирает из Overture всё, что в ней **есть**, и явно перечисляет то,
чего в ней нет. Это различие важно не для красоты отчёта, а потому что
пассажирская модель без части этих слоёв не считается вовсе, и по отчёту
должно быть видно, чего именно не хватило.

## Что Overture даёт

| Тема | Что получается | Зачем городу |
|---|---|---|
| ``buildings`` | контуры зданий | граница города и индекс зданий (§30.2) |
| ``places`` | точки интереса с именем и категорией | категории назначения (§5.5), POI-слои |
| ``transportation/segment`` | линии дорог и рельсов с классами | каркас, по которому игрок строит линии |
| ``transportation/connector`` | точки соединения сегментов | вершины графа: без них дорожная сеть несвязна |

## Чего Overture не даёт — и это не умолчание, а факт

- **Темы ``admin`` в релизе нет.** Проверено на живом каталоге: и
  ``admin/locality``, и ``admin/country`` отдают 404. Граница города поэтому
  берётся извне — из файла GeoJSON либо объединением контуров зданий
  (:func:`boundary_from_buildings`), и в манифесте записывается, **откуда**
  она взялась. Молча выдавать за границу что-то другое нельзя.
- **Маршрутов и расписаний общественного транспорта нет.** В сегментах есть
  поле ``routes``, и это легко принять за транспортные линии. Это не так: в
  проверенном bbox Воронежа в ``routes`` лежат ``М-4 «Дон»``, ``Р-22 «Каспий»``,
  ``European route E38``, ``Asian Highway AH61`` — обозначения **автомобильных**
  дорог. Транспортных маршрутов, упорядоченных остановок и интервалов в
  Overture нет; они приходят из каталога перевозчиков. Модуль это фиксирует в
  манифесте полем ``transit_source``.
- **Населения нет.** Слой населения — отдельный продукт (GHSL), см.
  ``passenger.population``.

## Почему манифест без времени

§32.5 требует, чтобы один и тот же год с одной и той же сетью давал один и тот же
результат. Дата загрузки в манифесте сделала бы два одинаковых города
неразличимыми, а хеш содержимого — различимыми. Поэтому в манифесте есть релиз,
хеш содержимого и размеры, и **нет** ``Date.now()``: файл города обязан быть
побайтово воспроизводимым.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
from typing import Any

logger = logging.getLogger("wikiroutes.gis.overture.city")


# ── Граница с явным порядком координат ─────────────────────────────────

#: Темы, которые читает модуль. Порядок — в отчёте о загрузке.
CITY_THEMES: tuple[tuple[str, str], ...] = (
    ("buildings", "building"),
    ("places", "place"),
    ("transportation", "segment"),
    ("transportation", "connector"),
)

#: Темы, которых в релизе нет (проверено на живом STAC).
ABSENT_THEMES: tuple[tuple[str, str], ...] = (
    ("admin", "locality"),
    ("admin", "country"),
)

# ── Игровой манифест (§18.4, §18.4a, §18.4c) ──────────────────────────

#: Значения специального спроса — семь, названных в документе.
SPECIAL_DEMAND: frozenset[str] = frozenset(
    {
        "airport",
        "entertainment",
        "ferry",
        "hospital",
        "park",
        "school",
        "university",
    }
)

#: Сколько регионов в каталоге (§18.4a: «один из 19 регионов»).
#:
#: **Список из 19 регионов документ не перечисляет**, поэтому принадлежность
#: региона проверить нечем, и поле принимается строкой без валидации по списку.
#: Выдумывать список значило бы проверять город по выдуманному правилу.
REGION_COUNT: int = 19

#: Поля игрового манифеста.
#:
#: §18.4b говорит, что регистрация города для игры — «семь полей», но не
#: перечисляет их. Здесь взяты те, которые документ называет обязательными и
#: которые клиент грузит в свою модель (§2964): код, страна, регион,
#: население, источник данных, имя и стартовая камера. Вычисленное качество
#: добавлено отдельно — §18.5 требует, чтобы уровень рубрики был в манифесте,
#: и он несёт пометку происхождения.
GAME_MANIFEST_FIELDS: tuple[str, ...] = (
    "city_code",
    "name",
    "country",
    "region",
    "population",
    "data_source",
    "initial_view_state",
)

#: Поля каталога, которых в игровом манифесте нет **сознательно**.
#:
#: `source_quality` и `level_of_detail` — авторская самооценка, которая в
#: источнике регулярно противоречит вычисленному уровню. §18.4a требует никогда
#: не показывать их рядом с вычисленным уровнем без пометки о происхождении;
#: проще их не включать вовсе. Поля публикации (`author`, `gallery`, `tags`,
#: `update`, `search_aliases`) относятся к каталогу на 406 городов, а не к игре.
CATALOG_ONLY_FIELDS: tuple[str, ...] = (
    "source_quality",
    "level_of_detail",
    "author",
    "github_id",
    "description",
    "tags",
    "gallery",
    "update",
    "search_aliases",
    "file_sizes",
    "grid_statistics",
    "is_test",
    "included_cities",
    "difficulty",
    "last_updated",
    "collaborators",
    "caretakers",
    "deprecation",
    "deprecation_history",
    "derived_from",
)


@dataclass(frozen=True, slots=True)
class CityIdentity:
    """Кто город: поля, по которым он адресуется и показывается.

    Ключ города — пара ``(country, city_code)``, а не один ``city_code``
    (§18.4c): трёхбуквенный код есть сокращение названия, а не идентификатор с
    глобальной гарангией уникальности — в реестре два разных `DAY` в США.
    """

    city_code: str
    name: str
    country: str
    region: str
    population: float
    data_source: str
    latitude: float
    longitude: float
    zoom: float = 12.0
    special_demand: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.city_code or not self.city_code.isupper():
            raise ValueError(
                f"код города обязан быть ЗАГЛАВНЫМИ, получено {self.city_code!r}"
            )
        if not self.country:
            raise ValueError("код страны обязателен: он входит в ключ города")
        unknown = set(self.special_demand) - SPECIAL_DEMAND
        if unknown:
            raise ValueError(
                "неизвестный специальный спрос: "
                + ", ".join(sorted(unknown))
                + "; допустимы: "
                + ", ".join(sorted(SPECIAL_DEMAND))
            )

    @property
    def key(self) -> tuple[str, str]:
        """Ключ города в каталоге — пара, а не одиночный код."""
        return (self.country, self.city_code)

    def initial_view_state(self) -> dict[str, float]:
        """Стартовая камера — вложенный объект (§18.4a)."""
        return {
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
            "zoom": float(self.zoom),
        }


@dataclass(frozen=True, slots=True)
class LatLonBBox:
    """Граница города с именованными полями вместо кортежа.

    Существует из-за конкретной ошибки, а не из-за стиля. В модулях Overture
    bbox везде передаётся как ``(min_lat, min_lon, max_lat, max_lon)``, а
    ``shapely``/``rasterio`` возвращают и ждут ``(min_x, min_y, max_x, max_y)``
    = ``(min_lon, min_lat, max_lon, max_lat)``. Оба порядка валидны, различаются
    местами и неразличимы на глаз: кортеж из четырёх чисел не скажет, что
    перепутан. Перепутанный порядок не выдаёт ошибку — он молча выбирает
    части данных с другой стороны планеты.

    Именно это и произошло при первой проверке этого модуля: bbox Воронежа,
    переданный в порядке rasterio, вернул части, покрывающие Саудовскую
    Аравию, и ответ был «сегментов не найдено» — без единого признака ошибки.
    """

    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def __post_init__(self) -> None:
        if self.min_lat > self.max_lat or self.min_lon > self.max_lon:
            raise ValueError(
                f"перепутаны границы: min > max (lat {self.min_lat}..{self.max_lat}, "
                f"lon {self.min_lon}..{self.max_lon}). Обычно это переставленные "
                "широта и долгота."
            )
        if not all(
            math.isfinite(value)
            for value in (self.min_lat, self.min_lon, self.max_lat, self.max_lon)
        ):
            raise ValueError("граница содержит нечисловое значение")

    @classmethod
    def from_project_tuple(cls, values: tuple[float, float, float, float]) -> "LatLonBBox":
        """Из порядка модулей Overture: ``(min_lat, min_lon, max_lat, max_lon)``."""
        min_lat, min_lon, max_lat, max_lon = (float(v) for v in values)
        return cls(min_lat, min_lon, max_lat, max_lon)

    @classmethod
    def from_xy(cls, values: tuple[float, float, float, float]) -> "LatLonBBox":
        """Из порядка ``shapely``/``rasterio``: ``(min_x, min_y, max_x, max_y)``."""
        min_x, min_y, max_x, max_y = (float(v) for v in values)
        return cls(min_y, min_x, max_y, max_x)

    def as_project_tuple(self) -> tuple[float, float, float, float]:
        """В порядке модулей Overture — то, что ждут все функции загрузки."""
        return (self.min_lat, self.min_lon, self.max_lat, self.max_lon)

    def as_xy(self) -> tuple[float, float, float, float]:
        """В порядке ``shapely``/``rasterio``."""
        return (self.min_lon, self.min_lat, self.max_lon, self.max_lat)

    def contains(self, lat: float, lon: float) -> bool:
        return (
            self.min_lat <= lat <= self.max_lat
            and self.min_lon <= lon <= self.max_lon
        )

    @property
    def area_km2(self) -> float:
        """Площадь прямоугольной границы в км²."""
        mean_lat = (self.min_lat + self.max_lat) / 2.0
        height_km = (self.max_lat - self.min_lat) * 110.574
        width_km = (self.max_lon - self.min_lon) * 111.320 * math.cos(math.radians(mean_lat))
        return height_km * width_km

    def summary(self) -> str:
        return (
            f"lat {self.min_lat:.4f}..{self.max_lat:.4f}, "
            f"lon {self.min_lon:.4f}..{self.max_lon:.4f}, "
            f"{self.area_km2:.0f} км²"
        )


# ── Манифест ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CityProvenance:
    """Откуда взялось содержимое города и чем измерено (§17.3).

    Даты загрузки здесь нет намеренно: она сделала бы два одинаковых города
    неразличимыми. Вместо неё — релиз и хеш содержимого.
    """

    release: str
    themes: dict[str, int] = field(default_factory=dict)
    parts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    boundary_source: str = "unknown"
    transit_source: str = "не в Overture: каталог перевозчиков"
    population_source: str = "не в Overture: отдельный продукт (GHSL)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "release": self.release,
            "themes": dict(sorted(self.themes.items())),
            "parts": {
                key: list(value) for key, value in sorted(self.parts.items())
            },
            "boundary_source": self.boundary_source,
            "transit_source": self.transit_source,
            "population_source": self.population_source,
            "absent_themes": [f"{theme}/{otype}" for theme, otype in ABSENT_THEMES],
        }

    def content_hash(self) -> str:
        """Хеш содержимого: одинаковый город — одинаковый хеш."""
        payload = json.dumps(
            {
                "release": self.release,
                "themes": dict(sorted(self.themes.items())),
                "boundary_source": self.boundary_source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class CityPackage:
    """Загруженный город: геометрия, места, дорожная сеть и манифест.

    Намеренно **не** содержит ни маршрутов общественного транспорта, ни
    расписания, ни населения: в Overture их нет (§33.1 — город это данные, но
    не те данные). Попытка собрать город без них должна падать здесь, а не
    в пассажирском расчёте.
    """

    slug: str
    bbox: LatLonBBox
    boundary: Any
    buildings: Any
    places: Any
    road_network: Any
    provenance: CityProvenance

    def manifest(self) -> dict[str, Any]:
        """Служебная сводка содержимого — **не** манифест города.

        Это то, что загружает открытый город. Манифест, который видит игра,
        строится через :func:`game_manifest`.
        """
        return {
            "bbox": {
                "min_lat": self.bbox.min_lat,
                "min_lon": self.bbox.min_lon,
                "max_lat": self.bbox.max_lat,
                "max_lon": self.bbox.max_lon,
            },
            "counts": {
                "buildings": _frame_len(self.buildings),
                "places": _frame_len(self.places),
                "roads": getattr(self.road_network, "segment_count", 0),
                "road_nodes": getattr(self.road_network, "node_count", 0),
            },
            "provenance": self.provenance.to_dict(),
            "content_hash": self.provenance.content_hash(),
        }

    def game_manifest(
        self,
        identity: CityIdentity,
        quality: Any = None,
        *,
        residents_match: bool | None = None,
    ) -> dict[str, Any]:
        """Игровой манифест города — семь полей плюс вычисленное качество.

        :param identity: кто город: код, страна, регион, население, камера.
        :param quality: результат :func:`rubric.evaluate_rubric`. Уровень
            **не принимается извне строкой**: §18.5 запрещает автору объявлять
            уровень своего города, поэтому манифест получает объект рубрики и
            берёт из него вычисленные значения вместе с версией.
        :param residents_match: результат инварианта ``demand_residents_match``
            (§18.5, одна из десяти обязательных проверок). ``None`` — проверка не
            выполнялась, и это пишется явно, а не как «сошлось».
        """
        manifest: dict[str, Any] = {
            "city_code": identity.city_code,
            "name": identity.name,
            "country": identity.country,
            "region": identity.region,
            "population": float(identity.population),
            "data_source": identity.data_source,
            "initial_view_state": identity.initial_view_state(),
            "special_demand": list(identity.special_demand),
            # Ключ города — пара. Одиночный код допускает столкновение, и в
            # реестре оно уже случалось (два разных `DAY` в США).
            "city_key": list(identity.key),
            "data_quality": _quality_block(quality),
            "demand_residents_match": residents_match,
        }
        return manifest

    def summary(self) -> str:
        manifest = self.manifest()
        counts = manifest["counts"]
        return (
            f"{self.slug}: зданий {counts['buildings']:,}, "
            f"мест {counts['places']:,}, дорог {counts['roads']:,}, "
            f"узлов {counts['road_nodes']:,}; "
            f"релиз {self.provenance.release}, хеш {manifest['content_hash']}"
        )


def _quality_block(quality: Any) -> dict[str, Any]:
    """Блок вычисленного качества с пометкой происхождения.

    Пометка ``origin: computed`` обязательна и здесь не формальность: §18.4a
    запрещает показывать вычисленный уровень рядом с авторской самооценкой без
    указания, откуда какое число. Самооценки в игровом манифесте нет (§30.2
    города — §18.4c), но след остаётся, и он важен при слиянии слоёв.

    Уровень ``unknown`` означает «не оценивали» и отделён от ``absent``
    («оценили, данных нет»): смешивать их нельзя (§27.1, правило 1).
    """
    if quality is None:
        return {
            "level": "unknown",
            "origin": "computed",
            "rubric_version": None,
            "raw_score": None,
            "weighted_score": None,
            "note": "рубрика не вычислялась",
        }
    return {
        "level": quality.level,
        "origin": "computed",
        "rubric_version": quality.rubric_version,
        "raw_score": round(float(quality.raw_score), 6),
        "weighted_score": round(float(quality.weighted_score), 6),
    }


def _frame_len(frame: Any) -> int:
    if frame is None:
        return 0
    try:
        return int(len(frame))
    except TypeError:
        return 0


def _length_m(geometry: Any) -> float:
    """Длина линии в метрах.

    ``geometry.length`` у географической геометрии возвращает **градусы**, а не
    метры, и подставить это число как метры — значит подставить число другой
    величины без единого признака: на широте 51° градус долготы короче
    километра почти вдвое, и время езды (§5.3) уехало бы почти вдвое.
    Поэтому длина считается геодезически на эллипсоиде WGS84.
    """
    if geometry is None or bool(getattr(geometry, "is_empty", True)):
        return 0.0
    try:
        from pyproj import Geod

        geod = Geod(ellps="WGS84")
        length = geod.geometry_length(geometry)
        if math.isfinite(length) and length > 0.0:
            return float(length)
    except Exception:  # noqa: BLE001 — нет pyproj, падаем на проекцию
        pass
    # Запасной путь: равнопромежуточная проекция. Метр там не точен, но
    # единица измерения хотя бы названа.
    try:
        return float(geometry.length)
    except Exception:  # noqa: BLE001
        return 0.0


def _field(record: Any, name: str, default: Any = None) -> Any:
    """Читает поле записи, различая «нет значения» и «значение == default».

    ``getattr(record, name, default) or default`` здесь неприменимо: в
    геообработке поле — это часто numpy-массив, у которого нет однозначного
    булева значения, и ``or`` падает с ``ValueError`` на втором же сегменте.
    Отдельно обрабатывается и ``NaN`` в скалярных полях вроде ``class``: он
    истинен, и ``or`` молча превратил бы пропуск в строку ``"nan"``.
    """
    value = getattr(record, name, default)
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    try:
        if len(value) == 0:  # noqa: PLR2004 — пустой массив/список
            return default
    except TypeError:
        pass
    return value


# ── Дорожная сеть ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RoadNetwork:
    """Дорожный граф города из сегментов и коннекторов Overture.

    Узлы графа — коннекторы Overture. Это не остановки: коннектор соединяет
    сегменты, и совпадения его с остановкой нет (§27.1 — не выдавать одно за
    другое). Остановки появляются позже, из маршрутов перевозчика.
    """

    #: ``узел → [(соседний узел, длина_м, класс)]`` — неориентированный граф.
    edges: dict[str, list[tuple[str, float, str]]] = field(default_factory=dict)
    #: ``узел → (широта, долгота)``.
    nodes: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: Сколько сегментов легло в граф (после отбраковки по классу).
    segment_count: int = 0

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    def neighbours(self, node: str) -> list[tuple[str, float, str]]:
        return self.edges.get(node, [])

    def degree_histogram(self) -> dict[int, int]:
        """Распределение степеней: проверка связности без обхода графа.

        Узлы степени 1 — тупики дорожной сети, их много и это нормально; узлы
        степени 0 означают, что коннектор ни к чему не привязан, и это уже
        дефект загрузки, а не свойство города.
        """
        histogram: dict[int, int] = {}
        for node in self.nodes:
            degree = len(self.edges.get(node, []))
            histogram[degree] = histogram.get(degree, 0) + 1
        return histogram


# ── Загрузка тем ──────────────────────────────────────────────────────


def resolve_theme_parts(
    release: str,
    theme: str,
    overture_type: str,
    bbox: LatLonBBox,
    *,
    retries: int = 2,
    retry_delay: float = 1.0,
    cache_dir: str | None = None,
) -> tuple[str, ...]:
    """S3-ключи частей темы, покрывающих границу, — через STAC-каталог.

    Это узкое место всей загрузки. Cloud scan по шаблону
    ``theme=X/type=Y/*`` читает **всю тему целиком** и только потом отбрасывает
    всё вне bbox: замер на Воронеже дал 678 с на одной теме ``buildings`` при
    251 968 строках результата. Разрешение частей через STAC и чтение только их
    даёт те же строки за 26 с — в 26 раз быстрее, потому что читается
    1–2 части вместо всех.

    Список частей кэшируется: он меняется только при смене релиза, а сам STAC
    стоит 1–4 с на тему и является чистой метаинформацией.
    """
    from .http import _http_resolve_stac_part_files_via_collection

    cache = _parts_cache(cache_dir)
    key = (
        f"{release}|{theme}|{overture_type}|"
        + ",".join(f"{value:.5f}" for value in bbox.as_project_tuple())
    )
    if cache is not None:
        cached = cache.get("stac_parts", key)
        if cached:
            return tuple(str(value) for value in cached)

    keys = tuple(
        _http_resolve_stac_part_files_via_collection(
            release, theme, overture_type, bbox.as_project_tuple(),
            retries=retries, retry_delay=retry_delay,
        )
    )
    if cache is not None:
        cache.put("stac_parts", key, list(keys))
    return keys


def _parts_cache(cache_dir: str | None) -> Any:
    """Кэш списков частей.

    Импорты плоские, а не ``..cache``: каталог ``wikiroutes`` сам добавлен в
    ``sys.path`` (см. ``overture/adapters.py``), и относительный ``..`` уводил
    бы за пределы пакета. Это не стиль, а граница: относительный импорт здесь
    просто не разрешается.
    """
    try:
        from cache import JsonCache
    except ImportError:  # noqa: BLE001 — без кэша тоже работает, чуть медленнее
        return None
    try:
        if cache_dir is None:
            from config import DEFAULT_CACHE_DIR

            cache_dir = DEFAULT_CACHE_DIR
        return JsonCache(cache_dir)
    except Exception:  # noqa: BLE001 — кэш не критичен для результата
        return None


def part_urls(keys: tuple[str, ...]) -> list[str]:
    """HTTP URL частей по их S3-ключам, через общий построитель проекта.

    Сборку делает ``_overture_host_url``, а не строка в этом модуле: ключ
    Overture начинается с имени бакета, и наивная склейка
    ``host + "/" + key`` даёт путь с бакеетом дважды и 404 без внятной
    причины — так и вышло при первой попытке.
    """
    from .http import _OVERTURE_HTTP_HOSTS, _overture_host_url

    host = _OVERTURE_HTTP_HOSTS[0]
    urls: list[str] = []
    for key in keys:
        bucket, _, obj_path = key.partition("/")
        urls.append(_overture_host_url(host, bucket, key, obj_path))
    return urls


def read_theme_parts(
    release: str,
    theme: str,
    overture_type: str,
    bbox: LatLonBBox,
    *,
    provider: str = "s3",
    retries: int = 2,
    retry_delay: float = 1.0,
    cache_dir: str | None = None,
) -> Any:
    """Читает bbox-подмножество темы, ограничиваясь найденными частями.

    Возвращает ``None``, если частей для границы нет или чтение не удалось:
    вызывающий решает, пробовать ли медленный общий путь.
    """
    import duckdb

    keys = resolve_theme_parts(
        release, theme, overture_type, bbox,
        retries=retries, retry_delay=retry_delay, cache_dir=cache_dir,
    )
    if not keys:
        return None
    urls = part_urls(keys)
    min_lat, min_lon, max_lat, max_lon = bbox.as_project_tuple()
    logger.info(
        "Overture: %s/%s — %d частей по STAC (вместо скана всей темы)",
        theme, overture_type, len(urls),
    )

    target = None
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("INSTALL httpfs; LOAD httpfs;")
        connection.execute("SET enable_http_metadata_cache=true;")
        connection.execute("SET http_keep_alive=true;")
        # Геометрия приходит WKB; без этого DuckDB отдаёт её как BLOB.
        connection.execute("INSTALL spatial; LOAD spatial;")
        query = f"""
            SELECT * FROM read_parquet({urls!r})
            WHERE bbox.xmin < {max_lon} AND bbox.xmax > {min_lon}
              AND bbox.ymin < {max_lat} AND bbox.ymax > {min_lat}
        """
        # ``execute(...).arrow()`` отдаёт RecordBatchReader, у которого нет
        # ``to_pandas()``; таблица получается через ``fetch_arrow_table()``.
        frame = connection.execute(query).fetch_arrow_table().to_pandas()
    except Exception as exc:  # noqa: BLE001 — откат на медленный путь
        logger.warning(
            "Overture: чтение %s/%s по частям не удалось: %s",
            theme, overture_type, exc,
        )
        return None
    finally:
        connection.close()
        if target is not None:
            import contextlib

            with contextlib.suppress(OSError):
                target.unlink()

    return _as_geodataframe(frame)


def _as_geodataframe(frame: Any) -> Any:
    """Превращает результат DuckDB в GeoDataFrame.

    Геометрия из GEOMETRY-типа DuckDB приходит в Arrow как **WKB-байты**, а не
    как готовые объекты shapely. Собрать ``GeoDataFrame`` поверх байтов можно,
    и он даже покажет верное число строк, — но любое обращение к геометрии
    упадёт. Поэтому байты разворачиваются в shapely явно.
    """
    if frame is None or len(frame) == 0:
        return None
    import geopandas as gpd

    if "geometry" not in frame.columns:
        return None

    if not isinstance(frame, gpd.GeoDataFrame):
        column = frame["geometry"]
        if column.dtype == object and len(column) and isinstance(
            column.iloc[0], (bytes, bytearray, memoryview)
        ):
            import shapely

            frame = frame.copy()
            frame["geometry"] = shapely.from_wkb(column)
        frame = gpd.GeoDataFrame(frame, geometry="geometry", crs="OGC:CRS84")

    valid = frame.geometry.notna() & ~frame.geometry.is_empty
    return frame[valid]


def read_theme(
    release: str,
    theme: str,
    overture_type: str,
    bbox: LatLonBBox,
    *,
    provider: str = "s3",
    cache_dir: str | None = None,
) -> Any:
    """Читает bbox-подмножество темы: сначала по частям, затем общий скан.

    Колонки не перечисляются: схема меняется между релизами, и жёсткий список
    колонок превратил бы смену версии в ошибку посреди загрузки города.

    Медленный путь (скан всей темы) остаётся не как основной, а как запасной:
    он нужен, когда части по STAC не нашлись — например, у темы, чьи items не
    перечислены в каталоге.
    """
    fast = read_theme_parts(
        release, theme, overture_type, bbox,
        provider=provider, cache_dir=cache_dir,
    )
    if fast is not None and len(fast) > 0:
        return fast
    from .http import _duckdb_read_overture

    logger.info(
        "Overture: %s/%s — части по STAC не дали данных, скан всей темы",
        theme, overture_type,
    )
    return _duckdb_read_overture(
        release, theme, overture_type, bbox.as_project_tuple(), provider=provider
    )


def build_road_network(
    segments: Any,
    connectors: Any,
    *,
    keep_classes: frozenset[str] | None = None,
) -> RoadNetwork:
    """Собирает дорожный граф из сегментов и коннекторов.

    Узел — коннектор. Ребро — сегмент: его два коннектора соединены, длина
    берётся из геометрии, класс — из ``class`` сегмента и остаётся в графе,
    потому что по нему игрок решает, где вообще можно пустить линию (§8.2).

    Сегменты без коннекторов пропускаются: у них нет концов, и в графе они
    не присоединены ни к чему — это не сеть, а набор линий.
    """
    if segments is None or connectors is None:
        return RoadNetwork()

    connector_points: dict[str, tuple[float, float]] = {}
    for record in connectors.itertuples():
        geometry = _field(record, "geometry")
        if geometry is None or bool(getattr(geometry, "is_empty", True)):
            continue
        try:
            centroid = geometry.centroid
        except Exception:  # noqa: BLE001 — геометрия может быть битой
            continue
        if not (math.isfinite(centroid.x) and math.isfinite(centroid.y)):
            continue
        connector_points[str(_field(record, "id"))] = (
            float(centroid.y),
            float(centroid.x),
        )

    nodes: dict[str, tuple[float, float]] = dict(connector_points)
    edges: dict[str, list[tuple[str, float, str]]] = {
        node: [] for node in connector_points
    }
    used = 0
    skipped_no_connectors = 0
    skipped_class = 0

    for record in segments.itertuples():
        klass = str(_field(record, "class", "") or "")
        if keep_classes is not None and klass not in keep_classes:
            skipped_class += 1
            continue
        segment_connectors = list(_field(record, "connectors", ()))
        if len(segment_connectors) < 2:
            skipped_no_connectors += 1
            continue
        # Коннекторов у сегмента может быть больше двух: перекрёсток внутри
        # сегмента — это тоже коннектор. Если взять только крайние, промежуточные
        # останутся в графе узлами степени 0 — то есть выглядят как «дособранная
        # сеть с дырами», хотя это обычное устройство данных. Поэтому сегмент
        # режется по **всем** коннекторам, а длина делится пропорционально
        # расстоянию между ними (``at`` — доля пути по сегменту).
        placed: list[tuple[float, str]] = []
        for item in segment_connectors:
            node = str(item["connector_id"])
            if node not in connector_points:
                continue
            placed.append((float(item["at"]), node))
        if len(placed) < 2:
            skipped_no_connectors += 1
            continue
        placed.sort(key=lambda entry: entry[0])

        total_length = _length_m(_field(record, "geometry"))
        if total_length <= 0.0:
            # Длина нужна для времени езды (§5.3). Подставить вместо неё
            # ничего нельзя: ноль метров и градусы — это разные единицы, и
            # молча выбрать одну из них значит выдать число другой величины.
            skipped_no_connectors += 1
            continue

        span = placed[-1][0] - placed[0][0]
        added = False
        for (from_at, from_node), (to_at, to_node) in zip(placed, placed[1:]):
            if from_node == to_node:
                continue
            share = (to_at - from_at) / span if span > 0.0 else 1.0
            length_m = total_length * share
            if length_m <= 0.0:
                length_m = total_length / max(1, len(placed) - 1)
            edges.setdefault(from_node, []).append((to_node, length_m, klass))
            edges.setdefault(to_node, []).append((from_node, length_m, klass))
            nodes.setdefault(from_node, connector_points[from_node])
            nodes.setdefault(to_node, connector_points[to_node])
            added = True
        if added:
            used += 1

    logger.info(
        "Overture: дорожный граф — рёбер %d, узлов %d, "
        "пропущено по классу %d, без коннекторов %d",
        used,
        len(nodes),
        skipped_class,
        skipped_no_connectors,
    )
    return RoadNetwork(edges=edges, nodes=nodes, segment_count=used)


def places_frame(places: Any) -> Any:
    """Приводит места к плоским колонкам, нужным пассажирскому расчёту.

    В релизе 2026-09-23.1 у ``place`` нет поля ``categories`` — вместо него
    ``basic_category`` и ``taxonomy`` (§17.3: чем измеряли). Имя берётся из
    ``names.primary``, с откатом на ``names.common``: поле ``names`` — это
    словарь с правилами вариантов, и «первое имя» в нём может быть пустым.
    """
    if places is None or len(places) == 0:
        return places

    frame = places.copy()

    def _primary_name(value: Any) -> str | None:
        if value is None:
            return None
        try:
            primary = value.get("primary")
        except AttributeError:
            return None
        if primary:
            return str(primary)
        try:
            common = value.get("common") or {}
        except AttributeError:
            return None
        if isinstance(common, dict) and common:
            # Сортировка по ключу: порядок map в parquet не задан, а порядок
            # имён попал бы в пакет города (§32.5).
            return str(sorted(common.items(), key=lambda item: item[0])[0][1])
        return None

    frame["name"] = [_primary_name(value) for value in frame.get("names")]
    if "basic_category" not in frame.columns:
        # Старые релизы отдавали ``categories`` как список; берём первый.
        # ``frame.get`` здесь не годится: отсутствующая колонка вернула бы
        # ``None``, и перебор ``None`` упал бы — а отсутствие колонки в новом
        # релизе это норма, а не ошибка данных.
        legacy = (
            frame["categories"]
            if "categories" in frame.columns
            else [None] * len(frame)
        )
        frame["basic_category"] = [
            str(list(value)[0]) if value is not None and len(list(value)) else None
            for value in legacy
        ]
    if "taxonomy" in frame.columns:
        frame["taxonomy_primary"] = [
            _none_if_nan(_raw(value, "primary")) for value in frame["taxonomy"]
        ]
    # Пустая категория приходит как NaN, и в категорию «nan» она превратила бы
    # настоящую категорию назначения — а та попадает в γ показателя (§5.5).
    #
    # Присваивание списка с ``None`` в колонку типа float64 **не** даёт
    # ``None``: pandas возвращает ``NaN``. Поэтому колонка приводится к
    # ``object`` явно — иначе «пропустил» и «категории нет» остались бы
    # неразличимы, то есть ровно то различие, ради которого всё затевалось.
    for column in ("name", "basic_category", "taxonomy_primary"):
        if column in frame.columns:
            frame[column] = _object_column(
                [_none_if_nan(value) for value in frame[column]], frame.index
            )
    return frame


def _raw(struct_value: Any, key: str) -> Any:
    """Читает поле вложенной структуры, не превращая пропуск в строку."""
    if struct_value is None:
        return None
    try:
        return struct_value.get(key)
    except AttributeError:
        return None


def _object_column(values: list[Any], index: Any) -> Any:
    """Колонка с настоящими ``None`` вместо ``NaN``."""
    import pandas as pd

    return pd.Series(values, index=index, dtype=object)


def _none_if_nan(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def boundary_from_buildings(buildings: Any) -> Any:
    """Объединение контуров зданий — замена отсутствующей темы ``admin``.

    Это **не** административная граница: объединение зданий покрывает
    застроенную часть и не покрывает леса, поля и водоёмы. Такая граница
    годна для отсечения данных, но не для площади города и не для рубрики
    (§18.5), и источник записывается в манифест отдельной строкой.
    """
    if buildings is None or len(buildings) == 0:
        return None
    import shapely

    geometries = [
        geometry
        for geometry in buildings.geometry
        if geometry is not None and not geometry.is_empty
    ]
    if not geometries:
        return None
    merged = shapely.union_all(geometries)
    if merged.is_empty:
        return None
    return merged


def load_boundary_from_geojson(path: str) -> Any:
    """Читает границу из GeoJSON — предпочтительный источник.

    Предпочтительный потому, что файл границы в проекте сам построен по
    зданиям Overture, но уже с отбором и сглаживанием: повторное объединение
    контуров дало бы другой результат на том же городе, а значит город перестал
    бы быть данными (§30.1).
    """
    from shapely.geometry import shape

    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    geometries = [
        shape(feature["geometry"])
        for feature in document.get("features", [])
        if feature.get("geometry")
    ]
    if not geometries:
        raise ValueError(f"в границе {path!r} нет геометрий")
    import shapely

    return shapely.union_all(geometries)


def load_city(
    slug: str,
    bbox: LatLonBBox,
    release: str,
    *,
    boundary_geojson: str | None = None,
    road_classes: frozenset[str] | None = None,
    provider: str = "s3",
    cache_dir: str | None = None,
) -> CityPackage:
    """Загружает город целиком и собирает пакет.

    :param bbox: граница загрузки. Обрезается по прямоугольнику, а не по
        полигону: STAC-части прямоугольные, и попытка выбрать их по полигону
        дала бы «город не найден» на городе, который в каталоге есть.
    :param boundary_geojson: файл границы. Если не задан, граница строится
        объединением контуров зданий и помечается в манифесте как оценка.
    :param road_classes: классы сегментов для дорожного графа. ``None`` — все.
    :param cache_dir: каталог кэша. ``None`` — кэш проекта; кэш ускоряет
        повторные загрузки, но на результат не влияет.
    """
    buildings = read_theme(
        release, "buildings", "building", bbox,
        provider=provider, cache_dir=cache_dir,
    )
    places = read_theme(
        release, "places", "place", bbox,
        provider=provider, cache_dir=cache_dir,
    )
    segments = read_theme(
        release, "transportation", "segment", bbox,
        provider=provider, cache_dir=cache_dir,
    )
    connectors = read_theme(
        release, "transportation", "connector", bbox,
        provider=provider, cache_dir=cache_dir,
    )

    if boundary_geojson:
        boundary = load_boundary_from_geojson(boundary_geojson)
        boundary_source = f"geojson:{boundary_geojson}"
    else:
        boundary = boundary_from_buildings(buildings)
        boundary_source = "union(buildings) — не административная граница"

    road_network = build_road_network(
        segments, connectors, keep_classes=road_classes
    )
    processed_places = places_frame(places)

    parts: dict[str, tuple[str, ...]] = {}
    for theme, overture_type, frame in (
        ("buildings", "building", buildings),
        ("places", "place", places),
        ("transportation", "segment", segments),
        ("transportation", "connector", connectors),
    ):
        key = f"{theme}/{overture_type}"
        try:
            parts[key] = resolve_theme_parts(
                release, theme, overture_type, bbox, cache_dir=cache_dir
            )
        except Exception:  # noqa: BLE001 — части не критичны для содержимого
            parts[key] = ()

    provenance = CityProvenance(
        release=release,
        themes={
            "buildings": _frame_len(buildings),
            "places": _frame_len(processed_places),
            "transportation/segment": _frame_len(segments),
            "transportation/connector": _frame_len(connectors),
        },
        parts=parts,
        boundary_source=boundary_source,
    )
    return CityPackage(
        slug=slug,
        bbox=bbox,
        boundary=boundary,
        buildings=buildings,
        places=processed_places,
        road_network=road_network,
        provenance=provenance,
    )


__all__ = [
    "ABSENT_THEMES",
    "CATALOG_ONLY_FIELDS",
    "CITY_THEMES",
    "GAME_MANIFEST_FIELDS",
    "REGION_COUNT",
    "SPECIAL_DEMAND",
    "CityIdentity",
    "CityPackage",
    "CityProvenance",
    "LatLonBBox",
    "RoadNetwork",
    "boundary_from_buildings",
    "build_road_network",
    "load_boundary_from_geojson",
    "load_city",
    "part_urls",
    "places_frame",
    "read_theme",
    "read_theme_parts",
    "resolve_theme_parts",
]