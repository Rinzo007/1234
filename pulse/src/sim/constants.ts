/**
 * Константы модели — дословно из Дизайн-документа (Приложение A).
 * Ни одно из этих чисел не показывается игроку «как есть» (§A).
 */

import type { TaktMinutes } from './types';

/** Режимы транспорта §7.1, A.1 [T]. */
export type Mode = 'bus' | 'tram' | 'metro' | 'rail';

export interface ModeSpec {
  readonly mode: Mode;
  /** Пассажиров в единице ПС при базовой платформе §A.1 */
  readonly baseCapacity: number;
  /** Базовая длина платформы, м §10.3 */
  readonly basePlatformM: number;
  /** Прирост вместимости за прирост длины §10.3 */
  readonly capacityPerStep: number;
  readonly platformStepM: number;
  /** Скорость, м/с (в общем потоке / выделенной / внеуличный) §A.1 */
  readonly speedsMs: readonly number[];
  /** Пеший радиус остановки, м §A.1, §12.6 */
  readonly walkRadiusM: number;
  /** Минимальный интервал, мин §8.2 */
  readonly minTaktMin: TaktMinutes;
  /** Пропускная способность пути, составов/ч в каждом направлении §9.7 */
  readonly trackCapacityPerHour: number;
  /** Стоимость строительства по видам движения/уровням, целых € млн/км §7.1 */
  readonly buildCostMlnPerKm: ReadonlyMap<string, number>;
  /** Стоимость добавления остановки на построенный путь, € млн §7.1 */
  readonly stopCostMln: number;
  /** Эксплуатация: € за состав в день + € за км §12.3 */
  readonly dailyPerVehicle: number;
  readonly perKm: number;
}

export const MODES: Readonly<Record<Mode, ModeSpec>> = {
  bus: {
    mode: 'bus',
    baseCapacity: 90, basePlatformM: 12, capacityPerStep: 0, platformStepM: 0,
    speedsMs: [5.0, 6.4], // 18 / 23 км/ч
    walkRadiusM: 500, minTaktMin: 3, trackCapacityPerHour: 90,
    buildCostMlnPerKm: new Map([['mixed', 0.4], ['dedicated', 2.5]]),
    stopCostMln: 0.4, dailyPerVehicle: 250, perKm: 5.0,
  },
  tram: {
    mode: 'tram',
    baseCapacity: 260, basePlatformM: 40, capacityPerStep: 65, platformStepM: 10,
    speedsMs: [5.3, 6.9, 9.2], // 19 / 25 / 33 км/ч
    walkRadiusM: 600, minTaktMin: 3, trackCapacityPerHour: 40,
    buildCostMlnPerKm: new Map([['mixed', 9], ['dedicated', 18], ['gradeSeparation', 85]]),
    stopCostMln: 2.5, dailyPerVehicle: 936, perKm: 9.36,
  },
  metro: {
    mode: 'metro',
    baseCapacity: 800, basePlatformM: 100, capacityPerStep: 200, platformStepM: 25,
    speedsMs: [19.4], // до 70 км/ч
    walkRadiusM: 800, minTaktMin: 2, trackCapacityPerHour: 30,
    buildCostMlnPerKm: new Map([['atGrade', 32], ['elevated', 62], ['tunnel', 120]]),
    stopCostMln: 60, dailyPerVehicle: 3413, perKm: 14.93,
  },
  rail: {
    mode: 'rail',
    baseCapacity: 980, basePlatformM: 140, capacityPerStep: 245, platformStepM: 35,
    speedsMs: [16.1, 21.7], // 58 / 78 км/ч
    walkRadiusM: 1500, minTaktMin: 3, trackCapacityPerHour: 20,
    buildCostMlnPerKm: new Map([['atGrade', 22], ['elevated', 48], ['tunnel', 95]]),
    stopCostMln: 35, dailyPerVehicle: 5096, perKm: 21.56,
  },
};

/** Дискретные шаги интервала §8.2 [T]. Метро дополнительно 2 мин, если путь не общий. */
export const TAKT_STEPS: readonly TaktMinutes[] =
  [3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 40, 60, 90, 120];

/** Пять частей суток §8.2 [T]: границы в часах. */
export const PERIODS = [
  { name: 'night',   from: 4,  to: 6  },
  { name: 'amPeak',  from: 6,  to: 9  },
  { name: 'day',     from: 9,  to: 15 },
  { name: 'pmPeak',  from: 15, to: 19 },
  { name: 'evening', from: 19, to: 24 },
] as const;

