/**
 * Сеть: пропускная способность пути, парк составов, предпросмотр цены до щелчка.
 * §9.1 «ничего не меняется до щелчка», §9.7 пропускная способность, §11.1 парк.
 */

import type { Network, Station, TransitLine } from './model';
import { MODES, FLEET_BUFFER, PERIODS, PREVIEW_PEOPLE_RADIUS_M, spacingWord, LEVEL_MULTIPLIERS, TBM_FEE_MLN } from './constants';
import type { DemandPoint } from './model';

export function stationById(network: Network, sid: string): Station {
  const s = network.stations.get(sid);
  if (!s) throw new Error(`Станция ${sid} не найдена`);
  return s;
}

/** Линии, реально останавливающиеся на станции (для загрузки и пересадок). */
export function linesAtStation(network: Network, sid: string): TransitLine[] {
  return network.lines.filter(
    (l) => !l.parked && l.stations.includes(sid as never),
  );
}

/**
 * Загрузка пути в составах/ч в каждом направлении по текущему ПЛАНУ (§9.7 [T]):
 * скрытая линия считается; план — считается; припаркованная — нет.
 */
export function trackLoadPerHour(line: TransitLine, sectionIndex: number): number {
  let sum = 0;
  for (const period of PERIODS) {
    const takt = line.timetable.takts[period.name];
    if (takt > 0) sum += (60 / takt);
  }
  // Среднее по пяти периодам с весом их длительности.
  const totalHours = PERIODS.reduce((s, p) => s + (p.to - p.from), 0);
  let weighted = 0;
  for (const period of PERIODS) {
    const takt = line.timetable.takts[period.name];
    if (takt > 0) weighted += (60 / takt) * (period.to - period.from);
  }
  void sectionIndex;
  return totalHours > 0 ? weighted / totalHours : 0;
}

/** Ограничения перетаскивания столбца интервала §9.7 [T]. */
export interface TaktCheck {
  readonly allowed: boolean;
  readonly reason?: string;
}

export function checkTaktApplicable(
  network: Network,
  line: TransitLine,
  sectionIndex: number,
  proposedTaktMin: number,
): TaktCheck {
  if (proposedTaktMin === 0) return { allowed: true };
  const spec = MODES[line.mode];
  if (proposedTaktMin < spec.minTaktMin) {
    return {
      allowed: false,
      reason: `Этот путь нельзя эксплуатировать чаще, чем раз в ${spec.minTaktMin} минуты`,
    };
  }
  // Все линии, использующие тот же физический путь (совпадающая пара станций).
  const secA = line.sections[sectionIndex];
  if (!secA) return { allowed: false, reason: 'Нет такой секции' };
  const users = network.lines.filter((l) =>
    !l.parked &&
    l.sections.some((s) =>
      (s.fromStationId === secA.fromStationId && s.toStationId === secA.toStationId) ||
      (s.fromStationId === secA.toStationId && s.toStationId === secA.fromStationId),
    ),
  );
  // §9.7: лимит пути диктует самый «слабый» режим из использующих его.
  // Для проверки перегрузки считаем вклад каждой линии в её собственном режиме.
  const capacity = Math.min(...users.map((u) => MODES[u.mode].trackCapacityPerHour));
  let load = 0;
  for (const u of users) {
    const isSelf = u.id === line.id;
    const ownCap = MODES[u.mode].trackCapacityPerHour;
    const takt = isSelf ? proposedTaktMin : trackLoadPerHour(u, 0) > 0 ? harmonicTakt(u) : 0;
    if (takt > 0) load += Math.min(60 / takt, ownCap);
  }
  if (load > capacity) {
    return { allowed: false, reason: 'Общий путь заполнен. Пусть другая линия ходит реже' };
  }
  return { allowed: true };
}

/** Средний действующий интервал линии по periods (для оценки загрузки общего пути). */
function harmonicTakt(line: TransitLine): number {
  const active = PERIODS.map((p) => line.timetable.takts[p.name]).filter((t) => t > 0);
  if (active.length === 0) return 0;
  return Math.round(60 / (active.reduce((s, t) => s + 60 / t, 0) / active.length));
}

