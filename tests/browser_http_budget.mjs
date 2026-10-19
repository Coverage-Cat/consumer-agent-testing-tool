import assert from 'node:assert/strict';
import http from 'node:http';
import {BrowserHTTP} from '../src/consumer_agent_testing_tool/data/surface_host/browser_http.mjs';

const hits = [], held = [];
const fixture = http.createServer(async (request, response) => {
  let body = '';
  for await (const chunk of request) body += chunk;
  hits.push({path: request.url, method: request.method, body});
  if (request.url === '/hold') {held.push(response); return;}
  if (request.url === '/broken') {response.destroy(); return;}
  response.end(request.url === '/six' ? '123456' : 'ok');
});
await new Promise(resolve => fixture.listen(0, '127.0.0.1', resolve));
const origin = 'http://budget.invalid:' + fixture.address().port;
const events = [], brokers = [];
const make = overrides => {
  const broker = new BrowserHTTP({origins: [origin], timeout_seconds: 2,
    rules: ['/hold', '/write', '/broken', '/six'].map(path =>
      ({origin, path, methods: ['GET', 'POST']})),
    worker: {destinations: {[origin]: '127.0.0.1'}, ...overrides}}, value => events.push(value));
  brokers.push(broker); return broker;
};
const wait = async condition => {
  const deadline = Date.now() + 1000;
  while (!condition() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 5));
  assert.ok(condition(), 'Fixture did not reach the expected state');
};
const get = (broker, path = '/write') => broker.fetch({url: origin + path, method: 'GET'});
try {
  const concurrency = make({max_concurrent_requests: 2});
  const first = get(concurrency, '/hold'), second = get(concurrency, '/hold');
  await wait(() => held.length === 2);
  const before = hits.length;
  await assert.rejects(get(concurrency), /concurrency budget/);
  assert.equal(hits.length, before, 'Concurrency denial contacted the target');
  held.splice(0).forEach(response => response.end('ok'));
  await Promise.all([first, second]);
  assert.equal((await get(concurrency)).body.toString(), 'ok');

  const count = make({max_requests: 2});
  await get(count);
  await assert.rejects(get(count, '/broken'));
  const countBefore = hits.length;
  await assert.rejects(get(count), /request-count budget/);
  assert.equal(hits.length, countBefore, 'Failed attempts were refunded or retried');
  assert.equal(count.usage().requests_admitted, 2);

  const requestBytes = make({max_total_request_bytes: 5});
  await requestBytes.fetch({url: origin + '/write', method: 'POST', body: Buffer.from('abc')});
  const bodyBefore = hits.length;
  await assert.rejects(requestBytes.fetch({
    url: origin + '/write', method: 'POST', body: Buffer.from('def')}), /request-byte budget/);
  assert.equal(hits.length, bodyBefore, 'Excess request bytes reached the target');
  assert.equal(requestBytes.usage().request_bytes_reserved, 3);

  const exact = make({max_total_response_bytes: 6});
  assert.equal((await get(exact, '/six')).body.toString(), '123456');
  const exactBefore = hits.length;
  await assert.rejects(get(exact), /response budget/);
  assert.equal(hits.length, exactBefore, 'Exhausted response budget admitted a new request');

  const responseBytes = make({max_total_response_bytes: 10});
  const pending = get(responseBytes, '/hold');
  // Attach the rejection handler before another response exhausts the budget.
  const pendingCheck = assert.rejects(pending, /response budget/);
  await wait(() => held.length === 1);
  await get(responseBytes, '/six');
  await assert.rejects(get(responseBytes, '/six'), /response budget/);
  await pendingCheck;
  assert.equal(responseBytes.active.size, 0);
  const exhaustedBefore = hits.length;
  await assert.rejects(get(responseBytes), /response budget/);
  assert.equal(hits.length, exhaustedBefore);
  assert.equal(responseBytes.usage().response_bytes_received, 12);
  held.length = 0;

  const closed = make({});
  const active = get(closed, '/hold');
  const activeCheck = assert.rejects(active, /closed/);
  await wait(() => held.length === 1);
  closed.close();
  await activeCheck;
  await assert.rejects(get(closed), /closed/);
  assert.equal(closed.active.size, 0);
  assert.ok(events.filter(event => event.enforcement === 'host-http-budget').length >= 6);
  assert.ok(events.filter(event => event.kind === 'browser_http').every(event => event.usage));
  console.log(JSON.stringify({status: 'passed', concurrency: true, request_count: true,
    request_bytes: true, cumulative_response_bytes: true, active_cancellation: true,
    close_is_terminal: true, destination_observations: hits.length}));
} finally {
  for (const broker of brokers) broker.close();
  fixture.closeAllConnections();
  await new Promise(resolve => fixture.close(resolve));
}