export type PeriodName = (typeof PERIODS)[number]['name'];

/** Множители воспринимаемого времени §6.4 (Wardman 2026, RP для рельсового пассажира). */
export const PT_MULTIPLIERS = {
  inVehicle: 1.00,
  walk: 1.67,        // S1
  wait: 1.72,        // S9
  displacement: 0.45,
  congestedDrive: 1.33, // S25
  parkingSearch: 1.60,
  headway: 0.60,     // среднее по атрибуту Headway; SP-город 0,36
} as const;

/** Пороги заполнения графика интервала §8.3 [T]. */
export const LOAD_THRESHOLDS = { amber: 0.85, red: 1.0 } as const;

/** Параметры построения пути §6.3 [S]. */
export const ROUTING = {
  MAX_TRANSFERS: 4,
  RANGE_QUERY_WINDOW_S: 30 * 60,
  MAX_RANGE_DEPARTURES: 24,
  MAX_WALK_TO_FROM_STATION_S: 45 * 60,
  MAX_TRANSFER_WALKING_TIME_S: 10 * 60,
  MAX_DRIVE_TO_FROM_STATION_S: 7 * 60,
  ARRIVAL_GAP_S: 50,
  WALKING_SPEED_MS: 1.0,
  WALKING_SPEED_ACCURATE_PATH_MS: 1.5,
  STATION_GROUP_DISTANCE_THRESHOLD_M: 150,
} as const;

/** Доход и стоимость времени §6.4 [S]. */
export const INCOME = {
  mean: 60_000,
  stddev: 25_000,
  min: 15_000,
  max: 200_000,
  hoursWorkedPerYear: 1860,
} as const;

/** Автомобиль §6.5 [S]. */
export const CAR = {
  costPerMeterCent: 65,      // $0,65/км → центов за метр (в центах доллара)
  parkingDailyCent: 500,     // $5/день
  parkingTimeEachEndS: 180,  // 180 с у каждого конца
  minMeaningfulDistanceM: 1000,
} as const;

/** Пробка по уровням спроса §6.5 [S]. */
export const CONGESTION_BY_LEVEL: Readonly<Record<string, number>> = {
  veryLow: 0.80, low: 0.90, mediumLow: 1.00, medium: 1.25, high: 1.50,
};

/** Уровни прокладки и множители стоимости §7.2 [S]. */
export const LEVEL_MULTIPLIERS: Readonly<Record<string, number>> = {
  deepTunnel: 4.5, tunnel: 2.0, cutAndCover: 1.0, trench: 0.5,
  atGrade: 0.35, ramp: 0.5, elevated: 0.8,
};

/** Сбор за мобилизацию ТБМ: $40 млн за непрерывный участок глубокого тоннеля §7.2 [S]. */
export const TBM_FEE_MLN = 40;

/** Попы §5.3: потолок 200, минимум 1. */
export const POP_SIZE_CAP = 200;

/** Парк: ⌈оборот/интервал⌉ × 1,15 §11.1 [T]. */
export const FLEET_BUFFER = 1.15;

/** Требования города §12.6, A.9 [T]. */
export const CITY_REQUIREMENTS = {
  coverageShare: 0.60,
  homesGrantMlnRange: [30, 220],
  jobsGrantMlnRange: [30, 220],
  townGrantMlnRange: [80, 420],
  townRatioVsCar: 1.6,
  townMaxTransfers: 2,
  townMorningFromH: 6,
  townMorningToH: 9,
  townCenterRadiusM: 1500,
  minDistrictPopulation: 3000,
  minTownPopulationForGrant: 6000,
} as const;

/** Лестница премий за долю поездок §12.7, A.10 [T]. */
export const SHARE_BONUSES_PCT: readonly number[] =
  [0.5, 1.25, 2.5, 4, 6, 8, 10, 12, 14, 16, 18, 20];

/** Спутниковые премии после 4 %: 5, 10, 15 городов §12.7 [T]. */
export const SATELLITE_BONUS_STEPS = [5, 10, 15] as const;

/** Шкала промежутка между остановками §9.1 [T]. */
export function spacingWord(meters: number): string {
  if (meters < 150) return 'слишком близко';
  if (meters < 300) return 'тесно';
  if (meters <= 500) return 'хорошо';
  if (meters <= 900) return 'широко';
  return 'слишком далеко';
}

