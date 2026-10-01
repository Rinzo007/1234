/**
 * PULSE — годовой цикл игры поверх ядра симуляции (§1–§2, §9, §12, §13).
 *
 * Ключевые правила документа, реализованные здесь:
 *  - Все изменения года применяются только при «Закрыть год» (§9.1): черновик
 *    (draft) не стоит денег и не влияет на симуляцию, пока не зафиксирован.
 *  - Капитал и эксплуатация разделены (§12.1); резерв под план года учитывается
 *    при проверке доступности средств (canAfford).
 *  - Sandbox (§12.9): стоимость продолжает считаться, бюджет может уйти в минус.
 *  - Требования города (§12.6) дают гранты; доли пассажиропотока и спутники — премии (§12).
 */

import type { CityPackage, Network, Station, TransitLine, YearReport } from '../sim/model';
import { MODES } from '../sim/constants';
import type { Mode, PeriodName } from '../sim/constants';
import { id } from '../sim/types';
import type { ImmutableId } from '../sim/types';
import { sanitizeCity } from '../sim/city';
import { buildGraph, nearestStopWithinRadius, raptor } from '../sim/raptor';
import {
  canAfford, evaluateDistrictRequirement, townConnected,
  nextShareBonus, satelliteBonus, applyFarePenalty,
} from '../sim/economy';
import type { Ledger } from '../sim/economy';
import { requiredFleet, segmentPriceCents } from '../sim/network';
import { closeYear } from '../sim/year';
import type { CloseYearInput } from '../sim/year';

export const START_CAPITAL_CENTS = 500_000_000; // 5 млн условных единиц — стартовый капитал MVP [Н]

/** Черновик плана года: всё, что игрок накликает, но ещё не применил (§9.1). */
export interface YearDraft {
  newStations: Station[];               // остановки, ещё не в реестре сети
  newLines: TransitLine[];              // добавленные линии (ещё не в сети)
  taktChanges: Map<string, Partial<TransitLine['timetable']['takts']>>; // lineId → новые takts
  parkedToggles: Set<string>;           // линия ↔ припарковать/снятие
  fare: { isStandardFare: boolean; customBaseCents?: number; customPerKmCents?: number };
}

export interface GameState {
  city: CityPackage;
  network: Network;
  ledger: Ledger;
  year: number;
  draft: YearDraft;
  lastReport: YearReport | null;
}

export function newGame(city: CityPackage, sandbox = false): GameState {
  sanitizeCity(city); // инварианты §5.3 — пакет обязан пройти санитайзер до старта
  return {
    city,
    network: { stations: new Map(), lines: [] },
    ledger: { capitalCents: START_CAPITAL_CENTS, reservedForPlanCents: 0, sandbox },
    year: 1,
    draft: { newStations: [], newLines: [], taktChanges: new Map(), parkedToggles: new Set(), fare: { isStandardFare: true } },
    lastReport: null,
  };
}

/** Цена всей добавленной линии: секции × режим × уровень × priceFactor города (§7.1–7.2, §12.10)
 *  + остановки (stopCostMln за каждую новую станцию черновика, §7). */
export function draftCostCents(state: GameState): number {
  let cost = 0;
  for (const st of state.draft.newStations) {
    if (!state.network.stations.has(st.id as string)) {
      cost += Math.round(MODES[st.mode].stopCostMln * 1e8 * state.city.priceFactor);
    }
  }
  for (const line of state.draft.newLines) {
    for (const section of line.sections) {
      if (section.sharedTrackLineId !== null) continue; // shared-путь не платится (§9.6)
      cost += segmentPriceCents(line, section.lengthM, section.levelKey, state.city.priceFactor);
    }
    // MVP: подвижной состав — только эксплуатация (§12.3), капитальная часть не списывается.
  }
  return Math.round(cost);
}

/** Обновить резерв капитала под текущий черновик (§9.1: деньги заморожены до закрытия года). */
export function refreshReserve(state: GameState): void {
  state.ledger.reservedForPlanCents = draftCostCents(state);
}

/** Добавить линию в черновик. Возвращает ошибку, если не хватает денег (не sandbox). */
export function addLineToDraft(state: GameState, line: TransitLine): string | null {
  const projected = draftCostCents({
    ...state,
    draft: { ...state.draft, newLines: [...state.draft.newLines, line] },
  });
  if (!canAfford({ ...state.ledger, reservedForPlanCents: 0 }, projected)) {
    return 'Недостаточно капитала под весь план года';
  }
  state.draft.newLines.push(line);
  refreshReserve(state);
  return null;
}

