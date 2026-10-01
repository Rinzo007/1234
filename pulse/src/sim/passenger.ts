/**
 * T2: воспринимаемое время, выбор режима, причины отказа. §6.4–§6.6.
 *
 * Ключевое решение §6.5.1 (данные Ha et al. 2020): доход влияет на вес ДЕНЕГ во времени,
 * а не на веса времени. Множители ходьбы/ожидания одинаковы для всех; различается VOT.
 */

import type { RaptorResult } from './raptor';
import { PT_MULTIPLIERS, ROUTING, INCOME, CAR, CONGESTION_BY_LEVEL, STANDARD_FARE_CENTS_PER_TRIP, STANDARD_FARE_CENTS_PER_KM } from './constants';
import type { RefusalReason } from './model';

export interface PerceivedJourney {
  readonly perceivedSec: number;
  readonly inVehicleSec: number;
  readonly walkSec: number;
  readonly waitSec: number;
  readonly fareCents: number;
  readonly transfers: number;
}

/** Стоимость времени пассажира из индивидуального дохода §6.4 [S]. Центы в секунду. */
export function valueOfTimeCentsPerSecond(income: number): number {
  const clamped = Math.min(Math.max(income, INCOME.min), INCOME.max);
  const perHourCents = (clamped * 100) / INCOME.hoursWorkedPerYear; // $60k → ≈32,3 $/ч
  return perHourCents / 3600;
}

/**
 * Итоговое воспринимаемое время поездки (§6.4): сумма участков × множители
 * + денежная составляющая, переведённая во время через стоимость времени.
 */
export function perceiveTransit(
  r: RaptorResult,
  fareCents: number,
  votCentsPerSec: number,
): PerceivedJourney {
  const walkSec = r.walkToSec + r.walkFromSec;
  const perceivedSec = Math.round(
    r.inVehicleSec * PT_MULTIPLIERS.inVehicle +
    walkSec * PT_MULTIPLIERS.walk +
    r.waitSec * PT_MULTIPLIERS.wait +
    fareCents / votCentsPerSec,
  );
  return {
    perceivedSec,
    inVehicleSec: r.inVehicleSec,
    walkSec,
    waitSec: r.waitSec,
    fareCents,
    transfers: r.transfers,
  };
}

/** Тариф §12.2 [T]: базовый + за км по прямой между началом и концом; пересадка бесплатно. */
export function fareCents(straightDistanceM: number, isStandardFare: boolean, customBase?: number, customPerKm?: number): number {
  if (!isStandardFare && customBase !== undefined && customPerKm !== undefined) {
    return Math.round(customBase + (customPerKm * straightDistanceM) / 1000);
  }
  return Math.round(STANDARD_FARE_CENTS_PER_TRIP + (STANDARD_FARE_CENTS_PER_KM * straightDistanceM) / 1000);
}

/** Воспринимаемая стоимость автоезды §6.5, §6.8 [S]. */
export function perceiveCar(
  drivingSeconds: number,
  drivingDistanceM: number,
  congestionLevel: string,
  votCentsPerSec: number,
): PerceivedJourney & { carCostCents: number } {
  const congMult = CONGESTION_BY_LEVEL[congestionLevel] ?? 1.0;
  const driveSec = Math.round(drivingSeconds * congMult);
  const costCents = Math.round((CAR.costPerMeterCent * drivingDistanceM) / 1000) + CAR.parkingDailyCent;
  // Граница применимости §6.8: поездки короче 1000 м автомобилем получают штраф за «возню».
  const shortTripPenaltySec = drivingDistanceM < CAR.minMeaningfulDistanceM ? 300 : 0;
  const parkingSearchSec = CAR.parkingTimeEachEndS * 2;
  const perceivedSec = Math.round(
    driveSec * PT_MULTIPLIERS.inVehicle +
    parkingSearchSec * PT_MULTIPLIERS.parkingSearch +
    shortTripPenaltySec +
    costCents / votCentsPerSec,
  );
  return { perceivedSec, inVehicleSec: driveSec, walkSec: 0, waitSec: parkingSearchSec, fareCents: costCents, transfers: 0, carCostCents: costCents };
}

