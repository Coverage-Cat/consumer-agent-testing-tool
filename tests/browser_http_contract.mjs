import assert from 'node:assert/strict';
import http from 'node:http';
import {BrowserHTTP} from '../src/consumer_agent_testing_tool/data/surface_host/browser_http.mjs';
let redirects = 0, headers;
const server = http.createServer(async (request, response) => {
  if (request.url === '/hang') return;
  if (request.url === '/redirect') {
    response.writeHead(302, {location: '/canary'}); response.end(); return;
  }
  if (request.url === '/canary') redirects++;
  if (request.url === '/large') {response.end('x'.repeat(1025)); return;}
  headers = request.headers;
  let body = '';
  for await (const chunk of request) body += chunk;
  response.setHeader('set-cookie', ['a=synthetic; Path=/', 'b=synthetic; Path=/']);
  response.end(body || 'pinned');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://unresolvable.invalid:' + server.address().port;
const policy = {origins: [origin], timeout_seconds: 0.1, worker: {
  destinations: {[origin]: '127.0.0.1'}, max_request_bytes: 1024, max_response_bytes: 1024,
}, rules: ['/ok', '/redirect', '/canary', '/large', '/hang'].map(path =>
  ({origin, path, methods: ['GET', 'POST'], effect: 'read'}))};
const broker = new BrowserHTTP(policy);
try {
  const response = await broker.fetch({url: origin + '/ok', method: 'POST',
    headers: {host: 'forbidden.invalid', 'proxy-authorization': 'must-not-forward',
      cookie: 'fixture=synthetic', 'content-length': '99999'}, body: Buffer.from('synthetic')});
  assert.equal(response.body.toString(), 'synthetic');
  assert.equal(headers.host, new URL(origin).host);
  assert.equal(headers.cookie, 'fixture=synthetic');
  assert.equal(headers['proxy-authorization'], undefined);
  assert.equal(headers['content-length'], '9');
  assert.equal(response.headers['set-cookie'], 'a=synthetic; Path=/\nb=synthetic; Path=/');
  assert.equal((await broker.fetch({url: origin + '/redirect', method: 'GET'})).status, 302);
  assert.equal(redirects, 0, 'HTTP client followed a redirect before policy review');
  for (const [url, method] of [
    [origin + '/undeclared', 'GET'], [origin + '/ok?changed=true', 'GET'],
    [origin + '/ok', 'DELETE'], [origin.replace('unresolvable.invalid', '127.0.0.1') + '/ok', 'GET'],
    [origin.replace('http://', 'http://user:pass@') + '/ok', 'GET'],
  ]) await assert.rejects(broker.fetch({url, method}), /authorized/);
  await assert.rejects(broker.fetch({url: origin + '/ok', method: 'POST',
    body: Buffer.alloc(1025)}), /request exceeds/);
  await assert.rejects(broker.fetch({url: origin + '/large', method: 'GET'}), /response exceeds/);
  await assert.rejects(broker.fetch({url: origin + '/hang', method: 'GET'}), /deadline/);
  assert.throws(() => new BrowserHTTP({...policy, worker: {...policy.worker,
    destinations: {[origin]: 'localhost'}}}), /literal IP/);
  assert.throws(() => new BrowserHTTP({...policy, worker: {...policy.worker,
    destinations: {[origin]: '127.0.0.1', 'http://extra.invalid': '127.0.0.1'}}}), /outside/);
  console.log(JSON.stringify({status: 'passed', pinned_destination: true,
    redirects_not_followed: true, bounded_requests: true, bounded_responses: true, deadline: true}));
} finally {
  broker.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