/** Добавить остановку в черновик (§9.1: не влияет на сеть и деньги до закрытия года). */
export function addStationToDraft(state: GameState, st: Station): void {
  if (state.network.stations.has(st.id as string)) return; // уже в реестре — не дублируем
  if (state.draft.newStations.some((s) => s.id === st.id)) return;
  state.draft.newStations.push(st);
  refreshReserve(state);
}

/** Изменение интервалов линии — уходит в черновик года (§9.1), применяется при «Закрыть год» (§8). */
export function setDraftTakts(state: GameState, lineId: string, takts: Partial<TransitLine['timetable']['takts']>): void {
  const cur = state.draft.taktChanges.get(lineId) ?? {};
  state.draft.taktChanges.set(lineId, { ...cur, ...takts });
}

/** Эффективные интервалы линии с учётом черновика — для отображения в редакторе тактов (§8.2). */
export function effectiveTakts(state: GameState, lineId: string): Record<PeriodName, number> | null {
  const line = state.network.lines.find((l) => l.id === lineId);
  if (line) return { ...line.timetable.takts, ...(state.draft.taktChanges.get(lineId) ?? {}) };
  const draftLine = state.draft.newLines.find((l) => l.id === lineId);
  if (draftLine) return { ...draftLine.timetable.takts, ...(state.draft.taktChanges.get(lineId) ?? {}) };
  return null;
}

/** Текущий тариф плана (стандарт §12.2 или свой) — для редактора. */
export function draftFare(state: GameState): YearDraft['fare'] {
  return state.draft.fare;
}

/** Установить тариф: стандартный или свой (базовая поездка + за км, центы) (§12.2). */
export function setDraftFare(state: GameState, fare: YearDraft['fare']): void {
  state.draft.fare = fare;
}

export function toggleParked(state: GameState, lineId: string): void {
  if (state.draft.parkedToggles.has(lineId)) state.draft.parkedToggles.delete(lineId);
  else state.draft.parkedToggles.add(lineId);
}

/** Применить черновик к сети (только внутри closeGameYear!). */
function commitDraft(state: GameState): void {
  // Сначала — новые остановки в реестр сети (§10.2), затем линии.
  for (const st of state.draft.newStations) {
    if (!state.network.stations.has(st.id as string)) {
      state.network.stations.set(st.id as string, st);
    }
  }
  for (const line of state.draft.newLines) {
    for (const sid of line.stations) {
      if (!state.network.stations.has(sid as string)) {
        throw new Error(`Станция ${sid} отсутствует в реестре сети (§10.2)`);
      }
    }
    state.network.lines.push(line);
  }
  for (const [lineId, takts] of state.draft.taktChanges) {
    const line = state.network.lines.find((l) => l.id === lineId);
    if (line) line.timetable.takts = { ...line.timetable.takts, ...takts };
  }
  for (const lineId of state.draft.parkedToggles) {
    const line = state.network.lines.find((l) => l.id === lineId);
    if (line) line.parked = !line.parked;
  }
}

export interface ClosedYear extends YearReport {
  grantsPaidCents: number;
  bonusesPaidCents: number;
}

