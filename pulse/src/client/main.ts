/**
 * PULSE — каркас клиента (§2 Дизайн-документа): карта + панель + годовой цикл.
 *
 * Координаты карты (lon/lat) ↔ метрическая плоскость города через проекцию
 * Web Mercator (src/client/geo.ts), а не линейным масштабом. Город по умолчанию
 * — песочница «Тестбург»; реальные города грузятся из Overture Maps ленивым
 * импортом (src/client/overture.ts + DuckDB-WASM) — в стартовый бандл не входят.
 * MapLibre также грузится динамическим импортом (code-splitting).
 * Настоящая подложка OpenStreetMap; при недоступности сети используется пустая
 * тёмная подложка (игра работает и без неё).
 */

import { makeTestCity, station } from '../../tests/fixtures';
import { id } from '../sim/types';
import type { Mode, PeriodName } from '../sim/constants';
import { TAKT_STEPS, TEMPLATES_RAIL, TEMPLATES_SURFACE, STANDARD_FARE_CENTS_PER_KM, STANDARD_FARE_CENTS_PER_TRIP } from '../sim/constants';
import { REFUSAL_LABELS } from '../sim/model';
import type { GeoAnchor } from './geo';
import { toLonLat as geoToLonLat, toMeters as geoToMeters } from './geo';
import {
  newGame, addLineToDraft, addStationToDraft, makeLine, closeGameYear, draftCostCents, refreshReserve,
  setDraftTakts, effectiveTakts, setDraftFare,
} from './game';
import type { GameState } from './game';

/** Тип maplibre — только на уровне типов: сам модуль приходит dynamic-import'ом. */
type MLMap = import('maplibre-gl').Map;
type MLGeoJSONSource = import('maplibre-gl').GeoJSONSource;

/** Сохранение/загрузка игры в localStorage: сериализуем Map сети и Set/Map черновика. */
const SAVE_KEY = 'pulse-save-v1';

function saveGame(s: GameState): void {
  const replacer = (_k: string, v: unknown) =>
    v instanceof Map ? { __m: [...v.entries()] }
    : v instanceof Set ? { __s: [...v.values()] }
    : v;
  localStorage.setItem(SAVE_KEY, JSON.stringify(s, replacer));
}

function loadGame(): GameState | null {
  const raw = localStorage.getItem(SAVE_KEY);
  if (!raw) return null;
  try {
    const revived = JSON.parse(raw, (_k, v) =>
      v && typeof v === 'object' && '__m' in v ? new Map(v.__m as [string, never][])
      : v && typeof v === 'object' && '__s' in v ? new Set(v.__s as string[])
      : v) as GameState;
    // Простейшая проверка структуры — иначе стартуем новую игру.
    if (typeof revived.year !== 'number' || !revived.city || !revived.network?.stations) return null;
    return revived;
  } catch {
    return null;
  }
}

// Якорь города: метры локальной плоскости (§5.1) ↔ lon/lat через Web Mercator (geo.ts).
// Песочница «Тестбург» условно размещена в Берлине; реальные города получают якорь при загрузке.
let anchor: GeoAnchor = { lon0: 13.4, lat0: 52.5 };
const toLonLat = (x: number, y: number): [number, number] => geoToLonLat(anchor, x, y);
const toMeters = (lon: number, lat: number): { x: number; y: number } => geoToMeters(anchor, lon, lat);

const state: GameState = loadGame() ?? newGame(makeTestCity(), false);
let tool: 'select' | 'station' | 'line' = 'select';
let mode: Mode = 'bus';
let pendingStationIds: ReturnType<typeof id>[] = [];
let map: MLMap | null = null;

/** Пересчёт якоря и перерисовка карты после смены города. */
function recenter(): void {
  if (!map) return;
  map.setCenter(geoToLonLat(anchor, 4000, 0));
  refreshGeoJson();
  refreshDemandLayer();
}

