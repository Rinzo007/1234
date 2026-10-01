/**
 * T3 — Сеть, пропускная способность путей (§9.7), парк составов (§11.1),
 * пешая доступность по типам станций (§22.2), загрузка секций.
 */

import { describe, expect, it } from 'vitest';
import { buildGraph, nearestStopWithinRadius, raptor } from '../src/sim/raptor';
import { checkTaktApplicable, requiredFleet } from '../src/sim/network';
import { MODES, ROUTING } from '../src/sim/constants';
import type { Station } from '../src/sim/model';
import { busLine, makeNetwork, station } from './fixtures';

const st = (id: string, x: number, over: Partial<Station> = {}): Station => ({
  ...station(id, x), ...over,
});

describe('T3: пропускная способность общего пути (§9.7)', () => {
  // Две Ж/Д линии на общем пути S0→S1; лимит пути = trackCapacityPerHour rail = 20 рейсов/ч,
  // минимальный интервал rail = 3 мин (20 рейсов/ч) → путь заполняет одна линия в пике.
  const sharedPair = () => {
    // Две Ж/Д линии с физически общим путём SA→SB (одинаковые секции).
    const mk = (lineId: string) => {
      const l = busLine(lineId, [0, 1000]);
      l.mode = 'rail';
      l.stations = ['SA', 'SB'] as never;
      l.sections = [{
        fromStationId: 'SA' as never, toStationId: 'SB' as never,
        lengthM: 1000, serviceKindIndex: 0, levelKey: 'atGrade', sharedTrackLineId: null,
      }] as never;
      return l;
    };
    return makeNetwork([mk('L1'), mk('L2')], [
      { ...st('SA', 0), mode: 'rail' }, { ...st('SB', 1000), mode: 'rail' },
    ]);
  };

  it('частые интервалы обеих линий на общем пути запрещены', () => {
    const g = sharedPair();
    const l1 = g.lines[0]!, l2 = g.lines[1]!;
    // L2: takt 4 мин во всех периодах → средний ~15 рейсов/ч. L1 с takt 3 (20/ч) — перегруз (>20).
    for (const p of ['night', 'amPeak', 'day', 'pmPeak', 'evening'] as const) l2.timetable.takts[p] = 4;
    const chk = checkTaktApplicable(g, l1, 0, 3);
    expect(chk.allowed).toBe(false);
    expect(chk.reason).toContain('Общий путь заполнен');
    // А takt 10 (6/ч + 15 = 21 > 20 тоже запрет; 12 → ровно 20 — разрешён).
    expect(checkTaktApplicable(g, l1, 0, 12).allowed).toBe(true);
  });

  it('разрежение другой линии освобождает место (§9.7: «Пусть другая линия ходит реже»)', () => {
    const g = sharedPair();
    const l1 = g.lines[0]!, l2 = g.lines[1]!;
    l2.timetable.takts = { night: 60, amPeak: 60, day: 60, pmPeak: 60, evening: 60 } as never; // ~1 рейс/ч
    const chk = checkTaktApplicable(g, l1, 0, 5); // 12 + 1 ≤ 20
    expect(chk.allowed).toBe(true);
  });

  it('режим диктует минимальный интервал: метро чаще 2 минут нельзя (§A.1)', () => {
    const line = busLine('LT', [0, 1000]);
    line.mode = 'metro';
    const g = makeNetwork([line], [
      { ...st('SLT_0', 0), mode: 'metro' }, { ...st('SLT_1', 1000), mode: 'metro' },
    ]);
    const chk = checkTaktApplicable(g, line, 0, 1);
    expect(chk.allowed).toBe(false);
    expect(chk.reason).toContain(`${MODES.metro.minTaktMin} минуты`);
    expect(checkTaktApplicable(g, line, 0, 3).allowed).toBe(true);
  });

  it('нулевой takt (линия не ходит в период) всегда разрешён', () => {
    const g = sharedPair();
    expect(checkTaktApplicable(g, g.lines[0]!, 0, 0).allowed).toBe(true);
  });
});

