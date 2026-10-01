/**
 * PULSE — геодезия клиента: метрическая плоскость города (§5.1) ↔ координаты карты.
 *
 * Вместо линейного маппинга «метры → градусы» используется настоящая проекция
 * Web Mercator (EPSG:3857), та же, что у MapLibre: метры города интерпретируются
 * как смещения в метрах EPSG:3857 от якорной точки города (lon0, lat0). Это даёт
 * корректный масштаб и направление на карте для любой широты реального города.
 */

const EARTH_R = 6_378_137; // радиус WGS84, м
const DEG = Math.PI / 180;

export interface GeoAnchor { readonly lon0: number; readonly lat0: number }

/** Метры локальной плоскости города (x — восток, y — север) → [lon, lat]. */
export function toLonLat(a: GeoAnchor, x: number, y: number): [number, number] {
  const lat0rad = a.lat0 * DEG;
  // Широта меняется нелинейно по Mercator: сначала переводим метры в координату EPSG:3857.
  const mercY0 = EARTH_R * Math.log(Math.tan(Math.PI / 4 + lat0rad / 2));
  const lon = a.lon0 + (x / (EARTH_R * Math.cos(lat0rad))) / DEG;
  const mercY = mercY0 + y;
  const lat = (2 * Math.atan(Math.exp(mercY / EARTH_R)) - Math.PI / 2) / DEG;
  return [lon, lat];
}

/** [lon, lat] → метры локальной плоскости города (x — восток, y — север). */
export function toMeters(a: GeoAnchor, lon: number, lat: number): { x: number; y: number } {
  const lat0rad = a.lat0 * DEG;
  const mercY0 = EARTH_R * Math.log(Math.tan(Math.PI / 4 + lat0rad / 2));
  const mercY = EARTH_R * Math.log(Math.tan(Math.PI / 4 + (lat * DEG) / 2));
  return {
    x: Math.round((lon - a.lon0) * DEG * EARTH_R * Math.cos(lat0rad)),
    y: Math.round(mercY - mercY0),
  };
}
