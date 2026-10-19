// Developer control for the native WebRTC policy; never loaded into the evaluated agent.
// Counts STUN datagrams a page sends to an undeclared UDP port, with the restricted
// facade's launch flags ("facade") or without its WebRTC policy ("control").
import dgram from 'node:dgram';
import http from 'node:http';
import path from 'node:path';
import {createRequire} from 'node:module';
import {NATIVE_ARGS} from '../src/consumer_agent_testing_tool/data/surface_host/browser.mjs';

const require = createRequire(path.join(process.cwd(), 'tooling/payment-ui/package.json'));
const mcpRequire = createRequire(require.resolve('@playwright/mcp'));
const {chromium} = mcpRequire('playwright');
const flags = process.argv[2] === 'facade' ? NATIVE_ARGS : ['--disable-background-networking'];
const udp = dgram.createSocket('udp4');
let datagrams = 0;
udp.on('message', () => datagrams++);
await new Promise(resolve => udp.bind(0, '127.0.0.1', resolve));
const server = http.createServer((request, response) => response.writeHead(200, {'content-type': 'text/html'})
  .end(`<p id="ice">gathering</p><script>(async () => {
    const peer = new RTCPeerConnection({iceServers: [{urls: 'stun:127.0.0.1:${udp.address().port}'}]});
    peer.createDataChannel('probe');
    peer.onicegatheringstatechange = () => document.querySelector('#ice').textContent = peer.iceGatheringState;
    await peer.setLocalDescription(await peer.createOffer());
  })()</script>`));
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await chromium.launch({headless: true, executablePath: require('playwright').chromium.executablePath(),
  args: flags});
try {
  const page = await (await browser.newContext()).newPage();
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await new Promise(resolve => setTimeout(resolve, 2500));
  console.log(JSON.stringify({flags, datagrams, state: await page.textContent('#ice')}));
} finally {
  await browser.close();
  server.close();
  udp.close();
}
