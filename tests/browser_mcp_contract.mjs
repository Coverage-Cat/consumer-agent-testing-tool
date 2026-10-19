// Actual upstream MCP server/SDK composition. This is test control code, never an agent tool.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import {BrowserMCP} from '../src/consumer_agent_testing_tool/data/surface_host/browser_mcp.mjs';

const root = process.cwd();
const require = createRequire(path.join(root, 'tooling/payment-ui/package.json'));
const mcpRequire = createRequire(require.resolve('@playwright/mcp'));
const {Client, InMemoryTransport} = await import(pathToFileURL(require.resolve('@modelcontextprotocol/client')));
const browser = await mcpRequire('playwright').chromium.launch({
  headless: true, executablePath: require('playwright').chromium.executablePath(),
});
const events = [];
const backend = new BrowserMCP({createConnection: require('@playwright/mcp').createConnection,
  Client, InMemoryTransport, version: '0.0.83',
  playwrightVersion: mcpRequire('playwright/package.json').version}, event => events.push(event));
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.setContent('<button onclick="this.textContent=\'Clicked\'">Safe action</button>');
  await backend.connect(context, path.join(root, '.cache/surface-host'), 1000);
  const snapshot = await backend.snapshot();
  const ref = snapshot.match(/button "Safe action" \[ref=([^\]]+)\]/)?.[1];
  assert.ok(ref, snapshot);
  const catalog = await backend.client.listTools();
  assert.ok(catalog.tools.some(tool => tool.name === 'browser_evaluate'));
  // Even direct use of the private client's tools/call cannot bypass the gate.
  for (const [name, args] of [
    ['browser_evaluate', {function: '() => document.body.textContent = "ESCAPED"'}],
    ['browser_run_code_unsafe', {code: '() => process.env'}],
    ['browser_click', {target: 'button'}],
    ['browser_click', {target: {toString: 'e2'}}],
    ['browser_snapshot', {filename: 'unexpected.txt'}],
    ['browser_tabs', {action: 'new', index: 0}],
    ['browser_snapshot', {_meta: {raw: true, cwd: '/'}}],
    ['toString', {}],
  ]) {
    await assert.rejects(backend.client.callTool({
      name, arguments: {_meta: {raw: true}, ...args},
    }));
  }
  assert.equal(await page.locator('button').innerText(), 'Safe action');
  await backend.call('browser_click', {target: ref});
  assert.match(await backend.snapshot(), /Clicked/);
  await assert.rejects(backend.call('browser_click', {target: 'e999999'}),
    error => error.message === 'Playwright MCP browser_click failed; obtain a fresh snapshot before retrying');
  assert.ok(events.some(event => event.kind === 'browser_backend_error'));
  const directory = backend.directory;
  assert.ok(fs.existsSync(directory));
  await backend.close();
  assert.equal(fs.existsSync(directory), false);
  await assert.rejects(backend.snapshot()); // A disconnected backend never falls back to direct Playwright.
  console.log(JSON.stringify({status: 'passed', backend: backend.info}));
} finally {
  await backend.close();
  await browser.close();
}
