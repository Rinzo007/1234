/**
 * T1: скимы по часовым окнам (базовый RAPTOR, раунды до 4 пересадок) §6.2–§6.3.
 *
 * Решение из §6.2: rRAPTOR НЕ используется — базовый RAPTOR, диапазон компенсируется
 * множителем интервала Headway из мета-анализа. Кэш по ключу
 * (начальная остановка, часовая полоса, версия сети) — требование §20.2/§6.2.
 */

import type { Network } from './model';
import { MODES, ROUTING, PT_MULTIPLIERS, STATION_TYPES } from './constants';
import type { PeriodName } from './constants';

export interface StopInfo {
  readonly stationId: string;
  /** Маршруты (линии), проходящие через остановку. */
  readonly routeIds: number[];
}

/** Компактное представление транзитной графы для RAPTOR. */
export interface TransitGraph {
  stops: StopInfo[];
  stopIndex: Map<string, number>;
  routes: RouteInfo[];
  /** Пешие связи между остановками в пределах порога группировки/пересадки. */
  walkEdges: Map<number, readonly { to: number; seconds: number }[]>;
  networkVersion: number;
}

export interface RouteInfo {
  lineId: string;
  mode: keyof typeof MODES;
  /** Упорядоченные индексы остановок. */
  stopSequence: number[];
  /** Множество остановок маршрута (для запрета входа на чужие остановки). */
  stopSet: Set<number>;
  /** Время в пути между соседними остановками, сек. */
  interStopSec: number[];
  /** Длительность периода в минутах по часам суток. */
  periodTaktMin: Record<PeriodName, number>;
  anchorMinute: number;
  isLoop: boolean;
}

const WALK_SPEED = ROUTING.WALKING_SPEED_MS; // 1 м/с [S]

export function buildGraph(network: Network, networkVersion: number): TransitGraph {
  const stopIndex = new Map<string, number>();
  const stops: StopInfo[] = [];
  const pos = (sid: string): number => {
    let i = stopIndex.get(sid);
    if (i === undefined) {
      i = stops.length;
      stopIndex.set(sid, i);
      stops.push({ stationId: sid, routeIds: [] });
    }
    return i;
  };

  const routes: RouteInfo[] = [];
  for (const line of network.lines) {
    if (line.parked) continue; // припаркованная линия не считается (§9.7)
    const seq = line.stations.map((s) => pos(s));
    if (line.isLoop && seq.length > 1) seq.push(seq[0]!);
    const spec = MODES[line.mode];
    // Остановки, принадлежащие этой линии (без дублей цикла) — для пересадок.
    const ownStops = new Set<number>(seq);
    const interStopSec = line.sections.map((sec) => {
      const speed = spec.speedsMs[Math.min(sec.serviceKindIndex, spec.speedsMs.length - 1)] ?? spec.speedsMs[0]!;
      return Math.max(1, Math.round(sec.lengthM / speed) + 8 /* dwell */);
    });
    const ri = routes.length;
    routes.push({
      lineId: line.id,
      mode: line.mode,
      stopSequence: seq,
      stopSet: ownStops,
      interStopSec,
      periodTaktMin: { ...line.timetable.takts },
      anchorMinute: line.timetable.anchorMinute,
      isLoop: line.isLoop,
    });
    for (const s of new Set(seq)) stops[s]!.routeIds.push(ri);
  }

  // Пешие связи: станции в радиусе пешей пересадки (§6.3 MAX_TRANSFER_WALKING_TIME).
  const walkEdges = new Map<number, { to: number; seconds: number }[]>();
  const st = (id: string) => network.stations.get(id);
  for (let i = 0; i < stops.length; i++) {
    const a = st(stops[i]!.stationId);
    if (!a) continue;
    const list: { to: number; seconds: number }[] = [];
    for (let j = 0; j < stops.length; j++) {
      if (i === j) continue;
      const b = st(stops[j]!.stationId);
      if (!b) continue;
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      const t = Math.round(d / WALK_SPEED);
      if (t <= ROUTING.MAX_TRANSFER_WALKING_TIME_S) list.push({ to: j, seconds: t });
    }
    walkEdges.set(i, list);
  }

  return { stops, stopIndex, routes, walkEdges, networkVersion };
}