async function initMap(): Promise<void> {
  // Code-splitting: maplibre (~800 КБ gzip) — отдельный чанк, грузится асинхронно.
  const maplibregl = (await import('maplibre-gl')).default;
  // CSS берётся из <link> в index.html (CDN); при сборке Vite вынесет его отдельно.
  map = new maplibregl.Map({
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
    if (!map) return;
    map.addSource('pulse', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
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
    map.addSource('city', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    map.addLayer({
      id: 'city-demand', type: 'circle', source: 'city',
      paint: {
        'circle-radius': 4,
        'circle-color': ['case', ['>', ['get', 'residents'], 0], '#3fb95066', '#f8514966'],
      },
    });
    refreshDemandLayer();
    refreshGeoJson();
  });

  map.on('click', onMapClick);
}

function refreshDemandLayer(): void {
  if (!map) return;
  const src = map.getSource('city') as MLGeoJSONSource | undefined;
  if (!src) return;
  src.setData({
    type: 'FeatureCollection',
    features: state.city.demandPoints.map((p) => ({
      type: 'Feature' as const,
      geometry: { type: 'Point' as const, coordinates: toLonLat(p.x, p.y) },
      properties: { residents: p.residents, jobs: p.jobs },
    })),
  });
}

function refreshGeoJson(): void {
  if (!map) return;
  const src = map.getSource('pulse') as MLGeoJSONSource | undefined;
  if (!src) return;
  const features: GeoJSON.Feature[] = [];
  const draftStationIds = new Set(state.draft.newStations.map((s) => s.id as string));
  const allLines = [...state.network.lines, ...state.draft.newLines];
  for (const line of allLines) {
    const coords = line.stations.map((sid) => {
      // Линии черновика ссылаются на станции, которых ещё нет в реестре сети (§9.1).
      const st = state.network.stations.get(sid as string)
        ?? state.draft.newStations.find((d) => (d.id as string) === (sid as string));
      if (!st) return null;
      return toLonLat(st.x, st.y);
    }).filter((c): c is [number, number] => c !== null);
    if (coords.length >= 2) {
      features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: coords }, properties: { color: line.color } });
    }
  }
  for (const st of state.network.stations.values()) {
    features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: toLonLat(st.x, st.y) }, properties: { pending: false } });
  }
  for (const st of state.draft.newStations) {
    features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: toLonLat(st.x, st.y) }, properties: { pending: true } });
  }
  for (const sid of pendingStationIds) {
    if (draftStationIds.has(sid as string)) continue;
    const st = state.network.stations.get(sid as string);
    if (st) features.push({ type: 'Feature', geometry: { type: 'Point', coordinates: toLonLat(st.x, st.y) }, properties: { pending: true } });
  }
  src.setData({ type: 'FeatureCollection', features });
}

let stationCounter = 0;
function onMapClick(e: { lngLat: { lng: number; lat: number } }): void {
  if (tool === 'station' || tool === 'line') {
    const { x, y } = toMeters(e.lngLat.lng, e.lngLat.lat);
    const sid = id(`UI_S_${++stationCounter}`);
    const st = station(sid as string, x, `Остановка ${stationCounter}`);
    st.y = y;
    if (tool === 'station') {
      // Инструмент «Станция»: остановка уходит в черновик года (§9.1) —
      // в сеть и за деньги она попадёт только при «Закрыть год».
      addStationToDraft(state, st);
    } else {
      // Инструмент «Линия»: точки временные, пока линия не собрана из них.
      state.network.stations.set(sid as string, st);
      pendingStationIds.push(sid);
    }
    refreshGeoJson();
    updatePanel();
  }
}

void initMap();

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
    `Черновик: линий ${state.draft.newLines.length}, станций ${state.draft.newStations.length}, точек выбрано ${pendingStationIds.length}, стоимость плана ${fmtMoney(draftCostCents(state))}`;
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
  // Отмена «линейной» временной точки.
  const sid = pendingStationIds.pop();
  if (sid) {
    state.network.stations.delete(sid as string);
    refreshGeoJson();
    updatePanel();
    return;
  }
  // Отмена последней остановки, добавленной инструментом «Станция» в черновик (§9.1).
  const last = state.draft.newStations.pop();
  if (last) {
    state.network.stations.delete(last.id as string);
    refreshReserve(state);
    refreshGeoJson();
    updatePanel();
  }
});

