"""Файлы выпуска города по контракту (§18.4, §A.5).

## Что требует контракт

| Файл | Что внутри |
|---|---|
| ``config.json`` | код, имя, bbox, население, стартовая камера |
| ``demand_data.json.gz`` | точки спроса и попы (§A.5f) |
| ``buildings_index.json.gz`` | индекс зданий и фундаментов |
| ``roads.geojson.gz`` | дороги |
| ``runways_taxiways_geojson.gz`` | взлётные полосы и тактические дороги |
| ``XXX.pmtiles`` | векторные тайлы |
| ``rubric.json`` | ответы рубрики с методологией — **отдельным файлом** |

Правила контракта, которые пишутся в коде, а не остаются в соглашении:

- **Имя файла пакета равно коду города.** Проверяющий скрипт не заглядывает
  внутрь архива, он сверяет имя, уже известное из индекса. Произвольное имя —
  это город, который невозможно однозначно адресовать (§18.4b).
- **Вложенных папок нет.** Распаковка дала бы путь на уровень глубже
  ожидаемого, а игра ищет ``config.json`` в корне.
- **Версия — ``X.Y.Z`` или ``vX.Y.Z``**, префикс в обе стороны. Формат
  проверяется, потому что от него зависит сравнение версий, а сравнение
  версий — это решение «обновлять ли карту у игрока».
- **Файлы уходят сжатыми.** Размер города определяет время загрузки, а загрузка
  — первое впечатление.
- **Ответы рубрики лежат рядом с манифестом отдельным документом** (§18.4a):
  рубрика эволюционирует, манифест стабилен. Смешивать их в одном файле значит
  заставлять переписывать 406 манифестов каждый раз, как меняется вес.

Ничего из этого не проверяется «по вкусу»: каждое правило имеет тест.
"""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import json
import math
from pathlib import Path
import re
from typing import Any, Sequence

from overture.city import CityIdentity, LatLonBBox, RoadNetwork

from .demand import DemandData


#: Имена файлов выпуска. Порядок — как в §A.5.
CONFIG_FILENAME: str = "config.json"
BUILDINGS_INDEX_FILENAME: str = "buildings_index.json.gz"
ROADS_FILENAME: str = "roads.geojson.gz"
RUNWAYS_FILENAME: str = "runways_taxiways_geojson.gz"
RUBRIC_FILENAME: str = "rubric.json"

#: Версия схемы данных спроса (§18.4).
DEMAND_SCHEMA_VERSION: int = 1

#: Версия схемы манифеста города (§18.4a требует ``schema_version`` = 1).
CITY_SCHEMA_VERSION: int = 1

#: Версия — ``X.Y.Z`` или ``vX.Y.Z`` с необязательным префиксом (§18.4b).
VERSION_PATTERN: re.Pattern[str] = re.compile(r"^v?\d+\.\d+\.\d+$")

#: Имя файла пакета — только строчные латинские буквы, цифры и дефис (§18.4a).
SLUG_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Допустимые расширения файлов выпуска. Вложенность папок запрещена.
RELEASE_EXTENSIONS: frozenset[str] = frozenset(
    {".json", ".gz", ".pmtiles"}
)


def validate_version(version: str) -> str:
    """Проверяет формат версии релиза (§18.4b).

    Формат проверяется, а не разбирается «мягко»: от него зависит сравнение
    версий, а сравнение версий — это решение об обновлении карты у игрока.
    """
    text = str(version).strip()
    if not VERSION_PATTERN.match(text):
        raise ValueError(
            f"версия {version!r} не соответствует формату: нужен X.Y.Z или "
            "vX.Y.Z с необязательным префиксом"
        )
    return text


def validate_slug(slug: str) -> str:
    """Проверяет идентификатор города: строчные латинские и дефис (§18.4a)."""
    text = str(slug).strip()
    if not SLUG_PATTERN.match(text):
        raise ValueError(
            f"идентификатор {slug!r} не соответствует шаблону: только строчные "
            "латинские буквы, цифры и дефис между ними"
        )
    return text


