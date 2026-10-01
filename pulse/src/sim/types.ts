/**
 * PULSE — базовые типы предметной области.
 * Девять «смысловых» типов по §30.3 Дизайн-документа.
 * Время — целые секунды от полуночи; деньги — целые единицы (центы/копейки).
 */

/** Секунды от полуночи. Целое. Никогда не плавающая точка и никогда не дата. §30.3 */
export type Second = number & { readonly __brand: 'Second' };

export const sec = (n: number): Second => {
  if (!Number.isInteger(n)) throw new Error(`Second must be integer, got ${n}`);
  return n as Second;
};

/** Деньги в целых единицах. Никогда не плавающая точка — детерминизм §32.5. */
export type Money = number & { readonly __brand: 'Money' };

export const money = (n: number): Money => {
  if (!Number.isInteger(n)) throw new Error(`Money must be integer, got ${n}`);
  return n as Money;
};

/** Секунды, измеренные по дорожному графу. Отличается от оценки. §5.3 */
export type MeasuredSecond = number & { readonly __brand: 'Measured' };

export const measuredSec = (n: number): MeasuredSecond => {
  if (!Number.isInteger(n)) throw new Error(`MeasuredSecond must be integer, got ${n}`);
  return n as MeasuredSecond;
};

/** Секунды, оценённые запасным путём. Попадают в отчёт как оценка. §17.3 */
export type EstimatedSecond = number & { readonly __brand: 'Estimated' };

/** Интервал — только из дискретного множества §8.2. 0 = движение прекращено. */
export type TaktMinutes =
  | 0 | 2 | 3 | 4 | 5 | 6 | 8 | 10 | 12 | 15 | 20 | 30 | 40 | 60 | 90 | 120;

/** Идентификатор неизменяем. Создаётся один раз. §31.1 */
export type ImmutableId = string & { readonly __brand: 'Id' };

export const id = (s: string): ImmutableId => s as ImmutableId;

/** Три состояния ответа, а не два. §30.3, §34.4 */
export type Outcome<T> =
  | { kind: 'value'; value: T }
  | { kind: 'unknown'; reason: string } // «нет данных» — НЕ ноль
  | { kind: 'empty'; proof: string };   // «пусто» — только с доказательством

export const value = <T>(v: T): Outcome<T> => ({ kind: 'value', value: v });
export const unknownOut = (reason: string): Outcome<never> => ({ kind: 'unknown', reason });
export const emptyOut = (proof: string): Outcome<never> => ({ kind: 'empty', proof });

/** Код города: 2–4 знака, две заглавные буквы впереди, цифры только в конце. §31.1 */
export const CITY_CODE_RE = /^[A-Z]{2}([A-Z]{0,2}|[A-Z][0-9]|[0-9]{1,2})$/;
