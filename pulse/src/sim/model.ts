/**
 * Модель данных: пакет города и сеть игрока. §31 Дизайн-документа.
 * Идентификаторы — строки, время — целые секунды от полуночи, деньги — целые центы.
 */

import type { ImmutableId, MeasuredSecond } from './types';
import type { Mode, PeriodName } from './constants';

// ────────────────────────────── Город (§31.2) ──────────────────────────────

export interface DemandPoint {
  readonly id: ImmutableId;
  /** x/y в метрах локальной плоской проекции города. */
  readonly x: number;
  readonly y: number;
  /** Жители и рабочие места. Это значения для отображения — они НЕ создают пассажиров (§5.2). */
  readonly residents: number;
  readonly jobs: number;
  /** Ссылки на попы, привязанные к этой точке (§5.3 инвариант: каждый поп перечислен дважды). */
  popIds: ImmutableId[]; // заполняется при построении пакета, затем санитайзер проверяет инварианты §5.3
}

export interface Pop {
  readonly id: ImmutableId;
  /** Сколько человек. Потолок 200, медиана ≈ 14 (§5.3). В UI не показывается. */
  readonly size: number;
  readonly residenceId: ImmutableId;
  readonly jobId: ImmutableId;
  /** Время автоезды посчитано офлайн по дорожному графу (§5.3). */
  readonly drivingSeconds: MeasuredSecond;
  readonly drivingDistanceM: number;
  /** Доход $/год; влияет на стоимость времени (§6.4). */
  readonly income: number;
  /** Сглаживание пиков для особых групп (§5.4). */
  readonly dampening: number;
}

export interface District {
  readonly id: ImmutableId;
  readonly name: string;
  readonly kind: 'homes' | 'jobs';
  readonly population: number;
  readonly polygon: readonly (readonly [number, number])[];
}

export interface SatelliteTown {
  readonly id: ImmutableId;
  readonly name: string;
  readonly center: { x: number; y: number };
  readonly population: number;
  /** Время автоезды до центра города, посчитано офлайн. */
  readonly drivingSecondsToCenter: MeasuredSecond;
}

export interface CityPackage {
  readonly schemaVersion: number;
  readonly code: string;          // ^[A-Z]{2}... §31.1
  readonly id: ImmutableId;       // com.pulse.city.<slug>, неизменяемый
  readonly name: string;
  readonly currencySymbol: string;
  /** Коэффициент строительных цен (§12.10: Берлин 1.0, Нью-Йорк ×2,3). */
  readonly priceFactor: number;
  readonly demandPoints: readonly DemandPoint[];
  readonly pops: readonly Pop[];
  readonly districts: readonly District[];
  readonly towns: readonly SatelliteTown[];
  /** Окна спроса модифицируемы: не зашиваем час→спрос (§11.3) [S]. */
  readonly demandLevelsByHour: readonly string[]; // 24 значения veryLow..high
}

// ────────────────────────────── Сеть (§10.1) ──────────────────────────────

/** Станция — не точка, а объект с характеристиками (§10.2). */
export interface Station {
  readonly id: ImmutableId;
  name: string;
  x: number;
  y: number;
  mode: Mode;
  /** Тип станции: catchmentMultiplier и др. (§10.2). */
  stationType: string;
  /** Длина платформы, м. Самая короткая платформа линии определяет состав (§10.3). */
  platformLengthM: number;
}

/** Секция между соседними остановками; у каждой свой уровень и режим (§10.1). */
export interface Section {
  readonly fromStationId: ImmutableId;
  readonly toStationId: ImmutableId;
  lengthM: number;
  /** Индекс массива speedsMs режима: общий поток / выделенный / внеуличный (§7.1). */
  serviceKindIndex: number;
  /** Множитель стоимости уровня прокладки (§7.2). */
  levelKey: string;
  /** Путь shared с другой линией: не платится и не увеличивает пропускную способность (§9.6). */
  sharedTrackLineId: ImmutableId | null;
}

/** Расписание: интервал по пяти частям суток + якорная минута (§8, §11). */
export interface LineTimetable {
  takts: Record<PeriodName, number>; // значение из TAKT_STEPS, 0 = off
  /** Якорная минута часа первого отправления (§8.7). */
  anchorMinute: number;
}

export interface TransitLine {
  readonly id: ImmutableId;
  name: string;
  color: string;
  mode: Mode;
  stations: ImmutableId[];        // упорядоченный список остановок
  sections: Section[];            // sections[i] между stations[i] и stations[i+1]
  timetable: LineTimetable;
  isLoop: boolean;                // кольцо — отдельный случай (§9.4)
  /** Включена ли линия в план года (§10.1). */
  planned: boolean;
  /** Припаркованная линия не считается в пропускной способности (§9.7). */
  parked: boolean;
  /** Скрытая линия продолжает считаться и платить (§9.7, §9.8). */
  hidden: boolean;
}

export interface Network {
  stations: Map<string, Station>;
  lines: TransitLine[];
}

// ─────────────────────────── План года (§9.1) ───────────────────────────

export interface YearPlan {
  /** Все изменения применяются только при закрытии года; черновик имеет нулевую цену. */
  addedLineIds: ImmutableId[];
  note?: string;
}

// ─────────────────────────── Результат года (§13) ───────────────────────────

export type RefusalReason =
  | 'noPath' | 'farFromStop' | 'carWins' | 'inVehicleTime'
  | 'waiting' | 'transfer' | 'fare' | 'overcrowding';

export const REFUSAL_LABELS: Readonly<Record<RefusalReason, string>> = {
  noPath: 'Нет пути',
  farFromStop: 'Далеко до остановки',
  carWins: 'Дорога не берёт',
  inVehicleTime: 'Время в пути',
  waiting: 'Ожидание',
  transfer: 'Пересадка',
  fare: 'Тариф',
  overcrowding: 'Переполнение',
};

export interface TopMetrics {
  passengersPerDay: number;
  transfersPerDay: number;
  walkCoverageShare: number;      // доля поездок города с началом или концом в охвате
  satisfactionPct: number;
  dailyBalanceCents: number;      // баланс дня или дотация в день
}

export interface YearReport {
  year: number;
  metrics: TopMetrics;
  /** Причины отказа с делением потери пропорционально вкладу (§6.6) [T]. */
  refusals: ReadonlyMap<RefusalReason, number>;
  totalTripsInCity: number;
  tripsNetworkCanServe: number;
  tripsServedWell: number;
  lineResults: readonly LineResult[];
  grantsPaidCents: number;
  bonusesPaidCents: number;
}

export interface LineResult {
  lineId: ImmutableId;
  passengersPerDay: number;
  lengthM: number;
  passengerKmPerDay: number;
  peakLoadPct: number;
  requiredFleet: number;
  fareboxRatio: number;
  subsidyPerPassengerCents: number;
  dailyResultCents: number;
  /** Заполненность по секциям для графика интервала (§8.3, §8.4). */
  sectionLoads: readonly number[];
}
