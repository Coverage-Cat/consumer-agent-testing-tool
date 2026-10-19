#!/usr/bin/node
import {spawn} from 'node:child_process';
import {chromium} from 'playwright';

// launchServer's temporary profile belongs to the root driver. Give the
// sandboxed browser its own private, unprivileged profile instead.
const args = process.argv.slice(2).map(value =>
  value.startsWith('--user-data-dir=') ? '--user-data-dir=/tmp/browser-profile' : value);
if (args.includes('--no-sandbox') || args.includes('--disable-setuid-sandbox'))
  throw Error('The worker requires the Chromium sandbox');
const child = spawn('/usr/bin/setpriv', [
  '--reuid=65532', '--regid=65534', '--clear-groups', '--bounding-set=-all',
  '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs',
  chromium.executablePath(), ...args,
], {stdio: ['inherit', 'inherit', 'inherit', 3, 4], env: {
  PATH: '/usr/local/bin:/usr/bin:/bin', HOME: '/tmp/browser-home', TMPDIR: '/tmp',
  LANG: 'C.UTF-8',
}});
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));
child.on('error', error => {console.error(error.message); process.exitCode = 1;});
child.on('exit', code => process.exit(code ?? 1));
