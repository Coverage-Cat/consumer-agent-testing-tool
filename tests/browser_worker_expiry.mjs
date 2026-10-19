import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {ContainerBrowser} from '../src/consumer_agent_testing_tool/data/surface_host/browser_worker.mjs';
const root = process.cwd(), require = createRequire(path.join(root, 'tooling/payment-ui/package.json'));
const worker = new ContainerBrowser(root, {
  ...JSON.parse(fs.readFileSync('.cache/browser-vm/connection.json', 'utf8')),
  image: JSON.parse(fs.readFileSync('.cache/browser-vm/worker-image.json', 'utf8')).image,
  lifetime_seconds: 30,
});
try {
  const browser = await worker.launch(require('playwright').chromium);
  // No host shutdown command: the worker's own watchdog must stop the browser.
  await Promise.race([
    new Promise(resolve => browser.once('disconnected', resolve)),
    new Promise((_, reject) => setTimeout(() => reject(Error('Worker failed to expire')), 40000).unref()),
  ]);
  const deadline = Date.now() + 5000;
  let containers;
  do {
    containers = await worker.run(['ps', '-aq', '--filter', 'name=^/' + worker.name + '$']);
    if (containers) await new Promise(resolve => setTimeout(resolve, 100));
  } while (containers && Date.now() < deadline);
  assert.equal(containers, '', 'Expired worker was not removed by Docker');
  console.log(JSON.stringify({status: 'passed', independent_expiry_seconds: 30,
    container_removed: true}));
} finally {
  await worker.close();
}