/** Час → часть суток §8.2 (0–3 ночи считаем вечерним периодом предыдущих суток). */
export function periodOfHour(h: number): PeriodName {
  if (h >= 4 && h < 6) return 'night';
  if (h >= 6 && h < 9) return 'amPeak';
  if (h >= 9 && h < 15) return 'day';
  if (h >= 15 && h < 19) return 'pmPeak';
  return 'evening';
}

/** Среднее ожидание при случайном прибытии = половина интервала × множитель Headway §A.5. */
export function perceivedWaitSec(taktMin: number): number {
  return Math.round((taktMin * 60) / 2 * PT_MULTIPLIERS.headway);
}

export interface RaptorResult {
  /** Индекс цели или -1. */
  targetStop: number;
  earliestArrivalSec: number;   // от момента входа на сеть
  transfers: number;
  inVehicleSec: number;
  waitSec: number;
  walkToSec: number;
  walkFromSec: number;
  rideSecondsByLine: Map<string, number>;
  boardings: { lineId: string; stop: number }[];
}

/**
 * Базовый RAPTOR §6.2: τ_k(p) — самое раннее время прибытия не более чем на k поездках.
 * Раунд: инициализация → обход маршрутов → пешие переходы. Стоп, когда нет улучшений.
 */
export function raptor(
  graph: TransitGraph,
  originStop: number,
  destStop: number,
  hour: number,
  walkToSec: number,
  walkFromSec: number,
): RaptorResult | null {
  const K = ROUTING.MAX_TRANSFERS; // 4 [S]
  const n = graph.stops.length;
  const INF = Number.POSITIVE_INFINITY;
  const tauPrev = new Float64Array(n).fill(INF);
  const tauCur = new Float64Array(n).fill(INF);
  const parentRoute = new Int32Array(n).fill(-1);
  const parentBoardIdx = new Int32Array(n).fill(-1);   // позиция входа в stopSequence маршрута
  const parentRideSec = new Float64Array(n).fill(0);
  const parentWaitSec = new Float64Array(n).fill(0);

  const startClock = hour * 3600;
  tauPrev[originStop] = startClock;

  let bestDest = INF;

  for (let round = 1; round <= K + 1; round++) {
    // 1. Инициализация: верхняя граница из предыдущего раунда.
    for (let p = 0; p < n; p++) tauCur[p] = tauPrev[p]!;
    let anyImprovement = false;

    // 2. Обход маршрутов. Каждый маршрут просматривается не более одного раза за раунд.
    for (let r = 0; r < graph.routes.length; r++) {
      const route = graph.routes[r]!;
      const seq = route.stopSequence;
      // Вход возможен только на остановки, принадлежащие этой линии (§6.2: маршрут обслуживает свои остановки).
      let boardIdx = -1;
      for (let i = 0; i < seq.length; i++) {
        const sIdx = seq[i]!;
        if (!route.stopSet.has(sIdx)) continue;
        if (tauPrev[sIdx] !== INF) { boardIdx = i; break; }
      }
      if (boardIdx === -1 || boardIdx >= seq.length - 1) continue;

      const takt = route.periodTaktMin[periodOfHour(hour)];
      if (!takt || takt <= 0) continue; // «U4 не ходит 06–09» (§12.6 диагностика)

      const headwaySec = takt * 60;
      const anchor = route.anchorMinute * 60;
      const readyAt = tauPrev[seq[boardIdx]!]!;
      // Время ближайшего рейса после готовности (якорная минута часа, §8.7).
      const offset = ((readyAt - anchor) % headwaySec + headwaySec) % headwaySec;
      const departureBase = readyAt + (offset === 0 ? 0 : headwaySec - offset);

      let cumTravel = 0;
      for (let i = boardIdx + 1; i < seq.length; i++) {
        cumTravel += route.interStopSec[i - 1] ?? 0;
        const p = seq[i]!;
        const arrival = departureBase + cumTravel;
        if (arrival < tauCur[p]! && arrival <= bestDest) {
          tauCur[p] = arrival;
          anyImprovement = true;
          parentRoute[p] = r;
          parentBoardIdx[p] = boardIdx;
          parentRideSec[p] = cumTravel;
          parentWaitSec[p] = departureBase - readyAt;
          if (p === destStop && arrival < bestDest) bestDest = arrival;
        }
      }
    }

    // 3. Пешие переходы: τ_k(p_j) = min{τ_k(p_j), τ_k(p_i) + w(p_i,p_j)}.
    // Проходим рёбра до сходимости (множество пеших связей транзитивно, §6.2).
    let walkChanged = true;
    let guard = 0;
    while (walkChanged && guard++ < 8) {
      walkChanged = false;
      for (const [from, edges] of [...graph.walkEdges.entries()].sort((a, b) => a[0] - b[0])) {
        const base = tauCur[from]!;
        if (base === INF) continue;
        for (const e of edges) {
          const cand = base + e.seconds;
          if (cand < tauCur[e.to]! && cand <= bestDest) {
            tauCur[e.to] = cand;
            anyImprovement = true;
            walkChanged = true;
            if (e.to === destStop) bestDest = cand;
          }
        }
      }
    }

    for (let p = 0; p < n; p++) if (tauCur[p]! < tauPrev[p]!) tauPrev[p] = tauCur[p]!;
    if (!anyImprovement) break;
  }

  if (bestDest === INF) return null;

  // Восстановление поездки по родителям.
  const rideByLine = new Map<string, number>();
  const boardings: { lineId: string; stop: number }[] = [];
  let totalRide = 0, totalWait = 0;
  let cur = destStop;
  let hops = 0;
  while (cur >= 0 && cur < n && parentRoute[cur]! >= 0 && hops++ <= K + 1) {
    const r = parentRoute[cur]!;
    const route = graph.routes[r]!;
    const boardStop = route.stopSequence[parentBoardIdx[cur]!]!;
    const ride = parentRideSec[cur] ?? 0;
    const wait = parentWaitSec[cur] ?? 0;
    totalRide += ride; totalWait += wait;
    rideByLine.set(route.lineId, (rideByLine.get(route.lineId) ?? 0) + ride);
    boardings.unshift({ lineId: route.lineId, stop: boardStop });
    cur = boardStop;
  }

  return {
    targetStop: destStop,
    earliestArrivalSec: Math.round(bestDest - startClock),
    transfers: Math.max(0, boardings.length - 1),
    inVehicleSec: Math.round(totalRide),
    waitSec: Math.round(totalWait),
    walkToSec,
    walkFromSec,
    rideSecondsByLine: rideByLine,
    boardings,
  };
}

