/** Смоук-тест конвейера загрузки города: DuckDB + httpfs читает places Overture по bbox. */
import { DuckDBInstance } from '@duckdb/node-api';

const URL_GLOB = 'https://overturemapswestus2.blob.core.windows.net/release/2026-09-23.1/theme=places/type=place/*.parquet';
// Таллинн, ~20×20 км
const lonMin = 24.65, latMin = 59.35, lonMax = 24.85, latMax = 59.53;

const db = await DuckDBInstance.create(':memory:');
const conn = await db.connect();
await conn.run(`INSTALL httpfs; LOAD httpfs;`);
const q = `
  SELECT id, categories.primary AS category,
         names[1].value AS name,
         x AS lon, y AS lat, confidence
  FROM read_parquet('${URL_GLOB}')
  WHERE x BETWEEN ${lonMin} AND ${lonMax} AND y BETWEEN ${latMin} AND ${latMax}
    AND categories.primary IN ('city','suburb','residential','commerce','office','education','health','government','industrial')
  LIMIT 20`;
const t0 = Date.now();
const rows = await conn.runAndReadAll(q);
console.log('elapsed ms:', Date.now() - t0);
console.log(rows.getRowObjects().slice(0, 10));
