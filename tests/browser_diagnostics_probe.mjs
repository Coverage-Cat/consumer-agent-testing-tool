// Developer test probes, never loaded into the evaluated browser agent.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
const require = createRequire(path.join(process.cwd(), 'tooling/payment-ui/package.json'));
const [mode, input] = process.argv.slice(2);
if (mode === 'quota') {
  const {BrowserDiagnostics} = await import('../src/consumer_agent_testing_tool/data/surface_host/browser_diagnostics.mjs');
  const capture = new BrowserDiagnostics(process.cwd(), {enabled: true, max_bytes: 1024}, input);
  const statuses = [capture.save(Buffer.alloc(1024), 'png').status,
    capture.save(Buffer.alloc(1024), 'png').status,
    capture.save(Buffer.alloc(1024, 1), 'png').status];
  console.log(JSON.stringify({statuses, bytes: capture.bytes, files: fs.readdirSync(input).length}));
} else if (mode === 'pixels') {
  const {PNG} = require('playwright-core/lib/utilsBundle');
  const image = PNG.sync.read(fs.readFileSync(input));
  console.log(JSON.stringify([[30, 30], [30, 90], [30, 150]].map(([x,y]) =>
    [...image.data.subarray((y * image.width + x) * 4, (y * image.width + x) * 4 + 4)])));
} else {
  const {chromium} = require('playwright');
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext();
    const origin = new URL(input).origin;
    await context.route('**/*', route =>
      new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const page = await context.newPage();
    let loaded = false;
    const responses = [];
    context.on('response', response => {
      responses.push({url:response.url(), status:response.status()});
      if (response.url().includes('/trace/file?') && response.ok()) loaded = true;
    });
    await page.goto(input);
    await page.getByText('Actions', {exact: true}).waitFor({timeout: 15000});
    for (let i = 0; i < 50 && !loaded; i++) await new Promise(resolve => setTimeout(resolve, 100));
    console.log(JSON.stringify({loaded_trace: loaded, responses, text: await page.ariaSnapshot({mode:'ai'})}));
  } finally {
    await browser.close();
  }
}
