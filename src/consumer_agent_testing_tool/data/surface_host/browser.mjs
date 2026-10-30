// Restricted Playwright MCP facade. No caller-provided selectors or JavaScript.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {BrowserDiagnostics} from './browser_diagnostics.mjs';
import {BrowserRequests} from './browser_requests.mjs';
import {ContainerBrowser} from './browser_worker.mjs';
import {BrowserHTTP} from './browser_http.mjs';
import {BrowserNetwork} from './browser_network.mjs';

// Each operation's own arguments: required, then optional. The agent's tool schema
// mirrors this table (browser_agent.OPERATIONS), so most mistakes never reach here.
export const OPERATIONS = {
  snapshot: [[], ['offset']], navigate: [['url'], []], tab: [['tab'], []],
  click: [['ref', 'generation'], []], type: [['ref', 'generation', 'text'], []],
  select: [['ref', 'generation', 'value'], []], press: [['ref', 'generation', 'key'], []],
  scroll: [['ref', 'generation'], []], wait: [['ref', 'generation', 'state'], []],
  upload: [['file'], ['ref', 'generation']], download: [['ref', 'generation'], []],
  dialog: [['accept'], ['text']], takeover: [[], []],
  // P3.4: a page-registered WebMCP tool, only when the policy sets webmcp (see pageTool).
  webmcp: [['tool', 'params'], []],
};
const ARGUMENTS = new Set(Object.values(OPERATIONS).flat(2));
const TYPES = {offset: 'integer', generation: 'integer', accept: 'boolean', params: 'object'};
const KEYS = ['Enter', 'Tab', 'Escape', 'Space', 'ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'];
const HINTS = {key: KEYS.join(', '), state: 'visible or hidden', accept: 'true or false',
  tool: 'a name from the snapshot\'s WebMCP line', params: 'a JSON object'};
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
// A page tool's arguments, as the backend's transport bounds them, and its output in one observation.
const PAGE_TOOL_ARGUMENTS = 64 * 1024, PAGE_TOOL_OUTPUT = 8000;
// Native Chromium shares the host network. Without a proxy this WebRTC policy gathers no
// UDP candidates, so STUN cannot reach undeclared hosts. A SharedWorker runs outside every
// page's request interception, so the constructor is disabled. WebRTC TCP relays and
// WebSockets opened by dedicated workers remain open and are labeled in reports.
export const NATIVE_ARGS = ['--disable-background-networking', '--webrtc-ip-handling-policy=disable_non_proxied_udp',
  '--disable-blink-features=SharedWorker'];
const MASK = '[password masked]';
const PLACEHOLDER = '<!doctype html><title></title>';
const INTERRUPTED = Symbol('interrupted by a JavaScript dialog');
// Ordinary outcomes (policy denials, invalid requests) are returned to the agent as
// observations. Contract violations and infrastructure failures still throw.
const refusal = (status, message) => Object.assign(Error(message), {observable: status});
const clip = (value, limit) => typeof value === 'string' && value.length > limit
  ? value.slice(0, limit) + '…' : value;
const later = (ms, value) => new Promise(resolve => setTimeout(() => resolve(value), ms));
const dialogName = type => (/^[aeiou]/.test(type) ? 'An ' : 'A ') + type + ' dialog';

function usage(kind) {
  const [required, optional] = OPERATIONS[kind];
  const named = key => HINTS[key] ? `${key} (${HINTS[key]})` : key;
  const parts = [...required.map(named), ...optional.map(key => named(key) + ' [optional]')];
  return `kind=${kind} takes ${parts.length ? parts.join(', ') : 'no other arguments'}`;
}

// Argument mistakes are ordinary agent errors, refused before anything runs. An operation
// or argument name the facade does not define is a contract violation and still throws.
export function validate(action) {
  const fields = Object.hasOwn(OPERATIONS, action.kind) ? OPERATIONS[action.kind] : null;
  const names = Object.keys(action).filter(key => key !== 'kind');
  if (!fields || names.some(key => !ARGUMENTS.has(key)))
    throw Error('Browser operation or argument is not exposed by this facade');
  const [required, optional] = fields, problems = [];
  const extra = names.filter(key => !required.includes(key) && !optional.includes(key));
  if (extra.length) problems.push(`does not take ${extra.join(', ')}`);
  const missing = required.filter(key => action[key] === undefined);
  if (missing.length) problems.push(`requires ${missing.join(', ')}`);
  for (const key of names.filter(key => !extra.includes(key))) {
    const type = TYPES[key] ?? 'string', value = action[key];
    // An integer too large to be exact is still out of range, which the operation reports.
    if (type === 'integer' ? !Number.isInteger(value) || value < 0 : type === 'object' ? !plain(value)
        : typeof value !== type)
      problems.push(`${key} must be ${type === 'integer' ? 'a nonnegative integer'
        : type === 'object' ? 'a JSON object' : 'a ' + type}`);
  }
  if (action.kind === 'upload' && (action.ref === undefined) !== (action.generation === undefined))
    problems.push('ref and generation go together');
  if (problems.length)
    throw refusal('invalid-action', `Invalid kind=${action.kind} arguments: ${problems.join('; ')}. ` +
      `${usage(action.kind)}. Nothing was done.`);
}

