// Explicit local proof; no model, service credentials or external destination.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {ContainerBrowser} from '../src/consumer_agent_testing_tool/data/surface_host/browser_worker.mjs';
const root = process.cwd();
const require = createRequire(path.join(root, 'tooling/payment-ui/package.json'));
const {chromium} = require('playwright');
const config = {...JSON.parse(fs.readFileSync('.cache/browser-vm/connection.json', 'utf8')),
  image: JSON.parse(fs.readFileSync('.cache/browser-vm/worker-image.json', 'utf8')).image};
const worker = new ContainerBrowser(root, config);
try {
  const browser = await worker.launch(chromium);
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.setContent('<h1>Offline worker</h1><button>Test action</button>');
  await page.getByRole('button').click();
  const title = await page.getByRole('heading').innerText();
  if (title !== 'Offline worker') throw Error('Worker browser did not render fixture');
  const bypasses = JSON.parse(await worker.run(['exec', '--user=0', worker.name,
    'node', '/opt/worker/probe.mjs']));
  if (!bypasses.passed) throw Error('Worker bypass probes failed');
  console.log(JSON.stringify({status: 'rendered-and-clicked', browser: browser.version(),
    network: 'none', chromium_sandbox: true, image: config.image, bypasses}));
} finally {
  await worker.close();
}