def package_filename(identity: CityIdentity, version: str) -> str:
    """Имя файла пакета — по коду города, версия отбрасывается.

    Имя **равно коду города**, и это не формальность: проверяющий скрипт не
    заглядывает внутрь архива, он сверяет имя, уже известное из индекса.
    """
    if not identity.city_code:
        raise ValueError("имя файла пакета равно коду города, а кода нет")
    return f"{identity.city_code}.zip"


@dataclass(frozen=True, slots=True)
class CityConfig:
    """``config.json`` — то, с чего игра начинает открывать город (§18.4)."""

    identity: CityIdentity
    bbox: LatLonBBox
    version: str
    schema_version: int = CITY_SCHEMA_VERSION
    #: Есть ли в городе взлётные полосы: от этого зависит, обязателен ли файл
    #: ``runways_taxiways_geojson.gz`` (проверка ``runways_taxiways_geojson``).
    has_runways: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "city_code": self.identity.city_code,
            "name": self.identity.name,
            "country": self.identity.country,
            "version": self.version,
            "schema_version": self.schema_version,
            "population": float(self.identity.population),
            "bbox": {
                "min_lat": self.bbox.min_lat,
                "min_lon": self.bbox.min_lon,
                "max_lat": self.bbox.max_lat,
                "max_lon": self.bbox.max_lon,
            },
            "initial_view_state": self.identity.initial_view_state(),
            "special_demand": list(self.identity.special_demand),
            "has_runways": bool(self.has_runways),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )


def _write_gzip_json(path: Path, payload: Any) -> int:
    """Записывает JSON сжатым и возвращает размер файла в байтах.

    ``mtime=0`` в ``gzip`` — не косметика: без него содержимое архива меняется
    при каждой сборке одного и того же города, и побайтовое сравнение пакетов
    перестаёт работать (§32.5 — детерминизм).
    """
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, allow_nan=False
    )
    raw = text.encode("utf-8")
    with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"), mtime=0) as stream:
        stream.write(raw)
    return path.stat().st_size


