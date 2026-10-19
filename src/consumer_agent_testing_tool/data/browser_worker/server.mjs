import fs from 'node:fs';
import {chromium} from 'playwright';

const lifetime = Number(process.env.SURFACE_BROWSER_LIFETIME_SECONDS || 240);
const loopbackProbe = process.env.SURFACE_BROWSER_PROBE_LOOPBACK_WEBRTC === '1';
if (!Number.isInteger(lifetime) || lifetime < 30 || lifetime > 3900)
  throw Error('Invalid browser worker lifetime');
// The worker expires independently of the host, including after a host crash.
// Docker --rm removes its tmpfs and container record after exit.
setTimeout(() => process.exit(124), lifetime * 1000);
const server = await chromium.launchServer({
  headless: true, chromiumSandbox: true,
  executablePath: '/opt/worker/chromium-wrapper.mjs',
  host: '127.0.0.1', port: 0, downloadsPath: '/tmp/browser-downloads',
  args: ['--disable-background-networking',
    ...(loopbackProbe ? ['--allow-loopback-in-peer-connection'] : [])],
});
fs.writeFileSync('/control/ready.json', JSON.stringify({
  endpoint: server.wsEndpoint(), uid: 65532, network: 'none',
  enforcement: 'linux-network-namespace-and-owner-firewall',
  chromium_sandbox: true,
  webrtc_loopback_probe: loopbackProbe,
}) + '\n', {mode: 0o600, flag: 'wx'});
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, async () => {
  await server.close();
  process.exit(0);
});
server.on('close', () => process.exit(0));
