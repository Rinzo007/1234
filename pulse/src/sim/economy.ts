/**
 * Экономика: капитал/эксплуатация, требования города, премии. §12.
 */

import type { CityPackage, Network, TransitLine } from './model';
import { MODES, CITY_REQUIREMENTS, SHARE_BONUSES_PCT, SATELLITE_BONUS_STEPS } from './constants';
import { lineLengthM } from './network';

// ─────────────── §12.1 Разделение денег ───────────────

export interface Ledger {
  /** Капитал — на что строить. Пополняется грантами и премиями. Не смешивается с эксплуатацией. */
  capitalCents: number;
  reservedForPlanCents: number;
  sandbox: boolean;
}

/** Правила Sandbox §12.9 [T]: стоимость продолжает учитываться; выключение не снимает долг. */
export function canAfford(ledger: Ledger, priceCents: number): boolean {
  if (ledger.sandbox) return true; // «Ваш бюджет может уйти ниже нуля»
  return ledger.capitalCents - ledger.reservedForPlanCents >= priceCents;
}

// ─────────────── §12.3 Ежедневный результат линии ───────────────

export interface DailyEconomics {
  revenueCents: number;
  movingCostCents: number;   // эксплуатация в движении — растёт с каждым км состава
  owningCostCents: number;   // владение — за каждый состав загруженных часов
  balanceCents: number;
  subsidyPerPassengerCents: number;
  fareboxRatio: number;
}

export function lineDailyEconomics(
  line: TransitLine,
  fleet: number,
  passengersPerDay: number,
  pkmPerDay: number,          // пассажиро-километры
): DailyEconomics {
  const spec = MODES[line.mode];
  const lengthKm = lineLengthM(line) / 1000;
  const dailyTripsKm = lengthKm * 2 * fleet; // туда-обратно каждым составом
  const movingCostCents = Math.round((spec.perKm * dailyTripsKm + spec.dailyPerVehicle * fleet) * 100);
  const owningCostCents = 0; // в MVP владение входит в dailyPerVehicle [Н: упрощение Takt]
  const revenueCents = Math.round(passengersPerDay * avgFareCentsOf(line, pkmPerDay, passengersPerDay));
  const balanceCents = revenueCents - movingCostCents - owningCostCents;
  const subsidyPerPassengerCents = passengersPerDay > 0 ? Math.round((movingCostCents + owningCostCents - revenueCents) / passengersPerDay) : 0;
  const cost = movingCostCents + owningCostCents;
  return {
    revenueCents,
    movingCostCents,
    owningCostCents,
    balanceCents,
    subsidyPerPassengerCents,
    fareboxRatio: cost > 0 ? revenueCents / cost : 0,
  };
}

function avgFareCentsOf(_line: TransitLine, pkm: number, pax: number): number {
  // Средняя дальность поездки × тарифная сетка §12.2.
  if (pax <= 0) return 0;
  void _line;
  return Math.round(60 + 12 * (pkm / pax)); // €0,60 + €0,12/км
}

// ─────────────── §12.6 Требования города ───────────────

export interface RequirementCard {
  readonly code: string;              // R1, T2... порядок задаёт карточка (§12.6)
  readonly kind: 'homes' | 'jobs' | 'town';
  readonly targetName: string;
  readonly grantCents: number;
  readonly met: boolean;
  readonly blockedReason?: string;
  readonly progressShare: number;     // 0..1
}

function grantBySize(pop: number, range: readonly [number, number]): number {
  const [lo, hi] = range;
  const t = Math.min(1, pop / 250_000);
  const mln = lo + (hi - lo) * t;
  return Math.round(mln * 1e8); // € млн → центы масштаба
}

/**
 * Покрытие района: доля жителей/рабочих мест района в пешем радиусе остановок
 * (радиус по режиму, по прямой §12.6). Остановкой считаются только те, где составы
 * действительно останавливаются: план/припаркованные/проходящие без остановки — нет.
 */
export function evaluateDistrictRequirement(
  city: CityPackage,
  network: Network,
  districtIndex: number,
): RequirementCard {
  const d = city.districts[districtIndex]!;
  const code = `${d.kind === 'homes' ? 'R' : 'J'}${districtIndex + 1}`;
  if (d.population < CITY_REQUIREMENTS.minDistrictPopulation) {
    return { code, kind: d.kind, targetName: d.name, grantCents: 0, met: false, blockedReason: 'Слишком мал для гранта', progressShare: 0 };
  }
  const coveredPoints = city.demandPoints.filter((p) => inPolygon(p.x, p.y, d.polygon));
  const total = d.kind === 'homes'
    ? coveredPoints.reduce((s, p) => s + p.residents, 0)
    : coveredPoints.reduce((s, p) => s + p.jobs, 0);
  let served = 0;
  for (const p of coveredPoints) {
    if (hasStopWithinWalkRadius(network, p.x, p.y)) served += d.kind === 'homes' ? p.residents : p.jobs;
  }
  const share = total > 0 ? served / total : 0;
  const grant = grantBySize(d.population, d.kind === 'homes' ? CITY_REQUIREMENTS.homesGrantMlnRange : CITY_REQUIREMENTS.jobsGrantMlnRange);
  return {
    code, kind: d.kind, targetName: d.name,
    grantCents: grant,
    met: share >= CITY_REQUIREMENTS.coverageShare,
    progressShare: share,
    blockedReason: share === 0 ? 'Покрытие не измерено — закройте год' : ('' as never),
  };
}

function hasStopWithinWalkRadius(network: Network, x: number, y: number): boolean {
  for (const station of network.stations.values()) {
    const spec = MODES[station.mode];
    const d = Math.hypot(station.x - x, station.y - y);
    if (d <= spec.walkRadiusM) {
      // Станция засчитывается, если хотя бы одна активная линия здесь останавливается.
      const used = network.lines.some((l) => !l.parked && l.planned && l.stations.includes(station.id as never));
      if (used) return true;
    }
  }
  return false;
}

export function inPolygon(x: number, y: number, poly: readonly (readonly [number, number])[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i]!, [xj, yj] = poly[j]!;
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/**
 * Город-спутник подключён, если поездка в центр в утро 6–9 ≤ 1,6 × автоезды,
 * до двух пересадок, заканчивается на станции в 1,5 км от центра §12.6 [T].
 */
export function townConnected(transitSeconds: number | null, carSeconds: number): boolean {
  if (transitSeconds === null) return false;
  return transitSeconds <= CITY_REQUIREMENTS.townRatioVsCar * carSeconds;
}

// ─────────────── §12.7 Премии за долю поездок ───────────────

export function nextShareBonus(tripsSharePct: number): { thresholdPct: number; bonusCents: number } | null {
  for (const th of SHARE_BONUSES_PCT) {
    if (tripsSharePct >= th) {
      // Сумма лестницы: чем выше порог, тем больше премия [Н: линейно от порога].
      return { thresholdPct: th, bonusCents: Math.round(th * 25 * 1e6) }; // 25 €млн за процент
    }
  }
  return null;
}

export function satelliteBonus(townsConnected: number): number | null {
  let best: number | null = null;
  for (const step of SATELLITE_BONUS_STEPS) if (townsConnected >= step) best = step;
  return best === null ? null : Math.round(best * 40 * 1e6);
}

/**
 * Спорное правило смягчено §12.2 [T+S]: нестандартный тариф снижает премию на 50 %,
 * но не отменяет её.
 */
export function applyFarePenalty(bonusCents: number, isStandardFare: boolean): number {
  return isStandardFare ? bonusCents : Math.round(bonusCents / 2);
}