def roads_geojson(network: RoadNetwork) -> dict[str, Any]:
    """``roads.geojson`` — дорожный граф в виде GeoJSON.

    Собирается из узлов-коннекторов и рёбер-сегментов, которые построил
    :class:`overture.city.RoadNetwork`. Обращается только к уже посчитанному и
    не пересчитывает длины: повторный расчёт здесь означал бы две разные сети в
    одном городе.
    """
    features: list[dict[str, Any]] = []
    for node, neighbours in sorted(network.edges.items()):
        lat, lon = network.nodes[node]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"kind": "connector", "id": node,
                               "degree": len(neighbours)},
            }
        )
    for node, neighbours in sorted(network.edges.items()):
        lat, lon = network.nodes[node]
        for other, length_m, klass in sorted(neighbours):
            # Ребро записывается один раз, из узла с меньшим идентификатором:
            # граф неориентированный, и обе записи удвоили бы сеть вдвое.
            if str(other) < str(node):
                continue
            other_lat, other_lon = network.nodes[other]
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[lon, lat], [other_lon, other_lat]],
                    },
                    "properties": {
                        "kind": "segment",
                        "from": node,
                        "to": other,
                        "length_m": round(float(length_m), 3),
                        "class": klass,
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


def _json_safe(value: Any) -> Any:
    """Заменяет NaN и бесконечности на ``None``.

    Пропуск в колонке pandas приходит как ``NaN``, и в JSON он не выражается.
    Молча превратить его в ``0`` значило бы заявить, что у здания нулевая высота;
    оставить ``NaN`` — не выразить его вовсе. Правильный представитель
    отсутствующего значения — ``null``.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    return value


def buildings_index(buildings: Any) -> dict[str, Any]:
    """``buildings_index.json`` — индекс зданий.

    Строится минимальный: идентификатор здания и его геометрия. Полноценный
    бинарный индекс с тайловой сеткой документ описывает как ``bin``; здесь
    JSON, и подмена формата объявлена, а не сделана молча.
    """
    if buildings is None:
        return {"type": "FeatureCollection", "features": []}
    features: list[dict[str, Any]] = []
    for record in buildings.itertuples():
        geometry = getattr(record, "geometry", None)
        if geometry is None or bool(getattr(geometry, "is_empty", True)):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": json.loads(json.dumps(geometry.__geo_interface__)),
                "properties": {
                    "id": str(getattr(record, "id", "")),
                    "height": _json_safe(getattr(record, "height", None)),
                    "num_floors": _json_safe(getattr(record, "num_floors", None)),
                    "class": _json_safe(getattr(record, "class", None)),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def rubric_document(
    quality: Any,
    answers: dict[str, str],
    methodology: str = "",
    sources: Sequence[str] = (),
) -> dict[str, Any]:
    """Файл ответов рубрики — отдельный документ (§18.4a).

    Рубрика лежит рядом с манифестом и меняется независимо от него: изменение
    веса не должно переписывать 406 манифестов. Поэтому здесь лежат и сырые
    ответы, и вычисленный из них уровень, и версия рубрики — а в манифесте
    остаётся только уровень.
    """
    return {
        "rubric_version": getattr(quality, "rubric_version", None),
        "answers": dict(sorted(answers.items())),
        "computed": quality.to_dict() if quality is not None else None,
        "methodology": methodology,
        "sources": list(sources),
    }


def write_release(
    directory: str | Path,
    identity: CityIdentity,
    config: CityConfig,
    demand: DemandData,
    *,
    rubric: dict[str, Any] | None = None,
    network: RoadNetwork | None = None,
    buildings: Any = None,
) -> dict[str, int]:
    """Пишет файлы выпуска в ``directory`` и возвращает их размеры.

    Порядок записи и состав файлов не меняются от запуска к запуску: размер
    нужен для ``file_sizes``, а сам он должен быть воспроизводим.
    """
    validate_version(config.version)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)

    sizes: dict[str, int] = {}

    config_path = target / CONFIG_FILENAME
    config_path.write_text(config.to_json() + "\n", encoding="utf-8")
    sizes[CONFIG_FILENAME] = config_path.stat().st_size

    demand_path = target / "demand_data.json.gz"
    _write_gzip_json(demand_path, demand.to_dict())
    sizes["demand_data.json.gz"] = demand_path.stat().st_size

    buildings_path = target / BUILDINGS_INDEX_FILENAME
    _write_gzip_json(buildings_path, buildings_index(buildings))
    sizes[BUILDINGS_INDEX_FILENAME] = buildings_path.stat().st_size

    roads_path = target / ROADS_FILENAME
    _write_gzip_json(roads_path, roads_geojson(network or RoadNetwork()))
    sizes[ROADS_FILENAME] = roads_path.stat().st_size

    if config.has_runways:
        runways_path = target / RUNWAYS_FILENAME
        _write_gzip_json(
            runways_path, {"type": "FeatureCollection", "features": []}
        )
        sizes[RUNWAYS_FILENAME] = runways_path.stat().st_size

    if rubric is not None:
        rubric_path = target / RUBRIC_FILENAME
        _write_gzip_json(rubric_path, rubric)
        sizes[RUBRIC_FILENAME] = rubric_path.stat().st_size

    return sizes


__all__ = [
    "BUILDINGS_INDEX_FILENAME",
    "CITY_SCHEMA_VERSION",
    "CONFIG_FILENAME",
    "DEMAND_SCHEMA_VERSION",
    "RELEASE_EXTENSIONS",
    "ROADS_FILENAME",
    "RUBRIC_FILENAME",
    "RUNWAYS_FILENAME",
    "SLUG_PATTERN",
    "VERSION_PATTERN",
    "CityConfig",
    "buildings_index",
    "package_filename",
    "roads_geojson",
    "rubric_document",
    "validate_slug",
    "validate_version",
    "write_release",
]
