// Real MCP/Chromium/HTTP integration through the isolated worker.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {RestrictedBrowser} from '../src/consumer_agent_testing_tool/data/surface_host/browser.mjs';
import {BrowserMCP} from '../src/consumer_agent_testing_tool/data/surface_host/browser_mcp.mjs';

const root = process.cwd();
const require = createRequire(path.join(root, 'tooling/payment-ui/package.json'));
const {Client, InMemoryTransport} = await import(pathToFileURL(require.resolve('@modelcontextprotocol/client')));
const {chromium} = require('playwright');
const events = [], hits = [];
let writes = 0, allow = false;
const fixture = http.createServer(async (request, response) => {
  let body = '';
  for await (const chunk of request) body += chunk;
  hits.push({path: request.url, host: request.headers.host, method: request.method,
    cookie: request.headers.cookie || '', body});
  if (request.url === '/redirect') {response.writeHead(302, {location: '/portal'}); response.end(); return;}
  if (request.url === '/forbidden-redirect') {response.writeHead(302, {location: '/private'}); response.end(); return;}
  if (request.url === '/write-redirect') {response.writeHead(307, {location: '/write'}); response.end(); return;}
  if (request.url === '/write') {writes++; response.end('saved'); return;}
  if (request.url === '/download') {
    response.writeHead(200, {'content-type': 'text/plain',
      'content-disposition': 'attachment; filename=fixture.txt'});
    response.end('isolated download'); return;
  }
  if (request.url !== '/portal') {response.writeHead(404); response.end(); return;}
  response.writeHead(200, {'content-type': 'text/html', 'set-cookie': 'fixture=synthetic; Path=/'});
  response.end(`<h1>Isolated portal</h1>
    <button onclick="fetch('/write',{method:'POST',body:'synthetic'}).catch(()=>{})">Save</button>
    <label>Fixture upload <input type=file onchange="document.querySelector('output').textContent=this.files[0].name"></label>
    <output></output><a href="/download">Download fixture</a>`);
});
await new Promise(resolve => fixture.listen(0, '127.0.0.1', resolve));
const origin = 'http://portal.test:' + fixture.address().port;
const config = {...JSON.parse(fs.readFileSync('.cache/browser-vm/connection.json', 'utf8')),
  image: JSON.parse(fs.readFileSync('.cache/browser-vm/worker-image.json', 'utf8')).image,
  destinations: {[origin]: '127.0.0.1'}};
const directory = fs.mkdtempSync(path.join(root, '.cache/browser-vm/workflow-'));
const upload = path.join(directory, 'synthetic.txt');
fs.writeFileSync(upload, 'synthetic upload');
const mcp = new BrowserMCP({createConnection: require('@playwright/mcp').createConnection,
  Client, InMemoryTransport, version: require('@playwright/mcp/package.json').version,
  playwrightVersion: require('playwright/package.json').version}, value => events.push(value));