$('closeYear').addEventListener('click', () => {
  if (tool === 'line' && pendingStationIds.length >= 2) {
    const line = makeLine(mode, pendingStationIds, state.network, `Линия ${state.network.lines.length + state.draft.newLines.length + 1}`, '#d43a3a');
    // Станции «линейных» кликов тоже должны попасть в сеть при закрытии года (§9.1).
    for (const sid of pendingStationIds) {
      const st = state.network.stations.get(sid as string);
      if (st) addStationToDraft(state, st);
    }
    const err = addLineToDraft(state, line);
    if (err) { log(`❌ ${err}`); return; }
    pendingStationIds = [];
  } else if (pendingStationIds.length > 0) {
    log('⚠ Выбран инструмент «Станция»: чтобы собрать линию, переключитесь на «Линия» (точки сохранены) и нажмите «Закрыть год».');
    return;
  }
  const report = closeGameYear(state);
  saveGame(state);
  const linesTxt = report.lineResults
    .map((lr) => `  линия ${lr.lineId}: ${lr.passengersPerDay.toLocaleString('ru-RU')} пасс/день, пик ${lr.peakLoadPct} %, флот ${lr.requiredFleet}`)
    .join('\n');
  const refTxt = [...report.refusals.entries()]
    .filter(([, v]) => v >= 1)
    .map(([k, v]) => `  ${REFUSAL_LABELS[k] ?? k}: ${Math.round(v).toLocaleString('ru-RU')}`)
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
  renderTaktEditor();
});

// ─────────────── Редактор тактов и тарифа (§8, §12.2) ───────────────
// Все правки уходят в черновик года и применяются только при «Закрыть год» (§9.1).

const PERIOD_LABELS: Record<PeriodName, string> = {
  night: 'Ночь', amPeak: 'Утро-пик', day: 'День', pmPeak: 'Вечер-пик', evening: 'Вечер',
};

function allEditableLines(): { id: string; name: string; mode: Mode }[] {
  return [...state.network.lines, ...state.draft.newLines].map((l) => ({ id: l.id as string, name: l.name, mode: l.mode }));
}

function renderTaktEditor(): void {
  const root = $('taktEditor');
  root.textContent = '';
  const lines = allEditableLines();
  if (lines.length === 0) {
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'Линий пока нет — постройте первую и закройте год.';
    root.appendChild(hint);
    return;
  }
  for (const line of lines) {
    const box = document.createElement('div');
    box.className = 'takt-line';
    const title = document.createElement('div');
    title.className = 'stat';
    title.textContent = `${line.name} (${line.mode})`;
    box.appendChild(title);

    const takts = effectiveTakts(state, line.id) ?? ({} as Record<PeriodName, number>);
    for (const period of Object.keys(PERIOD_LABELS) as PeriodName[]) {
      const row = document.createElement('label');
      row.className = 'takt-row';
      const lbl = document.createElement('span');
      lbl.textContent = PERIOD_LABELS[period];
      const sel = document.createElement('select');
      // Дискретные шаги интервала §8.2 + 0 = «выключено».
      const steps: number[] = [0, ...TAKT_STEPS];
      for (const t of steps) {
        const opt = document.createElement('option');
        opt.value = String(t);
        opt.textContent = t === 0 ? '— выкл —' : `${t} мин`;
        if (takts[period] === t) opt.selected = true;
        sel.appendChild(opt);
      }
      sel.addEventListener('change', () => {
        setDraftTakts(state, line.id, { [period]: Number(sel.value) } as Partial<Record<PeriodName, number>>);
        updatePanel();
      });
      row.appendChild(lbl);
      row.appendChild(sel);
      box.appendChild(row);
    }

    // Шаблоны расписания §8.5: наземные/рельсовые наборам.
    const tplRow = document.createElement('div');
    tplRow.className = 'row';
    const templates = line.mode === 'metro' || line.mode === 'rail' ? TEMPLATES_RAIL : TEMPLATES_SURFACE;
    for (const [name, vals] of Object.entries(templates)) {
      const btn = document.createElement('button');
      btn.textContent = name;
      btn.addEventListener('click', () => {
        const periods = Object.keys(PERIOD_LABELS) as PeriodName[];
        const patch: Partial<Record<PeriodName, number>> = {};
        periods.forEach((p, i) => { patch[p] = vals[i] ?? 0; });
        setDraftTakts(state, line.id, patch);
        renderTaktEditor();
        updatePanel();
      });
      tplRow.appendChild(btn);
    }
    box.appendChild(tplRow);
    root.appendChild(box);
  }

  // Тариф (§12.2): стандартный или свой; применяется с закрытием года.
  const fare = state.draft.fare;
  const fareBox = document.createElement('div');
  fareBox.className = 'takt-line';
  const fareTitle = document.createElement('div');
  fareTitle.className = 'stat';
  fareTitle.textContent = 'Тариф (§12.2)';
  fareBox.appendChild(fareTitle);

  const stdLabel = document.createElement('label');
  stdLabel.className = 'takt-row';
  const stdRadio = document.createElement('input');
  stdRadio.type = 'radio';
  stdRadio.name = 'fareMode';
  stdRadio.checked = fare.isStandardFare;
  stdRadio.addEventListener('change', () => {
    if (stdRadio.checked) setDraftFare(state, { isStandardFare: true });
    updatePanel();
  });
  const stdText = document.createElement('span');
  stdText.textContent = `Стандартный: ${(STANDARD_FARE_CENTS_PER_TRIP / 100).toFixed(2)} + ${(STANDARD_FARE_CENTS_PER_KM / 100).toFixed(2)} за км`;
  stdLabel.append(stdRadio, stdText);
  fareBox.appendChild(stdLabel);

  const custRadio = document.createElement('input');
  custRadio.type = 'radio';
  custRadio.name = 'fareMode';
  custRadio.checked = !fare.isStandardFare;
  const baseInput = document.createElement('input');
  baseInput.type = 'number';
  baseInput.min = '0';
  baseInput.step = '5';
  baseInput.value = String((fare.customBaseCents ?? STANDARD_FARE_CENTS_PER_TRIP) / 100);
  baseInput.style.width = '70px';
  const perKmInput = document.createElement('input');
  perKmInput.type = 'number';
  perKmInput.min = '0';
  perKmInput.step = '5';
  perKmInput.value = String((fare.customPerKmCents ?? STANDARD_FARE_CENTS_PER_KM) / 100);
  perKmInput.style.width = '70px';
  const applyCustom = (): void => {
    setDraftFare(state, {
      isStandardFare: false,
      customBaseCents: Math.round(Number(baseInput.value) * 100),
      customPerKmCents: Math.round(Number(perKmInput.value) * 100),
    });
    updatePanel();
  };
  custRadio.addEventListener('change', () => { if (custRadio.checked) applyCustom(); });
  baseInput.addEventListener('change', applyCustom);
  perKmInput.addEventListener('change', applyCustom);
  const custLabel = document.createElement('label');
  custLabel.className = 'takt-row';
  custLabel.append(custRadio, document.createTextNode(' Свой: '), baseInput, document.createTextNode(' + '), perKmInput, document.createTextNode(' за км'));
  fareBox.appendChild(custLabel);
  root.appendChild(fareBox);
}

