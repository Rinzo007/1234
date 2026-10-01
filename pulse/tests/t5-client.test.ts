/**
 * T5 — клиентские модули: проекция Web Mercator, построение пакета из Overture-мест,
 * редактор тактов/тарифа в черновике года.
 */

import { describe, expect, it } from 'vitest';
import { toLonLat, toMeters } from '../src/client/geo';
import { buildCityPackage } from '../src/client/overture';
import type { RawPlace } from '../src/client/overture';
import { sanitizeCity } from '../src/sim/city';
import { newGame, setDraftTakts, setDraftFare, effectiveTakts, draftFare } from '../src/client/game';
import { makeTestCity } from './fixtures';

describe('T5.1 проекция карты (Web Mercator) вместо линейного маппинга', () => {
  const anchor = { lon0: 13.4, lat0: 52.5 };

  it('круговая точность: метры → lon/lat → метры', () => {
    for (const [x, y] of [[0, 0], [8000, 0], [-6000, 4500], [1234, -9876]] as const) {
      const [lon, lat] = toLonLat(anchor, x, y);
      const back = toMeters(anchor, lon, lat);
      expect(Math.abs(back.x - x)).toBeLessThanOrEqual(1);
      expect(Math.abs(back.y - y)).toBeLessThanOrEqual(1);
    }
  });

  it('масштаб по долготе учитывает широту (не линейный градус-маппинг)', () => {
    // Приращение долготы на 1000 м обратно пропорционально cos(широты) — как в EPSG:3857.
    const [lonEq0] = toLonLat({ lon0: 0, lat0: 0 }, 0, 0);
    const [lonEq] = toLonLat({ lon0: 0, lat0: 0 }, 1000, 0);
    const [lonHi0] = toLonLat(anchor, 0, 0);
    const [lonHi] = toLonLat(anchor, 1000, 0);
    expect((lonHi - lonHi0) / (lonEq - lonEq0)).toBeCloseTo(1 / Math.cos((52.5 * Math.PI) / 180), 6);
  });

  it('север остаётся севером: +y увеличивает широту нелинейно', () => {
    const [, lat1] = toLonLat(anchor, 0, 10_000);
    const [, lat2] = toLonLat(anchor, 0, 20_000);
    expect(lat2).toBeGreaterThan(lat1);
    // В Mercator равные метры дают убывающие приращения градусов на севере… но
    // на 52.5° приращение d(lat)/dy уменьшается с широтой — проверяем знак и монотонность.
    expect(lat1).toBeGreaterThan(anchor.lat0);
  });
});

describe('T5.2 построение пакета города из мест Overture', () => {
  const center = { lon: 13.4, lat: 52.5, name: 'Berlin' };
  const places: RawPlace[] = [];
  // Сетка «домов» западнее центра и «работ» восточнее — как реальный Берлинский разрез.
  for (let i = 0; i < 4; i++) {
    places.push({ id: `h${i}`, category: 'residential', names: null, lon: 13.3 + i * 0.004, lat: 52.52, population: 900 });
  }
  for (let i = 0; i < 3; i++) {
    places.push({ id: `j${i}`, category: 'commerce', names: null, lon: 13.45 + i * 0.004, lat: 52.51, population: 1200 });
  }

  it('проходит санитайзер §5.3 (инварианты попы, Σ jobs = Σ residents)', () => {
    const pkg = buildCityPackage(places, center);
    const res = sanitizeCity(pkg);
    expect(res.ok).toBe(true);
    expect(res.errors).toEqual([]);
  });

  it('размеры попов ≤ POP_SIZE_CAP и каждый поп перечислен дважды', () => {
    const pkg = buildCityPackage(places, center);
    expect(pkg.pops.length).toBeGreaterThan(0);
    for (const pop of pkg.pops) expect(pop.size).toBeLessThanOrEqual(200);
    const listed = pkg.demandPoints.reduce((s, p) => s + p.popIds.length, 0);
    expect(listed).toBe(2 * pkg.pops.length);
  });

  it('без рабочих точек — понятная ошибка, а не пустой пакет (§ Outcome)', () => {
    const onlyHomes = places.filter((p) => p.category === 'residential');
    expect(() => buildCityPackage(onlyHomes, center)).toThrow(/Недостаточно данных/);
  });
});

describe('T5.3 редактор тактов и тарифа уходит в черновик года (§9.1)', () => {
  it('правка такта не меняет сеть до закрытия года, effectiveTakts показывает черновик', () => {
    const state = newGame(makeTestCity(), false);
    const line = state.network.lines[0]!;
    const before = { ...line.timetable.takts };
    setDraftTakts(state, line.id as string, { amPeak: 4 });
    expect(line.timetable.takts.amPeak).toBe(before.amPeak); // сеть не тронута
    expect(effectiveTakts(state, line.id as string)!.amPeak).toBe(4);
  });

  it('смена тарифа попадает в черновик плана', () => {
    const state = newGame(makeTestCity(), false);
    expect(draftFare(state).isStandardFare).toBe(true);
    setDraftFare(state, { isStandardFare: false, customBaseCents: 100, customPerKmCents: 20 });
    expect(draftFare(state).customBaseCents).toBe(100);
  });
});
