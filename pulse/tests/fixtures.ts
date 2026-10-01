/**
 * Синтетический город-песочница для тестов T1–T4 и ручного запуска клиента.
 * Структура: два жилых района, один деловой, линия автобуса через центр.
 */

import type { CityPackage, DemandPoint, Network, Pop, SatelliteTown, Station, TransitLine } from '../src/sim/model';
import { id, sec } from '../src/sim/types';
import { DEFAULT_TAKTS, PERIODS } from '../src/sim/constants';
import type { PeriodName } from '../src/sim/constants';

/** Час-представитель каждой из пяти частей суток (для тестов T1–T3). */
export const HOUR_OF_PERIOD: Record<PeriodName, number> = {
  night: 5, amPeak: 8, day: 12, pmPeak: 17, evening: 21,
};

void PERIODS; // границы периодов зафиксированы в constants.ts (§8.2)

const P = (pid: string, x: number, y: number, residents: number, jobs: number): DemandPoint => ({
  id: id(pid), x, y, residents, jobs, popIds: [],
});

export function makeTestCity(): CityPackage {
  // Линейный город: дома слева (x≈0..2000), работа справа (x≈8000..10000).
  const points: DemandPoint[] = [];
  for (let i = 0; i < 5; i++) points.push(P(`H_${i}`, i * 400, 0, 4000, 0));
  for (let i = 0; i < 5; i++) points.push(P(`J_${i}`, 8000 + i * 400, 0, 0, 4000));

  const pops: Pop[] = [];
  let totalPeople = 0;
  for (let h = 0; h < 5; h++) {
    for (let j = 0; j < 5; j++) {
      // 20 попов по 1000 человек = 20 000 жителей = 20 000 рабочих мест (§5.3 инварианты).
      const size = 1000;
      totalPeople += size;
      const pid = id(`POP_${h}_${j}`);
      const resId = id(`H_${h}`);
      const jobId = id(`J_${j}`);
      pops.push({
        id: pid, size, residenceId: resId, jobId,
        drivingSeconds: sec(Math.round((8000 + j * 400 - h * 400) * 1.3 / 15.56)), // 56 км/ч
        drivingDistanceM: 8000 + j * 400 - h * 400,
        income: 60_000,
        dampening: 1,
      });
      points[h]!.popIds.push(pid);
      points[5 + j]!.popIds.push(pid);
    }
  }
  void totalPeople;

  const towns: SatelliteTown[] = [{
    id: id('TOWN_A'), name: 'Городок А', center: { x: -6000, y: 0 }, population: 20_000,
    drivingSecondsToCenter: sec(720),
  }];

  return {
    schemaVersion: 1,
    code: 'DEB',
    id: id('com.pulse.city.testburg'),
    name: 'Тестбург',
    currencySymbol: '€',
    priceFactor: 1.0,
    demandPoints: points,
    pops,
    districts: [
      { id: id('D_HOMES'), name: 'Жилой район', kind: 'homes', population: 20_000, polygon: [[-500, -1000], [2500, -1000], [2500, 1000], [-500, 1000]] },
      { id: id('D_JOBS'), name: 'Деловой район', kind: 'jobs', population: 20_000, polygon: [[7500, -1000], [10500, -1000], [10500, 1000], [7500, 1000]] },
    ],
    towns,
    demandLevelsByHour: Array.from({ length: 24 }, (_, h) =>
      h >= 6 && h < 9 ? 'high' : h >= 15 && h < 19 ? 'high' : h >= 4 && h < 22 ? 'medium' : 'veryLow'),
  };
}

export function station(sid: string, x: number, name?: string): Station {
  return { id: id(sid), name: name ?? sid, x, y: 0, mode: 'bus', stationType: 'standard', platformLengthM: 12 };
}

export function busLine(lineId: string, xs: readonly number[]): TransitLine {
  const takts = {} as Record<PeriodName, number>;
  const names: PeriodName[] = ['night', 'amPeak', 'day', 'pmPeak', 'evening'];
  names.forEach((n, i) => { takts[n] = DEFAULT_TAKTS[i]!; });
  const stations = xs.map((_x, i) => id(`S${lineId}_${i}`));
  const sections = xs.slice(1).map((_x, i) => ({
    fromStationId: stations[i]!, toStationId: stations[i + 1]!,
    lengthM: xs[i + 1]! - xs[i]!, serviceKindIndex: 0, levelKey: 'atGrade', sharedTrackLineId: null,
  }));
  return {
    id: id(lineId), name: `Линия ${lineId}`, color: '#d43a3a', mode: 'bus',
    stations, sections,
    timetable: { takts, anchorMinute: 0 },
    isLoop: false, planned: true, parked: false, hidden: false,
  };
}

export function makeNetwork(lines: readonly TransitLine[], stations: readonly Station[]): Network {
  return { stations: new Map(stations.map((s) => [s.id as string, s])), lines: [...lines] };
}

/** Сеть «всё покрыто»: одна автобусная линия от x=0 до x=10000 с остановками каждые 800 м. */
export function fullCoverageNetwork(): Network {
  const xs = [0, 800, 1600, 2400, 8000, 8800, 9600, 10000];
  const stations = xs.map((x, i) => station(`SL_${i}`, x, `Остановка ${i}`));
  const line = busLine('L1', xs);
  line.stations = stations.map((s) => s.id);
  line.sections = xs.slice(1).map((_x, i) => ({
    fromStationId: stations[i]!.id, toStationId: stations[i + 1]!.id,
    lengthM: xs[i + 1]! - xs[i]!, serviceKindIndex: 0, levelKey: 'atGrade', sharedTrackLineId: null,
  }));
  return makeNetwork([line], stations);
}