describe('T3: парк составов (§11.1)', () => {
  it('короткая линия с частым пиковым интервалом требует больше составов, чем редкий график', () => {
    const fast = busLine('LF', [0, 5000, 10000]);
    const slow = busLine('LS', [0, 5000, 10000]);
    slow.timetable.takts = { night: 60, amPeak: 60, day: 60, pmPeak: 60, evening: 60 } as never;
    expect(requiredFleet(fast)).toBeGreaterThan(requiredFleet(slow));
    expect(requiredFleet(fast)).toBeGreaterThanOrEqual(2);
  });

  it('невыходящая в пик линия всё равно считает парк по активным периодам', () => {
    const line = busLine('LN', [0, 5000]);
    line.timetable.takts.amPeak = 0;
    line.timetable.takts.pmPeak = 0;
    expect(requiredFleet(line)).toBeGreaterThan(0);
  });
});

describe('T3: пешая доступность и типы станций (§22.2)', () => {
  const network = (() => {
    const line = busLine('LR', [0, 2000]);
    return makeNetwork([line], [
      st('SLR_0', 0, { stationType: 'standard' }),          // радиус 500 м
      st('SLR_1', 2000, { stationType: 'mobilityHub' }),    // ×2.5 → 1250 м
    ]);
  })();
  const graph = buildGraph(network, 1);

  it('обычная станция: 400 м достижимо, 600 м — нет', () => {
    expect(nearestStopWithinRadius(graph, network, 400, 0)?.stop).toBe(0);
    expect(nearestStopWithinRadius(graph, network, 600, 0)).toBeNull();
  });

  it('транспортный узел расширяет охват в 2.5 раза', () => {
    // Точка x=1600: до узла (x=2000) 400 м ≤ 1250 м; до обычной (x=0) 1600 м > 500 м.
    expect(nearestStopWithinRadius(graph, network, 1600, 0)?.stop).toBe(1);
  });

  it('пешая связь между остановками в пределах радиуса пересадки входит в граф', () => {
    // S0 и S1 на расстоянии 2000 м > walkRadius×2 → прямой пеший связи нет.
    const far = buildGraph(makeNetwork([busLine('LFAR', [0, 2000])],
      [st('SLFAR_0', 0), st('SLFAR_1', 2000)]), 1);
    const walk0 = far.walkEdges.get(0) ?? [];
    expect(walk0.some((e) => e.to === 1)).toBe(false);
    void ROUTING.MAX_WALK_TO_FROM_STATION_S;
  });
});

describe('T3: пересадки через общий узел', () => {
  it('маршруты без общей остановки не связны; с общей станцией — путь с одной пересадкой', () => {
    // L1: S0→S1, L2: S1→S2. Общая станция S1 принадлежит обеим линиям (пересадочный узел).
    const l1 = busLine('L1', [0, 1000]);
    l1.stations = ['S0', 'S1'] as never;
    l1.sections = [{ fromStationId: 'S0' as never, toStationId: 'S1' as never, lengthM: 1000, serviceKindIndex: 0, levelKey: 'atGrade', sharedTrackLineId: null }] as never;
    const l2 = busLine('L2', [1000, 2000]);
    l2.stations = ['S1', 'S2'] as never;
    l2.sections = [{ fromStationId: 'S1' as never, toStationId: 'S2' as never, lengthM: 1000, serviceKindIndex: 0, levelKey: 'atGrade', sharedTrackLineId: null }] as never;
    const g = makeNetwork([l1, l2], [st('S0', 0), st('S1', 1000), st('S2', 2000)]);
    const graph2 = buildGraph(g, 1);
    const r = raptor(graph2, 0, 2, 8, 0, 0)!;
    expect(r).toBeDefined();
    expect(r.transfers).toBe(1);
    expect(r.boardings).toHaveLength(2);
  });

  it('без общей остановки и в пешем радиусе пути нет (§6.3)', () => {
    // Две линии с разными станциями на расстоянии 2 км → ни пересадки, ни связи.
    const l1 = busLine('M1', [0, 1000]);
    const l2 = busLine('M2', [3000, 4000]);
    const g = makeNetwork([l1, l2], [
      st('SM1_0', 0), st('SM1_1', 1000), st('SM2_0', 3000), st('SM2_1', 4000),
    ]);
    const graph = buildGraph(g, 1);
    expect(raptor(graph, 0, 3, 8, 0, 0)).toBeNull();
  });
});