/** Ближайшая остановка режима к точке (x,y) в пределах пешего радиуса типа станции. */
export function nearestStopWithinRadius(
  graph: TransitGraph,
  network: Network,
  x: number,
  y: number,
  modesFilter?: ReadonlySet<string>,
): { stop: number; walkSec: number; distanceM: number } | null {
  let best: { stop: number; walkSec: number; distanceM: number } | null = null;
  for (let i = 0; i < graph.stops.length; i++) {
    const sid = graph.stops[i]!.stationId;
    const station = network.stations.get(sid);
    if (!station) continue;
    if (modesFilter && !modesFilter.has(station.mode)) continue;
    const spec = MODES[station.mode];
    const typeMult = catchmentMult(network, sid);
    const radius = spec.walkRadiusM * typeMult;
    const d = Math.hypot(station.x - x, station.y - y);
    if (d > radius) continue;
    if (d > ROUTING.MAX_WALK_TO_FROM_STATION_S * WALK_SPEED) continue;
    const walkSec = Math.round(d / (WALK_SPEED * walkSpeedMult(network, sid)));
    if (!best || walkSec < best.walkSec) best = { stop: i, walkSec, distanceM: d };
  }
  return best;
}

function catchmentMult(network: Network, sid: string): number {
  const s = network.stations.get(sid);
  return s ? (STATION_TYPES[s.stationType]?.catchmentMultiplier ?? 1.0) : 1.0;
}

function walkSpeedMult(network: Network, sid: string): number {
  const s = network.stations.get(sid);
  return s ? (STATION_TYPES[s.stationType]?.walkSpeedMultiplier ?? 1.0) : 1.0;
}