/** Парк составов: ⌈оборот/интервал⌉ × 1,15 §11.1 [T]. */
export function requiredFleet(line: TransitLine): number {
  const spec = MODES[line.mode];
  const lengthM = lineLengthM(line);
  const dwellStops = line.stations.length * (8 + avgExtraDwell(line));
  const outAndBack = 2;
  const speedMs = spec.speedsMs[Math.max(...line.sections.map((s) => s.serviceKindIndex))] ?? spec.speedsMs[0]!;
  const runTimeS = (lengthM / speedMs) * outAndBack + dwellStops * outAndBack;
  const peakTakt = Math.min(...PERIODS.map((p) => line.timetable.takts[p.name]).filter((t) => t > 0), 120);
  if (peakTakt <= 0) return 0;
  return Math.ceil((runTimeS / 60 / peakTakt) * FLEET_BUFFER);
}

function avgExtraDwell(line: TransitLine): number {
  void line;
  return 0; // extraDwell типа станции добавляется в detailed-прогоне T3; MVP — базовые 8 с
}

export function lineLengthM(line: TransitLine): number {
  return line.sections.reduce((s, x) => s + x.lengthM, 0);
}

// ─────────────── Предпросмотр цены до щелчка (§9.1, §9.8) ───────────────

export interface SegmentEstimate {
  /** Что сделает щелчок, например «Продолжить от Kottbusser Tor». */
  readonly headline: string;
  readonly distanceToPrevStopM: number;
  readonly spacingWord: string;
  readonly peopleInRadius: number;
  readonly jobsInRadius: number;
  /** Цена в целых центах €×10⁵ (упрощённо: € млн × 100 000 000). */
  readonly priceCents: number;
  readonly blockedReason: string | null;
}

/**
 * Оценка стоимости участка: цена пути = длина × €/км режима × множитель уровня × коэффициент города.
 * Правило из §9.1: предпросмотр и применение обязаны брать цену из одного места — обе функции
 * вызывают эту, расхождение невозможно.
 */
export function segmentPriceCents(
  line: TransitLine,
  lengthM: number,
  levelKey: string,
  cityPriceFactor: number,
): number {
  const spec = MODES[line.mode];
  const kinds = [...spec.buildCostMlnPerKm.values()];
  const perKmMln = kinds[Math.min(line.mode === 'bus' || line.mode === 'tram' ? 0 : 0, kinds.length - 1)]!;
  const levelMult = LEVEL_MULTIPLIERS[levelKey] ?? 1.0;
  const mlnEuro = (lengthM / 1000) * perKmMln * levelMult * cityPriceFactor;
  const tbm = levelKey === 'deepTunnel' ? TBM_FEE_MLN : 0;
  // € млн → «центы» масштаба игры: 1 €млн = 1e8 центов.
  return Math.round((mlnEuro + tbm) * 1e8);
}

/** Число жителей/рабочих мест в радиусе 500 м (радиус карточки, не радиус режима, §9.1 [T]). */
export function peopleInPreviewRadius(points: readonly DemandPoint[], x: number, y: number): { people: number; jobs: number } {
  let people = 0;
  let jobs = 0;
  const r2 = PREVIEW_PEOPLE_RADIUS_M * PREVIEW_PEOPLE_RADIUS_M;
  for (const p of points) {
    const dx = p.x - x;
    const dy = p.y - y;
    if (dx * dx + dy * dy <= r2) { people += p.residents; jobs += p.jobs; }
  }
  return { people, jobs };
}

export function estimateSegment(
  line: TransitLine,
  prevStation: Station | null,
  nextX: number,
  nextY: number,
  points: readonly DemandPoint[],
  levelKey: string,
  cityPriceFactor: number,
): SegmentEstimate {
  const dist = prevStation ? Math.hypot(nextX - prevStation.x, nextY - prevStation.y) : 0;
  const { people, jobs } = peopleInPreviewRadius(points, nextX, nextY);
  const blocked = prevStation && dist < 150 ? 'слишком близко: 120 м до ближайшей остановки' : null;
  return {
    headline: prevStation ? `Продолжить от ${prevStation.name}` : `Начать линию ${line.name}`,
    distanceToPrevStopM: Math.round(dist),
    spacingWord: spacingWord(dist),
    peopleInRadius: people,
    jobsInRadius: jobs,
    priceCents: segmentPriceCents(line, dist, levelKey, cityPriceFactor),
    blockedReason: blocked,
  };
}
