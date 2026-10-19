// Deterministic test probe running as the same unprivileged UID as Chromium.
// This executable is not exposed by the agent facade.
import fs from 'node:fs';
import net from 'node:net';
import dgram from 'node:dgram';
import {Resolver} from 'node:dns/promises';
import {spawnSync} from 'node:child_process';

const [tcpPort, udpPort, controlPort] = process.argv.slice(2).map(Number);
async function connect(host, port) {
  return await new Promise(resolve => {
    const socket = net.connect({host, port});
    const timer = setTimeout(() => {socket.destroy(); resolve('timeout');}, 350);
    socket.on('connect', () => {clearTimeout(timer); socket.destroy(); resolve('connected');});
    socket.on('error', error => {clearTimeout(timer); resolve(error.code);});
  });
}
const attempts = {};
for (const [name, host, port] of [
  ['tcp-loopback', '127.0.0.1', tcpPort], ['tcp-ipv6', '::1', tcpPort],
  ['private-control', '127.0.0.1', controlPort],
  ['host-alias', 'host.docker.internal', tcpPort],
  ['gateway', '192.168.5.2', tcpPort], ['direct-egress', '192.0.2.1', tcpPort],
]) attempts[name] = await connect(host, port);
for (const host of ['127.0.0.1', '::1', '192.0.2.1']) {
  const socket = dgram.createSocket(host.includes(':') ? 'udp6' : 'udp4');
  attempts['udp-' + host] = await new Promise(resolve => {
    socket.send(Buffer.from('unprivileged-udp'), udpPort, host,
      error => {resolve(error?.code || 'sent-without-delivery-proof'); socket.close();});
  });
}
const resolver = new Resolver({timeout: 200, tries: 1});
resolver.setServers(['127.0.0.1:' + udpPort]);
try { await resolver.resolve4('rebind.invalid'); attempts.dns = 'resolved'; }
catch (error) { attempts.dns = error.code; }
for (const file of ['/control/ready.json', '/proc/1/environ', '/proc/1/fd']) {
  try {
    file.endsWith('/fd') ? fs.readdirSync(file) : fs.readFileSync(file);
    attempts[file] = 'readable';
  } catch (error) { attempts[file] = error.code; }
}
const child = spawnSync(process.execPath, ['-e', `
  const net=require('net');const s=net.connect(${tcpPort},'127.0.0.1');
  s.on('connect',()=>{console.log('connected');s.destroy()});
  s.on('error',e=>console.log(e.code));
  setTimeout(()=>{s.destroy();console.log('timeout')},300).unref();
`], {encoding: 'utf8', timeout: 2000, env: {PATH: '/usr/bin:/bin'}});
attempts.child_process = child.stdout.trim();
console.log(JSON.stringify({uid: process.getuid(), attempts,
  capabilities: fs.readFileSync('/proc/self/status', 'utf8').split('\n')
    .filter(line => /^(Cap|NoNewPrivs|Seccomp)/.test(line))}));
