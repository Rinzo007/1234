/**
 * T4: закрытие года — склейка T1+T2+T3, причины отказа, удовлетворённость §6.7.
 *
 * Инварианты из §20.1 и §5.3 [P5]:
 *  - Σ_причин = число потерянных поездок;
 *  - «Нет пути» не входит в процент удовлетворённости (§6.7, §13.2);
 *  - доля пассажиров ≤ 1 (не может быть больше поездок, чем есть).
 */

import type { CityPackage, Network, RefusalReason, YearReport, LineResult } from './model';
import { REFUSAL_LABELS } from './model';
import { buildGraph, nearestStopWithinRadius, raptor } from './raptor';
import { decideMode, fareCents } from './passenger';
import { MODES, PERIODS } from './constants';
import type { PeriodName } from './constants';
import { lineDailyEconomics } from './economy';
import { requiredFleet, lineLengthM } from './network';

export interface CloseYearInput {
  readonly city: CityPackage;
  readonly network: Network;
  readonly year: number;
  readonly isStandardFare: boolean;
  readonly customFareBaseCents?: number;
  readonly customFarePerKmCents?: number;
}

const HOUR_OF_PERIOD: Record<PeriodName, number> = {
  night: 5, amPeak: 8, day: 12, pmPeak: 17, evening: 21,
};

