// Disposable test fixture, started only by trusted docker-exec control.
// DNS deliberately changes between two loopback addresses with TTL zero.
// It is never started by the normal browser-worker entrypoint.
import fs from 'node:fs';
import http from 'node:http';
import dgram from 'node:dgram';

if (process.getuid() !== 0) throw Error('Browser canary requires trusted test control');
let phase = 1;
const hits = {http: [], websocket: [], dns: [], stun: 0};
const record = '/control/browser-canary.json';
const servers = [];
let httpPort, stunPort;
function save() {
  fs.writeFileSync(record + '.tmp', JSON.stringify({pid: process.pid, http_port: httpPort,
    stun_port: stunPort, phase, hits}) + '\n', {mode: 0o600});
  fs.renameSync(record + '.tmp', record);
}
function handler(request, response) {
  // /control/* is reached only by the trusted root test process.
  // Browser test URLs never use these paths.
  if (request.url === '/control/reset') {
    hits.http.length = 0; hits.websocket.length = 0; hits.dns.length = 0; hits.stun = 0;
    save(); response.end('reset'); return;
  }
  if (request.url === '/control/phase/1' || request.url === '/control/phase/2') {
    phase = Number(request.url.at(-1)); save(); response.end('phase set'); return;
  }
  hits.http.push({path: request.url, method: request.method, address: request.socket.localAddress});
  save();
  if (request.url === '/worker.js') {
    response.writeHead(200, {'content-type': 'application/javascript',
      'service-worker-allowed': '/', 'cache-control': 'no-store'});
    response.end(`
      self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
      self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
      self.addEventListener('message', event => {
        if (event.data?.kind !== 'probe') return;
        const port = event.ports[0];
        event.waitUntil(fetch(event.data.url, {cache:'no-store'})
          .then(response => port.postMessage({status:response.status}))
          .catch(error => port.postMessage({error:String(error)})));
      });`);
  } else {
    response.writeHead(200, {'access-control-allow-origin': '*', 'cache-control': 'no-store',
      connection: 'close'});
    response.end('synthetic canary');
  }
}
for (const address of ['127.0.0.1', '127.0.0.2']) {
  const server = http.createServer(handler);
  server.on('upgrade', (request, socket) => {
    hits.websocket.push({path: request.url, address: socket.localAddress}); save();
    socket.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(httpPort || 0, address, resolve);
  });
  httpPort ||= server.address().port;
  servers.push(server);
}
const dns = dgram.createSocket('udp4');
dns.on('message', (packet, peer) => {
  if (packet.length < 17 || packet.length > 512) return;
  let cursor = 12;
  const labels = [];
  while (cursor < packet.length && packet[cursor]) {
    const length = packet[cursor++];
    if (length > 63 || cursor + length >= packet.length) return;
    labels.push(packet.subarray(cursor, cursor + length).toString('ascii'));
    cursor += length;
  }
  cursor++;
  if (cursor + 4 > packet.length) return;
  const type = packet.readUInt16BE(cursor), name = labels.join('.');
  const answer = name.endsWith('.rebind.test') || name === 'rebind.test';
  hits.dns.push({name, type, phase}); save();
  const header = Buffer.alloc(12);
  packet.copy(header, 0, 0, 2);
  header.writeUInt16BE(answer ? 0x8180 : 0x8183, 2);
  header.writeUInt16BE(1, 4);
  header.writeUInt16BE(answer && type === 1 ? 1 : 0, 6);
  const question = packet.subarray(12, cursor + 4);
  const address = Buffer.from([127, 0, 0, phase]);
  // A record, TTL 0; no caching may conceal the changing-DNS control.
  const rr = Buffer.from([0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 0, 0, 4]);
  const response = answer && type === 1
    ? Buffer.concat([header, question, rr, address]) : Buffer.concat([header, question]);
  dns.send(response, peer.port, peer.address);
});
await new Promise((resolve, reject) => {dns.once('error', reject); dns.bind(53, '127.0.0.1', resolve);});
const stun = dgram.createSocket('udp4');
stun.on('message', () => {hits.stun++; save();});
await new Promise(resolve => stun.bind(0, '127.0.0.1', resolve));
stunPort = stun.address().port;
save();
setTimeout(() => process.exit(0), 90000);
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => process.exit(0));
