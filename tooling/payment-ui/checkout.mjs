// Host-side checkout rehearsal: actual consumer MCP tool, registered UI, and
// Stripe-hosted fields. No planner, operator API, quote mutation, or card API.
import {Client, StreamableHTTPClientTransport} from '@modelcontextprotocol/client';
import {chromium} from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';
import crypto from 'node:crypto';
import {checkoutConfigOrExit} from './checkout-config.mjs';

const {root, origin, expectedUid, checkpoint, billing: testBilling} = checkoutConfigOrExit('checkout.mjs');
const appUrl = origin + '/__consumer_agent_testing_tool_payment_ui__';
const hostUrl = 'https://consumer-agent-testing-tool.invalid/payment-host';
const stripeHosts = new Set(['js.stripe.com', 'api.stripe.com', 'hooks.stripe.com',
  'm.stripe.network', 'r.stripe.com', 'm.stripe.com', 'q.stripe.com']);
let token = checkpoint.intake_access_token;
const privateSecrets = new Set([token]);
const run = path.join(root, 'runs', 'payment-ui-' + crypto.randomUUID());
fs.mkdirSync(run, {recursive: true, mode: 0o700});
let browser, context, page, prepared, latest, saved = false;
const calls = [], errors = [], denied = new Set();
let stripeTestKeyObserved = false;
function scrub(value) {
  if (typeof value === 'string') {
    for (const secret of privateSecrets) value = value.split(secret).join('[REDACTED]');
    return value.replace(/([?&](?:token|client_secret|access_token)=)[^&\s"'<>]+/gi, '$1[REDACTED]');
  }
  if (Array.isArray(value)) return value.map(scrub);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) =>
    [k, ['intake_access_token','client_secret','resume_token','polling_token','review_token'].includes(k)
      ? '[REDACTED]' : scrub(v)]));
  return value;
}
function event(kind, data) {
  const entry = scrub({at: new Date().toISOString(), kind, ...data});
  fs.appendFileSync(path.join(run, 'events.jsonl'), JSON.stringify(entry) + '\n', {mode: 0o600});
}
function observe(value) {
  if (!value || typeof value !== 'object') return;
  for (const [key,item] of Object.entries(value)) {
    if (['intake_access_token','client_secret'].includes(key) && typeof item === 'string') privateSecrets.add(item);
    if (item && typeof item === 'object') observe(item);
  }
}
const capabilities = {extensions: {'io.modelcontextprotocol/ui': {mimeTypes: ['text/html;profile=mcp-app']}}};
const client = new Client({name: 'consumer-agent-testing-tool-secure-payment-host', version: '0.1.0'}, {capabilities});
const transport = new StreamableHTTPClientTransport(new URL(origin + '/api/consumer/mcp'), {
  requestInit: {redirect: 'error'},
  fetch: async (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url || input);
    if (url.origin !== origin || url.pathname !== '/api/consumer/mcp') throw Error('Denied non-consumer MCP destination');
    return fetch(input, {...init, redirect: 'error'});
  },
});
async function call(name, arguments_ = {}) {
  if (!['consumer_intake_issues', 'umbrella_consumer_payment'].includes(name)) throw Error('Checkout tool allowlist');
  if (name === 'umbrella_consumer_payment') {
    if (arguments_.payment_collection_requested) {
      if (calls.length !== 1 || !['payment_needed'].includes(latest?.detail_status || latest?.status))
        throw Error('Fresh payment_needed status required before setup');
    } else {
      if (!prepared || arguments_.setup_intent_id !== prepared._meta?.payment_ui?.setup?.setup_intent_id
          || !/^pm_[A-Za-z0-9]+$/.test(arguments_.payment_method_id || ''))
        throw Error('Completion must match this form’s prepared intent and Stripe payment method');
      if (Object.keys(arguments_).some(k => !['setup_intent_id','payment_method_id','uid','intake_access_token'].includes(k)))
        throw Error('Unexpected completion fields');
    }
  }
  event('tool_call', {name, argument_keys: Object.keys(arguments_)});
  const result = await client.callTool({name, arguments: {...arguments_, uid: expectedUid, intake_access_token: token}});
  observe(result);
  const body = result.structuredContent || {};
  if (body.uid && body.uid !== expectedUid) throw Error('Response belongs to another application');
  if (body.intake_access_token) token = body.intake_access_token;
  latest = body;
  calls.push(name);
  fs.writeFileSync(path.join(run, 'consumer-checkpoint.json'),
    JSON.stringify({origin, uid: expectedUid, intake_access_token: token, latest}, null, 2), {mode: 0o600});
  event('tool_result', {name, status: body.status, detail_status: body.detail_status,
    post_choose: body.post_choose, payment_ui: body.payment_ui, payment_setup: body.payment_setup,
    is_error: result.isError || false});
  if (result.isError) throw Error('Consumer tool rejected the request');
  return result;
}
function safeUrl(value) { try { const u = new URL(value); return u.origin + u.pathname; } catch {return '[internal]';} }
async function inspectForm() {
  const frames = [];
  for (const [index, frame] of page.frames().entries()) {
    let fields = [];
    try {
      fields = await frame.locator('input,select,button').evaluateAll(nodes => nodes.map(e => ({
        tag: e.tagName, type: e.type, name: e.name, placeholder: e.getAttribute('placeholder'),
        label: e.getAttribute('aria-label'), text: e.tagName === 'BUTTON' ? e.innerText : undefined
      })));
    } catch {}
    frames.push({index, url: safeUrl(frame.url()), fields});
  }
  const app = page.frames().find(f => f.url() === appUrl);
  return scrub({run, text: app ? await app.locator('body').innerText() : 'App frame not loaded',
    frames, stripeTestKeyObserved, deniedHosts: [...denied], errors, latest_status: latest?.status});
}
async function shutdown() {
  if (browser) await browser.close();
  await client.close();
  fs.writeFileSync(path.join(run, 'result.json'), JSON.stringify(scrub({
    status: saved ? 'payment_method_confirmed' : 'not_completed', calls,
    latest, errors, deniedHosts: [...denied], stripeTestKeyObserved,
    renderer: 'Registered consumer UI with scoped legacy window.openai host compatibility bridge',
    local_ui_correction: 'Pass fake billing details to confirmSetup because the registered form hides its email field',
    card_values_recorded: false,
  }), null, 2), {mode: 0o600});
}
try {
  if (process.stdin.isTTY) process.stdin.setRawMode(true);
  await client.connect(transport);
  await client.listTools();
  await call('consumer_intake_issues');
  if ((latest.detail_status || latest.status) !== 'payment_needed') throw Error('Application is not payment_needed');
  prepared = await call('umbrella_consumer_payment', {payment_collection_requested: true});
  const setup = prepared._meta?.payment_ui?.setup;
  console.log(JSON.stringify({stage: 'prepared', status: latest.status, payment_ui: latest.payment_ui,
    setup_present: !!setup, setup_keys: setup ? Object.keys(setup) : [], environment: setup?.environment}));
  if (setup?.environment !== 'test' || !setup.publishable_key?.startsWith('pk_test_') || !setup.client_secret)
    throw Error('Secure renderer requires a complete Stripe test SetupIntent');
  const resource = await client.readResource({uri: 'ui://coveragecat/umbrella-payment'});
  const content = resource.contents.find(c => c.text && c.mimeType === 'text/html;profile=mcp-app');
  if (!content) throw Error('Registered payment UI unavailable');
  event('ui_resource', {uri: content.uri, sha256: crypto.createHash('sha256').update(content.text).digest('hex')});
  const confirmPattern = /return stripe\.confirmSetup\(\{\s*elements,\s*clientSecret,\s*redirect: "if_required"\s*\}\);/g;
  if ([...content.text.matchAll(confirmPattern)].length !== 1)
    throw Error('Registered UI changed; billing correction requires review');
  const correctedHtml = content.text.replace(confirmPattern, () => `return stripe.confirmSetup({
    elements, clientSecret, redirect: "if_required",
    confirmParams: {payment_method_data: {billing_details: ${JSON.stringify(testBilling).replaceAll('<','\\u003c')}}}
  });`);
  event('local_ui_correction', {
    reason: 'Stripe requires billing email when Payment Element uses email: never',
    correction: 'Supply the provided fake shopper billing details in confirmParams',
    sha256: crypto.createHash('sha256').update(correctedHtml).digest('hex'),
  });
  const shim = `<script>window.openai={toolOutput:${JSON.stringify(prepared.structuredContent).replaceAll('<','\\u003c')},
    toolResponseMetadata:${JSON.stringify(prepared._meta).replaceAll('<','\\u003c')},
    callTool:(name,args)=>window.__coverageCatTool({name,args})};</script>`;
  const html = correctedHtml.replace('<head>', '<head>' + shim);
  const tmp = path.join(root, '.cache', 'payment-browser-temp');
  fs.mkdirSync(tmp, {recursive: true});
  process.env.TMPDIR = tmp;
  process.env.PLAYWRIGHT_BROWSERS_PATH = path.join(root, '.cache', 'ms-playwright');
  browser = await chromium.launch({headless: true, args: ['--disable-background-networking']});
  context = await browser.newContext({serviceWorkers: 'block'});
  await context.routeWebSocket('**/*', socket => socket.close());
  await context.exposeBinding('__coverageCatTool', async (source, request) => {
    if (source.frame.url() !== appUrl || request.name !== 'umbrella_consumer_payment') throw Error('Denied UI tool call');
    const response = await call(request.name, request.args);
    saved = ['waiting_on_carrier','bound','active'].includes(latest.status) ||
      latest.payment_method_saved === true || latest.payment_method_on_file === true;
    console.log(JSON.stringify({stage: 'completion_response', response: scrub(latest)}));
    return response;
  });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.url() === hostUrl) return route.fulfill({contentType: 'text/html',
      body: `<html><body><h1>Local consumer agent payment interface</h1><iframe src="${appUrl}" sandbox="allow-scripts allow-same-origin allow-forms" style="width:100%;height:850px;border:0"></iframe></body></html>`});
    if (request.url() === appUrl) return route.fulfill({contentType: 'text/html', body: html});
    if (!stripeHosts.has(url.hostname)) { denied.add(url.hostname); return route.abort('blockedbyclient'); }
    const secretData = request.url() + (request.postData() || '') + JSON.stringify(request.headers());
    if (/(?:pk|sk)_live_/.test(secretData)) { errors.push('Blocked live Stripe key'); return route.abort(); }
    if (secretData.includes('pk_test_')) stripeTestKeyObserved = true;
    return route.continue();
  });
  page = await context.newPage();
  page.on('pageerror', error => errors.push(scrub(error.message)));
  await page.goto(hostUrl, {waitUntil: 'domcontentloaded'});
  console.log(JSON.stringify({stage: 'form_open', ...await inspectForm()}));
  const lines = readline.createInterface({input: process.stdin, terminal: false});
  for await (const line of lines) {
    try {
      const command = JSON.parse(line);
      if (command.action === 'inspect') console.log(JSON.stringify(await inspectForm()));
      else if (command.action === 'fill') {
        const frame = page.frames()[command.frame];
        if (!frame || !stripeHosts.has(new URL(frame.url()).hostname) || !stripeTestKeyObserved)
          throw Error('Card entry is restricted to Stripe-hosted fields in observed test mode');
        await frame.locator(command.selector).fill(command.value);
        console.log(JSON.stringify({filled: command.selector}));
      } else if (command.action === 'save') {
        const app = page.frames().find(f => f.url() === appUrl);
        await app.getByRole('button', {name: 'Save payment method', exact: true}).click({timeout: 15000});
        console.log(JSON.stringify({clicked: 'Save payment method'}));
      } else if (command.action === 'status') {
        await call('consumer_intake_issues');
        console.log(JSON.stringify({stage: 'status', response: scrub(latest)}));
      } else if (command.action === 'screenshot') {
        await page.screenshot({path: path.join(run, 'payment-ui.png'),
          mask: page.frames().filter(f => stripeHosts.has(new URL(f.url()).hostname)).map(f => f.locator('body'))});
        console.log(JSON.stringify({screenshot: path.join(run, 'payment-ui.png')}));
      } else if (command.action === 'close') break;
      else throw Error('Unknown secure UI action');
    } catch (error) { console.log(JSON.stringify({error: scrub(error.message)})); }
  }
  await shutdown();
  console.log(JSON.stringify({closed: true, saved, run}));
} catch (error) {
  errors.push(scrub(error.message));
  console.log(JSON.stringify({error: scrub(error.message), run}));
  await shutdown();
  process.exitCode = 1;
}