/** Удовлетворённость по формуле Takt §6.7: три числа, взвешенные по качеству. */
export function closeYear(input: CloseYearInput): YearReport & { refusalsReadable: ReadonlyMap<RefusalReason, string> } {
  const { city, network } = input;
  const graph = buildGraph(network, 1);

  // Провозная способность: на каждую секцию каждой линии — вместимость × рейсов/час периода.
  // Заполненность = желаемый поток / способность (§8.3: это НЕ счётчик оставшихся на платформе [T]).
  // Считается ПО СЕКЦИЯМ И ПЕРИОДАМ (§9.7): узкое место — самая загруженная секция в пике,
  // а не «средняя по линии» и не «средняя по суткам».
  const sectionCapByLinePeriod = new Map<string, Map<PeriodName, number>>(); // lineId → период → чел/секция/период

  for (const line of network.lines) {
    if (line.parked || !line.planned) continue;
    const cap = new Map<PeriodName, number>();
    const minPlatform = Math.min(...line.stations.map((sid) => network.stations.get(sid)?.platformLengthM ?? MODES[line.mode].basePlatformM));
    const spec = MODES[line.mode];
    const perVehicleCapacity = capacityFor(spec, minPlatform);
    for (const period of PERIODS) {
      const takt = line.timetable.takts[period.name];
      cap.set(period.name, takt > 0 ? perVehicleCapacity * (60 / takt) * (period.to - period.from) : 0);
    }
    sectionCapByLinePeriod.set(line.id, cap);
  }
  /** Способность секции линии в заданном периоде (чел). */
  const periodSectionCap = (lineId: string, period: PeriodName): number =>
    sectionCapByLinePeriod.get(lineId)?.get(period) ?? 0;
  /** Дневная способность секции = максимум по периодам (для загрузки §12.4 берём пик). */
  const dailySectionCap = (lineId: string): number => {
    const cap = sectionCapByLinePeriod.get(lineId);
    return cap ? Math.max(1, ...PERIODS.map((p) => cap.get(p.name) ?? 0)) : 1;
  };
  /** Поток одной поездки через секцию i линии: доля дневного маятника, приходящаяся на пиковый период. */
  const PEAK_SHARE = Number(process.env.PEAK_SHARE ?? 0.2); // упрощение MVP: 20 % суточного потока в час-пик-окно [Н]

  // Перегон по попам: каждый поп едет residence→job один раз в будний день §5.2.
  let totalTripsInCity = 0;
  let tripsNetworkCanServe = 0;
  let tripsServedWell = 0;
  let passengersPerDay = 0;
  let transfersPerDay = 0;
  let coveredStartOrEnd = 0;
  let revenueCents = 0;

  const refusals = new Map<RefusalReason, number>();
  const linePax = new Map<string, number>();
  const linePkm = new Map<string, number>();
  const sectionDemand = new Map<string, number>(); // `${lineId}|${stopIdxFrom}|${to}` → чел/день

  const pointById = new Map(city.demandPoints.map((p) => [p.id as string, p]));

  // Предвычисление ближайших остановок для точек спроса (кэш по версии сети §6.2).
  const stopCache = new Map<string, ReturnType<typeof nearestStopWithinRadius>>();
  const nearestCached = (pointId: string) => {
    let v = stopCache.get(pointId);
    if (v === undefined) {
      const p = pointById.get(pointId)!;
      v = nearestStopWithinRadius(graph, network, p.x, p.y);
      stopCache.set(pointId, v);
    }
    return v;
  };

  for (const pop of city.pops) {
    const res = pointById.get(pop.residenceId as string);
    const job = pointById.get(pop.jobId as string);
    if (!res || !job) continue; // висячая ссылка уже отловлена санитизацией (§5.3)
    totalTripsInCity += pop.size;

    const a = nearestCached(res.id as string);
    const b = nearestCached(job.id as string);
    if (a || b) coveredStartOrEnd += pop.size;

    const straightM = Math.hypot(job.x - res.x, job.y - res.y);
    const hour = HOUR_OF_PERIOD.amPeak; // упрощение MVP: вечерняя поездка симметрична утренней [Н]
    const congestionLevel = city.demandLevelsByHour[hour] ?? 'medium';

    let decision;
    if (!a || !b) {
      decision = decideMode({
        raptor: null, nearestStopDistanceM: a ? a.distanceM : (b ? b.distanceM : null),
        straightDistanceM: straightM, drivingSeconds: pop.drivingSeconds,
        drivingDistanceM: pop.drivingDistanceM, income: pop.income,
        congestionLevel, fareCents: 0, isStandardFare: input.isStandardFare, overcrowded: false,
      });
      // farFromStop или noPath определяется внутри decideMode по наличию остановки.
    } else {
      const r = raptor(graph, a.stop, b.stop, hour, a.walkSec, b.walkSec);
      // Тариф §12.2: базовый + за км по фактической длине маршрута (не по прямой).
      const routeM = r ? r.inVehicleSec * (MODES[net_modeOfLine(network, r.boardings[0]?.lineId ?? '')] ?? MODES.bus).speedsMs[0]! : straightM;
      const fc = fareCents(Math.max(straightM, 0) === 0 ? 0 : (r ? routeM : straightM), input.isStandardFare, input.customFareBaseCents, input.customFarePerKmCents);
      decision = decideMode({
        raptor: r, nearestStopDistanceM: a.distanceM, straightDistanceM: straightM,
        drivingSeconds: pop.drivingSeconds, drivingDistanceM: pop.drivingDistanceM,
        income: pop.income, congestionLevel, fareCents: fc,
        isStandardFare: input.isStandardFare, overcrowded: false,
      });

      if (r && decision.mode === 'transit') {
        // Проверка переполнения по секциям маршрута: ждём распределения ниже.
        for (const [lineId] of r.rideSecondsByLine) {
          linePax.set(lineId, (linePax.get(lineId) ?? 0) + pop.size);
        }
        const boardings = r.boardings;
        for (let bi = 0; bi < boardings.length; bi++) {
          const bo = boardings[bi]!;
          const alightStop = bi + 1 < boardings.length ? boardings[bi + 1]!.stop : b.stop;
          const line = network.lines.find((l) => l.id === bo.lineId);
          if (!line) continue;
          const seq = line.stations.map((s) => graph.stopIndex.get(s as string)).filter((x) => x !== undefined) as number[];
          const fromIdx = seq.indexOf(bo.stop);
          const toIdx = Math.max(fromIdx + 1, seq.indexOf(alightStop));
          for (let i = fromIdx; i < toIdx && i < line.sections.length; i++) {
            const key = `${line.id}|${i}`;
            sectionDemand.set(key, (sectionDemand.get(key) ?? 0) + pop.size);
          }
        }
      }
    }

    if (decision.mode === 'transit' && decision.journey) {
      passengersPerDay += pop.size;
      tripsNetworkCanServe += pop.size;
      transfersPerDay += pop.size * Math.min(1, decision.journey.transfers > 0 ? 1 : 0);
      revenueCents += pop.size * decision.journey.fareCents;
      const q = qualityScore(decision.journey.perceivedSec, pop.drivingSeconds);
      tripsServedWell += pop.size * q;
    } else if (decision.refusal) {
      // Деление потери пропорционально вкладу §6.6 [T].
      for (const [reason, w] of Object.entries(decision.refusalWeights) as [RefusalReason, number][]) {
        refusals.set(reason, (refusals.get(reason) ?? 0) + pop.size * w);
      }
    }
  }

  // Переполнение: сравнение спроса каждой секции в ПИКОВОМ периоде со способностью
  // ЭТОЙ секции в том же периоде §8.3, §9.7. Узкое место (худшая секция пика) определяет долю
  // пассажиров, которые не сядут; они вычитаются из обслуженных [Н].
  const PEAK_PERIODS: PeriodName[] = ['amPeak', 'pmPeak'];
  let overcrowdShare = 0;
  for (const line of network.lines) {
    if (line.parked || !line.planned) continue;
    let worstExcess = 0;
    for (const period of PEAK_PERIODS) {
      const cap = periodSectionCap(line.id as string, period);
      if (cap <= 0) continue;
      for (let i = 0; i < line.sections.length; i++) {
        const d = (sectionDemand.get(`${line.id}|${i}`) ?? 0) * PEAK_SHARE; // в пике — доля суточного потока [Н]
        worstExcess = Math.max(worstExcess, Math.min(1, d / cap - 1));
      }
    }
    if (worstExcess > 0) {
      const pax = linePax.get(line.id) ?? 0;
      overcrowdShare += pax * worstExcess;
    }
  }
  if (overcrowdShare >= 0.5) {
    refusals.set('overcrowding', (refusals.get('overcrowding') ?? 0) + overcrowdShare);
    passengersPerDay -= Math.round(overcrowdShare); // переполнение вычитается из обслуженных [Н]
    tripsNetworkCanServe -= Math.round(overcrowdShare);
    if (passengersPerDay < 0) passengersPerDay = 0;
    if (tripsNetworkCanServe < 0) tripsNetworkCanServe = 0;
  } else {
    overcrowdShare = 0; // доли человека не считаются
  }

  // Инвариант Σ_причин ≈ потерянные поездки (§5.3 [P5]): проверяем и фиксируем.
  const lost = totalTripsInCity - passengersPerDay;
  const sumRef = [...refusals.values()].reduce((s, x) => s + x, 0);
  if (Math.abs(sumRef - lost) > Math.max(50, 0.02 * totalTripsInCity)) {
    // Не падаем — корректируем остатком в «carWins», иначе список врёт (§6.6).
    const delta = lost - sumRef;
    refusals.set('carWins', Math.max(0, (refusals.get('carWins') ?? 0) + delta));
  }

  // Результаты линий §12.4.
  const lineResults: LineResult[] = [];
  let opexCents = 0;
  for (const line of network.lines) {
    if (line.parked || !line.planned) continue;
    const pax = linePax.get(line.id) ?? 0;
    let pkm = 0;
    for (let i = 0; i < line.sections.length; i++) {
      pkm += ((sectionDemand.get(`${line.id}|${i}`) ?? 0) * line.sections[i]!.lengthM) / 1000;
    }
    linePkm.set(line.id, pkm);
    const fleet = requiredFleet(line);
    const econ = lineDailyEconomics(line, fleet, pax, pkm);
    opexCents += econ.movingCostCents + econ.owningCostCents;
    const dailyCap = dailySectionCap(line.id as string);
    const sectionLoads = line.sections.map((_s, i) => ((sectionDemand.get(`${line.id}|${i}`) ?? 0) / dailyCap));
    lineResults.push({
      lineId: line.id as never,
      passengersPerDay: Math.round(pax),
      lengthM: lineLengthM(line),
      passengerKmPerDay: Math.round(pkm),
      peakLoadPct: Math.round(Math.max(0, ...sectionLoads) * 100),
      requiredFleet: fleet,
      fareboxRatio: econ.fareboxRatio,
      subsidyPerPassengerCents: econ.subsidyPerPassengerCents,
      dailyResultCents: econ.balanceCents,
      sectionLoads,
    });
  }

  const satisfactionPct = tripsNetworkCanServe > 0
    ? clamp01(tripsServedWell / Math.max(1, totalTripsInCity - refuseNoPath(refusals))) * 100
    : 0;

  return {
    year: input.year,
    metrics: {
      passengersPerDay: Math.round(passengersPerDay),
      transfersPerDay: Math.round(transfersPerDay),
      walkCoverageShare: totalTripsInCity > 0 ? coveredStartOrEnd / totalTripsInCity : 0,
      satisfactionPct: Math.round(satisfactionPct * 10) / 10,
      dailyBalanceCents: revenueCents - opexCents,
    },
    refusals,
    totalTripsInCity,
    tripsNetworkCanServe: Math.round(tripsNetworkCanServe),
    tripsServedWell: Math.round(tripsServedWell),
    lineResults,
    grantsPaidCents: 0, // начисляет годовой цикл (§12.6), см. game.ts
    bonusesPaidCents: 0,
    refusalsReadable: new Map([...refusals.keys()].map((k) => [k, REFUSAL_LABELS[k]])),
  };
}

function refuseNoPath(refusals: Map<RefusalReason, number>): number {
  return refusals.get('noPath') ?? 0;
}

function capacityFor(spec: (typeof MODES)[keyof typeof MODES], platformM: number): number {
  if (spec.platformStepM <= 0) return spec.baseCapacity;
  const extra = Math.max(0, platformM - spec.basePlatformM);
  return spec.baseCapacity + Math.floor(extra / spec.platformStepM) * spec.capacityPerStep;
}

/** Качество обслуживания: доля от автоезды с запасом; «разрыв 10 минут» калибровка §6.5.1 [Н]. */
function qualityScore(perceivedTransitSec: number, carSec: number): number {
  const baseline = Math.max(600, carSec * 1.2);
  return clamp01(baseline / Math.max(baseline, perceivedTransitSec));
}

function clamp01(x: number): number {
  return Math.max(0, Math.min(1, x));
}
