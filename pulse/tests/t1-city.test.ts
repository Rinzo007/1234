/**
 * T1 — Городской пакет (§5, §31.1).
 * Инварианты: каждый поп перечислен ровно дважды; размер попа ≤ 200 (в фикстуре намеренно
 * больше — санитайзер должен это отловить); код города по регулярному выражению;
 * офлайн-оценка времени автоезды (§5.3: 56 км/ч × коэффициент длины пути 1,3).
 */

import { describe, expect, it } from 'vitest';
import { sanitizeCity, estimateDrivingSeconds, popClass } from '../src/sim/city';
import { CITY_CODE_RE, id } from '../src/sim/types';
import { CITY_REQUIREMENTS, POP_SIZE_CAP } from '../src/sim/constants';
import { makeTestCity } from './fixtures';

describe('T1: городской пакет', () => {
  it('корректный пакет проходит санитайзер без ошибок (§5.3)', () => {
    // Фикстура по умолчанию намеренно «крупная» (попы по 1000 > cap 200) — для T4-скорости.
    // Для проверки валидности уменьшаем попы до 20 и синхронизируем residents/jobs точек.
    const city = makeTestCity();
    const perPoint = city.pops.length / 5; // 5 pop'ов на точку
    const shrunkPoints = city.demandPoints.map((p) => ({
      ...p,
      residents: p.residents > 0 ? perPoint * 20 : 0,
      jobs: p.jobs > 0 ? perPoint * 20 : 0,
    }));
    const okCity = {
      ...city,
      demandPoints: shrunkPoints,
      pops: city.pops.map((pp) => ({ ...pp, size: 20 })),
    };
    const res = sanitizeCity(okCity);
    expect(res.errors).toEqual([]);
    expect(res.ok).toBe(true);
  });

  it('инвариант §5.3: каждый popId встречается ровно в двух точках спроса', () => {
    const city = makeTestCity();
    const counts = new Map<string, number>();
    for (const p of city.demandPoints) {
      for (const pid of p.popIds) counts.set(pid, (counts.get(pid) ?? 0) + 1);
    }
    expect(counts.size).toBe(city.pops.length);
    for (const c of counts.values()) expect(c).toBe(2);
  });

  it('санитайзер отклоняет поп с нулевым размером и висячую ссылку (§5.3)', () => {
    const city = makeTestCity();
    const broken = {
      ...city,
      pops: [
        ...city.pops.slice(1),
        { ...city.pops[0]!, size: 0, residenceId: id('H_0'), jobId: id('GHOST') },
      ],
    };
    const res = sanitizeCity(broken);
    expect(res.errors.length).toBeGreaterThan(0);
  });

  it('фикстура отклоняется строго по двум ожидаемым причинам (§5.3, §18.5)', () => {
    const res = sanitizeCity(makeTestCity());
    expect(POP_SIZE_CAP).toBe(200);
    // Песочница намеренно «крупная»: попы по 1000 > cap 200 и Σ размеров ≠ Σ residents.
    const kinds = new Set(res.errors.map((e) =>
      e.includes('вне диапазона') ? 'cap' : e.includes('demand_residents_match') ? 'sum' : 'other'));
    expect([...kinds].sort()).toEqual(['cap', 'sum']);
  });

  it('несогласованность Σ residents ≠ Σ размеров попов ловится (§18.5)', () => {
    const city = makeTestCity();
    const skewed = {
      ...city,
      demandPoints: city.demandPoints.map((p, i) =>
        i === 0 ? { ...p, residents: p.residents + 1 } : p),
    };
    const errs = sanitizeCity(skewed).errors.join('\n');
    expect(errs).toContain('demand_residents_match');
  });

  it('код города соответствует формату §31.1', () => {
    expect(CITY_CODE_RE.test(makeTestCity().code)).toBe(true);     // DEB
    expect(CITY_CODE_RE.test('NY')).toBe(true);
    expect(CITY_CODE_RE.test('DE1')).toBe(true);
    expect(CITY_CODE_RE.test('D')).toBe(false);
    expect(CITY_CODE_RE.test('deb')).toBe(false);
    expect(CITY_CODE_RE.test('TOOLONG')).toBe(false);
  });

  it('офлайн-оценка времени автоезды: 8 км при 56 км/ч × 1,3 ≈ 673 с (§5.3)', () => {
    const s = estimateDrivingSeconds({ drivingDistanceM: 8000 });
    expect(s).toBe(669); // Math.round(8000×1,3 ÷ (56 км/ч = 15,556 м/с)) = 669 с
  });

  it('класс рабочего места определяет доход/сглаживание (§5.4)', () => {
    expect(popClass('JOBS_0')).toBeNull();     // 4 буквы — не класс; префикс строго 2–3 (§5.4)
    expect(popClass('AIR_1')).toBe('AIR');    // аэропорт — особый класс §5.4
    expect(popClass('towncenter')).toBeNull();
    expect(CITY_REQUIREMENTS).toBeDefined();
  });
});