/** Закрытие года (§9.1, §13): сеть меняется, деньги списываются, приходит отчёт. */
export function closeGameYear(state: GameState): ClosedYear {
  const capex = draftCostCents(state); // считаем ДО коммита — черновик ещё не в сети
  commitDraft(state);

  const input: CloseYearInput = {
    city: state.city,
    network: state.network,
    year: state.year,
    isStandardFare: state.draft.fare.isStandardFare,
    ...(state.draft.fare.customBaseCents !== undefined ? { customFareBaseCents: state.draft.fare.customBaseCents } : {}),
    ...(state.draft.fare.customPerKmCents !== undefined ? { customFarePerKmCents: state.draft.fare.customPerKmCents } : {}),
  };
  const report = closeYear(input);

  // Эксплуатация: годовой баланс линий (365 дней) из отчёта (§12.3).
  const dailyOpexBalance = report.lineResults.reduce((s, lr) => s + lr.dailyResultCents, 0);

  // Гранты по требованиям города (§12.6): карточка на каждый район.
  let grants = 0;
  for (let i = 0; i < state.city.districts.length; i++) {
    const res = evaluateDistrictRequirement(state.city, state.network, i);
    if (res.met && res.grantCents > 0) grants += res.grantCents;
  }

  // Премии: доля поездок на ОТ (§12 SHARE_BONUSES) и связность спутников (§12.6).
  const tripsSharePct = report.totalTripsInCity > 0
    ? (report.metrics.passengersPerDay / report.totalTripsInCity) * 100
    : 0;
  let bonuses = 0;
  const shareBonus = nextShareBonus(tripsSharePct);
  if (shareBonus) bonuses += applyFarePenalty(shareBonus.bonusCents, state.draft.fare.isStandardFare);

  const graph = buildGraph(state.network, 1);
  let connectedTowns = 0;
  for (const town of state.city.towns) {
    const stop = nearestStopWithinRadius(graph, state.network, town.center.x, town.center.y);
    let transitSec: number | null = null;
    if (stop) {
      // Спутник «подключён», если есть путь хотя бы до одной точки с рабочими местами (§12.6) [Н].
      const job = state.city.demandPoints.find((p) => p.jobs > 0);
      if (job) {
        const b = nearestStopWithinRadius(graph, state.network, job.x, job.y);
        if (b) {
          const r = raptor(graph, stop.stop, b.stop, 8, stop.walkSec, b.walkSec);
          if (r) transitSec = r.inVehicleSec + r.waitSec + r.walkToSec + r.walkFromSec;
        }
      }
    }
    if (townConnected(transitSec, town.drivingSecondsToCenter)) connectedTowns++;
  }
  const satBonus = satelliteBonus(connectedTowns);
  if (satBonus) bonuses += satBonus;

  // Движение денег (§12.1): капитал − CAPEX + гранты + премии; эксплуатация — отдельный поток.
  state.ledger.capitalCents += -capex + grants + bonuses;
  if (!state.ledger.sandbox && state.ledger.capitalCents < 0) {
    // Bankruptcy-гейм-овер MVP: фиксируем ноль и предупреждаем в отчёте [Н].
    state.ledger.capitalCents = 0;
  }

  const finalReport: ClosedYear = {
    ...report,
    grantsPaidCents: grants,
    bonusesPaidCents: bonuses,
  };
  state.lastReport = finalReport;

  // Сброс черновика на следующий год (§9.1). Тариф остаётся, пока игрок его не меняет.
  state.draft = { newStations: [], newLines: [], taktChanges: new Map(), parkedToggles: new Set(), fare: state.draft.fare };
  state.ledger.reservedForPlanCents = 0;
  state.year += 1;
  void dailyOpexBalance; // MVP: операционный баланс показываем в отчёте, деньгами не двигаем [Н]
  return finalReport;
}

/** Создание линии из кликов по карте: станции уже в реестре, секции — по расстояниям (§10.1). */
export function makeLine(
  mode: Mode,
  stationIds: readonly ImmutableId[],
  network: Network,
  name: string,
  color: string,
): TransitLine {
  const stations = stationIds.map((s) => s as string);
  if (stations.length < 2) throw new Error('Линии нужно минимум две остановки');
  const sections = stations.slice(0, -1).map((from, i) => {
    const a = network.stations.get(from)!;
    const b = network.stations.get(stations[i + 1]!)!;
    return {
      fromStationId: id(from),
      toStationId: id(stations[i + 1]!),
      lengthM: Math.max(1, Math.round(Math.hypot(b.x - a.x, b.y - a.y))),
      serviceKindIndex: mode === 'metro' || mode === 'rail' ? 2 : 0, // внеуличный/выделенный (§7.1)
      levelKey: 'atGrade',
      sharedTrackLineId: findShared(network, from, stations[i + 1]!),
    };
  });
  return {
    id: id(`L_${Date.now()}_${Math.round(Math.random() * 1e6)}`),
    name, color, mode,
    stations: [...stationIds],
    sections,
    timetable: { takts: { night: 0, amPeak: 10, day: 12, pmPeak: 10, evening: 15 }, anchorMinute: 0 },
    isLoop: false, planned: true, parked: false, hidden: false,
  };
}

/** Если между теми же станциями уже есть секция другой линии — путь shared (§9.6). */
function findShared(network: Network, from: string, to: string): ImmutableId | null {
  for (const l of network.lines) {
    for (const s of l.sections) {
      const same = (s.fromStationId === from && s.toStationId === to)
        || (s.fromStationId === to && s.toStationId === from);
      if (same) return l.id;
    }
  }
  return null;
}

export { requiredFleet, MODES };
