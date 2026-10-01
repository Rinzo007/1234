/**
 * PULSE — загрузка реальных городов из Overture Maps (§17 «источники данных», [S]).
 *
 * Источник: публичные параллельные воркеры Overture (geojsonparquet), слой
 * «places» — точки интереса с категорией и population. Данные читаются напрямую
 * из Apache Parquet в браузере через DuckDB-WASM (без сервера). Модуль грузится
 * лениво (dynamic import) — сам DuckDB (~5 МБ) не попадает в стартовый бандл.
 *
 * Как строится пакет города (§5):
 *  - жилые точки = категории residential / address (спрос на жильё);
 *  - рабочие точки = категории workplace (commerce/office/education/health…);
 *  - попы (§5.3): каждый жилой кластер связан с ближайшим рабочим кластером;
 *    размер попа ≤ POP_SIZE_CAP, Σ жителей = Σ размеров;
 *  - автоезда оценивается запасным путём §5.3 (56 км/ч × 1,3) — дорог в этом
 *    запросе нет, поэтому значение помечается как оценка [Н].
 */

import type { CityPackage, DemandPoint, District, Pop, SatelliteTown } from '../sim/model';
import { id, measuredSec } from '../sim/types';
import { FALLBACK_CAR_DETOUR, FALLBACK_CAR_SPEED_KMH, POP_SIZE_CAP } from '../sim/constants';
import { sanitizeCity } from '../sim/city';

const DUCKDB_WASM_URL = 'https://cdn.jsdelivr.net/npm/@duckdb/wasm@1/dist/duckdb-mjs.js';
const BUNDLED_WASM_URL = 'https://cdn.jsdelivr.net/npm/@duckdb/wasm@1/dist/duckdb-coi.wasm';
const OVERTURE_PLACES = 'https://overturemapswestus2.blob.core.windows.net/release/2024-09-18-beta.1/theme=places/type=place/*.parquet';

/** Ограничение области запроса — bbox вокруг выбранного места, ~20×20 км. */
export interface PlaceQueryBox { lonMin: number; latMin: number; lonMax: number; latMax: number }

export interface RawPlace { id: string; category: string; names: string | null; lon: number; lat: number; population: number | null }

const WORKPLACE_RE = /^(commerce|education|health|government|industrial|office|essential)$/;
const RESIDENTIAL_RE = /^(residential|address)$/;

let dbPromise: Promise<DuckDbLike> | null = null;

interface DuckDbLike {
  query(sql: string): Promise<unknown[]>;
}

/** Ленивая инициализация DuckDB-WASM (один раз на страницу). */
async function getDuckDb(): Promise<DuckDbLike> {
  if (!dbPromise) {
    dbPromise = (async () => {
      const mod = await import(/* @vite-ignore */ DUCKDB_WASM_URL);
      const { WASM_LIBRARIES, initializeDuckDB, openDuckDB } = mod as {
        WASM_LIBRARIES: { mvp: { mainModule: string; multiplexerWorker?: string; worker?: string } };
        initializeDuckDB: (config: unknown, progress: unknown) => Promise<void>;
        openDuckDB: (progress: unknown, config: unknown) => Promise<{ registerFileBuffer?: unknown; connection?: unknown; close?: unknown }>;
      };
      WASM_LIBRARIES.mvp.mainModule = BUNDLED_WASM_URL;
      await initializeDuckDB({ pauseOnLoad: false }, () => {});
      const db = await openDuckDB(() => {}, { path: '/tmp/pulse.duckdb', allowFallbackToLocalStorage: true });
      const conn = (db as unknown as { connect: () => Promise<{ query: (sql: string) => Promise<unknown[]> }> }).connect();
      return { query: (sql: string) => conn.then((c) => c.query(sql)) };
    })();
  }
  return dbPromise;
}

/** SQL выборка мест в bbox (категория верхнего уровня + population + координаты). */
function placesSql(box: PlaceQueryBox): string {
  return `
    SELECT id,
           string_split(class, '.')[1] AS category,
           json_extract_string(names, '$[0].value') AS name,
           geometry.x AS lon,
           geometry.y AS lat,
           TRY_CAST(string_split(type, ':')[2] AS BIGINT) AS population
    FROM read_parquet('${OVERTURE_PLACES}')
    WHERE bbox && _s_bbox(ST_MakeEnvelope(${box.lonMin}, ${box.latMin}, ${box.lonMax}, ${box.latMax}))
      AND confidence > 0.5
    LIMIT 20000`;
}

