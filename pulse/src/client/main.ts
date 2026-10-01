/**
 * PULSE — каркас клиента (§2 Дизайн-документа): карта + панель + годовой цикл.
 *
 * MVP: координаты карты (lon/lat) отображаются в метрическую плоскость города
 * линейным масштабом; город-песочница «Тестбург» из тестовых фикстур.
 * Настоящая подложка OpenStreetMap через MapLibre; при недоступности сети
 * используется пустая тёмная подложка (игра работает и без неё).
 */

import maplibregl from 'maplibre-gl';
import { makeTestCity, station } from '../../tests/fixtures';
import { id } from '../sim/types';
import type { Mode } from '../sim/constants';
import {
  newGame, addLineToDraft, makeLine, closeGameYear, draftCostCents,
} from './game';
import type { GameState } from './game';

// Метры города → градусы: 1° ≈ 111 320 м на экваторе. Город ~16 км → центр (0,0).
const M_PER_DEG = 111_320;
const toLonLat = (x: number, y: number): [number, number] => [x / M_PER_DEG, y / M_PER_DEG];
const toMeters = (lon: number, lat: number): { x: number; y: number } => ({
  x: Math.round(lon * M_PER_DEG),
  y: Math.round(lat * M_PER_DEG),
});

const state: GameState = newGame(makeTestCity(), false);
let tool: 'select' | 'station' | 'line' = 'select';
let mode: Mode = 'bus';
let pendingStationIds: ReturnType<typeof id>[] = [];

const map = new maplibregl.Map({
  container: 'maplibre',
  style: {
    version: 8,
    sources: {
      osm: {
        type: 'raster',
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
        tileSize: 256,
        attribution: '© OpenStreetMap contributors',
      },
    },
    layers: [{ id: 'osm', type: 'raster', source: 'osm' }],
  },
  center: toLonLat(4000, 0),
  zoom: 11,
});

map.on('style.load', () => {
  map.addSource('pulse', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
  });
  map.addLayer({
    id: 'pulse-lines', type: 'line', source: 'pulse',
    filter: ['==', '$type', 'LineString'],
    paint: { 'line-color': ['get', 'color'], 'line-width': 4 },
  });
  map.addLayer({
    id: 'pulse-stations', type: 'circle', source: 'pulse',
    filter: ['==', '$type', 'Point'],
    paint: {
      'circle-radius': 6,
      'circle-color': ['case', ['boolean', ['get', 'pending'], false], '#d29922', '#58a6ff'],
      'circle-stroke-color': '#0d1117', 'circle-stroke-width': 2,
    },
  });
  // Точки спроса города (жители/работы) — §5.1: они объясняют спрос, но не создают пассажиров.
  map.addSource('city', {
    type: 'geojson',
    data: {
      type: 'FeatureCollection',
      features: state.city.demandPoints.map((p) => ({
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: toLonLat(p.x, p.y) },
        properties: { residents: p.residents, jobs: p.jobs },
      })),
    },
  });
  map.addLayer({
    id: 'city-demand', type: 'circle', source: 'city',
    paint: {
      'circle-radius': ['match', ['get', 'kind'], '', 4, 4],
      'circle-color': ['case', ['>', ['get', 'residents'], 0], '#3fb95066', '#f8514966'],
    },
  });
  refreshGeoJson();
});

function refreshGeoJson(): void {
  const src = map.getSource('pulse') as maplibregl.GeoJSONSource | undefined;
  if (!src) return;
  const features: GeoJSON.Feature[] = [];
  const allLines = [...state.network.lines, ...state.draft.newLines];
  for (const line of allLines) {
    const coords = line.stations.map((sid) => {
      const st = state.network.stations.get(sid as string)!;
      return toLonLat(st.x, st.y);
    });
    features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: coords }, properties: { color: line.color } });
  }
  for (const st of state.network.stations.values()) {
    features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: toLonLat(st.x, st.y) }, properties: { pending: false } });
  }
  for (const sid of pendingStationIds) {
    const st = state.network.stations.get(sid as string);
    if (st) features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: toLonLat(st.x, st.y) }, properties: { pending: true } });
  }
  src.setData({ type: 'FeatureCollection', features });
}

