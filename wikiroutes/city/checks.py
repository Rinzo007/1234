"""Десять обязательных проверок города (§18.5).

Проверки б��аются **до** того, как город попадёт в список, и четыре из десяти
проверяют согласованность спроса, а не наличие файлов. Это не случайно: город
может содержать все файлы и при этом считаться неверно.

Два порога документ называет, но не задаёт числом:

- ``demand_point_spacing`` — «допустимое расстояние между точками спроса»;
- ``demand_phantom_points`` — «нет точек спроса без пассажиров».

Поэтому оба задаются **параметрами** и не имеют значений по умолчанию: подставить
число, которого нет в документе, значит проверять город по выдуманному правилу
и выдавать это за проверку по документу.

Остальные восемь проверок либо требуют только наличия файла, либо счисляются
точно, и потому выполняются без параметров.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .demand import DemandData
from .package import (
    BUILDINGS_INDEX_FILENAME,
    CONFIG_FILENAME,
    ROADS_FILENAME,
    RUNWAYS_FILENAME,
    validate_version,
)


#: Десять проверок в порядке контракта (§18.5, §A.5).
CHECK_NAMES: tuple[str, ...] = (
    "config_json",
    "demand_data",
    "demand_point_spacing",
    "demand_phantom_points",
    "demand_residents_match",
    "buildings_index",
    "roads_geojson",
    "runways_taxiways_geojson",
    "city_pmtiles",
    "config_version_matches_tag",
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Результат одной проверки."""

    name: str
    passed: bool
    #: Что именно не так. Для успешной проверки — пусто.
    detail: str = ""

    def __str__(self) -> str:
        mark = "пройдена" if self.passed else "НЕ ПРОЙДЕНА"
        tail = f": {self.detail}" if self.detail else ""
        return f"{self.name} — {mark}{tail}"


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Отчёт приёма города."""

    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if not result.passed)

    def names(self) -> tuple[str, ...]:
        return tuple(result.name for result in self.results)

    def summary(self) -> str:
        state = (
            f"город принят: {len(self.results)}/{len(self.results)}"
            if self.passed
            else f"город отклонён: {len(self.failed)} из {len(self.results)} проверок"
        )
        lines = [state]
        for result in self.results:
            if not result.passed:
                lines.append(f"  {result}")
        return "\n".join(lines)


def check_file(path: Path, name: str) -> CheckResult:
    """Проверка наличия и непустоты файла."""
    if not path.exists():
        return CheckResult(name, False, f"нет файла {path.name}")
    if path.stat().st_size <= 0:
        return CheckResult(name, False, f"файл {path.name} пуст")
    return CheckResult(name, True)


def check_config(directory: Path) -> CheckResult:
    import json

    path = Path(directory) / CONFIG_FILENAME
    if not path.exists():
        return CheckResult("config_json", False, f"нет файла {CONFIG_FILENAME}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return CheckResult("config_json", False, f"не читается: {exc}")
    required = ("city_code", "name", "country", "version", "bbox", "population")
    missing = [field for field in required if field not in payload]
    if missing:
        return CheckResult(
            "config_json", False, "нет обязательных полей: " + ", ".join(missing)
        )
    return CheckResult("config_json", True)


def check_demand_point_spacing(
    demand: DemandData,
    *,
    min_spacing_m: float,
    max_spacing_m: float,
) -> CheckResult:
    """Допустимое расстояние между точками спроса (§18.5).

    Числа **не заданы документом**, поэтому передаются параметрами. Проверка
    ищет две вещи: соседние точки слишком близко (данные раздроблены) и слишком
    далеко (масса размазана и точек не хватает).

    Расстояние считается между соседями по сетке, а не между всеми парами: полный
    перебор на 7 000 точках — это 25 миллионов пар ради проверки, которой
    достаточно на соседей.
    """
    if len(demand.points) < 2:
        return CheckResult(
            "demand_point_spacing", False, "точек меньше двух: расстояние не из чего"
        )

    from .distance import nearest_neighbour_distances_m

    distances = nearest_neighbour_distances_m(
        [(point.lon, point.lat) for point in demand.points]
    )
    too_close = [d for d in distances if d < min_spacing_m]
    too_far = [d for d in distances if d > max_spacing_m]
    if too_close:
        worst = min(too_close)
        return CheckResult(
            "demand_point_spacing",
            False,
            f"{len(too_close)} точек ближе {min_spacing_m:.0f} м "
            f"(минимум {worst:.0f} м): точки раздроблены",
        )
    if too_far:
        worst = max(too_far)
        return CheckResult(
            "demand_point_spacing",
            False,
            f"{len(too_far)} точек дальше {max_spacing_m:.0f} м "
            f"(максимум {worst:.0f} м): масса размазана",
        )
    return CheckResult("demand_point_spacing", True)


def check_phantom_points(demand: DemandData) -> CheckResult:
    """Нет точек спроса без пассажиров (§18.5).

    Точка без жителей и без рабочих мест — артефакт геометрии, а не точка
    спроса. Такие точки зря занимают зоны и портят зонирование (§20.2), а по
    файлу выглядят как обычные данные.
    """
    phantoms = demand.phantom_points()
    if phantoms:
        sample = ", ".join(phantoms[:5])
        more = "" if len(phantoms) <= 5 else f" (ещё {len(phantoms) - 5})"
        return CheckResult(
            "demand_phantom_points",
            False,
            f"{len(phantoms)} точек без жителей и рабочих мест: {sample}{more}",
        )
    return CheckResult("demand_phantom_points", True)


def check_residents_match(demand: DemandData) -> CheckResult:
    """Счётчики жителей совпадают с данными попов (§18.5, инвариант §5.3)."""
    mismatches = demand.residents_mismatch()
    if mismatches:
        total = sum(abs(value) for value in mismatches.values())
        worst = max(mismatches.items(), key=lambda item: abs(item[1]))
        return CheckResult(
            "demand_residents_match",
            False,
            f"{len(mismatches)} точек расходятся на {total:,.0f} человек; "
            f"худшая — {worst[0]} ({worst[1]:+,.0f})",
        )
    unreferenced = demand.unreferenced_pops()
    if unreferenced:
        return CheckResult(
            "demand_residents_match",
            False,
            f"{len(unreferenced)} попов ссылаются на несуществующие точки",
        )
    return CheckResult("demand_residents_match", True)


def check_unreachable_destinations(demand: DemandData) -> CheckResult:
    """Начала без достижимого назначения — это состояние города, а не сбой.

    Не входит в десять обязательных проверок и не влияет на приём: поездки
    существуют, инвариант выполнен, а порог притяжения §5.5 отсёк назначения
    дальше заданного расстояния. Но молчать об этом нельзя — иначе часть
    населения выглядит посчитанной по полной картине, какой её нет.
    """
    unreachable = demand.destinations_unreachable()
    if not unreachable:
        return CheckResult("destinations_unreachable", True, "у всех начал есть назначения")
    people = sum(
        pop.size for pop in demand.pops if pop.id in set(unreachable)
    )
    return CheckResult(
        "destinations_unreachable",
        True,
        f"{len(unreachable)} начал без достижимого назначения, "
        f"{people:,.0f} человек — состояние города, не сбой сборки",
    )


def check_version_matches_tag(
    directory: Path,
    release_tag: str,
) -> CheckResult:
    """Версия в конфигурации совпадает с меткой выпуска (§18.5, §A.5).

    Проверка идентичности версии. Расхождение означало бы, что город собран из
    одних данных и помечен версией других, и по такой метке нельзя решить,
    обновлять ли карту у игрока (§27.1, инцидент 5).
    """
    import json

    path = Path(directory) / CONFIG_FILENAME
    if not path.exists():
        return CheckResult(
            "config_version_matches_tag", False, f"нет файла {CONFIG_FILENAME}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return CheckResult("config_version_matches_tag", False, f"не читается: {exc}")
    declared = str(payload.get("version", ""))
    try:
        validate_version(declared)
        expected = validate_version(str(release_tag))
    except ValueError as exc:
        return CheckResult("config_version_matches_tag", False, str(exc))
    if declared != expected:
        return CheckResult(
            "config_version_matches_tag",
            False,
            f"в конфигурации {declared!r}, метка выпуска {expected!r}",
        )
    return CheckResult("config_version_matches_tag", True)


def run_checks(
    directory: str | Path,
    *,
    demand: DemandData | None = None,
    release_tag: str = "",
    min_spacing_m: float | None = None,
    max_spacing_m: float | None = None,
    require_pmtiles: bool = False,
    has_runways: bool = False,
) -> CheckReport:
    """Прогоняет десять проверок и возвращает отчёт.

    :param min_spacing_m: нижняя граница расстояния между точками спроса.
        Документом не задана — без этого параметра проверка не выполняется, и
        это отмечается как «не проверено», а не как «пройдена».
    :param require_pmtiles: город без тайлов — город, который не отрисуется.
        По умолчанию не требуется: тайлы собираются отдельным инструментом.
    """
    base = Path(directory)
    results: list[CheckResult] = [
        check_config(base),
        check_file(base / "demand_data.json.gz", "demand_data"),
        check_file(base / BUILDINGS_INDEX_FILENAME, "buildings_index"),
        check_file(base / ROADS_FILENAME, "roads_geojson"),
    ]

    # Взлётные полосы: файл обязателен, только если город их заявил (§18.4).
    results.append(
        check_file(base / RUNWAYS_FILENAME, "runways_taxiways_geojson")
        if has_runways
        else CheckResult(
            "runways_taxiways_geojson", True, "город не заявил взлётных полос"
        )
    )

    results.append(
        check_file(base / "city.pmtiles", "city_pmtiles")
        if require_pmtiles
        else CheckResult(
            "city_pmtiles", True, "тайлы не требовались: собираются отдельно"
        )
    )

    if demand is not None:
        if min_spacing_m is None or max_spacing_m is None:
            results.append(
                CheckResult(
                    "demand_point_spacing",
                    False,
                    "не проверена: документ не задаёт допустимое расстояние, "
                    "а без него любой порог был бы выдумкой",
                )
            )
        else:
            results.append(
                check_demand_point_spacing(
                    demand,
                    min_spacing_m=float(min_spacing_m),
                    max_spacing_m=float(max_spacing_m),
                )
            )
        results.append(check_phantom_points(demand))
        results.append(check_residents_match(demand))
    else:
        for name in (
            "demand_point_spacing",
            "demand_phantom_points",
            "demand_residents_match",
        ):
            results.append(
                CheckResult(name, False, "не проверена: файл спроса не передан")
            )

    results.append(check_version_matches_tag(base, release_tag))
    if demand is not None:
        # Не входит в десять обязательных проверок: это состояние города, а не
        # условие приёма. Добавляется в отчёт, чтобы молчание о нём было
        # невозможно.
        results.append(check_unreachable_destinations(demand))
    return CheckReport(results=tuple(results))


__all__ = [
    "CHECK_NAMES",
    "CheckReport",
    "CheckResult",
    "check_config",
    "check_demand_point_spacing",
    "check_file",
    "check_phantom_points",
    "check_residents_match",
    "check_version_matches_tag",
    "run_checks",
]
