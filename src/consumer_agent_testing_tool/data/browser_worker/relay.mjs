// Trusted docker-exec helper. No destination argument or general RPC surface.
import fs from 'node:fs';
import net from 'node:net';
const ready = JSON.parse(fs.readFileSync('/control/ready.json', 'utf8'));
const url = new URL(ready.endpoint);
if (process.getuid() !== 0 || url.hostname !== '127.0.0.1' || url.protocol !== 'ws:')
  throw Error('Invalid private worker endpoint');
const socket = net.connect(Number(url.port), '127.0.0.1');
process.stdin.pipe(socket);
socket.pipe(process.stdout);
socket.on('error', () => process.exit(1));
socket.on('close', () => process.exit(0));
process.stdin.on('end', () => socket.end());
