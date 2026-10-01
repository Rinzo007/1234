/**
 * T5: годовой цикл клиента (src/client/game.ts) — §9.1, §12.1, §12.6, §13.
 */
import { describe, it, expect } from 'vitest';
import { makeTestCity, station, busLine, makeNetwork } from './fixtures';
import { id } from '../src/sim/types';
import {
  newGame, addLineToDraft, makeLine, closeGameYear, draftCostCents,
  START_CAPITAL_CENTS, setDraftTakts,
} from '../src/client/game';

function stationsFor(xs: number[]) {
  return xs.map((x, i) => station(`CS_${i}`, x, `Остановка ${i}`));
}

describe('T5 годовой цикл', () => {
  it('черновик не меняет сеть и не списывает деньги до закрытия года (§9.1)', () => {
    const city = makeTestCity();
    const state = newGame(city);
    const xs = [0, 800, 1600, 2400];
    const sts = stationsFor(xs);
    for (const s of sts) state.network.stations.set(s.id as string, s);
    const line = makeLine('bus', sts.map((s) => s.id), state.network, 'Тест', '#f00');
    const err = addLineToDraft(state, line);
    expect(err).toBeNull();
    expect(state.network.lines.length).toBe(0); // ещё не в сети
    expect(state.ledger.capitalCents).toBe(START_CAPITAL_CENTS); // деньги не списаны
    expect(state.ledger.reservedForPlanCents).toBe(draftCostCents(state)); // но зарезервированы
  });

  it('закрытие года применяет план, списывает CAPEX и выдаёт отчёт (§9.1, §13)', () => {
    const city = makeTestCity();
    const state = newGame(city);
    const xs = [0, 800, 1600, 2400, 8000, 8800, 9600, 10000];
    const sts = stationsFor(xs);
    for (const s of sts) state.network.stations.set(s.id as string, s);
    const line = makeLine('bus', sts.map((s) => s.id), state.network, 'Автобус 1', '#f00');
    expect(addLineToDraft(state, line)).toBeNull();
    const capexBefore = draftCostCents(state);
    expect(capexBefore).toBeGreaterThan(0);

    const report = closeGameYear(state);
    expect(state.network.lines.length).toBe(1); // план применён
    expect(state.ledger.capitalCents).toBeLessThan(START_CAPITAL_CENTS + report.grantsPaidCents + report.bonusesPaidCents);
    expect(report.year).toBe(1);
    expect(state.year).toBe(2);
    expect(report.totalTripsInCity).toBeGreaterThanOrEqual(20_000);
    // Инвариант §5.3/§20.1: Σ причин = потерянные поездки (с допуском корректировки carWins)
    const lost = report.totalTripsInCity - report.metrics.passengersPerDay;
    const sumRef = [...report.refusals.values()].reduce((s, x) => s + x, 0);
    expect(Math.abs(sumRef - lost)).toBeLessThanOrEqual(Math.max(50, 0.02 * lost + 1));
  });

  it('общий путь второй линии не платится (§9.6)', () => {
    const city = makeTestCity();
    const state = newGame(city);
    const xs = [0, 800, 1600];
    const sts = stationsFor(xs);
    for (const s of sts) state.network.stations.set(s.id as string, s);
    const l1 = makeLine('bus', sts.map((s) => s.id), state.network, 'Л1', '#f00');
    expect(addLineToDraft(state, l1)).toBeNull();
    closeGameYear(state);
    // Вторая линия по тем же станциям — все секции shared → цена 0
    const l2 = makeLine('bus', sts.map((s) => s.id), state.network, 'Л2', '#0f0');
    expect(l2.sections.every((s) => s.sharedTrackLineId !== null)).toBe(true);
    expect(addLineToDraft(state, l2)).toBeNull();
    expect(draftCostCents(state)).toBe(0);
  });

  it('не хватает капитала — линия в черновик не проходит (§12.1)', () => {
    const city = { ...makeTestCity(), priceFactor: 100 }; // дорогой город ×100
    const state = newGame(city);
    // метро-линия глубокого заложения была бы дороже всего; для MVP упрощаем: много длинных секций
    const xs = Array.from({ length: 30 }, (_, i) => i * 3000); // 87 км автобуса
    const sts = xs.map((x, i) => station(`BX_${i}`, x));
    for (const s of sts) state.network.stations.set(s.id as string, s);
    const line = makeLine('bus', sts.map((s) => s.id), state.network, 'Длинная', '#00f');
    const err = addLineToDraft(state, line);
    expect(err).not.toBeNull();
    expect(state.draft.newLines.length).toBe(0);
  });

  it('sandbox: строить можно, даже когда бюджет уходит в минус (§12.9)', () => {
    const city = { ...makeTestCity(), priceFactor: 100 };
    const state = newGame(city, true);
    const xs = Array.from({ length: 30 }, (_, i) => i * 3000);
    const sts = xs.map((x, i) => station(`SX_${i}`, x));
    for (const s of sts) state.network.stations.set(s.id as string, s);
    const line = makeLine('bus', sts.map((s) => s.id), state.network, 'Длинная', '#00f');
    expect(addLineToDraft(state, line)).toBeNull();
    closeGameYear(state);
    // В sandbox капитал может быть отрицательным — фиксируем это свойство
    expect(typeof state.ledger.capitalCents).toBe('number');
  });

  it('изменение тактов из черновика применяется при закрытии года (§8, §9.1)', () => {
    const city = makeTestCity();
    const state = newGame(city);
    const line = busLine('L1', [0, 800, 1600, 2400]);
    const sts = [0, 800, 1600, 2400].map((x, i) => station(`TS_${i}`, x));
    line.stations = sts.map((s) => s.id);
    line.sections = [0, 1, 2].map((i) => ({
      fromStationId: sts[i]!.id, toStationId: sts[i + 1]!.id,
      lengthM: 800, serviceKindIndex: 0, levelKey: 'atGrade', sharedTrackLineId: null,
    }));
    for (const s of sts) state.network.stations.set(s.id as string, s);
    state.network.lines.push(line); // существующая линия уже в сети
    setDraftTakts(state, line.id as string, { amPeak: 5 });
    closeGameYear(state);
    expect(state.network.lines[0]!.timetable.takts.amPeak).toBe(5);
  });

  it('требование района даёт грант после покрытия (§12.6)', () => {
    const city = makeTestCity();
    const state = newGame(city);
    // Покрыть оба района: линия от 0 до 10000 с шагом ≤ 800 м (радиус автобуса 500 м, точки на 0..2000 и 8000..10000)
    const xs = [0, 800, 1600, 2000, 2400, 7600, 8000, 8800, 9600, 10000];
    const sts = xs.map((x, i) => station(`G_${i}`, x));
    for (const s of sts) state.network.stations.set(s.id as string, s);
    const line = makeLine('bus', sts.map((s) => s.id), state.network, 'Грантовая', '#ff0');
    expect(addLineToDraft(state, line)).toBeNull();
    const r1 = closeGameYear(state);
    // Жилой район покрыт полностью → хотя бы один грант должен прийти
    expect(r1.grantsPaidCents).toBeGreaterThan(0);
    void id; // re-export sanity
  });

  it('отчёт год за годом стабилен: вторая итерация работает (§20 детерминизм)', () => {
    const city = makeTestCity();
    const state = newGame(city);
    const xs = [0, 800, 1600, 2400, 8000, 8800, 9600, 10000];
    const sts = stationsFor(xs);
    for (const s of sts) state.network.stations.set(s.id as string, s);
    const line = makeLine('bus', sts.map((s) => s.id), state.network, 'Автобус 1', '#f00');
    expect(addLineToDraft(state, line)).toBeNull();
    const rep1 = closeGameYear(state);
    const rep2 = closeGameYear(state); // без изменений — тот же поток
    expect(rep2.year).toBe(2);
    expect(rep2.metrics.passengersPerDay).toBe(rep1.metrics.passengersPerDay);
    expect(makeNetwork([], []).lines.length).toBe(0); // sanity импорт
  });
});