/** Простейшая кластеризация точек по сетке cellSize (метры локальной плоскости). */
function clusterPoints(points: { x: number; y: number; weight: number }[], cellSize: number) {
  const cells = new Map<string, { x: number; y: number; weight: number }>();
  for (const p of points) {
    const key = `${Math.floor(p.x / cellSize)}:${Math.floor(p.y / cellSize)}`;
    const c = cells.get(key);
    if (c) {
      const w = c.weight + p.weight;
      c.x = (c.x * c.weight + p.x * p.weight) / w;
      c.y = (c.y * c.weight + p.y * p.weight) / w;
      c.weight = w;
    } else {
      cells.set(key, { ...p });
    }
  }
  return [...cells.values()].filter((c) => c.weight > 0);
}

export interface LoadProgress { step: string; pct: number }

/**
 * Построить CityPackage по МЕСТАМ (сырые строки слоя places Overture Maps).
 * Экспортируется отдельно от сетевой части, чтобы чистая функция была тестируемой.
 */
export function buildCityPackage(places: readonly RawPlace[], center: { lon: number; lat: number; name: string }): CityPackage {
  if (places.length === 0) throw new Error('В этой области Overture не вернул мест — выберите плотнее к центру города');

  // Метры локальной плоскости: 1° ≈ 111 320 м × cos(lat) по широте, по долготе — 111 320 м.
  const mPerDegLon = 111_320 * Math.cos(center.lat * Math.PI / 180);
  const toXY = (lon: number, lat: number) => ({
    x: Math.round((lon - center.lon) * mPerDegLon),
    y: Math.round((lat - center.lat) * 111_320),
  });

  const homesRaw = places.filter((p) => RESIDENTIAL_RE.test(p.category)).map((p) => ({ ...toXY(p.lon, p.lat), weight: p.population ?? 200 }));
  const jobsRaw = places.filter((p) => WORKPLACE_RE.test(p.category)).map((p) => ({ ...toXY(p.lon, p.lat), weight: p.population != null ? Math.max(50, p.population) : 300 }));
  if (homesRaw.length === 0 || jobsRaw.length === 0) {
    throw new Error(`Недостаточно данных: жилых кластеров ${homesRaw.length}, рабочих ${jobsRaw.length}`);
  }

  const homes = clusterPoints(homesRaw, 800);
  const jobs = clusterPoints(jobsRaw, 800);

  // Баланс спроса (§5.3 инвариант Σ jobs = Σ residents): рабочие точки получают
  // веса пропорционально жилым — распределение мест по близости к домам [Н].
  const totalHomes = Math.round(homes.reduce((s, h) => s + h.weight, 0));
  const jobWeightSum = jobs.reduce((s, j) => s + j.weight, 0);
  for (const j of jobs) j.weight = (j.weight * totalHomes) / jobWeightSum;

  // Точки спроса (§5.1): residents/jobs — для отображения; пассажиров создают попы (§5.2).
  const demandPoints: DemandPoint[] = [];
  homes.forEach((h, i) => demandPoints.push({ id: id(`OH_${i}`), x: h.x, y: h.y, residents: Math.round(h.weight), jobs: 0, popIds: [] }));
  // Инвариант §5.3 Σ jobs = Σ residents: целочисленные веса рабочих точек строятся
  // жадно с точным остатком в последней точке (сумма округлений ровно равна сумме жителей).
  const sumR = demandPoints.reduce((s, p) => s + p.residents, 0);
  let assigned = 0;
  jobs.forEach((j, i) => {
    const jobsInt = i === jobs.length - 1 ? sumR - assigned : Math.min(Math.round(j.weight), sumR - assigned - (jobs.length - 1 - i));
    assigned += jobsInt;
    demandPoints.push({ id: id(`OJ_${i}`), x: j.x, y: j.y, residents: 0, jobs: jobsInt, popIds: [] });
  });

  // Попы (§5.3): каждый дом ←→ ближайшая работа; размеры ≤ POP_SIZE_CAP; инвариант
  // Σ размеров = сумма по обоим концам обеспечивается тем, что один поп вешается на оба конца.
  // Рабочие точки принимают попы до своей ёмкости jobs (очередь по близости к дому).
  const pops: Pop[] = [];
  let popIdx = 0;
  const carSpeedMs = FALLBACK_CAR_SPEED_KMH / 3.6;
  const jobRemaining = jobs.map((j) => Math.round(j.weight));
  for (let hi = 0; hi < homes.length; hi++) {
    const h = homes[hi]!;
    // Порядок рабочих точек: от ближайшей к дальней — остатки уходят дальше [Н].
    const order = jobs.map((j, ji) => ({ ji, d: Math.hypot(j.x - h.x, j.y - h.y) })).sort((a, b) => a.d - b.d);
    let remaining = Math.round(h.weight);
    let oi = 0;
    while (remaining > 0 && oi < order.length) {
      const { ji, d } = order[oi]!;
      if (jobRemaining[ji]! <= 0) { oi++; continue; }
      const take = Math.min(remaining, jobRemaining[ji]!, POP_SIZE_CAP);
      jobRemaining[ji]! -= take;
      remaining -= take;
      const pid = id(`OP_${popIdx++}`);
      pops.push({
        id: pid, size: take,
        residenceId: demandPoints[hi]!.id,
        jobId: demandPoints[homes.length + ji]!.id,
        drivingSeconds: measuredSec(Math.round(d * FALLBACK_CAR_DETOUR / carSpeedMs)), // [S]-оценка §5.3
        drivingDistanceM: Math.round(d * FALLBACK_CAR_DETOUR),
        income: 60_000,
        dampening: 1,
      });
      demandPoints[hi]!.popIds.push(pid);
      demandPoints[homes.length + ji]!.popIds.push(pid);
    }
    if (remaining > 0) {
      // Все работы исчерпаны теоретически невозможно (Σ равны) — защита от дрейфа округления.
      throw new Error(`Не удалось разместить ${remaining} человек попа для жилого кластера ${hi} (§5.3)`);
    }
  }

  const totalPeople = pops.reduce((s, p) => s + p.size, 0);
  const districts: District[] = [
    { id: id('OD_HOMES'), name: 'Жилые районы', kind: 'homes', population: totalPeople, polygon: [[-1000, -1000], [1000, -1000], [1000, 1000], [-1000, 1000]] },
    { id: id('OD_JOBS'), name: 'Рабочие места', kind: 'jobs', population: totalPeople, polygon: [[-1000, -1000], [1000, -1000], [1000, 1000], [-1000, 1000]] },
  ];
  const towns: SatelliteTown[] = [];

  const slug = center.name.toLowerCase().replace(/[^a-zа-я0-9]+/gi, '-');
  const code = (`RU${slug.slice(0, 2).toUpperCase().replace(/[^A-Z]/g, 'X')}`).padEnd(3, '0').slice(0, 4);
  const pkg: CityPackage = {
    schemaVersion: 1,
    code: /^[A-Z]{2}[0-9]{1,2}$/.test(code) ? code : 'RU00', // §31.1
    id: id(`com.pulse.city.${slug}`),
    name: center.name,
    currencySymbol: '€',
    priceFactor: 1.0,
    demandPoints,
    pops,
    districts,
    towns,
    demandLevelsByHour: Array.from({ length: 24 }, (_, h) =>
      (h >= 6 && h < 9) || (h >= 15 && h < 19) ? 'high' : h >= 4 && h < 22 ? 'medium' : 'veryLow'),
  };
  const res = sanitizeCity(pkg); // инварианты §5.3 обязаны пройти
  if (!res.ok) throw new Error(`Пакет не прошёл санитайзер (§5.3): ${res.errors.join('; ')}`);
  return pkg;
}