// The pinned serializer's text pipeline for a field: its value whitespace-normalized,
// then any CSS generated content (::before, ::after) appended, then YAML quoting.
const normalized = text => text.replace(/[\u200b\u00ad]/g, '').trim().replace(/\s+/g, ' ');
const ESCAPES = {'\\': '\\', '"': '"', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t'};
function scalar(text) {
  if (text.length < 2 || !text.startsWith('"') || !text.endsWith('"')) return text;
  return text.slice(1, -1).replace(/\\(x[0-9a-fA-F]{2}|.)/g, (escape, code) =>
    code.length === 3 ? String.fromCharCode(parseInt(code.slice(1), 16)) : ESCAPES[code] ?? escape);
}

// One emitted snapshot line. References come only from a node's own attribute list,
// never from page-controlled names or text that merely contain "[ref=...]".
export function parseLine(line) {
  const match = /^( *)- (.*)$/.exec(line);
  if (!match) return null;
  let rest = match[2], key = '', end;
  if (rest.startsWith("'")) {
    let index = 1;
    while (index < rest.length) {
      if (rest[index] === "'") {
        if (rest[index + 1] !== "'") break;
        index++;
      }
      key += rest[index++];
    }
    end = index;
    rest = rest.slice(index + 1);
  } else {
    const split = rest.search(/:( |$)/);
    key = split < 0 ? rest : rest.slice(0, split);
    end = key.length;
    rest = split < 0 ? '' : rest.slice(split);
  }
  const node = /^([a-z][\w-]*)(?: "(?:[^"\\]|\\.)*")?((?: \[[^\]]*\])*)$/.exec(key);
  const attributes = node?.[2] ?? '';
  return {indent: match[1].length, role: node?.[1] ?? null, attributes,
    // Where the node's own attribute list starts in the emitted line.
    at: match[1].length + 2 + end - attributes.length,
    ref: /\[ref=([^\]]+)\]/.exec(attributes)?.[1] ?? null,
    value: rest.startsWith(': ') ? rest.slice(2) : null};
}

// Focus moves ([active]) are not changes to a target.
function steady(line) {
  const node = parseLine(line);
  if (!node?.role || node.role === 'text' || !node.attributes.includes(' [active]')) return line;
  return line.slice(0, node.at) + node.attributes.replace(' [active]', '') +
    line.slice(node.at + node.attributes.length);
}

export function references(text) {
  const refs = new Set();
  for (const line of text.split('\n')) {
    const node = parseLine(line);
    if (node?.ref && node.role !== 'text') refs.add(node.ref);
  }
  return refs;
}

// The target's own line and descendants, independent of where the target sits.
export function subtree(text, ref) {
  const lines = text.split('\n');
  const start = lines.findIndex(line => parseLine(line)?.ref === ref);
  if (start < 0) return null;
  const depth = lines[start].search(/\S/), rows = [lines[start]];
  for (let index = start + 1; index < lines.length; index++) {
    if (lines[index].trim() && lines[index].search(/\S/) <= depth) break;
    rows.push(lines[index]);
  }
  return rows.map(line => steady(line).slice(depth)).join('\n');
}

export function maskValues(text, values) {
  const secrets = [...values].map(normalized).filter(Boolean);
  if (!secrets.length) return text;
  const hidden = value => {
    const shown = scalar(value);
    return secrets.some(secret => shown === secret || shown.startsWith(secret + ' '));
  };
  const masked = (line, node) => line.slice(0, line.length - node.value.length) + MASK;
  let owner = null;
  return text.split('\n').map(line => {
    const node = parseLine(line);
    if (!node) return line;
    if (node.role === 'textbox') {
      owner = node.value === null ? node.indent : null;
      return node.value !== null && hidden(node.value) ? masked(line, node) : line;
    }
    if (owner !== null && node.indent <= owner) owner = null;
    if (owner !== null && node.role === 'text' && node.indent === owner + 2 && node.value !== null && hidden(node.value))
      return masked(line, node);
    return line;
  }).join('\n');
}

