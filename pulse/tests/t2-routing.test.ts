/**
 * T2 — Построение пути (RAPTOR, §6.2–§6.3) и воспринимаемое время (§6.4).
 * Проверяются: дискретные шаги интервала, якорная минута, множители Wardman,
 * штраф за пересадку, лимит 4 пересадок, «линия не ходит в пик», пешая доступность.
 */

import { describe, expect, it } from 'vitest';
import { buildGraph, periodOfHour, perceivedWaitSec, raptor, nearestStopWithinRadius } from '../src/sim/raptor';
import { perceiveTransit, transitProbability, valueOfTimeCentsPerSecond, fareCents } from '../src/sim/passenger';
import { TAKT_STEPS, PT_MULTIPLIERS, ROUTING, INCOME, STANDARD_FARE_CENTS_PER_TRIP, STANDARD_FARE_CENTS_PER_KM } from '../src/sim/constants';
import type { RaptorResult } from '../src/sim/raptor';
import { fullCoverageNetwork, HOUR_OF_PERIOD, busLine, makeNetwork, station } from './fixtures';

const dummyJourney = (over: Partial<RaptorResult>): RaptorResult => ({
  targetStop: 1, earliestArrivalSec: 1000, transfers: 0,
  inVehicleSec: 900, waitSec: 100, walkToSec: 200, walkFromSec: 100,
  rideSecondsByLine: new Map(), boardings: [], ...over,
});

describe('T2: график интервалов и расписание', () => {
  it('шаги интервала дискретны (§8.2 [T]): от 3 до 120 минут, ровно 14 ступеней', () => {
    expect(TAKT_STEPS).toEqual([3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 40, 60, 90, 120]);
  });

  it('границы пяти частей суток (§8.2)', () => {
    expect(periodOfHour(5)).toBe('night');
    expect(periodOfHour(7)).toBe('amPeak');
    expect(periodOfHour(12)).toBe('day');
    expect(periodOfHour(17)).toBe('pmPeak');
    expect(periodOfHour(22)).toBe('evening');
  });

  it('воспринимаемое ожидание = полинтервала × множитель Headway 0,60 (§A.5)', () => {
    // takt 10 мин → 300 с × 0,6 = 180 с.
    expect(perceivedWaitSec(10)).toBe(Math.round(300 * PT_MULTIPLIERS.headway));
    expect(perceivedWaitSec(0)).toBe(0);
  });
});

describe('T2: RAPTOR по фикстуре полного покрытия', () => {
  const network = fullCoverageNetwork();
  const graph = buildGraph(network, 1);

  it('граф построен: 1 маршрут, остановки и секции согласованы', () => {
    expect(graph.routes).toHaveLength(1);
    expect(graph.stops.length).toBe(8);
    expect(graph.routes[0]!.stopSequence).toHaveLength(8);
    expect(graph.routes[0]!.interStopSec).toHaveLength(7);
  });

  it('прямая поездка через весь город существует и состоит из одной посадки', () => {
    const r = raptor(graph, 0, 7, HOUR_OF_PERIOD.amPeak, 0, 0);
    expect(r).not.toBeNull();
    expect(r!.transfers).toBe(0);
    expect(r!.boardings).toHaveLength(1);
    // 10 км при 5 м/с + 8 с остановки на перегон ≈ 2000 + 56 с.
    expect(r!.inVehicleSec).toBeGreaterThanOrEqual(1900);
    expect(r!.inVehicleSec).toBeLessThanOrEqual(2200);
  });

  it('якорная минута часа (§8.7): ожидание кратна интервалу и сдвигается якорем', () => {
    // Ночной takts = 15 мин (DEFAULT_TAKTS[0]), anchor = 0. Готов в :00 → 0 ожидания;
    // якорь :07 → ближайший рейс через 7 минут; оба значения кратны интервалу.
    const line = busLine('LA', [0, 1000]);
    const g = buildGraph(makeNetwork([line], [station('S0', 0), station('S1', 1000)]), 1);
    const headway = line.timetable.takts.night * 60;
    const a0 = raptor(g, 0, 1, HOUR_OF_PERIOD.night, 0, 0)!;
    line.timetable.anchorMinute = 7;
    const g7 = buildGraph(makeNetwork([line], [station('S0', 0), station('S1', 1000)]), 2);
    const a7 = raptor(g7, 0, 1, HOUR_OF_PERIOD.night, 0, 0)!;
    expect(a0.waitSec % headway).toBe(0);
    expect(a7.waitSec % headway).toBe(0);
    expect(a7.waitSec).toBe(7 * 60);
  });

  it('линия с takts.amPeak = 0 не даёт путь в час пик (§12.6 диагностика)', () => {
    const line = busLine('LX', [0, 1000, 2000]);
    line.timetable.takts.amPeak = 0;
    const g = buildGraph(makeNetwork([line], [station('SLX_0', 0), station('SLX_1', 1000), station('SLX_2', 2000)]), 1);
    const peak = raptor(g, 0, 2, HOUR_OF_PERIOD.amPeak, 0, 0);
    const day = raptor(g, 0, 2, HOUR_OF_PERIOD.day, 0, 0);
    expect(peak).toBeNull();
    expect(day).not.toBeNull();
  });
});