/** Взвешенная вероятность выбрать транспорт (logit по воспринимаемому времени) §6.5. */
export function transitProbability(transitPerceivedSec: number, carPerceivedSec: number): number {
  // Калибровка §6.5.1 [Н]: разрыв в 10 минут повышает вероятность сесть за руль на ≈30 %.
  // beta подобран так, чтобы Δ=600 с давал ~+0,3 к вероятности автомобиля на типичном пороге.
  const BETA_PER_SEC = 0.0011;
  const d = carPerceivedSec - transitPerceivedSec;
  return 1 / (1 + Math.exp(-BETA_PER_SEC * d));
}

export interface ModeDecision {
  readonly mode: 'transit' | 'driving' | 'walking';
  readonly refusal: RefusalReason | null;
  /** Вклад каждой причины в потерю удовлетворённости — делится пропорционально (§6.6) [T]. */
  readonly refusalWeights: Partial<Record<RefusalReason, number>>;
  readonly journey: PerceivedJourney | null;
  readonly probability: number;
}

const SATURATION_TIME_SEC = 90 * 60; // порог «слишком долго» для причины «Время в пути» [Н]

/**
 * Выбор режима по воспринимаемому времени и доступности §6.5.
 * Причины отказа — закрытый список §6.6; при нескольких причинах потеря делится пропорционально вкладу.
 */
export function decideMode(input: {
  raptor: RaptorResult | null;
  nearestStopDistanceM: number | null;   // null = нет остановки в пешем радиусе
  straightDistanceM: number;
  drivingSeconds: number;
  drivingDistanceM: number;
  income: number;
  congestionLevel: string;
  fareCents: number;
  isStandardFare: boolean;
  overcrowded: boolean;
}): ModeDecision {
  const vot = valueOfTimeCentsPerSecond(input.income);
  const walkOnlySec = Math.round(input.straightDistanceM / ROUTING.WALKING_SPEED_MS * PT_MULTIPLIERS.walk);

  // Нет ни одного маршрута?
  if (!input.raptor) {
    const refusal: RefusalReason = input.nearestStopDistanceM === null ? 'farFromStop' : 'noPath';
    return {
      mode: input.straightDistanceM <= 1500 ? 'walking' : 'driving',
      refusal,
      refusalWeights: { [refusal]: 1 },
      journey: null,
      probability: 0,
    };
  }

  const transit = perceiveTransit(input.raptor, input.fareCents, vot);
  const car = perceiveCar(input.drivingSeconds, input.drivingDistanceM, input.congestionLevel, vot);
  const pTransit = transitProbability(transit.perceivedSec, car.perceivedSec);

  // Разложение потери удовлетворённости по причинам §6.6.
  const weights: Partial<Record<RefusalReason, number>> = {};
  const add = (r: RefusalReason, w: number) => { weights[r] = (weights[r] ?? 0) + w; };
  if (input.overcrowded) add('overcrowding', 1);
  if (!input.isStandardFare && pTransit < 0.5) add('fare', 0.5);
  if (transit.perceivedSec > SATURATION_TIME_SEC) add('inVehicleTime', 0.5);
  if (transit.waitSec * PT_MULTIPLIERS.wait > 10 * 60) add('waiting', 0.5);
  if (transit.transfers >= 2) add('transfer', 0.5);
  if (car.perceivedSec < transit.perceivedSec) add('carWins', 1);

  const totalW = Object.values(weights).reduce((s, x) => s + (x ?? 0), 0) || 1;
  for (const k of Object.keys(weights) as RefusalReason[]) {
    weights[k] = (weights[k] ?? 0) / totalW; // пропорциональное деление потери [T]
  }

  const mode: ModeDecision['mode'] =
    input.straightDistanceM <= 1500 && walkOnlySec <= transit.perceivedSec ? 'walking'
      : pTransit >= 0.5 ? 'transit' : 'driving';

  return {
    mode,
    refusal: mode === 'transit' ? null : (Object.keys(weights)[0] as RefusalReason ?? 'carWins'),
    refusalWeights: weights,
    journey: transit,
    probability: pTransit,
  };
}