renderTaktEditor();

// ─────────────── Загрузка реального города: Overture Maps (§17) ───────────────

$('loadCity').addEventListener('click', async () => {
  const query = ($('cityQuery') as HTMLInputElement).value.trim() || 'Berlin';
  log(`Ищу «${query}» в Nominatim…`);
  try {
    const geo = await fetch(
      `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(query)}`,
      { headers: { Accept: 'application/json' } },
    ).then((r) => r.json() as Promise<{ lat: string; lon: string; display_name: string }[]>);
    if (!geo.length) throw new Error('Место не найдено');
    const place = geo[0]!;
    const cityName = place.display_name.split(',')[0] ?? query;
    anchor = { lon0: Number(place.lon), lat0: Number(place.lat) };

    // Ленивая загрузка модуля Overture (DuckDB-WASM ~5 МБ) — вне стартового бандла.
    const { loadOvertureCity, boxAround } = await import('./overture');
    log('Загружаю места из Overture Maps (первый раз качает DuckDB-WASM)…');
    const pkg = await loadOvertureCity(boxAround(anchor.lon0, anchor.lat0), { lon: anchor.lon0, lat: anchor.lat0, name: cityName },
      (p) => log(`${p.step} ${p.pct} %`));

    // Новый город = новая игра: сеть игрока к чужому спросу неприменима.
    const fresh = newGame(pkg, state.ledger.sandbox);
    Object.assign(state, fresh);
    pendingStationIds = [];
    saveGame(state);
    log(`Город «${pkg.name}» загружен: попы ${pkg.pops.length}, точек спроса ${pkg.demandPoints.length}. Стройте сеть!`);
    recenter();
    updatePanel();
    renderTaktEditor();
  } catch (e) {
    log(`❌ ${e instanceof Error ? e.message : String(e)}`);
  }
});

updatePanel();