describe('T2: воспринимаемое время и выбор режима', () => {
  it('формула §6.4: поездка×1,0 + пешком×1,67 + ожидание×1,72 + тариф/стоимость-времени', () => {
    const vot = valueOfTimeCentsPerSecond(INCOME.mean); // $60k / 1860 ч
    const j = perceiveTransit(dummyJourney({}), 200, vot);
    const expected = Math.round(
      900 * PT_MULTIPLIERS.inVehicle +
      300 * PT_MULTIPLIERS.walk +
      100 * PT_MULTIPLIERS.wait +
      200 / vot,
    );
    expect(j.perceivedSec).toBe(expected);
  });

  it('стоимость времени растёт с доходом и ограничена диапазоном (§6.4 [S])', () => {
    const low = valueOfTimeCentsPerSecond(10_000);   // ниже min → клампится к 15k
    const mid = valueOfTimeCentsPerSecond(INCOME.min);
    const high = valueOfTimeCentsPerSecond(1_000_000); // выше max → клампится к 200k
    expect(low).toBe(mid);
    expect(high).toBeGreaterThan(mid);
  });

  it('калибровка §6.5.1: разрыв в 10 минут ощутимо сдвигает вероятность', () => {
    const pEq = transitProbability(1000, 1000);
    expect(pEq).toBeCloseTo(0.5, 5);
    const pSlow = transitProbability(1600, 1000); // транспорт на 10 мин дольше
    expect(pSlow).toBeLessThan(0.5);
    expect(pEq - pSlow).toBeGreaterThan(0.1);
  });

  it('тариф §12.2: €0,60 + €0,12/км по прямой', () => {
    expect(fareCents(8000, true)).toBe(STANDARD_FARE_CENTS_PER_TRIP + STANDARD_FARE_CENTS_PER_KM * 8);
    expect(fareCents(0, true)).toBe(STANDARD_FARE_CENTS_PER_TRIP);
    expect(fareCents(8000, false, 100, 20)).toBe(100 + 20 * 8); // пользовательский тариф
  });

  it('пеший радиус автобусной остановки 500 м (§A.1); за радиусом — null', () => {
    const network = fullCoverageNetwork();
    const graph = buildGraph(network, 1);
    const near = nearestStopWithinRadius(graph, network, 400, 0);
    const far = nearestStopWithinRadius(graph, network, 5000, 0);
    expect(near).not.toBeNull();
    expect(near!.distanceM).toBeLessThanOrEqual(MODES_BUS_WALK_RADIUS());
    expect(far).toBeNull(); // x=5000 дальше всего: 2600 м > 500 м
  });

  it('лимит пересадок §6.3: MAX_TRANSFERS = 4', () => {
    expect(ROUTING.MAX_TRANSFERS).toBe(4);
  });
});

function MODES_BUS_WALK_RADIUS(): number {
  return 500; // §A.1 bus.walkRadiusM
}
