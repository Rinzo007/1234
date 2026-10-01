import { it } from 'vitest';
import { closeYear } from '../src/sim/year';
import { makeTestCity, fullCoverageNetwork } from './fixtures';

it('dbg year', () => {
  const city = makeTestCity();
  const net = fullCoverageNetwork();
  const rep = closeYear({ city, network: net, year: 1, isStandardFare: true });
  console.log('DBG3', JSON.stringify({ ps: process.env.PEAK_SHARE, pax: rep.metrics.passengersPerDay, serve: rep.tripsNetworkCanServe, refusals: [...rep.refusals.entries()] }));
});
