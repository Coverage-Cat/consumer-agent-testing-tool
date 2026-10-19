import fs from 'node:fs';
import path from 'node:path';
import {parseArgs} from 'node:util';

export function checkoutUsage(script) {
  return `Usage: node tooling/payment-ui/${script} --origin ORIGIN --uid APPLICATION_UID ` +
    '--checkpoint FILE --billing-file FILE\n' +
    'Use an authorized test server and synthetic billing details only. Files must be inside the workspace.\n' +
    'The private checkpoint must contain origin, uid, and intake_access_token matching the selected target.';
}

export function checkoutOrigin(value) {
  const invalid = () => new Error('--origin must be an HTTPS origin (HTTP is allowed only on loopback)');
  if (typeof value !== 'string' || /[\u0000-\u0020\u007f\s\\?#@]/.test(value)) throw invalid();
  let url;
  try { url = new URL(value); } catch { throw invalid(); }
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) ||
      url.username || url.password || url.pathname !== '/') throw invalid();
  return url.origin;
}

function privateJson(root, filename, label) {
  let resolved;
  try { resolved = fs.realpathSync(path.resolve(root, filename)); }
  catch { throw new Error(`${label} must be an existing JSON file inside the workspace`); }
  const relative = path.relative(root, resolved);
  if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative))
    throw new Error(`${label} must be inside the workspace (including symlink targets)`);
  try { return JSON.parse(fs.readFileSync(resolved, 'utf8')); }
  catch { throw new Error(`${label} must contain valid JSON`); }
}

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const nonempty = value => typeof value === 'string' && value.trim().length > 0;

export function readCheckoutConfig(args, workspace = process.cwd()) {
  let values;
  try {
    ({values} = parseArgs({args, options: {
      help: {type: 'boolean', short: 'h'},
      origin: {type: 'string'}, uid: {type: 'string'},
      checkpoint: {type: 'string'}, 'billing-file': {type: 'string'},
    }, allowPositionals: false, strict: true}));
  } catch {
    throw new Error('Invalid checkout arguments; use --help for the required options');
  }
  if (values.help) return null;
  for (const name of ['origin', 'uid', 'checkpoint', 'billing-file']) {
    if (!nonempty(values[name])) throw new Error(`--${name} is required; there is no saved default`);
  }
  const origin = checkoutOrigin(values.origin);
  const root = fs.realpathSync(workspace);
  const checkpoint = privateJson(root, values.checkpoint, 'Checkpoint');
  if (!object(checkpoint) || checkpoint.uid !== values.uid ||
      (checkpoint.latest?.uid !== undefined && checkpoint.latest.uid !== values.uid))
    throw new Error('Checkpoint does not match --uid');
  if (!nonempty(checkpoint.origin) || checkoutOrigin(checkpoint.origin) !== origin)
    throw new Error('Checkpoint origin must match --origin; unbound legacy checkpoints must be reviewed first');
  if (!nonempty(checkpoint.intake_access_token))
    throw new Error('Checkpoint must contain an intake_access_token');
  const billing = privateJson(root, values['billing-file'], 'Billing file');
  if (!object(billing) || Object.keys(billing).some(key => !['name', 'email', 'address'].includes(key)) ||
      !nonempty(billing.name) || !nonempty(billing.email) || !object(billing.address) ||
      !['line1', 'city', 'state', 'postal_code'].every(key => nonempty(billing.address[key])) ||
      Object.entries(billing.address).some(([key, value]) =>
        !['line1', 'line2', 'city', 'state', 'postal_code', 'country'].includes(key) || !nonempty(value)))
    throw new Error('Billing file requires name, email, and address (line1, city, state, postal_code); no card fields');
  return {root, origin, expectedUid: values.uid, checkpoint, billing};
}

export function checkoutConfigOrExit(script) {
  try {
    const config = readCheckoutConfig(process.argv.slice(2));
    if (config) return config;
    console.log(checkoutUsage(script));
    process.exit(0);
  } catch (error) {
    console.error(`Checkout configuration: ${error.message}`);
    process.exit(2);
  }
}
