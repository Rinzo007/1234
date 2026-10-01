/**
 * Санитация данных города и проверка инвариантов §5.3, §18.5, §31.2.
 * Правило: инвариант проверяется при приёме города, а не подразумевается (§5.3) [P5].
 */

import type { CityPackage, Pop } from './model';
import { CITY_CODE_RE } from './types';
import { POP_SIZE_CAP } from './constants';

export interface SanitizationResult {
  readonly ok: boolean;
  readonly errors: readonly string[];
  /** Попы, исключённые как вырожденные (начало = конец, нулевое время в пути) §5.3. */
  readonly droppedDegeneratePops: number;
}

/** Класс попа по префиксу идентификатора рабочей точки §5.4 [S]. */
export function popClass(jobId: string): string | null {
  const m = /^([A-Z]{2,3})_/.exec(jobId);
  return m ? (m[1] as string) : null;
}

/**
 * Проверка инвариантов пакета города:
 *  - сумма residents = сумма размеров попов = сумма jobs (в агрегате);
 *  - Σ|popIds| = 2 × число попов (каждый поп перечислен дважды, без дублей);
 *  - ноль висячих ссылок residenceId/jobId;
 *  - размер попа ∈ [1, 200];
 *  - код города соответствует регулярному выражению §31.1.
 */
export function sanitizeCity(city: CityPackage): SanitizationResult {
  const errors: string[] = [];

  if (!CITY_CODE_RE.test(city.code)) {
    errors.push(`Код города «${city.code}» не соответствует схеме ^[A-Z]{2}...$ (§31.1)`);
  }

  const pointById = new Map<string, { residents: number; jobs: number }>();
  for (const p of city.demandPoints) {
    if (pointById.has(p.id)) errors.push(`Дублирующийся id точки спроса ${p.id}`);
    pointById.set(p.id, { residents: p.residents, jobs: p.jobs });
  }

  // Инвариант: сумма popIds = 2 × число попов (§5.3 [P5]).
  let listed = 0;
  const listedOnce = new Map<string, number>();
  for (const p of city.demandPoints) {
    listed += p.popIds.length;
    for (const pid of p.popIds) listedOnce.set(pid, (listedOnce.get(pid) ?? 0) + 1);
  }
  if (listed !== 2 * city.pops.length) {
    errors.push(`Σ popIds = ${listed} ≠ 2 × ${city.pops.length} попов: пропуск или дубль (§5.3)`);
  }
  for (const [, count] of [...listedOnce.entries()].sort((a, b) => a[0] < b[0] ? -1 : 1)) {
    if (count > 2) { errors.push(`Поп перечислен больше двух раз в точках спроса (§5.3)`); break; }
  }

  let degenerate = 0;
  let popSizeSum = 0;
  for (const pop of city.pops) {
    popSizeSum += pop.size;
    if (pop.size < 1 || pop.size > POP_SIZE_CAP) {
      errors.push(`Поп ${pop.id}: размер ${pop.size} вне диапазона [1, ${POP_SIZE_CAP}] (§5.3)`);
    }
    if (!pointById.has(pop.residenceId)) errors.push(`Поп ${pop.id}: висячая ссылка residenceId=${pop.residenceId}`);
    if (!pointById.has(pop.jobId)) errors.push(`Поп ${pop.id}: висячая ссылка jobId=${pop.jobId}`);
    // Вырожденные попы реальны: начало = конец, drivingSeconds = 0 (§5.3).
    if (pop.residenceId === pop.jobId && pop.drivingSeconds === 0) degenerate++;
  }

  const sumResidents = city.demandPoints.reduce((s, p) => s + p.residents, 0);
  const sumJobs = city.demandPoints.reduce((s, p) => s + p.jobs, 0);
  if (sumResidents !== popSizeSum) {
    errors.push(`Σ residents (${sumResidents}) ≠ Σ размеров попов (${popSizeSum}) — проверка demand_residents_match (§18.5)`);
  }
  if (sumJobs !== sumResidents) {
    errors.push(`Σ jobs (${sumJobs}) ≠ Σ residents (${sumResidents}) (§5.3 инвариант согласованности)`);
  }

  return { ok: errors.length === 0, errors, droppedDegeneratePops: degenerate };
}

/**
 * У упрощённого поставщика без дорожных данных автоезды считаются оценкой:
 * 56 км/ч при извилистости ×1,3, и они помечаются как оценка на странице проверки (§5.3, §17.3).
 */
export function estimateDrivingSeconds(pop: Pick<Pop, 'drivingDistanceM'>, speedKmh = 56, detour = 1.3): number {
  return Math.round((pop.drivingDistanceM * detour) / (speedKmh * 1000 / 3600));
}