const browser = new RestrictedBrowser(chromium, async () => {
  if (!allow) throw Error('No transaction approval');
  allow = false;
  return {authorized: true};
}, root, value => events.push(value), mcp);
const rule = (pathname, method = 'GET', effect = 'read') => ({
  id: pathname, origin, path: pathname, methods: [method], effect,
});
try {
  let snapshot = await browser.open({url: origin + '/portal', origins: [origin],
    rules: [rule('/portal'), rule('/redirect'), rule('/forbidden-redirect'), rule('/write-redirect'),
      rule('/download'), rule('/write', 'POST', 'write')],
    timeout_seconds: 10, private_uploads: {'synthetic.txt': upload}, max_file_bytes: 1024,
    download_directory: directory, worker: config});
  assert.match(snapshot.text, /Isolated portal/);
  const actual = JSON.parse(await browser.worker.run(['inspect', browser.worker.name]))[0];
  assert.equal(actual.HostConfig.NetworkMode, 'none');
  assert.equal(actual.HostConfig.Privileged, false);
  assert.equal(actual.HostConfig.ReadonlyRootfs, true);
  assert.ok(actual.Mounts.every(mount => mount.Type === 'tmpfs'));
  assert.equal(actual.HostConfig.AutoRemove, true);
  const click = async name => {
    snapshot = await browser.snapshot();
    const ref = snapshot.text.split('\n').find(line => line.includes(`"${name}"`))?.match(/\[ref=([^\]]+)\]/)?.[1];
    assert.ok(ref, snapshot.text);
    return browser.action({kind: 'click', ref, generation: snapshot.generation});
  };
  await click('Save');
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(writes, 0, 'An unapproved write reached the fixture');
  allow = true;
  await click('Save');
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(writes, 1);
  assert.equal(hits.find(hit => hit.path === '/write').cookie, 'fixture=synthetic');
  await click('Save');
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(writes, 1, 'The one-use decision was reused');
  await browser.navigate(origin + '/redirect');
  assert.equal(browser.page.url(), origin + '/portal');
  assert.match((await browser.snapshot()).text, /Isolated portal/);
  const failedNavigation = browser.page.waitForEvent('framenavigated');
  await assert.rejects(browser.navigate(origin + '/forbidden-redirect'));
  await failedNavigation;
  assert.equal(hits.some(hit => hit.path === '/private'), false);
  await browser.navigate(origin + '/portal');
  snapshot = await browser.snapshot();
  const uploadRef = snapshot.text.match(/button "Fixture upload" \[ref=([^\]]+)\]/)?.[1];
  assert.ok(uploadRef, snapshot.text);
  await browser.action({kind: 'upload', ref: uploadRef, generation: snapshot.generation, file: 'synthetic.txt'});
  assert.equal(await browser.page.locator('input').evaluate(element => element.files[0].name), 'synthetic.txt');
  snapshot = await browser.snapshot();
  const downloadRef = snapshot.text.match(/link "Download fixture" \[ref=([^\]]+)\]/)?.[1];
  await browser.action({kind: 'download', ref: downloadRef, generation: snapshot.generation});
  const download = fs.readdirSync(directory).find(name => name.endsWith('.fixture'));
  assert.ok(download, 'Download did not reach the scoped directory');
  assert.equal(fs.readFileSync(path.join(directory, download), 'utf8'), 'isolated download');
  // Deliberately remove application interception. The kernel must still deny
  // both the canary and the root-only worker transport.
  const unguarded = await browser.browser.newContext({serviceWorkers: 'allow'});
  const direct = await unguarded.newPage();
  const control = browser.worker.ready.endpoint.replace('ws:', 'http:');
  const attempts = {};
  for (const [name, url] of [['direct-fetch', origin + '/private'], ['private-control', control]]) {
    await assert.rejects(direct.goto(url, {timeout: 1500}));
    attempts[name] = 'browser-navigation-failed';
  }
  await unguarded.close();
  assert.equal(hits.some(hit => hit.path === '/private'), false);
  assert.ok(hits.every(hit => hit.host === new URL(origin).host));
  const bypasses = JSON.parse(await browser.worker.run(['exec', '--user=0', browser.worker.name,
    'node', '/opt/worker/probe.mjs']));
  assert.equal(bypasses.passed, true);
  console.log(JSON.stringify({status: 'passed', backend: mcp.info, image: config.image,
    runtime: browser.worker.runtime,
    consent: {writes, denied_or_reused: 2}, redirected_navigation: 'passed',
    upload: 'passed', download: 'passed', attempts, bypasses,
    target_requests: hits, http_broker_requests: events.filter(row => row.kind === 'browser_http').length}));
} catch (error) {
  console.error(JSON.stringify({error: error.message, events, hits}));
  throw error;
} finally {
  await browser.close();
  await new Promise(resolve => fixture.close(resolve));
}