/** Радиус карточки предпросмотра — 500 м независимо от режима §9.1 [T]. */
export const PREVIEW_PEOPLE_RADIUS_M = 500;

/** Готовые шаблоны расписания §8.5 [T]. Значение 0 = off. */
export const TEMPLATES_SURFACE: Readonly<Record<string, readonly TaktMinutes[]>> = {
  'Пик':       [15, 5, 10, 5, 12],
  'Плотный':   [6, 4, 5, 4, 6],
  'Плоский 10':[10, 10, 10, 10, 10],
  'Эко':       [30, 12, 20, 12, 20],
};

export const TEMPLATES_RAIL: Readonly<Record<string, readonly TaktMinutes[]>> = {
  'Пик':         [60, 20, 40, 20, 40],
  'Получасово':  [60, 30, 30, 30, 60],
  'Часовой':     [60, 60, 60, 60, 60],
  'Редкий':      [0, 60, 120, 60, 120],
};

/** Новая линия по умолчанию §8.5 [T]. */
export const DEFAULT_TAKTS: readonly TaktMinutes[] = [15, 10, 12, 10, 15];

/** Типы станций §10.2 [S]+[Н]. */
export interface StationTypeSpec {
  readonly catchmentMultiplier: number;
  readonly transferRadiusMultiplier: number;
  readonly walkSpeedMultiplier: number;
  readonly extraDwellTimeS: number;
}

export const STATION_TYPES: Readonly<Record<string, StationTypeSpec>> = {
  standard:   { catchmentMultiplier: 1.0, transferRadiusMultiplier: 1.0, walkSpeedMultiplier: 1.0, extraDwellTimeS: 0 },
  parkRide:   { catchmentMultiplier: 5.0, transferRadiusMultiplier: 1.0, walkSpeedMultiplier: 1.0, extraDwellTimeS: 25 },
  express:    { catchmentMultiplier: 0.8, transferRadiusMultiplier: 1.0, walkSpeedMultiplier: 1.0, extraDwellTimeS: 10 },
  airport:    { catchmentMultiplier: 3.0, transferRadiusMultiplier: 2.0, walkSpeedMultiplier: 0.5, extraDwellTimeS: 45 },
  mobilityHub:{ catchmentMultiplier: 2.5, transferRadiusMultiplier: 4.0, walkSpeedMultiplier: 1.2, extraDwellTimeS: 35 },
  coachStation:{ catchmentMultiplier: 2.0, transferRadiusMultiplier: 3.0, walkSpeedMultiplier: 1.5, extraDwellTimeS: 30 },
  district:   { catchmentMultiplier: 1.5, transferRadiusMultiplier: 2.5, walkSpeedMultiplier: 1.0, extraDwellTimeS: 15 },
  legacy:     { catchmentMultiplier: 3.0, transferRadiusMultiplier: 3.0, walkSpeedMultiplier: 0.9, extraDwellTimeS: 30 },
  depot:      { catchmentMultiplier: 0.5, transferRadiusMultiplier: 1.0, walkSpeedMultiplier: 1.0, extraDwellTimeS: 0 },
};

/** Особые классы попов §5.4 [S]. */
export const POP_CLASS_RULES: Readonly<Record<string, { incomeMultiplier: number; dampening: number }>> = {
  AIR: { incomeMultiplier: 1.5, dampening: 1.0 },
  UNI: { incomeMultiplier: 0.6, dampening: 0.3 },
  GOV: { incomeMultiplier: 1.0, dampening: 0.4 },
  X:   { incomeMultiplier: 1.0, dampening: 1.0 },
};

/** Запасной расчёт автоезды для города без дорожных данных §5.3: 56 км/ч, извилистость ×1,3. */
export const FALLBACK_CAR_SPEED_KMH = 56;
export const FALLBACK_CAR_DETOUR = 1.3;

/** Стандартный тариф §12.2 [T]: €0,60 за поездку + €0,12 за км (по прямой). */
export const STANDARD_FARE_CENTS_PER_TRIP = 60;
export const STANDARD_FARE_CENTS_PER_KM = 12;

/** Дотация на пассажира: около €0,30 — рабочая линия §12.3 [T]. */
export const SUBSIDY_PER_PASSENGER_OK_CENTS = 30;
