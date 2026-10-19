// Browser-originated bypass probes. No policy hooks are relied on for denial.
// A deliberate firewall bypass first proves which detectors see actual traffic.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {ContainerBrowser} from '../src/consumer_agent_testing_tool/data/surface_host/browser_worker.mjs';
const root = process.cwd(), require = createRequire(path.join(root, 'tooling/payment-ui/package.json'));
const worker = new ContainerBrowser(root, {
  ...JSON.parse(fs.readFileSync('.cache/browser-vm/connection.json', 'utf8')),
  image: JSON.parse(fs.readFileSync('.cache/browser-vm/worker-image.json', 'utf8')).image,
  probe_loopback_webrtc: true,
});
let bypass = false;
const firewallRule = ['OUTPUT', '-o', 'lo', '-m', 'owner', '--uid-owner', '65532', '-j', 'ACCEPT'];
const read = async () => JSON.parse(await worker.run(['exec', '--user=0', worker.name,
  'cat', '/control/browser-canary.json']));
const rootGet = async url => worker.run(['exec', '--user=0', worker.name, 'node', '-e',
  'fetch(process.argv[1]).then(r=>r.text()).then(console.log)', url]);
const observations = {};
try {
  const browser = await worker.launch(require('playwright').chromium);
  observations.runtime = {...worker.runtime, chromium: browser.version()};
  await worker.run(['exec', '-d', '--user=0', worker.name, 'node', '/opt/worker/browser-canary.mjs']);
  let ready;
  const deadline = Date.now() + 5000;
  while (!ready && Date.now() < deadline) {
    try {ready = await read();} catch {await new Promise(resolve => setTimeout(resolve, 50));}
  }
  assert.ok(ready?.http_port && ready?.stun_port, 'Root canaries did not become ready');
  observations.resolver = await worker.run(['exec', '--user=0', worker.name, 'cat', '/etc/resolv.conf']);
  const origin = 'http://127.0.0.1:' + ready.http_port;
  const context = await browser.newContext({serviceWorkers: 'allow'});
  await context.grantPermissions(['local-network-access']);
  observations.probe_treatment = {host_permission_grants:['local-network-access'],
    webrtc_loopback_probe: worker.ready.webrtc_loopback_probe};
  assert.equal(worker.ready.webrtc_loopback_probe, true);
  const flushDNS = async () => {
    // Trusted test control only, absent from the agent's restricted facade.
    // These WebUI commands are pinned in Chromium 153.0.8010.12's
    // chrome/browser/resources/net_internals/browser_bridge.js.
    const admin = await context.newPage();
    await admin.goto('chrome://net-internals/#dns');
    await admin.evaluate(() => {
      chrome.send('clearHostResolverCache');
      chrome.send('flushSocketPools');
    });
    await admin.close();
  };
  // Trusted bootstrap only. Fetch, WebSocket, SW and STUN probes have no route handler.
  await context.route(origin + '/bootstrap', route => route.fulfill({
    contentType: 'text/html', body: '<h1>Browser network probes</h1>',
  }));
  const page = await context.newPage();
  observations.browser_errors = [];
  page.on('console', message => {
    if (message.type() === 'error') observations.browser_errors.push(message.text());
  });
  page.on('requestfailed', request => observations.browser_errors.push({
    url:request.url(), failure:request.failure()}));
  await page.goto(origin + '/bootstrap');
  assert.equal(await page.evaluate(() => isSecureContext && 'serviceWorker' in navigator), true);
  const attempts = async label => page.evaluate(async ({origin, stunPort, label}) => {
    const bounded = promise => Promise.race([promise,
      new Promise(resolve => setTimeout(() => resolve({timeout:true}), 1800))]);
    const fetchProbe = () => fetch(origin + '/fetch-' + label, {cache:'no-store'})
      .then(response => ({status:response.status})).catch(error => ({error:String(error)}));
    const socketProbe = () => new Promise(resolve => {
      const socket = new WebSocket(origin.replace('http:', 'ws:') + '/ws-' + label);
      socket.onopen = () => {socket.close(); resolve({opened:true});};
      socket.onerror = () => resolve({error:true});
      setTimeout(() => {socket.close(); resolve({timeout:true});}, 1700);
    });
    const swProbe = () => new Promise(resolve => {
      const channel = new MessageChannel();
      channel.port1.onmessage = event => {channel.port1.close(); resolve(event.data);};
      navigator.serviceWorker.controller.postMessage(
        {kind:'probe', url:origin + '/sw-' + label}, [channel.port2]);
    });
    const rtcProbe = async () => {
      const pc = new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:' + stunPort}]});
      const errors = [], candidates = [];
      pc.onicecandidateerror = event => errors.push({code:event.errorCode, text:event.errorText});
      pc.onicecandidate = event => {if (event.candidate) candidates.push(event.candidate.type);};
      pc.createDataChannel('synthetic');
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise(resolve => setTimeout(resolve, 1400));
      const state = pc.iceGatheringState;
      pc.close(); return {state, errors, candidates};
    };
    return {fetch:await bounded(fetchProbe()), websocket:await bounded(socketProbe()),
      service_worker:await bounded(swProbe()), webrtc:await rtcProbe()};
  }, {origin, stunPort:ready.stun_port, label});

  // Deliberately broken disposable boundary: all browser UID loopback traffic.
  await worker.run(['exec', '--user=0', worker.name, 'iptables', '-I', ...firewallRule]);
  bypass = true;
  await page.evaluate(async origin => {
    await navigator.serviceWorker.register(origin + '/worker.js');
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller)
      await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, {once:true}));
  }, origin);
  await rootGet(origin + '/control/reset');
  observations.positive = await attempts('positive');
  // Same origin, two DNS answers. Clear the browser's cache and connection pool
  // between phases so a retained connection cannot hide the rebinding control.
  for (const phase of [1, 2]) {
    await rootGet(origin + '/control/phase/' + phase);
    await flushDNS();
    await page.evaluate(async url => {
      await fetch(url, {mode:'no-cors', cache:'no-store'});
    }, `http://rebind.test:${ready.http_port}/dns-positive-${phase}`);
  }
  observations.positive_canaries = await read();
  assert.ok(observations.positive_canaries.hits.http.some(hit => hit.path === '/fetch-positive'));
  assert.ok(observations.positive_canaries.hits.http.some(hit => hit.path === '/sw-positive'));
  assert.ok(observations.positive_canaries.hits.websocket.some(hit => hit.path === '/ws-positive'));
  assert.ok(observations.positive_canaries.hits.stun > 0,
    'WebRTC positive control sent no observable STUN traffic; denial is not qualified');
  for (const phase of [1, 2]) {
    assert.ok(observations.positive_canaries.hits.http.some(hit =>
      hit.path === `/dns-positive-${phase}` && hit.address === `127.0.0.${phase}`));
  }
  await worker.run(['exec', '--user=0', worker.name, 'iptables', '-D', ...firewallRule]);
  bypass = false;
  await rootGet(origin + '/control/reset');
  observations.enforced = await attempts('enforced');
  for (const phase of [1, 2]) {
    await rootGet(origin + '/control/phase/' + phase);
    await flushDNS();
    observations.enforced['dns_phase_' + phase] = await page.evaluate(async url => {
      const abort = new AbortController();
      setTimeout(() => abort.abort(), 1200);
      try {await fetch(url, {mode:'no-cors', cache:'no-store', signal:abort.signal}); return 'reached';}
      catch (error) {return String(error);}
    }, `http://rebind.test:${ready.http_port}/dns-enforced-${phase}`);
  }
  observations.enforced_canaries = await read();
  assert.deepEqual(observations.enforced_canaries.hits, {http:[], websocket:[], dns:[], stun:0});
  const counts = await worker.run(['exec', '--user=0', worker.name, 'iptables', '-nvxL', 'OUTPUT']);
  observations.firewall = counts;
  assert.ok(observations.enforced.fetch.error || observations.enforced.fetch.timeout);
  assert.ok(observations.enforced.service_worker.error || observations.enforced.service_worker.timeout);
  observations.status = 'passed';
  observations.image = worker.config.image;
  observations.webrtc_delivery_qualified = true;
  observations.limits = ['No model-directed attempt or cross-platform claim',
    'Local-network permission and loopback ICE are explicit test treatments, not Muse settings'];
  console.log(JSON.stringify(observations));
} catch (error) {
  observations.last_canaries = await read().catch(() => null);
  console.error(JSON.stringify({error:error.message, observations}));
  throw error;
} finally {
  if (bypass) await worker.run(['exec', '--user=0', worker.name, 'iptables', '-D', ...firewallRule]).catch(() => {});
  await worker.close();
}
