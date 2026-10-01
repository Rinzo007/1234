import { it } from 'vitest';
import { buildGraph, nearestStopWithinRadius, raptor } from '../src/sim/raptor';
import { makeTestCity, fullCoverageNetwork } from './fixtures';

it('dbg pax', () => {
  const city = makeTestCity();
  const net = fullCoverageNetwork();
  const graph = buildGraph(net, 1);
  const res = city.demandPoints[0]!, job = city.demandPoints[5]!;
  const a = nearestStopWithinRadius(graph, net, res.x, res.y);
  const b = nearestStopWithinRadius(graph, net, job.x, job.y);
  console.log('a', a, 'b', b);
  if (a && b) console.log('r', raptor(graph, a.stop, b.stop, 8, a.walkSec, b.walkSec));
});