export class RestrictedBrowser extends BrowserNetwork {
  constructor(chromium, authorize, root, observe = () => {}, mcp, executablePath) {
    super();
    this.chromium = chromium;
    this.authorize = authorize;
    this.generation = 0;
    this.refs = new Set();
    this.takeover = false;
    this.tabs = new Map();
    this.tabIds = new WeakMap();
    this.openers = new WeakMap();
    this.dialogs = new Map();
    this.choosers = new Map();
    this.denials = new WeakMap();
    this.unclaimed = [];
    this.visited = new WeakSet();
    this.deferred = [];
    this.opening = new Set();
    this.tabEvents = [];
    this.handled = [];
    this.autoAnswers = 0;
    this.wakers = new Set();
    this.root = root;
    this.observe = observe;
    this.mcp = mcp;
    this.executablePath = executablePath;
    this.tabSerial = 0;
    this.lastText = '';
    this.cuts = new Set();
  }
  async open(policy) {
    this.policy = policy;
    this.limit = policy.snapshot_max_chars ?? 20000;
    if (!Number.isSafeInteger(this.limit) || this.limit < 1000) throw Error('Invalid snapshot limit');
    if (!['agent', 'accept', 'dismiss'].includes(policy.dialogs ?? 'agent')) throw Error('Invalid dialog policy');
    if (policy.scenario_generation_floor !== undefined) {
      if (!policy.scenario_request_ledger || !Number.isSafeInteger(policy.scenario_generation_floor) ||
          policy.scenario_generation_floor < 0) throw Error('Invalid scenario generation floor');
      this.generation = policy.scenario_generation_floor;
    }
    if (policy.scenario_storage_state && !policy.scenario_private_storage)
      throw Error('Storage restoration requires the private scenario capability');
    if (policy.task_storage_state && !policy.task_private_storage)
      throw Error('Task storage restoration requires the private task capability');
    this.diagnostics = new BrowserDiagnostics(this.root, policy.diagnostics || {enabled: false},
      policy.diagnostics_directory);
    if (policy.worker) {
      this.http = new BrowserHTTP(policy, this.observe);
      this.worker = new ContainerBrowser(this.root, policy.worker, this.observe);
      this.browser = await this.worker.launch(this.chromium);
    } else {
      this.browser = await this.chromium.launch({headless: true, executablePath: this.executablePath,
        tracesDir: this.diagnostics.work, args: NATIVE_ARGS});
    }
    this.context = await this.browser.newContext({serviceWorkers: 'block',
      ...(policy.scenario_storage_state ? {storageState: policy.scenario_storage_state} : {}),
      ...(policy.task_storage_state ? {storageState: policy.task_storage_state} : {})});
    await this.diagnostics.start(this.context);
    this.context.setDefaultTimeout(policy.timeout_seconds * 1000);
    if (policy.scenario_request_ledger) this.requests = new BrowserRequests(this);
    await this.context.routeWebSocket('**/*', socket => {
      this.observe({kind: 'network_denied', enforcement: 'playwright-websocket-route',
        reason: 'Page WebSocket access is disabled', url: socket.url()});
      socket.close();
    });
    await this.context.route('**/*', route => this.admit(route));
    this.context.on('page', page => this.adopt(page));
    this.page = await this.context.newPage();
    const directory = path.join(this.root, '.cache', 'surface-host');
    fs.mkdirSync(directory, {recursive: true});
    await this.mcp.connect(this.context, directory, policy.timeout_seconds * 1000);
    await this.navigate(policy.url);
    return this.snapshot();
  }
  async admit(route) {
    const request = route.request(), url = new URL(request.url());
    const rule = this.ruleFor(url, request.method());
    let frame = null;
    try { frame = request.frame(); } catch {}
    const page = frame?.page() ?? null, main = request.isNavigationRequest() && (!frame || frame === page.mainFrame());
    const deny = (enforcement, reason) => {
      this.observe({kind: 'network_denied', enforcement, reason, url: request.url(), method: request.method()});
      // Lets the tab that shows the resulting error page say it was a policy block. A new
      // tab's first navigation has no page yet; that tab's error page claims it later.
      if (main) {
        const row = {url: clip(request.url(), 500), reason};
        if (page) this.denials.set(page, row);
        else this.unclaimed.push(row);
      }
      return route.abort();
    };
    if (main && (!page || !this.redirectGuardReady.has(page))) {
      // A popup's first navigation starts before Playwright exposes its page, and a new
      // tab can navigate before its redirect guard is installed. Commit an inert placeholder
      // instead; once the guard exists the trusted host reloads the page through the normal
      // policy checks below. Nothing reaches the network first; the opener, the fragment
      // and the history length are kept.
      if (request.method() !== 'GET')
        return deny('popup-first-request', 'A new tab may open only with a GET navigation');
      this.deferred.push({url: request.url(), page});
      if (page) this.track(this.release(page));
      return route.fulfill({status: 200, contentType: 'text/html', body: PLACEHOLDER});
    }
    if ((!rule && !this.requests) || !this.policy.origins.includes(url.origin) || url.username || url.password)
      return deny('playwright-route', 'Request has no destination rule');
    try {
      if (!frame || !this.redirectGuardReady.has(frame.page()))
        throw Error('Response guard was not active before this request started');
      await this.guardRequestFrame(frame);
    } catch (error) {
      return deny('redirect-guard-readiness', error.message);
    }
    if (this.requests) return this.requests.admit(route);
    if (rule.effect === 'write') {
      try {
        await this.authorize('browser_request', {
          url: request.url(), method: request.method(), body: request.postData() || '',
          rule: rule.id,
        });
      } catch { return route.abort(); }
    }
    try { return await this.continueRequest(route); }
    catch (error) {
      this.observe({kind: 'network_denied', enforcement: 'host-pinned-http-broker',
        reason: error.message, url: request.url(), method: request.method()});
      return route.abort();
    }
  }
  adopt(page) {
    const id = String(++this.tabSerial);
    this.tabs.set(id, page);
    this.tabIds.set(page, id);
    page.on('close', () => this.closed(page, id));
    page.on('dialog', dialog => {
      // Never answered silently: the agent responds, or the declared policy default does.
      const row = {page, dialog, tab: id, type: dialog.type(), message: dialog.message(),
        default_value: dialog.defaultValue()};
      this.dialogs.set(page, row);
      for (const wake of this.wakers) wake();
      this.observe({kind: 'browser_dialog', event: 'opened', tab: id, type: row.type,
        message: clip(row.message, 2000)});
    });
    page.on('filechooser', chooser => {
      this.choosers.set(page, {chooser, tab: id, multiple: chooser.isMultiple(), cleared: false});
      this.observe({kind: 'browser_file_chooser', event: 'opened', tab: id, multiple: chooser.isMultiple()});
    });
    page.on('download', download => {if (!this.downloadPending) download.cancel();});
    page.on('framenavigated', frame => {
      if (frame !== page.mainFrame() || frame.url().startsWith('chrome-error:')) return;
      this.denials.delete(page);
      if (frame.url() !== 'about:blank') this.visited.add(page);
    });
    this.watchFrames(page);
    this.track((async () => {
      const opener = await page.opener().catch(() => null);
      this.openers.set(page, opener ? this.tabIds.get(opener) ?? null : null);
      if (this.tabs.size > 1) this.tabEvents.push({event: 'opened', tab: id, opener: this.openers.get(page)});
      await this.release(page);
    })().catch(() => page.close().catch(() => {})));
  }
  // New tabs finish their guarded first load before observations announce them.
  track(work) {
    const settled = work.catch(() => {});
    this.opening.add(settled);
    settled.finally(() => this.opening.delete(settled));
  }
  // Reloads a held first navigation once the page's redirect guard exists and the
  // placeholder has committed. Fails closed: a page without a guard is closed by adopt().
  async release(page) {
    await this.guardRedirects(page);
    const deadline = Date.now() + this.policy.timeout_seconds * 1000;
    while (this.deferred.length) {
      const url = page.url().split('#')[0];
      const index = this.deferred.findIndex(row => row.url === url && (row.page ?? page) === page);
      if (index >= 0) {
        this.deferred.splice(index, 1);
        try {
          // Linux Chromium can drop a reload issued while the placeholder is still loading.
          await page.waitForLoadState('domcontentloaded', {timeout: Math.max(1, deadline - Date.now())})
            .catch(() => {});
          await page.reload({waitUntil: 'domcontentloaded', timeout: this.policy.timeout_seconds * 1000});
        } catch (error) {
          this.observe({kind: 'browser_tab_load', tab: this.tabIds.get(page), status: 'failed',
            reason: clip(error.message, 300)});
        }
        return;
      }
      // The placeholder may not have committed yet; on a busy machine that takes seconds. Wait
      // only while a held navigation can still be this page's: one recorded for it, or one
      // recorded before any page existed while this one is still blank.
      const blank = url === '' || url === 'about:blank', left = deadline - Date.now();
      if (left <= 0 || !this.deferred.some(row => row.page === page || (!row.page && blank))) return;
      await page.waitForEvent('framenavigated', {predicate: frame => frame === page.mainFrame(),
        timeout: left}).catch(() => {});
    }
  }
  redirectDenied(page, url) {
    this.denials.set(page, {url: clip(url, 500), reason: 'Redirect destination is not an explicitly allowed read'});
  }
  closed(page, id) {
    this.tabs.delete(id);
    this.dialogs.delete(page);
    this.choosers.delete(page);
    this.deferred = this.deferred.filter(row => row.page !== page);
    this.tabEvents.push({event: 'closed', tab: id});
    if (page === this.page) {
      // Playwright MCP also moves to another tab; select ours explicitly before acting.
      this.page = [...this.tabs.values()].at(-1) ?? null;
      this.reselect = true;
    }
  }
  async continueRequest(route) {
    if (this.http) return this.http.fulfill(route);
    return route.continue();
  }
  async storageState() {
    if (!this.policy.scenario_request_ledger || !this.policy.scenario_private_storage)
      throw Error('Private scenario storage capability is unavailable');
    const state = await this.context.storageState();
    // The owned fixture issues a new session cookie at /portal. Never replay
    // an old authentication credential, or restore a different origin.
    return {cookies: [], origins: state.origins.filter(row => this.policy.origins.includes(row.origin))};
  }
  async taskStorageState() {
    if (!this.policy.task_private_storage) throw Error('Private task storage capability is unavailable');
    const state = await this.context.storageState();
    const hosts = new Set(this.policy.origins.map(origin => new URL(origin).hostname));
    return {
      cookies: state.cookies.filter(row => hosts.has(row.domain.replace(/^\./, ''))),
      origins: state.origins.filter(row => this.policy.origins.includes(row.origin)),
    };
  }
  // Policy is checked before anything executes; a denied destination is never loaded.
  destination(url) {
    let destination;
    try { destination = new URL(url); } catch { throw refusal('invalid-action', 'Navigation needs an absolute URL'); }
    if (!this.policy.origins.includes(destination.origin) || destination.username || destination.password)
      throw refusal('denied', 'Navigation destination denied');
    if (!this.requests && !this.ruleFor(destination, 'GET'))
      throw refusal('denied', 'Navigation destination has no read rule');
  }
  async go(url) {
    await this.guardRedirects(this.page);
    await this.interruptible(this.mcp.call('browser_navigate', {url}));
    this.refs.clear();
  }
  async navigate(url) {
    this.destination(url);
    await this.go(url);
  }
  // A JavaScript dialog suspends its page, and every page sharing its event loop (a
  // same-origin popup and its opener). Playwright MCP then waits on such a page until the
  // dialog is answered, which only the agent or the declared default can do. Host waits
  // therefore end when a dialog opens; an abandoned call settles in the background.
  async interruptible(promise, ms) {
    let wake;
    const interrupted = new Promise(resolve => { wake = () => resolve(INTERRUPTED); this.wakers.add(wake); });
    promise.catch(() => {});
    try { return await Promise.race([promise, interrupted, ...(ms ? [later(ms, INTERRUPTED)] : [])]); }
    finally { this.wakers.delete(wake); }
  }
  // Trusted host evaluation, never agent JavaScript: password values are secrets the
  // user typed or the page filled, so they stay out of the model view and evidence.
  async passwords() {
    const values = new Set();
    for (const frame of this.page.frames()) {
      const found = await this.interruptible(frame.locator('input[type="password" i]')
        .evaluateAll(inputs => inputs.map(input => input.value)), this.policy.timeout_seconds * 1000)
        .catch(error => {
          if (frame.isDetached()) return [];
          throw error;
        });
      // Fail closed: an unreadable frame could hold a password the snapshot shows.
      if (found === INTERRUPTED) throw Error('Password fields could not be inspected');
      for (const value of found) if (value) values.add(value);
    }
    return values;
  }
  async pageText() {
    const before = await this.passwords();
    const text = await this.interruptible(this.mcp.snapshot());
    if (text === INTERRUPTED) throw Error('A JavaScript dialog opened during the snapshot');
    for (const value of await this.passwords()) before.add(value);
    return maskValues(text, before);
  }
  waiting() {
    return this.dialogs.size > 0 || this.choosers.get(this.page)?.cleared === false;
  }
  describe(row) {
    return {type: row.type, message: clip(row.message, 2000),
      ...(row.type === 'prompt' ? {default_value: clip(row.default_value, 500)} : {})};
  }
  async answer(row, accept, text, by) {
    const promptText = row.type === 'prompt' && accept ? text ?? row.default_value : undefined;
    // Playwright MCP answers only its current tab's dialog, then renders every tab's header,
    // which another open dialog could stall. Otherwise the trusted host answers the same
    // Playwright dialog directly; MCP clears its own record when the dialog closes.
    const backend = row.page === this.page && !this.reselect && this.dialogs.size === 1;
    this.dialogs.delete(row.page);
    const answered = backend && await this.interruptible(this.mcp.call('browser_handle_dialog',
      {accept, ...(promptText === undefined ? {} : {promptText})})).then(result => result !== INTERRUPTED, () => false);
    if (!answered) await (accept ? row.dialog.accept(promptText) : row.dialog.dismiss()).catch(() => {});
    const handled = {type: row.type, message: clip(row.message, 2000),
      response: accept ? 'accepted' : 'dismissed', by};
    this.handled.push(handled);
    this.observe({kind: 'browser_dialog', event: 'handled', tab: row.tab, ...handled});
  }
  // Control follows a dialog to its tab, as a browser brings that tab forward; announced.
  focusDialog(row) {
    this.page = row.page;
    this.reselect = true;
    this.tabEvents.push({event: 'selected', tab: row.tab, reason: 'javascript-dialog'});
  }
  // What the agent must handle first. A JavaScript dialog in any tab blocks: the current
  // tab's, else the oldest elsewhere, whose tab becomes current. A file chooser in the
  // current tab is reported beside the page.
  async modal() {
    // A declared default answers dialogs, but a page that keeps raising them is handed
    // to the agent after ten answers in one action rather than answered forever.
    while (this.dialogs.size) {
      const dialog = this.dialogs.get(this.page) ?? this.dialogs.values().next().value;
      if ((this.policy.dialogs ?? 'agent') === 'agent' || this.autoAnswers >= 10) {
        if (dialog.page !== this.page) this.focusDialog(dialog);
        return {blocking: true, ...this.describe(dialog)};
      }
      this.autoAnswers++;
      await this.answer(dialog, this.policy.dialogs === 'accept', undefined, 'policy');
    }
    const chooser = this.choosers.get(this.page);
    if (!chooser) return null;
    if (!chooser.cleared) {
      // The facade keeps the chooser and its modal semantics; release Playwright MCP,
      // which otherwise refuses every later tool while its file chooser is pending.
      await this.interruptible(this.mcp.call('browser_file_upload', {})).catch(() => {});
      chooser.cleared = true;
    }
    return {blocking: false, type: 'file-chooser', multiple: chooser.multiple,
      instruction: Object.keys(this.policy.private_uploads ?? {}).length
        ? 'Use kind=upload with an approved file, or kind=dialog with accept=false to cancel.'
        : 'No file is approved for this task; use kind=dialog with accept=false to cancel.'};
  }
  // New tabs finish their guarded first load before they are announced; a dialog, which
  // can hold that load, is surfaced instead of waited out.
  async sync() {
    const deadline = Date.now() + this.policy.timeout_seconds * 1000;
    while (this.opening.size && !this.dialogs.size && Date.now() < deadline)
      await this.interruptible(Promise.allSettled([...this.opening]), Math.max(1, deadline - Date.now()));
    if (!this.page) throw Error('No task-owned browser tab remains open');
    await this.select();
  }
  // Playwright MCP acts on and snapshots its current tab, which must be ours. Selecting
  // renders every tab's header there, so never while a dialog is open. True when in step.
  async select() {
    if (this.reselect && !this.dialogs.size) {
      this.reselect = false;
      try {
        if (await this.interruptible(this.mcp.call('browser_tabs',
            {action: 'select', index: this.context.pages().indexOf(this.page)})) === INTERRUPTED) this.reselect = true;
      } catch (error) {
        this.reselect = true;
        throw error;
      }
    }
    return !this.reselect;
  }
  async settle() {
    await this.sync();
    await this.requests?.drain();
    // A round in which the declared default answered a dialog made progress; the
    // per-action answer limit bounds those rounds.
    for (let stalled = 0, failures = 0; stalled < 5;) {
      const answered = this.autoAnswers, modal = await this.modal();
      if (modal?.blocking) return {modal};
      try {
        if (await this.select()) {
          const text = await this.pageText();
          if (!this.waiting()) return {text, modal};
        }
      } catch (error) {
        // A dialog (in any tab) that opened mid-capture is handled next round; frame
        // churn is retried once.
        if (!this.waiting() && ++failures > 1) throw error;
        await later(100);
      }
      if (this.autoAnswers === answered) stalled++;
    }
    throw Error('Browser modal state did not settle');
  }
  async title(page) {
    if (this.dialogs.has(page)) return null;
    return clip(await Promise.race([page.title().catch(() => ''), later(1000, '')]), 200);
  }
  async tabList() {
    return Promise.all([...this.tabs].map(async ([id, page]) => {
      const dialog = this.dialogs.get(page), error = page.url().startsWith('chrome-error:');
      // A new tab denied before it had a page shows an error page as its first document,
      // which claims that denial.
      if (error && !this.denials.has(page) && !this.visited.has(page) && this.unclaimed.length)
        this.denials.set(page, this.unclaimed.shift());
      const blocked = error ? this.denials.get(page) : null;
      return {id, url: clip(page.url(), 500), title: await this.title(page), active: page === this.page,
        opener: this.openers.get(page) ?? null,
        ...(dialog && page !== this.page ? {modal: this.describe(dialog)} : {}),
        ...(blocked ? {blocked} : {})};
    }));
  }
  async notices() {
    const result = {tabs: await this.tabList()};
    if (this.tabEvents.length) result.tab_events = this.tabEvents.splice(0);
    if (this.handled.length) result.dialogs_handled = this.handled.splice(0);
    return result;
  }
  window(offset) {
    const text = this.lastText;
    if (offset === 0 && text.length <= this.limit) {
      for (const ref of references(text)) this.refs.add(ref);
      return {text};
    }
    let end = Math.min(text.length, offset + this.limit);
    if (end < text.length) {
      const cut = text.lastIndexOf('\n', end);
      if (cut > offset) end = cut + 1;
    }
    const slice = text.slice(offset, end);
    // A slice that starts inside a long line shows the rest of it as page text, which can
    // look like a node. References come only from lines shown from their start.
    const inside = offset > 0 && text[offset - 1] !== '\n', first = slice.indexOf('\n');
    for (const ref of references(!inside ? slice : first < 0 ? '' : slice.slice(first + 1))) this.refs.add(ref);
    if (end < text.length) this.cuts.add(end);
    const range = {offset, end, total_chars: text.length, ...(end < text.length ? {next_offset: end} : {})};
    const marker = end < text.length
      ? `[Snapshot truncated: characters ${offset}-${end} of ${text.length} shown. Request {"kind": "snapshot", "offset": ${end}} for more of generation ${this.generation}.]`
      : `[End of snapshot generation ${this.generation}: characters ${offset}-${end} of ${text.length} shown.]`;
    return {text: slice.replace(/\n$/, '') + '\n' + marker, truncated: range};
  }
  async emit(text, status = {}) {
    this.lastText = text;
    this.shown = this.page;
    this.generation++;
    this.refs = new Set();
    this.cuts = new Set();
    const view = this.window(0);
    return {generation: this.generation, ...status, text: view.text,
      ...(view.truncated ? {truncated: view.truncated} : {}), ...await this.notices(),
      fidelity: 'Playwright MCP accessibility snapshot; Muse serializer unknown',
      browser_version: this.browser.version(), backend: this.mcp.info};
  }
  observation(state, status = {}) {
    if (state.modal?.blocking) return this.suspended(state.modal, status);
    const {blocking, ...modal} = state.modal ?? {};
    return this.emit(state.text, {...status, ...(state.modal ? {modal} : {})});
  }
  async snapshot(status = {}) {
    return this.observation(await this.settle(), status);
  }
  // A JavaScript dialog suspends the page, so no snapshot exists until it is answered. An
  // action refused or failed because of it keeps that status, so it is counted as such.
  async suspended(modal, status = {}) {
    const {blocking, ...dialog} = modal;
    return {status: status.status ?? 'dialog-open', ...(status.message ? {message: status.message} : {}),
      ...(status.page_tool ? {page_tool: status.page_tool} : {}),
      generation: this.generation, modal: dialog,
      instruction: dialog.type === 'prompt'
        ? 'Respond with kind=dialog and accept true or false; include text to answer the prompt.'
        : 'Respond with kind=dialog and accept true or false before any other action.',
      ...await this.notices()};
  }
  // Nothing ran: an open dialog must be answered first.
  blocked(modal, message = `${dialogName(modal.type)} is open; nothing was done. Respond with kind=dialog first.`) {
    return this.suspended(modal, {status: 'invalid-action', message});
  }
  continuation(offset) {
    if (!this.lastText || offset >= this.lastText.length)
      throw refusal('invalid-action', `Offset is beyond the end of snapshot generation ${this.generation}`);
    // Continue exactly where an emitted slice ended (a line longer than the limit is cut
    // inside); any other offset is aligned to its line start. The generation is unchanged.
    const start = this.cuts.has(offset) ? offset : this.lastText.lastIndexOf('\n', offset - 1) + 1;
    const view = this.window(start);
    return {generation: this.generation, text: view.text, ...(view.truncated ? {truncated: view.truncated} : {})};
  }
  async action(action) {
    if (this.busy) throw Error('A browser operation is already in progress');
    this.busy = true;
    this.autoAnswers = 0;
    try { return await this.dispatch(action); }
    catch (error) {
      if (!error.observable) throw error;
      // Nothing executed; the agent's latest snapshot and references stay current.
      return {status: error.observable, message: error.message, generation: this.generation};
    }
    finally { this.busy = false; }
  }
  approved(name) {
    const declared = this.policy.private_uploads?.[name];
    if (!declared) throw refusal('denied', 'Upload fixture not approved');
    const file = fs.realpathSync(declared);
    if (!file.startsWith(this.root + path.sep) || fs.statSync(file).size > this.policy.max_file_bytes)
      throw Error('Upload fixture path or size denied');
    return this.worker ? {name: path.basename(file), mimeType: 'application/octet-stream',
      buffer: fs.readFileSync(file)} : file;
  }
  async dispatch(action) {
    validate(action);
    if (action.kind === 'takeover') {this.takeover = true; return {status: 'human-takeover'};}
    if (this.takeover) throw Error('Agent control paused for human takeover');
    if (action.kind === 'webmcp' && this.policy.webmcp !== true)
      throw refusal('invalid-action', 'kind=webmcp is not available: this browser task has no WebMCP page tools. ' +
        'Nothing was done.');
    // Offset 0 (or none) takes a fresh snapshot; a positive offset continues the current one.
    if (action.kind === 'snapshot') return action.offset ? this.continuation(action.offset) : this.snapshot();
    if (action.kind === 'dialog') return this.respond(action);
    if (action.kind === 'press' && !KEYS.includes(action.key))
      throw refusal('invalid-action', `Key not supported; use one of ${KEYS.join(', ')}`);
    if (action.kind === 'wait' && !['visible', 'hidden'].includes(action.state))
      throw refusal('invalid-action', 'Wait needs an explicit visible or hidden state');
    const files = action.kind === 'upload' ? this.approved(action.file) : null;
    await this.sync();
    // A declared dialog default answers here; otherwise the agent must respond first.
    const modal = await this.modal();
    if (modal?.blocking) return this.blocked(modal);
    const chooser = this.choosers.get(this.page);
    if (chooser && action.kind !== 'upload')
      throw refusal('invalid-action', 'A file chooser is open; use kind=upload with an approved file, or kind=dialog with accept=false to cancel');
    if (chooser) return this.attempt('upload', async () => {
      await chooser.chooser.setFiles(files);
      this.choosers.delete(this.page);
      this.observe({kind: 'browser_file_chooser', event: 'uploaded', tab: chooser.tab, file: action.file});
    });
    if (action.kind === 'webmcp') return this.pageTool(action);
    if (action.kind === 'navigate') {
      this.destination(action.url);
      return this.attempt('navigate', () => this.go(action.url));
    }
    if (action.kind === 'upload' && action.ref === undefined)
      throw refusal('invalid-action', 'Upload needs an element reference unless a file chooser is open');
    if (action.kind === 'tab') {
      if (!this.tabs.has(action.tab)) throw refusal('invalid-action', 'Tab does not belong to this task');
      this.page = this.tabs.get(action.tab);
      this.reselect = true;
      return this.attempt('tab', async () => {
        await this.guardRedirects(this.page);
        await this.sync();
      });
    }
    // References belong to the tab they were shown in. If that tab closed or control moved,
    // nothing runs and the agent gets the current tab instead.
    if (this.page !== this.shown) return this.snapshot({status: 'stale-reference',
      message: 'Stale browser reference: the current tab changed and nothing was done. A fresh snapshot is included.'});
    // The latest emitted snapshot stays current: answer from it without a new generation.
    if (action.generation !== this.generation || !this.refs.has(action.ref))
      throw refusal('stale-reference', 'Stale or unexposed browser reference; nothing was done. ' +
        `Use a reference shown in snapshot generation ${this.generation}.`);
    // Validity is per target: the element must still render exactly as emitted.
    // Unrelated live text elsewhere on the page does not invalidate it.
    const current = await this.settle();
    if (current.modal?.blocking) return this.blocked(current.modal,
      `${dialogName(current.modal.type)} opened before the action ran; nothing was done. Respond with kind=dialog first.`);
    if (subtree(current.text, action.ref) !== subtree(this.lastText, action.ref))
      return this.observation(current, {status: 'stale-reference',
        message: 'Stale browser reference: the target changed and nothing was done. A fresh snapshot is included.'});
    return this.attempt(action.kind, () => this.perform(action, files));
  }
  // Runs one admitted operation. Whatever happens next is an observation of the page;
  // only a browser that can no longer produce a snapshot ends the task.
  async attempt(kind, run) {
    try { await run(); }
    catch (error) {
      if (error.observable) return this.snapshot({status: error.observable, message: error.message});
      // Upstream errors can contain generated code, selectors or paths; they stay in
      // developer-only evidence. The agent gets the outcome and the current page.
      return this.snapshot({status: 'action-failed',
        message: `The browser could not complete ${kind}; the page may have changed. A fresh snapshot is included.`});
    }
    return this.snapshot();
  }
  // P3.4, the "generic WebMCP page tools, not Muse" lane: one tool the current page registered, by a name from
  // the latest snapshot's WebMCP line. Like a click it acts on the page, so any request it sends meets the same
  // network rules and write approvals; its read_only hint is the page's claim, never trusted. The output is the
  // page's own text (untrusted, password values masked as in snapshots, bounded), beside a fresh snapshot, since
  // the page may have changed. The broker charges it to max_actions like any action.
  async pageTool(action) {
    if (this.page !== this.shown) return this.snapshot({status: 'stale-reference',
      message: 'Stale WebMCP tool: the current tab changed and nothing was done. A fresh snapshot is included.'});
    if (typeof action.tool !== 'string' || !this.mcp.pageToolNames?.has(action.tool))
      throw refusal('invalid-action', `The current page lists no WebMCP tool ${JSON.stringify(clip(action.tool, 100))}; ` +
        `use a name from the WebMCP line of snapshot generation ${this.generation}. Nothing was done.`);
    if (Object.hasOwn(action.params, '_meta') || JSON.stringify(action.params).length > PAGE_TOOL_ARGUMENTS)
      throw refusal('invalid-action', 'WebMCP tool params must be a JSON object of at most 64 KiB without _meta. ' +
        'Nothing was done.');
    let output = null, failure = {};
    try {
      const called = await this.interruptible(this.mcp.callPageTool(action.tool, action.params));
      if (called !== INTERRUPTED) output = called.text;
    } catch {
      // A page-reported error or a backend refusal: an ordinary outcome; the details stay in developer evidence.
      failure = {status: 'action-failed', message: `The page's WebMCP tool ${action.tool} failed; the page may ` +
        'have changed. A fresh snapshot is included.'};
    }
    if (output !== null) {
      try {
        for (const value of await this.passwords()) output = output.split(value).join(MASK);
        output = clip(output, PAGE_TOOL_OUTPUT);
      } catch { output = null; }  // Fail closed: unreadable password fields could hold what it echoes.
    }
    return this.snapshot({...failure, page_tool: {tool: action.tool, provenance: 'page-provided, untrusted',
      output}});
  }
  async perform(action, files) {
    const locator = this.page.locator(`aria-ref=${action.ref}`);
    const timeout = this.policy.timeout_seconds * 1000;
    // A dialog the operation raises ends the wait; the next observation reports it.
    const run = promise => this.interruptible(promise);
    if (action.kind === 'click') await run(this.mcp.call('browser_click', {target: action.ref}));
    if (action.kind === 'type') await run(this.mcp.call('browser_type', {target: action.ref, text: action.text}));
    if (action.kind === 'select')
      await run(this.mcp.call('browser_select_option', {target: action.ref, values: [action.value]}));
    if (action.kind === 'press' && await run(locator.focus({timeout})) !== INTERRUPTED)
      await run(this.mcp.call('browser_press_key', {key: action.key}));
    if (action.kind === 'scroll') await run(locator.scrollIntoViewIfNeeded({timeout}));
    if (action.kind === 'upload') await run(locator.setInputFiles(files, {timeout}));
    if (action.kind === 'download') {
      this.downloadPending = true;
      try {
        const started = await run(Promise.all([this.page.waitForEvent('download', {timeout}),
          this.mcp.call('browser_click', {target: action.ref})]));
        if (started !== INTERRUPTED) {
          const directory = fs.realpathSync(this.policy.download_directory);
          if (!directory.startsWith(this.root + path.sep)) throw Error('Download directory denied');
          const file = path.join(directory, crypto.randomUUID() + '.fixture');
          await started[0].saveAs(file);
          if (fs.statSync(file).size > this.policy.max_file_bytes) {
            fs.unlinkSync(file);
            throw refusal('denied', 'Downloaded fixture exceeds the size limit and was deleted');
          }
        }
      } finally {this.downloadPending = false;}
    }
    if (action.kind === 'wait') {
      try { await run(locator.waitFor({state: action.state, timeout})); }
      catch { throw refusal('action-failed', `The element did not become ${action.state} within the timeout`); }
    }
  }
  async respond(action) {
    // The agent answers its current tab's dialog itself; a declared default never preempts it.
    const dialog = this.dialogs.get(this.page);
    if (dialog) {
      if (action.text !== undefined && (dialog.type !== 'prompt' || !action.accept))
        throw refusal('invalid-action', 'Text applies only when accepting a prompt dialog');
      await this.answer(dialog, action.accept, action.text, 'agent');
      return this.snapshot();
    }
    // Releases a pending file chooser from Playwright MCP, applies a declared default to
    // other tabs' dialogs, and makes a remaining dialog's tab current: one the agent has
    // not read yet.
    const modal = await this.modal();
    if (modal?.blocking) return this.blocked(modal,
      `${dialogName(modal.type)} is open in tab ${this.tabIds.get(this.page)}; read it before answering. Nothing was answered.`);
    const chooser = this.choosers.get(this.page);
    if (chooser) {
      if (action.accept || action.text !== undefined)
        throw refusal('invalid-action', 'Use kind=upload with an approved file, or accept=false to cancel the file chooser');
      this.choosers.delete(this.page);
      this.observe({kind: 'browser_file_chooser', event: 'cancelled', tab: chooser.tab});
      return this.snapshot();
    }
    throw refusal('invalid-action', 'No JavaScript dialog or file chooser is open in this tab');
  }
  async close() {
    try { await this.diagnostics?.stop(); }
    finally {
      try { await this.mcp?.close(); }
      finally {
        this.http?.close();
        if (this.worker) await this.worker.close();
        else if (this.browser) await this.browser.close();
      }
    }
  }
  returnControl() {
    if (this.busy) throw Error('A browser operation is already in progress');
    this.takeover = false; this.refs.clear();
  }
}
