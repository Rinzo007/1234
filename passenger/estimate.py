"""Оценка рабочих мест из слоя застройки GHSL — **оценка, а не измерение**.

Слой населения ``GHS_POP`` содержит только жителей, а масса назначения в
гравитационной модели (§5.5) — это рабочие места. Без них OD-матрица не
строится вовсе: :func:`passenger.demand._od_matrix` берёт назначения по
``jobs > 0`` и вернула бы пустой результат.

Рабочие места оцениваются из слоя застроенной поверхности ``GHS_BUILT_S``
(квадратных метров кровли и площадей в ячейке): мест работы тем больше, где
больше построено. Связь рабочих мест с застройкой — не измеренный закон, а
**калибровочный параметр**, и в этом весь смысл модуля: параметр назван,
записан в результат и показан вместе с тем, как он меняет ответ.

Что здесь неправомерно и поэтому сделано явно:

- **``m2_per_job`` — свободный параметр.** 100 м² застройки на одно рабочее
  место взято как разумное значение для смешанной городской застройки, а не
  измерено. От него зависит абсолютное число рабочих мест, и подмена его
  другим числом молча меняет модель. Поэтому :attr:`JobsEstimate.sensitivity`
  показывает ответ при нескольких значениях, а :attr:`JobsEstimate.measured`
  равен ``False`` всегда.
- **Застройка — не рабочие места.** Жилая застройка даёт площадь, но не
  рабочие места; производственные площади дают и то и другое. Слой не
  различает назначение зданий, поэтому оценка смешивает жильё, офисы и
  производство и смещена в сторону жилых площадей.
- **Оценка не повышает уровень данных по рубрике** (§18.5). Рубрика отвечает
  на вопрос «чем измеряли», и подмена измерения оценкой уровень не меняет —
  она меняет его вниз.

Число рабочих мест, полученное здесь, годится, чтобы модель **заработала** и
стала проверяемой. Годится ли оно, чтобы на него опираться в решениях, —
вопрос к рубрике и к измеренному расхождению (§17.3), а не к этому модулю.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .population import _as_xy, _cell_area_m2, _require_rasterio


#: Продукт GHSL, из которого берётся площадь застройки.
BUILT_SURFACE_PRODUCT: str = "GHS_BUILT_S E2030 / 100m / GLOBE_R2023A"

#: Калибровочный параметр: квадратных метров застройки на одно рабочее место.
#: **Не измерено.** Взято как разумное значение для смешанной городской
#: застройки. От него прямо зависит абсолютное число рабочих мест.
DEFAULT_M2_PER_JOB: float = 100.0

#: Значения параметра, на которых показывается чувствительность оценки.
SENSITIVITY_STEPS: tuple[float, ...] = (50.0, 100.0, 200.0)


@dataclass(frozen=True, slots=True)
class JobsEstimate:
    """Оценка рабочих мест с её происхождением и чувствительностью.

    Объект существует затем, чтобы оценку нельзя было прочитать как измерение:
    ``measured`` всегда ``False``, а ``m2_per_job`` и ``sensitivity`` рядом с
    числом, а не в сноске.
    """

    #: Всегда ``False``: рабочие места не измерены, а оценены.
    measured: bool
    #: Слой, из которого взята площадь.
    product: str
    #: Калибровочный параметр, м² застройки на рабочее место.
    m2_per_job: float
    #: Суммарная застроенная площадь в границе, м².
    total_built_m2: float
    #: Оценённое число рабочих мест.
    total_jobs: float
    #: Ячеек в окне, где застройка не измерена.
    cells_without_built_data: int
    #: ``(m2_per_job, total_jobs)`` при нескольких значениях параметра.
    sensitivity: tuple[tuple[float, float], ...]
    notes: tuple[str, ...]

    def summary(self) -> str:
        return (
            f"рабочие места — ОЦЕНКА из {self.product}: "
            f"{self.total_jobs:,.0f} при {self.m2_per_job:.0f} м² застройки "
            f"на место (не измерено); уровень данных по рубрике §18.5 "
            f"понижается"
        )


class BuiltSurfaceRaster:
    """Слой застроенной поверхности GHSL (GHS_BUILT_S).

    Отдан в равноплощадной проекции Моллвейде (ESRI:54009), а слой населения —
    в градусах (EPSG:4326). Читать их одним и тем же окном нельзя: одинаковые
    координаты в двух системах — это разные места, и «совпадение ячеек» значило
    бы совпадение ничего. Поэтому окно слоя застройки переводится в его
    собственную СКП, а содержимое пересчитывается на сетку населения.
    """

    __slots__ = ("_path", "_src")

    def __init__(self, path: str) -> None:
        self._path = str(path)
        self._src: Any = None

    def __enter__(self) -> "BuiltSurfaceRaster":
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
    def crs(self) -> str:
        return "" if self._src is None else str(self._src.crs)

    def density_on_grid(
        self,
        shape: tuple[int, int],
        dst_transform: Any,
        dst_crs: Any,
        bbox_degrees: tuple[float, float, float, float],
    ) -> Any:
        """Застроенность (м² застройки на м² земли) на чужой сетке.

        Возвращается **плотность**, а не сумма: пересчёт суммы пересчётом
        среднего по площади дал бы потерю площади, и у края окна застройка
        занижалась бы в зависимости от того, как края ложатся на ячейки. Плотность
        от площади не зависит, и умножение её на площадь ячейки назначения даёт
        верные квадратные метры в любом месте.
        """
        import numpy as np
        from rasterio.crs import CRS
        from rasterio.warp import Resampling, reproject, transform_bounds
        from rasterio.windows import from_bounds

        if self._src is None:
            raise RuntimeError("слой застройки не открыт")

        src_bbox = transform_bounds(
            CRS.from_string("EPSG:4326"), self._src.crs, *_as_xy(bbox_degrees)
        )
        # Запас по краю окна: пересчёт усреднением берёт соседние ячейки, и без
        # запаса крайнее кольцо считалось бы по обрезанным данным.
        pad_x = (src_bbox[2] - src_bbox[0]) * 0.05
        pad_y = (src_bbox[3] - src_bbox[1]) * 0.05
        window = from_bounds(
            src_bbox[0] - pad_x,
            src_bbox[1] - pad_y,
            src_bbox[2] + pad_x,
            src_bbox[3] + pad_y,
            transform=self._src.transform,
        )
        raw = self._src.read(1, window=window, masked=True)
        src_transform = self._src.window_transform(window)

        cell_area_src = abs(self._src.transform.a * self._src.transform.e)
        density = np.asarray(raw.filled(0.0), dtype="float64") / cell_area_src

        destination = np.zeros(shape, dtype="float64")
        reproject(
            source=density,
            destination=destination,
            src_transform=src_transform,
            src_crs=self._src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.average,
        )
        return destination

    def covered_by(
        self,
        shape: tuple[int, int],
        dst_transform: Any,
        dst_crs: Any,
        bbox_degrees: tuple[float, float, float, float],
    ) -> Any:
        """Маска ячеек назначения, где застройка **покрыта** данными.

        Отдельно от плотности, потому что «нулевая застройка» и «застройка не
        измерена» — разные вещи, и сведение их к нулю превратило бы участки без
        данных в пустые рабочие места.
        """
        import numpy as np
        from rasterio.crs import CRS
        from rasterio.warp import Resampling, reproject, transform_bounds
        from rasterio.windows import from_bounds

        if self._src is None:
            raise RuntimeError("слой застройки не открыт")

        src_bbox = transform_bounds(
            CRS.from_string("EPSG:4326"), self._src.crs, *_as_xy(bbox_degrees)
        )
        pad_x = (src_bbox[2] - src_bbox[0]) * 0.05
        pad_y = (src_bbox[3] - src_bbox[1]) * 0.05
        window = from_bounds(
            src_bbox[0] - pad_x,
            src_bbox[1] - pad_y,
            src_bbox[2] + pad_x,
            src_bbox[3] + pad_y,
            transform=self._src.transform,
        )
        valid = (~self._src.read(1, window=window, masked=True).mask).astype("float64")
        src_transform = self._src.window_transform(window)

        destination = np.zeros(shape, dtype="float64")
        reproject(
            source=valid,
            destination=destination,
            src_transform=src_transform,
            src_crs=self._src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.average,
        )
        return destination > 0.0


def jobs_from_built_surface(
    density_grid: Any,
    covered: Any,
    cell_area_m2: float,
    m2_per_job: float = DEFAULT_M2_PER_JOB,
) -> tuple[Any, JobsEstimate]:
    """Переводит застроенность в оценку рабочих мест по сетке.

    :param density_grid: застроенность в м²/м² на сетке назначения.
    :param covered: маска покрытия слоем застройки.
    :param cell_area_m2: площадь ячейки назначения, м².
    :param m2_per_job: калибровочный параметр, **не измерен**.
    """
    import numpy as np

    if m2_per_job <= 0.0:
        raise ValueError("m2_per_job должен быть положительным")
    built_m2 = density_grid * cell_area_m2 * covered
    jobs = built_m2 / m2_per_job

    total_built = float(built_m2.sum())
    total_jobs = float(jobs.sum())
    return jobs, JobsEstimate(
        measured=False,
        product=BUILT_SURFACE_PRODUCT,
        m2_per_job=float(m2_per_job),
        total_built_m2=total_built,
        total_jobs=total_jobs,
        cells_without_built_data=int((~covered).sum()),
        sensitivity=tuple(
            (step, float(total_built / step)) for step in SENSITIVITY_STEPS
        ),
        notes=(
            f"рабочие места оценены из застроенной площади при "
            f"{m2_per_job:.0f} м² на место — параметр не измерен",
            "слой застройки не различает жильё, офисы и производство, поэтому "
            "оценка смещена в сторону жилых площадей",
            "уровень данных по рубрике §18.5 понижается: измерение заменено "
            "оценкой",
        ),
    )