let stationCounter = 0;
map.on('click', (e) => {
  if (tool === 'station' || tool === 'line') {
    const { x, y } = toMeters(e.lngLat.lng, e.lngLat.lat);
    const sid = id(`UI_S_${++stationCounter}`);
    const st = station(sid as string, x, `Остановка ${stationCounter}`);
    st.y = y;
    state.network.stations.set(sid as string, st);
    pendingStationIds.push(sid);
    refreshGeoJson();
    updatePanel();
  }
});

// ─────────────── Панель ───────────────

const $ = (sel: string) => document.querySelector(sel) as HTMLElement;
const fmtMoney = (cents: number): string => {
  const units = cents / 100;
  const abs = Math.abs(units);
  const s = abs >= 1e6 ? `${(units / 1e6).toFixed(2)} млн` : abs >= 1e3 ? `${(units / 1e3).toFixed(1)} тыс.` : `${units.toFixed(0)}`;
  return `${s} ${state.city.currencySymbol}`;
};

function updatePanel(): void {
  $('yearLabel').textContent = String(state.year);
  const cap = $('capital');
  cap.textContent = fmtMoney(state.ledger.capitalCents);
  cap.className = state.ledger.capitalCents < 0 ? 'neg' : 'money';
  $('reserve').textContent = fmtMoney(state.ledger.reservedForPlanCents);
  $('pax').textContent = state.lastReport ? state.lastReport.metrics.passengersPerDay.toLocaleString('ru-RU') : '—';
  $('sat').textContent = state.lastReport ? `${state.lastReport.metrics.satisfactionPct} %` : '—';
  $('draftInfo').textContent =
    `Черновик: линий ${state.draft.newLines.length}, точек выбрано ${pendingStationIds.length}, стоимость плана ${fmtMoney(draftCostCents(state))}`;
}

function log(text: string): void {
  const el = $('log');
  el.textContent = text;
}

document.querySelectorAll('#tools button').forEach((b) => {
  b.addEventListener('click', () => {
    tool = (b as HTMLElement).dataset.tool as typeof tool;
    document.querySelectorAll('#tools button').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
    if (tool !== 'line' && tool !== 'station') { /* select ничего не строит */ }
  });
});

document.querySelectorAll('#modes button').forEach((b) => {
  b.addEventListener('click', () => {
    mode = (b as HTMLElement).dataset.mode as Mode;
    document.querySelectorAll('#modes button').forEach((x) => x.classList.remove('active'));
    b.classList.add('active');
  });
});

$('undoStation').addEventListener('click', () => {
  const sid = pendingStationIds.pop();
  if (sid) {
    state.network.stations.delete(sid as string);
    refreshGeoJson();
    updatePanel();
  }
});

$('closeYear').addEventListener('click', () => {
  if (tool === 'line' && pendingStationIds.length >= 2) {
    const line = makeLine(mode, pendingStationIds, state.network, `Линия ${state.network.lines.length + state.draft.newLines.length + 1}`, '#d43a3a');
    const err = addLineToDraft(state, line);
    if (err) { log(`❌ ${err}`); return; }
    pendingStationIds = [];
  } else if (pendingStationIds.length > 0) {
    log('⚠ Выбран инструмент «Станция»: чтобы собрать линию, переключитесь на «Линия» (точки сохранены) и нажмите «Закрыть год».');
    return;
  }
  const report = closeGameYear(state);
  const linesTxt = report.lineResults
    .map((lr) => `  линия ${lr.lineId}: ${lr.passengersPerDay.toLocaleString('ru-RU')} пасс/день, пик ${lr.peakLoadPct} %, флот ${lr.requiredFleet}`)
    .join('\n');
  const refTxt = [...report.refusals.entries()]
    .filter(([, v]) => v >= 1)
    .map(([k, v]) => `  ${k}: ${Math.round(v).toLocaleString('ru-RU')}`)
    .join('\n');
  log(
    `Год ${report.year} закрыт.\n` +
    `Пассажиров/день: ${report.metrics.passengersPerDay.toLocaleString('ru-RU')} из ${report.totalTripsInCity.toLocaleString('ru-RU')}\n` +
    `Удовлетворённость: ${report.metrics.satisfactionPct} %\n` +
    `Гранты: ${fmtMoney(report.grantsPaidCents)}, премии: ${fmtMoney(report.bonusesPaidCents)}\n` +
    `Причины потерь поездок:\n${refTxt || '  —'}\n` +
    `Линии:\n${linesTxt || '  —'}`,
  );
  refreshGeoJson();
  updatePanel();
});

updatePanel();