/**
 * Построить CityPackage по месту: box — область поиска (Overture places через
 * DuckDB-WASM), center — якорь (lon/lat) и название города. Возвращает пакет,
 * готовый к newGame().
 */
export async function loadOvertureCity(
  box: PlaceQueryBox,
  center: { lon: number; lat: number; name: string },
  onProgress: (p: LoadProgress) => void = () => {},
): Promise<CityPackage> {
  onProgress({ step: 'Запрос к Overture Maps…', pct: 10 });
  const db = await getDuckDb();
  const rows = (await db.query(placesSql(box))) as Record<string, ArrayLike<unknown>>[];
  // DuckDB возвращает колончатый формат — разворачиваем в объекты.
  const places: RawPlace[] = [];
  if (rows.length === 1 && !Array.isArray(rows[0])) {
    const r = rows[0]! as unknown as Record<string, ArrayLike<string | number | null>>;
    const n = (r.id ?? { length: 0 }).length;
    for (let i = 0; i < n; i++) {
      places.push({
        id: String(r.id![i]), category: String(r.category?.[i] ?? ''),
        names: r.name ? String(r.name[i]) : null,
        lon: Number(r.lon![i]), lat: Number(r.lat![i]),
        population: r.population?.[i] != null ? Number(r.population[i]) : null,
      });
    }
  } else {
    for (const r of rows as unknown as RawPlace[]) places.push(r);
  }
  onProgress({ step: `Мест получено: ${places.length}. Строим попы…`, pct: 55 });
  const pkg = buildCityPackage(places, center);
  onProgress({ step: 'Готово', pct: 100 });
  return pkg;
}

/** Прямоугольник поиска ±10 км вокруг точки. */
export function boxAround(lon: number, lat: number, km = 10): PlaceQueryBox {
  const dLat = km / 111.32;
  const dLon = km / (111.32 * Math.cos(lat * Math.PI / 180));
  return { lonMin: lon - dLon, latMin: lat - dLat, lonMax: lon + dLon, latMax: lat + dLat };
}
