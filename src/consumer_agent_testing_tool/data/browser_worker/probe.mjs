// Root-owned canaries establish that the detector sees allowed traffic, then
// observe attempted TCP/UDP/DNS from the browser UID and its subprocess.
import fs from 'node:fs';
import net from 'node:net';
import dgram from 'node:dgram';
import {spawn} from 'node:child_process';
const driverStatus = fs.readFileSync('/proc/1/status', 'utf8');
const driverCaps = BigInt('0x' + driverStatus.match(/^CapEff:\s+([a-f0-9]+)$/m)[1]);
if (driverCaps & (1n << 12n)) throw Error('Driver retained CAP_NET_ADMIN');
const chromiumProcesses = fs.readdirSync('/proc').filter(name => /^\d+$/.test(name)).flatMap(pid => {
  try {
    const status = fs.readFileSync('/proc/' + pid + '/status', 'utf8');
    if (!/^Name:\s+chrome/m.test(status)) return [];
    return [{name: status.match(/^Name:\s+(.+)$/m)[1],
      uid: Number(status.match(/^Uid:\s+(\d+)/m)[1]),
      effective_caps: status.match(/^CapEff:\s+(.+)$/m)[1],
      uid_map: fs.readFileSync('/proc/' + pid + '/uid_map', 'utf8').trim()}];
  } catch { return []; } // A browser subprocess may exit during enumeration.
});
if (!chromiumProcesses.length || chromiumProcesses.some(row => row.uid !== 65532))
  throw Error('Chromium is not running under its separate unprivileged identity');
if (chromiumProcesses.some(row => BigInt('0x' + row.effective_caps) !== 0n &&
    row.uid_map.split(/\s+/).join(' ') !== '65532 65532 1'))
  throw Error('A capable Chromium process is outside the expected single-UID sandbox namespace');
const hits = {tcp: 0, tcp6: 0, udp: 0, udp6: 0};
const tcp = net.createServer(socket => {hits.tcp++; socket.end();});
const tcp6 = net.createServer(socket => {hits.tcp6++; socket.end();});
await new Promise(resolve => tcp.listen(0, '127.0.0.1', resolve));
const port = tcp.address().port;
await new Promise(resolve => tcp6.listen(port, '::1', resolve));
const udp = dgram.createSocket('udp4'), udp6 = dgram.createSocket('udp6');
udp.on('message', () => hits.udp++);
udp6.on('message', () => hits.udp6++);
await new Promise(resolve => udp.bind(0, '127.0.0.1', resolve));
const udpPort = udp.address().port;
await new Promise(resolve => udp6.bind(udpPort, '::1', resolve));
for (const host of ['127.0.0.1', '::1'])
  await new Promise((resolve, reject) => {
    const socket = net.connect(port, host);
    socket.on('error', reject); socket.on('connect', () => {socket.end(); resolve();});
  });
for (const host of ['127.0.0.1', '::1']) {
  const socket = dgram.createSocket(host.includes(':') ? 'udp6' : 'udp4');
  await new Promise((resolve, reject) => socket.send(Buffer.from('positive-control'), udpPort, host,
    error => {socket.close(); error ? reject(error) : resolve();}));
}
await new Promise(resolve => setTimeout(resolve, 100));
const positive = {...hits};
if (Object.values(positive).some(value => value !== 1)) throw Error('Canary positive control failed');
const controlPort = Number(new URL(JSON.parse(fs.readFileSync('/control/ready.json')).endpoint).port);
try {
  const child = spawn('/usr/bin/setpriv', [
    '--reuid=65532', '--regid=65534', '--clear-groups', '--bounding-set=-all',
    '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs',
    process.execPath, '/opt/worker/probe-child.mjs', String(port), String(udpPort), String(controlPort),
  ], {stdio: ['ignore', 'pipe', 'pipe'], env: {PATH: '/usr/bin:/bin'}});
  let output = '', errors = '';
  const timer = setTimeout(() => child.kill('SIGKILL'), 15000);
  child.stdout.on('data', bytes => output += bytes);
  child.stderr.on('data', bytes => errors += bytes);
  await new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve() : reject(Error(errors || 'Probe failed')));
  }).finally(() => clearTimeout(timer));
  await new Promise(resolve => setTimeout(resolve, 100));
  const result = JSON.parse(output);
  const unexpected = Object.fromEntries(Object.entries(hits).map(([key, value]) =>
    [key, value - positive[key]]));
  const passed = result.uid === 65532 && Object.values(unexpected).every(value => value === 0) &&
    !Object.values(result.attempts).some(value => ['connected', 'readable', 'resolved'].includes(value)) &&
    result.capabilities.filter(value => value.startsWith('Cap')).every(value => /:\s+0+$/.test(value));
  console.log(JSON.stringify({passed, positive_control: positive, unexpected_canary_hits: unexpected,
    driver_net_admin: false, chromium_processes: chromiumProcesses, ...result}));
  if (!passed) process.exitCode = 1;
} finally {
  tcp.close(); tcp6.close(); udp.close(); udp6.close();
}
