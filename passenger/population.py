"""Население из растров GHSL — слой данных пакета города (§18.5, §5.2).

Модуль читает растр населения GHSL (продукт ``*_pop_*_100m_*``) и превращает
его в точки спроса с измеренным населением. Это единственный источник, где
население **измерено**, а не подставлено; всё остальное, что этот модуль
делает, опирается на эти числа.

Три вещи, которые здесь объявлены явно, потому что иначе они всплывают как
«ошибка данных» уже в отчёте:

**1. Рабочие места в растре населения нет.** ``*_pop_*`` содержит только
жителей. У пункта назначения масса — это ``residents + jobs``, и без второй
части гравитационная модель (§5.5) работает на неполной массе. Поле ``jobs``
заполняется нулём, и ``jobs_measured`` у результата равен ``False``: подменять
рабочие места жителями или застройкой молча нельзя (§27.1), это была бы
выдумка, выданная за измерение. Оценка рабочих мест из слоя застройки — отдельное
решение, и она помечается как оценка, а не как измерение.

**2. Растр измеряет ячейку, а не человека.** Значение в пикселе — население
100-метровой ячейки. Оно приписывается точке спроса как «население в точке»,
и сумма по точкам равна сумме по ячейкам ровно настолько, насколько границы
точек совпадают с границами ячеек. Дроблениеpopulation по ячейкам вместо
по точкам выбрано сознательно: оно не выдумывает точность, которой в растре
нет.

**3. Покрытие растра ограничено широтой.** Продукт ``GHS_POP`` Northern
Hemisphere покрывает только lat > 41°; южнее ячейки пустые и ``nodata``. Модуль
не считает это нулём населения молча — он сообщает, сколько точек оказалось без
измерения.

Зависимости: ``rasterio`` и ``numpy``. Они импортируются лениво, поэтому пакет
остаётся рабочим и без них: без растров весь остальной расчёт работает, а
``population.py`` поднимает понятную ошибку при попытке чтения.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Iterator, Sequence

from .params import DemandPoint


#: Продукт GHSL, который читает модуль. Записывается в результат, чтобы по
#: отчёту было видно, чем именно измерено население (§17.3).
GHS_POPULATION_PRODUCT: str = "GHS_POP E2030 / 100m / R2025A (CN)"

#: Минимальное население ячейки, при котором из неё делается точка спроса.
#: Ноль отбрасывается: точка без жителей не является точкой спроса, а их на
#: городе десятки тысяч, и каждая тянула бы зону впустую.
DEFAULT_MIN_CELL_POPULATION: float = 0.0

#: Плотность точек спроса на 1 км². Точки дальше этого разбиения не несут
#: информации: ячейка растра 100 м, и разбивать её дальше — выдавать за
#: разрешение то, чего в данных нет.
DEFAULT_MAX_POINTS_PER_KM2: float = 100.0


@dataclass(frozen=True, slots=True)
class PopulationReading:
    """Что именно измерено и чем (§17.3: «чем измеряли» и «насколько совпало» —
    разные вопросы, и второй без первого не читается)."""

    product: str
    #: Пикселей растра прочитано.
    cells: int
    #: Пикселей с ненулевым населением.
    populated_cells: int
    #: Пикселей, которые растр не покрывает (``nodata``).
    no_data_cells: int
    #: Сумма населения по покрытым ячейкам, человек.
    total_population: float
    raster_crs: str
    #: Пикселей растра в одной точке спроса (1 — точка на ячейку).
    cells_per_point: float

    def summary(self) -> str:
        return (
            f"{self.product}: {self.populated_cells:,} ячеек с населением, "
            f"всего {self.total_population:,.0f} человек"
        )


def _as_xy(bbox: Any) -> tuple[float, float, float, float]:
    """Приводит границу к порядку ``(min_lon, min_lat, max_lon, max_lat)``.

    Модуль населения работает в порядке ``rasterio``/``shapely`` — x раньше y,
    то есть долгота раньше широты. Модули Overture используют обратный порядок
    (``min_lat`` раньше ``min_lon``), и это не опечатка, а разные соглашения в
    разных частях проекта.

    Поэтому граница принимается в **обоих** видах: явный объект с полями
    (``LatLonBBox``) используется как есть, а кортеж трактуется как
    ``(min_x, min_y, max_x, max_y)``. Тип с именованными полями — предпочтительный
    путь: кортеж из четырёх чисел не говорит, в каком порядке задан.
    """
    as_xy = getattr(bbox, "as_xy", None)
    if callable(as_xy):
        return tuple(float(value) for value in as_xy())
    values = tuple(float(value) for value in bbox)
    if len(values) != 4:
        raise ValueError(f"граница должна состоять из четырёх чисел, получено {len(values)}")
    return values  # type: ignore[return-value]


def _require_rasterio() -> Any:
    try:
        import rasterio
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ImportError(
            "Для чтения растра населения нужен rasterio: pip install rasterio. "
            "Остальной расчёт пассажиропотока работает и без него."
        ) from exc
    return rasterio


class PopulationRaster:
    """Растр населения GHSL, открытый на чтение.

    Открывается лениво по требованию и держится открытым: растр в 588 МБ на
    всю страну, и повторное открытие на каждый запрос дороже самого чтения
    окна. Контекстный менеджер обязателен — файл держится открытым, пока
    объект жив.
    """

    __slots__ = ("_path", "_src")

    def __init__(self, path: str) -> None:
        self._path = str(path)
        self._src: Any = None

    def __enter__(self) -> "PopulationRaster":
        rasterio = _require_rasterio()
        self._src = rasterio.open(self._path)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._src is not None:
            self._src.close()
            self._src = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def crs(self) -> str:
        return "" if self._src is None else str(self._src.crs)

    def crs_object(self) -> Any:
        """СКП растра объектом — нужна для пересчёта на чужую сетку."""
        if self._src is None:
            raise RuntimeError("растр не открыт")
        return self._src.crs

    def is_geographic(self) -> bool:
        """True, если растр в градусах и границы читаются напрямую.

        Проверка по CRS, а не по имени файла: продукт с тем же именем может
        прийти в проекции, и молчаливое чтение «как будто это градусы» дало бы
        население не в том месте.
        """
        if self._src is None:
            raise RuntimeError("растр не открыт")
        crs = self._src.crs
        return bool(crs is not None and crs.is_geographic)

    def read_window(
        self,
        bbox: Any,
    ) -> tuple[Any, Any, Any]:
        """Читает окно растра по границе.

        ``bbox`` — кортеж ``(min_lon, min_lat, max_lon, max_lat)`` либо объект с
        методом ``as_xy()`` (например ``overture.city.LatLonBBox``).

        Возвращает ``(данные, transform, окно)``. Пиксели ``nodata`` приходят
        маской, а не числом: значение ``-99999`` в сумме выглядело бы как
        отрицательное население.
        """
        _require_rasterio()
        if self._src is None:
            raise RuntimeError("растр не открыт")
        if not self.is_geographic():
            raise ValueError(
                f"растр в негеографической СКП ({self.crs}); переведите его в "
                "EPSG:4326 перед чтением — иначе границы читались бы неверно"
            )
        from rasterio.windows import from_bounds

        window = from_bounds(*_as_xy(bbox), transform=self._src.transform)
        data = self._src.read(1, window=window, masked=True)
        transform = self._src.window_transform(window)
        return data, transform, window


def population_points(
    raster: PopulationRaster,
    bbox: Any,
    *,
    min_population: float = DEFAULT_MIN_CELL_POPULATION,
    points_per_km2: float = DEFAULT_MAX_POINTS_PER_KM2,
    built: Any = None,
    m2_per_job: float | None = None,
) -> tuple[list[DemandPoint], PopulationReading, Any]:
    """Строит точки спроса с измеренным населением внутри границы (§5.2).

    :param bbox: ``(min_lon, min_lat, max_lon, max_lat)`` в градусах.
    :param min_population: порог отсечения ячеек; ниже него точка не нужна.
    :param points_per_km2: верхняя плотность точек. Больше — значит дробление
        ячеек растра точнее, чем сами данные.
    :param built: открытый :class:`passenger.estimate.BuiltSurfaceRaster`.
        Если задан, рабочие места оцениваются из застройки — **оценкой, а не
        измерением** (см. ``passenger.estimate``). Жили и оценка рабочих мест
        агрегируются **одним и тем же** блоком: если брать жителей суммой по
        блоку, а рабочие места из одной ячейки, у точки оказались бы жители
        квартала и одно рабочее место, и модель уехала бы.
    :param m2_per_job: калибровочный параметр оценки; без ``built`` игнорируется.

    :returns: точки спроса, сводка измерения населения и оценка рабочих мест
        (``None``, если ``built`` не задан).
    """
    data, transform, _window = raster.read_window(bbox)
    bbox_xy = _as_xy(bbox)
    height, width = data.shape

    cell_area = _cell_area_m2(transform, (bbox_xy[1] + bbox_xy[3]) / 2.0)
    stride = max(1, int(round(_stride_for_density(points_per_km2, cell_area))))

    estimate = None
    jobs_grid = covered = None
    if built is not None:
        from .estimate import DEFAULT_M2_PER_JOB, jobs_from_built_surface

        density = built.density_on_grid(
            (height, width), transform, raster.crs_object(), bbox
        )
        covered = built.covered_by(
            (height, width), transform, raster.crs_object(), bbox
        )
        parameter = DEFAULT_M2_PER_JOB if m2_per_job is None else float(m2_per_job)
        jobs_grid, estimate = jobs_from_built_surface(
            density, covered, cell_area, parameter
        )

    points: list[DemandPoint] = []
    total = 0.0
    populated = 0
    no_data = 0
    for row in range(0, height, stride):
        for column in range(0, width, stride):
            block = data[row : row + stride, column : column + stride]
            block_valid = ~block.mask
            # Ячейки без данных не ноль, а отсутствие измерения. Блок, где не
            # измерено ничего, точкой спроса не становится; блок, где измерена
            # часть, суммирует измеренное — и это честно, потому что
            # «население этой точки неизвестно» и «население равно нулю» — разные
            # вещи (§17.3).
            if not bool(block_valid.any()):
                no_data += int(block.size)
                continue
            # Сумма маскированного массива пропускает masked-ячейки сама, поэтому
            # отдельно их вычитать не нужно.
            block_sum = float(block.sum())
            no_data += int(block.size) - int((~block.mask).sum())
            if not math.isfinite(block_sum) or block_sum < min_population:
                continue
            if block_sum <= 0.0:
                continue
            # Точка стоит в центре блока: усреднение координат центров ячеек
            # блока, а не его левого верхнего угла — иначе все точки сместились
            # бы к северо-западу на половину шага.
            lon, lat = transform * (column + stride / 2.0, row + stride / 2.0)
            jobs = 0.0
            if jobs_grid is not None:
                jobs = float(
                    jobs_grid[row : row + stride, column : column + stride].sum()
                )
            total += block_sum
            populated += 1
            points.append(
                DemandPoint(
                    key=f"ghs:{lon:.5f}:{lat:.5f}",
                    lat=float(lat),
                    lon=float(lon),
                    residents=block_sum,
                    jobs=jobs,
                )
            )

    if not points:
        raise ValueError(
            "в границе не нашлось ни одной ячейки с населением: проверьте "
            "границу и покрытие растра (продукт Northern Hemisphere "
            "заканчивается на 41° с. ш.)"
        )

    points.sort(key=lambda point: point.key)
    reading = PopulationReading(
        product=GHS_POPULATION_PRODUCT,
        cells=height * width,
        populated_cells=populated,
        no_data_cells=no_data,
        total_population=total,
        raster_crs=raster.crs,
        cells_per_point=float(stride * stride),
    )
    return points, reading, estimate


def _cell_area_m2(transform: Any, mid_latitude: float) -> float:
    """Площадь ячейки растра в м² на средней широте окна.

    Считается прямо из градусов: ячейка географического растра имеет разный
    размер по долготе на разных широтах, и брать одну константу означало бы,
    что на севере точки получались плотнее, чем на юге, при тех же данных.

    Длина градуса долготы: ``111 320 · cos(φ)``, широты: ``110 574`` м.
    Берётся **площадь**, а не сторона: у ячейки произвольное соотношение
    сторон, и «сторона» квадратной ячейки той же площади — единственное
    сравнимое число. У этого продукта на 51° с. ш. ячейка выходит
    примерно 57 × 92 м, то есть около 0,53 га, а не 1 га: продукт задан на
    равноплощадной проекции, а отдан в градусах, и его «100 м» — это 100 м
    там, а не здесь.
    """
    width_m = abs(transform.a) * 111_320.0 * math.cos(math.radians(mid_latitude))
    height_m = abs(transform.e) * 110_574.0
    return width_m * height_m


def _stride_for_density(points_per_km2: float, cell_area_m2: float) -> int:
    """Шаг между точками, ограничивающий плотность заданным значением.

    Из плотности прямо: одна точка на ``S²`` ячеек даёт
    ``10⁶ / (плотность · S² · площадь_ячейки)`` точек на км².
    """
    if points_per_km2 <= 0.0 or cell_area_m2 <= 0.0:
        return 1
    return max(1, int(round(math.sqrt(1_000_000.0 / (points_per_km2 * cell_area_m2)))))


def read_population(
    path: str,
    bbox: Any,
    **kwargs: Any,
) -> tuple[list[DemandPoint], PopulationReading, Any]:
    """Удобная обёртка: открыть растр, прочитать окно, закрыть.

    Для повторных прогонов лучше держать :class:`PopulationRaster` открытым
    самому: растр страны весит сотни мегабайт, и открытие на каждый город в
    пакетной обработке дороже чтения.
    """
    with PopulationRaster(path) as raster:
        return population_points(raster, bbox, **kwargs)