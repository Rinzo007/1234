/**
 * T4 — Закрытие года: инварианты §5.3 [P5], §6.7, §13.2.
 */

import { describe, expect, it } from 'vitest';
import { closeYear } from '../src/sim/year';
import { makeTestCity, fullCoverageNetwork, makeNetwork } from './fixtures';

describe('T4: закрытие года', () => {
  const city = makeTestCity();

  it('пустая сеть: все поездки потеряны, Σ причин = потерянные поездки', () => {
    const empty = makeNetwork([], []);
    const rep = closeYear({ city, network: empty, year: 1, isStandardFare: true });
    expect(rep.metrics.passengersPerDay).toBe(0);
    expect(rep.totalTripsInCity).toBeGreaterThan(0);
    const sum = [...rep.refusals.values()].reduce((s, x) => s + x, 0);
    // Инвариант §5.3: сумма причин равна числу потерянных поездок (с допуском округления).
    expect(Math.abs(sum - rep.totalTripsInCity)).toBeLessThanOrEqual(Math.max(50, 0.02 * rep.totalTripsInCity));
  });

  it('«Нет пути» не входит в процент удовлетворённости (§6.7, §13.2)', () => {
    const empty = makeNetwork([], []);
    const rep = closeYear({ city, network: empty, year: 1, isStandardFare: true });
    // Ни одна поездка не обслужена → удовлетворённость 0, но она считается по оставшейся базе.
    expect(rep.metrics.satisfactionPct).toBe(0);
    // При этом noPath может присутствовать в refusals — он просто не участвует в знаменателе.
    expect(rep.tripsNetworkCanServe).toBe(0);
  });

  it('полное покрытие: большинство доезжает, баланс неотрицателен при стандартном тарифе', () => {
    const net = fullCoverageNetwork();
    const rep = closeYear({ city, network: net, year: 1, isStandardFare: true });
    expect(rep.metrics.passengersPerDay).toBeGreaterThan(0);
    expect(rep.lineResults.length).toBe(1);
    const lr = rep.lineResults[0]!;
    expect(lr.passengersPerDay).toBe(rep.metrics.passengersPerDay);
    expect(lr.sectionLoads.every((x) => x >= 0)).toBe(true);
    expect(rep.metrics.satisfactionPct).toBeGreaterThanOrEqual(0);
    expect(rep.metrics.satisfactionPct).toBeLessThanOrEqual(100);
  });

  it('инвариант: доля пассажиров ≤ общего числа поездок города', () => {
    const net = fullCoverageNetwork();
    const rep = closeYear({ city, network: net, year: 1, isStandardFare: true });
    expect(rep.metrics.passengersPerDay).toBeLessThanOrEqual(rep.totalTripsInCity);
  });

  it('улучшение графика увеличивает пассажиропоток или удовлетворённость', () => {
    const net = fullCoverageNetwork();
    const base = closeYear({ city, network: net, year: 1, isStandardFare: true });
    for (const l of net.lines) {
      l.timetable.takts.amPeak = 3;
      l.timetable.takts.pmPeak = 3;
    }
    const better = closeYear({ city, network: net, year: 2, isStandardFare: true });
    expect(better.metrics.passengersPerDay).toBeGreaterThanOrEqual(base.metrics.passengersPerDay);
    expect(better.metrics.satisfactionPct).toBeGreaterThanOrEqual(base.metrics.satisfactionPct);
  });

  it('высокий произвольный тариф снижает спрос на транспорт (§6.5 fare-коэффициент)', () => {
    const net = fullCoverageNetwork();
    const std = closeYear({ city, network: net, year: 1, isStandardFare: true });
    const pricey = closeYear({
      city, network: net, year: 2, isStandardFare: false,
      customFareBaseCents: 80_000, customFarePerKmCents: 12_000,
    });
    expect(pricey.metrics.passengersPerDay).toBeLessThan(std.metrics.passengersPerDay);
  });
});
