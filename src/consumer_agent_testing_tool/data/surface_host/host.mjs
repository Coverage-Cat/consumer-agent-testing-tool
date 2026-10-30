// Generic MCP/browser host. JSON lines on stdin/stdout are a PRIVATE coordinator channel.
// The embedded-app lane is generic MCP Apps host evidence (MCP Apps 2026-01-26), not Muse evidence.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import readline from 'node:readline';
import crypto from 'node:crypto';
import {RestrictedBrowser} from './browser.mjs';
import {BrowserMCP} from './browser_mcp.mjs';
import {BrowserNetwork} from './browser_network.mjs';
import {EmbeddedBrowser} from './embedded_browser.mjs';
import {clickEmbedded} from './embedded_click.mjs';

const [tooling, root] = process.argv.slice(2);
const require = createRequire(path.join(tooling, 'package.json'));
const load = name => import(pathToFileURL(require.resolve(name)).href);
const {Client, StreamableHTTPClientTransport, InMemoryTransport, ProtocolError} =
  await load('@modelcontextprotocol/client');
const {getToolUiResourceUri, buildAllowAttribute} = await load('@modelcontextprotocol/ext-apps/app-bridge');
const {chromium} = require('playwright');
const {build} = require('esbuild');
let scenario, client, browser, context, page;
let restricted;
let embedded;
let view = null;  // The rendered MCP Apps view's lifecycle while a render is active.
let bridgeScript = null;  // The host page's bridge bundle; it is static, so it is built once.
let callbacks = 0;
let inputClosed = false;
let catalog = new Map();
let renderPhase = 'host';
const callbackWaiters = new Map();
const MIME = 'text/html;profile=mcp-app';
// MCP Apps capability is the CLIENT's declaration; servers need not declare anything.
const uiCaps = {extensions: {'io.modelcontextprotocol/ui': {mimeTypes: [MIME]}}};
const hostOrigin = 'https://surface-host.invalid', appOrigin = 'https://surface-app.invalid';
const hostURL = `${hostOrigin}/index`, scriptURL = `${hostOrigin}/bridge.js`;
// The app document (legacy bridge, isolated renderer) and the sandbox proxy (native MCP Apps
// renderer) live on the app origin, never on the host page's: web hosts MUST keep them apart.
const appURL = `${appOrigin}/widget`, proxyURL = `${appOrigin}/sandbox`;
// Fixed width, flexible height: the view sizes itself (size-changed) up to MAX_VIEW_HEIGHT.
const VIEW_HEIGHT = 900, MAX_VIEW_HEIGHT = 6000;
// How long the host waits for ui/resource-teardown's response: after a completed step, and after a
// stopped step or on close (the coordinator waits 5 s for close; a step's host budget is +5 s).
const TEARDOWN_MS = 3000, STOP_TEARDOWN_MS = 1000;
const VIEW_EVENT_LIMIT = 200, VIEW_LOG_LIMIT = 50, PAYLOAD_LIMIT = 16 * 1024;
// The largest resources/read or resources/list result relayed to a view (JSON characters).
const RESOURCE_LIMIT = 5 * 1024 * 1024;
const send = value => { if (!inputClosed) process.stdout.write(JSON.stringify(value) + '\n'); };
const observe = value => send({event: 'observation', value});
const failure = (message, category) => Object.assign(Error(message), {category});
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
// An HTTP 401 or 403 from the endpoint: its credential (or the lack of one) was refused.
const refused = error => [401, 403].includes(error?.status);
// JSON-RPC errors and SDK result validation come from the server under test.
const serviceFailure = error => {
  if (ProtocolError.isInstance(error) && !error.category) error.category = 'tool/API';
  if (refused(error) && !error.category) error.category = 'authentication';
  throw error;
};
function privatePath(value) {
  const resolved = fs.realpathSync(value);
  if (!resolved.startsWith(root + path.sep)) throw Error('Path escapes workspace');
  return resolved;
}
function originAllowed(value, origins) {
  const url = new URL(value);
  return !url.username && !url.password && origins.includes(url.origin);
}
// Resolves with the coordinator's whole reply: result or error, plus rejected/abort flags.
function rpcRequest(name, args) {
  if (inputClosed) return Promise.reject(Error('Coordinator channel closed'));
  const id = ++callbacks;
  const promise = new Promise((resolve, reject) => callbackWaiters.set(id, {resolve, reject}));
  send({event: 'callback', id, name, arguments: args});
  return promise;
}
async function rpcCallback(name, args) {
  const reply = await rpcRequest(name, args);
  if (reply.error) throw Error(reply.error);
  return reply.result;
}
// Server-supplied values in diagnostics stay short.
const clip = (text, limit = 200) => text.length > limit ? text.slice(0, limit - 3) + '...' : text;
const shown = (value, limit) => clip(JSON.stringify(value) ?? String(value), limit);
// A tool's UI link lives on its tools/list definition (MCP Apps 2026-01-26).
function uiLink(tool) {
  const modern = tool._meta?.ui?.resourceUri !== undefined;
  try {
    const uri = getToolUiResourceUri(tool);  // _meta.ui.resourceUri, then deprecated _meta["ui/resourceUri"]
    if (uri) return {uri, source: modern ? 'tool-descriptor' : 'tool-descriptor-deprecated'};
  } catch {
    // A link no host can load (not a ui:// string) is a server defect, recorded as such.
    const value = shown(modern ? tool._meta.ui.resourceUri : tool._meta?.['ui/resourceUri']);
    return {uri: null, source: modern ? 'tool-descriptor' : 'tool-descriptor-deprecated', invalid: value,
      reason: `Invalid UI resource URI on the tool definition: ${value}`};
  }
  const legacy = tool._meta?.['openai/outputTemplate'];
  if (legacy === undefined) return null;
  return typeof legacy === 'string' && legacy.startsWith('ui://')
    ? {uri: legacy, source: 'openai-output-template'}
    : {uri: null, source: 'openai-output-template', invalid: shown(legacy),
      reason: `Invalid UI resource URI in openai/outputTemplate: ${shown(legacy)}`};
}
function resultLink(result) {
  const meta = result?._meta || {};
  const uri = meta.ui?.resourceUri ?? meta['ui/resourceUri'] ?? meta['openai/outputTemplate'];
  return typeof uri === 'string' && uri.startsWith('ui://') ? uri : null;
}
// _meta.ui.visibility (MCP Apps 2026-01-26) defaults to ["model", "app"]. A value that is not an
// array is a server defect: fail closed, so neither the model nor a view may use the tool.
function visibility(tool) {
  const declared = tool?._meta?.ui?.visibility;
  if (declared === undefined) return {model: true, app: true};
  if (!Array.isArray(declared)) return {model: false, app: false, declared: shown(declared), invalid: true};
  return {model: declared.includes('model'), app: declared.includes('app'), declared};
}
async function connect(input) {
  // The coordinator's transport credential (scenario auth) arrives beside the scenario, on this
  // private channel only: the endpoint with a query key applied, and the headers to add.
  const {transport: credential, ...contract} = input;
  scenario = contract;
  if (!originAllowed(scenario.endpoint, scenario.allowed_origins)) throw failure('Endpoint denied', 'setup/infrastructure');
  if (credential && new URL(credential.url).origin !== new URL(scenario.endpoint).origin)
    throw failure('Credential endpoint mismatch', 'setup/infrastructure');
  const target = credential?.url ?? scenario.endpoint, added = Object.entries(credential?.headers ?? {});
  // 'auto' probes server/discover (MCP 2026-07-28) and falls back to initialize; the default
  // stays the plain 2025 handshake, byte-identical to earlier runs.
  const negotiate = scenario.version_negotiation === 'auto';
  client = new Client({name: 'consumer-agent-testing-tool-host', version: '1.0.0'},
    {capabilities: uiCaps, ...(negotiate ? {versionNegotiation: {mode: 'auto'}} : {})});
  try {
    await client.connect(new StreamableHTTPClientTransport(new URL(scenario.endpoint), {
      requestInit: {redirect: 'error'},
      fetch: (input, init) => {
        const url = typeof input === 'string' ? input : input.url || input.toString();
        if (url !== scenario.endpoint) throw Error('MCP transport destination denied');
        const headers = new Headers(init?.headers);
        for (const [name, value] of added) headers.set(name, value);
        return fetch(target, {...init, headers, redirect: 'error'});
      },
    }));
  } catch (error) {
    // -32022 (unsupported protocol version) on the 2025 handshake: a server of a newer era only.
    if (!negotiate && error?.code === -32022)
      throw failure('The server refused the MCP 2025 initialize handshake (unsupported protocol version); ' +
        'set version_negotiation: auto to reach MCP 2026-07-28 servers', 'capability-unavailable');
    if (refused(error))
      throw failure(`The MCP endpoint refused the connection (HTTP ${error.status}); ` + (credential
        ? 'check the API key the scenario auth names' : 'declare scenario auth if it requires an API key'),
      'authentication');
    throw error;
  }
  const listed = await client.listTools();
  catalog = new Map(listed.tools.map(tool => [tool.name, tool]));
  for (const name of Object.keys(scenario.tools)) {
    if (!catalog.has(name)) throw failure(`Tool unavailable: ${name}`, 'setup/infrastructure');
  }
  const links = Object.fromEntries(listed.tools.map(tool => [tool.name, uiLink(tool)]).filter(([, link]) => link));
  return {discovery: 'passed',
    // Available when this client declares the extension and the server links UI from a tool.
    negotiation: Object.values(links).some(link => link.uri) ? 'passed' : 'unavailable',
    rendering: 'not-exercised', interaction: 'not-exercised', completion: 'not-exercised',
    client: uiCaps, server: client.getServerCapabilities(), ui_links: links,
    server_info: client.getServerVersion() ?? null,
    protocol_version: client.getNegotiatedProtocolVersion() ?? null,
    protocol_era: client.getProtocolEra() ?? null, version_negotiation: negotiate ? 'auto' : 'legacy',
    // Not "visibility": in event rows that name is the envelope's audience, which would hide this.
    tool_visibility: Object.fromEntries(listed.tools.filter(tool => tool._meta?.ui?.visibility !== undefined)
      .map(tool => [tool.name, visibility(tool)])),
    instructions: client.getInstructions() ?? null, tools: listed.tools};
}

// MCP Apps 2026-01-26 "Restrictive Default" for an omitted ui.csp, plus what the sandbox proxy
// must also enforce: no plugins, no nested frames, base URIs limited to the document's origin.
const DEFAULT_CSP = ["default-src 'none'", "script-src 'self' 'unsafe-inline'", "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:", "media-src 'self' data:", "connect-src 'none'", "frame-src 'none'",
  "object-src 'none'", "base-uri 'self'"].join('; ');
const CSP_FIELDS = ['connectDomains', 'resourceDomains', 'frameDomains', 'baseUriDomains'];
// A declared domain enters a directive only as an exact origin (an optional "*." label): no
// keywords, bare schemes, paths, quotes or separators that could widen or rewrite the policy.
const CSP_SOURCE = /^(?:https?|wss?):\/\/(?:\*\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*(?::\d{1,5})?\/?$/i;
function contentPolicy(declared) {
  if (declared === undefined) return {source: 'restrictive-default', header: DEFAULT_CSP, domains: null, rejected: []};
  if (!object(declared)) return {source: 'restrictive-default', header: DEFAULT_CSP, domains: null,
    rejected: [{field: 'csp', value: shown(declared)}]};
  const domains = Object.fromEntries(CSP_FIELDS.map(field => [field, []])), rejected = [];
  for (const field of CSP_FIELDS) {
    if (declared[field] === undefined) continue;
    if (!Array.isArray(declared[field])) { rejected.push({field, value: shown(declared[field])}); continue; }
    for (const value of declared[field]) {
      if (typeof value === 'string' && CSP_SOURCE.test(value)) domains[field].push(value);
      else rejected.push({field, value: shown(value)});
    }
  }
  // The spec's construction from declared domains (Security Implications, CSP Enforcement).
  const list = field => domains[field].join(' '), resources = list('resourceDomains');
  const header = ["default-src 'none'", `script-src 'self' 'unsafe-inline' ${resources}`,
    `style-src 'self' 'unsafe-inline' ${resources}`, `connect-src 'self' ${list('connectDomains')}`,
    `img-src 'self' data: ${resources}`, `font-src 'self' ${resources}`, `media-src 'self' data: ${resources}`,
    `frame-src ${list('frameDomains') || "'none'"}`, "object-src 'none'",
    `base-uri ${list('baseUriDomains') || "'self'"}`].map(directive => directive.trim()).join('; ');
  return {source: 'declared', header, domains, rejected};
}
// Declared Permission Policy features are honored on the view's iframes (hosts MAY); no others.
function grantedPermissions(declared) {
  if (!object(declared)) return {};
  return Object.fromEntries(['camera', 'microphone', 'geolocation', 'clipboardWrite']
    .filter(key => declared[key]).map(key => [key, {}]));
}
// The OpenAI Apps SDK declares a widget's CSP as openai/widgetCSP with snake_case lists.
const openaiCsp = value => object(value) ? Object.fromEntries([['connect_domains', 'connectDomains'],
  ['resource_domains', 'resourceDomains'], ['frame_domains', 'frameDomains']]
  .filter(([key]) => value[key] !== undefined).map(([key, field]) => [field, value[key]])) : value;
async function listedMeta(uri) {
  try {
    let cursor;
    for (let index = 0; index < 5; index++) {
      const listed = await client.listResources(cursor ? {cursor} : undefined);
      const entry = listed.resources.find(item => item.uri === uri);
      if (entry || !listed.nextCursor) return entry?._meta;
      cursor = listed.nextCursor;
    }
  } catch { /* Servers may leave UI resources out of resources/list, or list no resources at all. */ }
  return undefined;
}
// A UI resource's _meta.ui comes from its resources/read content, else from its resources/list
// entry (the ext-apps SDK documents the listing as the fallback). The legacy bridge also reads
// openai/widgetCSP. Malformed metadata fails closed to the restrictive default.
async function resourceMetadata(content, uri, bridge) {
  const read = meta => {
    if (meta?.ui !== undefined) return object(meta.ui) ? meta.ui : {csp: null};
    if (bridge === 'legacy-openai' && meta?.['openai/widgetCSP'] !== undefined)
      return {csp: openaiCsp(meta['openai/widgetCSP']), openai: true};
    return undefined;
  };
  let found = read(content._meta), source = 'resource-content';
  if (found === undefined) {
    found = read(await listedMeta(uri));
    source = found === undefined ? 'none' : 'resource-listing';
  }
  return {...(found ?? {}), source: found?.openai ? `${source} (openai/widgetCSP)` : source};
}
// What this host offers a view (McpUiHostCapabilities); messages and context are recorded.
// serverResources only when the UI step names resources the view may read.
const hostCapabilities = (policy, permissions, resources) => ({openLinks: {}, serverTools: {},
  ...(resources ? {serverResources: {}} : {}), logging: {},
  message: {text: {}}, updateModelContext: {text: {}, structuredContent: {}},
  sandbox: {permissions, ...(policy.domains ? {csp: policy.domains} : {})}});
// A view's resources/read and resources/list, proxied to the server for the UI step's
// callback_resources only; the listing shows only those. Each is one server request, as the ext-apps
// reference host forwards them: a listing is the server's page with its nextCursor (listResources()
// would merge every page), and nothing comes from the client's cache. The view gets the server's
// result (or its JSON-RPC error) within RESOURCE_LIMIT; evidence keeps sizes and SHA-256, never content.
async function viewResource(kind, params, readable, record) {
  const event = kind === 'read' ? 'ui_resource_read' : 'ui_resource_list';
  const uri = kind === 'read' ? (typeof params?.uri === 'string' ? params.uri : null) : undefined;
  const subject = uri === undefined ? {} : {uri: uri === null ? null : clip(uri, 2000)};
  const refuse = (disposition, reason, details = {}) => {
    record({kind: event, ...subject, disposition, reason, ...details});
    return {error: {code: -32000, message: `Resource request ${disposition} by the host: ${reason}`}};
  };
  try {
    let result, summary;
    if (kind === 'read') {
      if (uri === null || !readable.has(uri)) return refuse('denied', "not one of the UI step's callback_resources");
      result = await client.request({method: 'resources/read', params: {uri}});
    } else {
      const cursor = typeof params?.cursor === 'string' ? params.cursor : undefined;
      if (cursor?.length > 2000) return refuse('denied', 'cursor longer than 2000 characters');
      const listed = await client.request({method: 'resources/list', params: cursor === undefined ? {} : {cursor}});
      const shown = listed.resources.filter(item => readable.has(item.uri));
      result = {resources: shown, ...(listed.nextCursor ? {nextCursor: listed.nextCursor} : {})};
      summary = {listed: listed.resources.length, shown: shown.slice(0, 100).map(item => clip(item.uri, 2000))};
    }
    const text = JSON.stringify(result);
    if (text.length > RESOURCE_LIMIT) return refuse('withheld', `result larger than ${RESOURCE_LIMIT} characters`,
      {characters: text.length, sha256: hash(text)});
    const body = item => typeof item.text === 'string' ? item.text : typeof item.blob === 'string' ? item.blob : '';
    summary ??= {content_count: result.contents.length, contents: result.contents.slice(0, 20).map(item => ({
      uri: clip(String(item.uri), 2000), mimeType: item.mimeType ?? null,
      encoding: typeof item.text === 'string' ? 'text' : 'blob', characters: body(item).length, sha256: hash(body(item))}))};
    record({kind: event, ...subject, disposition: 'proxied', characters: text.length, ...summary});
    return {result};
  } catch (error) {
    // The server's JSON-RPC error reaches the view with its code and data (a resource-not-found
    // -32602 carries {uri}); others are this host's failure. The bridge SDK encodes a legacy
    // resource-not-found -32002 as -32602 (MCP 2026-07-28, every revision): relayed so, and the
    // event records both codes.
    const code = Number.isSafeInteger(error?.code) ? error.code : -32603, relayed = code === -32002 ? -32602 : code;
    const message = clip(String(error?.message || 'Resource request failed'));
    let data;
    try { data = error?.data !== undefined && JSON.stringify(error.data).length <= PAYLOAD_LIMIT ? error.data : undefined; }
    catch { data = undefined; }
    record({kind: event, ...subject, disposition: 'failed', code, ...(relayed === code ? {} : {view_code: relayed}),
      message, ...(data === undefined ? {} : {data_characters: JSON.stringify(data).length})});
    return {error: {code: relayed, message, ...(data === undefined ? {} : {data})}};
  }
}

// The sandbox proxy document, serialized into its page; never run in Node. As in the ext-apps
// reference sandbox, document.write gives the inner view this document's origin and CSP. Messages
// other than ui/notifications/sandbox-* pass through unchanged, in both directions, and only to or
// from that origin: a view that navigates itself away loses the relay.
function sandboxProxy(host) {
  const reserved = 'ui/notifications/sandbox-';
  const features = {camera: 'camera', microphone: 'microphone', geolocation: 'geolocation',
    clipboardWrite: 'clipboard-write'};
  const inner = document.createElement('iframe');
  inner.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
  let loaded = false;
  window.addEventListener('message', event => {
    const data = event.data, method = typeof data?.method === 'string' ? data.method : '';
    if (event.source === window.parent) {
      if (event.origin !== host) return;
      if (method === reserved + 'resource-ready') {
        if (loaded || typeof data.params?.html !== 'string') return;
        loaded = true;
        const allow = Object.keys(features).filter(key => data.params.permissions?.[key])
          .map(key => features[key]).join('; ');
        if (allow) inner.setAttribute('allow', allow);
        document.body.appendChild(inner);
        const target = inner.contentDocument;
        target.open();
        target.write(data.params.html);
        target.close();
      } else if (loaded && !method.startsWith(reserved)) inner.contentWindow?.postMessage(data, self.origin);
    } else if (event.source === inner.contentWindow && event.origin === self.origin
        && !method.startsWith(reserved)) {
      window.parent.postMessage(data, host);
    }
  });
  window.parent.postMessage({jsonrpc: '2.0', method: reserved + 'proxy-ready', params: {}}, host);
}
const PROXY_HTML = '<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%;' +
  'overflow:hidden}iframe{display:block;border:0;width:100%;height:100%}</style></head><body><script>(' +
  sandboxProxy + ')(' + JSON.stringify(hostOrigin) + ');</script></body></html>';

// The host page's official AppBridge (no default server-tool forwarding), serialized into the
// bundled page script; never run in Node. The host sends nothing to the view before it initializes.
function hostBridge(AppBridge, PostMessageTransport) {
  const frame = document.querySelector('iframe');
  const record = (kind, value) => window.__surfaceViewEvent(kind, value).catch(() => {});
  let bridge, input, initialized = false, settled = false, sent = false, delivering = null;
  window.__surfaceStart = async data => {
    input = data;
    // hostContext describes this host; the width is fixed and the height follows the view.
    const hostContext = {theme: 'light', displayMode: 'inline', availableDisplayModes: ['inline'],
      locale: navigator.language, timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      platform: 'web', containerDimensions: {width: frame.clientWidth, maxHeight: data.maxHeight},
      ...(data.tool ? {toolInfo: {tool: data.tool}} : {})};
    window.__surfaceHostContext = hostContext;
    bridge = new AppBridge(null, {name: 'consumer-agent-testing-tool', version: '1'}, data.capabilities,
      {hostContext});
    bridge.oncalltool = params => window.__surfaceCallback(params.name, params.arguments || {});
    if (data.resources) {
      // The host answers with the server's result or a JSON-RPC error, keeping its code and data.
      const relay = async (kind, params) => {
        const reply = await window.__surfaceResource(kind, params ?? {});
        if (reply.error) throw Object.assign(Error(reply.error.message), {code: reply.error.code, data: reply.error.data});
        return reply.result;
      };
      bridge.onreadresource = params => relay('read', params);
      bridge.onlistresources = params => relay('list', params);
    }
    // The coordinator records the message and says whether a conversation gets it; when none does,
    // the view is told (McpUiMessageResult.isError: the host rejected or failed to deliver it).
    bridge.onmessage = async params => (await record('message', params))?.delivered === true ? {} : {isError: true};
    bridge.onupdatemodelcontext = async params => { await record('model_context', params); return {}; };
    bridge.onopenlink = async params => { await record('open_link', params); return {}; };
    // Only inline is available; the reply names the mode in effect.
    bridge.onrequestdisplaymode = async params => { await record('display_mode', params); return {mode: 'inline'}; };
    bridge.onloggingmessage = params => { record('log', params); };
    bridge.onrequestteardown = params => { record('teardown_request', params ?? {}); };
    bridge.onsizechange = ({width, height}) => {
      const previous = window.__surfaceViewSize;
      const size = {width: width ?? null, height: height ?? null, count: (previous?.count ?? 0) + 1,
        applied_height: previous?.applied_height ?? null};
      if (Number.isFinite(height) && height > 0) {
        size.applied_height = Math.min(Math.ceil(height), data.maxHeight);
        frame.style.height = size.applied_height + 'px';
      }
      window.__surfaceViewSize = size;
    };
    bridge.oninitialized = async () => {
      initialized = true;
      delivering = bridge.sendToolInput({arguments: data.toolInput});
      await delivering;
      window.__surfaceInitialized = true;
    };
    // One view per render: the proxy receives the raw HTML once.
    if (data.proxy) bridge.onsandboxready = () => {
      if (sent) return;
      sent = true;
      bridge.sendSandboxResourceReady({html: data.html, permissions: data.permissions,
        ...(data.csp ? {csp: data.csp} : {})});
    };
    // The SDK transport posts to "*" and checks only the source window. Pin both directions to the
    // app origin, so a document the view navigates its frame to is never a bridge peer.
    window.addEventListener('message', event => {
      if (event.source !== frame.contentWindow || event.origin === data.origin) return;
      event.stopImmediatePropagation();
      record('foreign_origin', {origin: event.origin});
    }, true);
    const transport = new PostMessageTransport(frame.contentWindow, frame.contentWindow);
    transport.send = async message => { frame.contentWindow.postMessage(message, data.origin); };
    await bridge.connect(transport);
    frame.src = data.src;
  };
  window.__surfaceDeliver = async () => {
    if (settled) return;
    settled = true;
    await bridge.sendToolResult(input.source);
  };
  window.__surfaceTeardown = async (reason, wait) => {
    if (!initialized) return {status: 'not-initialized', tool_cancelled: false};
    await delivering?.catch(() => {});
    // A view that never got the source call's result learns it was cancelled.
    const cancelled = !settled;
    settled = true;
    if (cancelled) await bridge.sendToolCancelled({reason});
    try {
      await bridge.teardownResource({reason}, {timeout: wait});
      return {status: 'acknowledged', tool_cancelled: cancelled};
    } catch (error) {
      // The SDK's own timeout has a string code; a view's JSON-RPC error has a numeric one (a view
      // without a teardown handler answers -32601). Anything else failed on this host's side.
      if (error?.code === 'REQUEST_TIMEOUT') return {status: 'no-response', tool_cancelled: cancelled};
      if (Number.isInteger(error?.code)) return {status: 'rejected', tool_cancelled: cancelled, code: error.code};
      return {status: 'failed', tool_cancelled: cancelled};
    }
  };
}
async function bundle() {
  const entry = `import {AppBridge, PostMessageTransport} from '@modelcontextprotocol/ext-apps/app-bridge';
(${hostBridge})(AppBridge, PostMessageTransport);`;
  const built = await build({stdin: {contents: entry, resolveDir: tooling, sourcefile: 'bridge.js'},
    bundle: true, write: false, format: 'esm', platform: 'browser', logLevel: 'silent'});
  return built.outputFiles[0].text;
}
const hostDocument = (bridge, allow) => `<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0"><iframe sandbox="allow-scripts allow-same-origin allow-forms"${allow ? ` allow="${allow}"` : ''}
 src="${bridge ? 'about:blank' : appURL}" style="display:block;border:0;width:100%;height:${VIEW_HEIGHT}px"></iframe>
${bridge ? `<script type="module" src="${scriptURL}"></script>` : ''}</body></html>`;

// window.openai as the OpenAI Apps SDK gives it to widgets: a host compatibility adapter, not
// evidence that any product supports it. Members this host lacks reject, and every unknown member
// read is recorded. Member list basis: assumed (OpenAI Apps SDK reference; no copy is pinned here).
// Serialized into the app frame before its scripts run; never run in Node.
function openaiShim({url, input, output, metadata, maxHeight}) {
  if (window.location.href !== url || window.parent === window) return;
  const record = (kind, value) => window.__surfaceViewEvent(kind, value);
  let widgetState = null;
  const unsupported = member => () => {
    record('unsupported', {member}).catch(() => {});
    return Promise.reject(new Error(`window.openai.${member} is not implemented by this test host`));
  };
  const api = {
    toolInput: input, toolOutput: output, toolResponseMetadata: metadata,
    theme: 'light', locale: navigator.language, displayMode: 'inline', maxHeight,
    get widgetState() { return widgetState; },
    callTool: (name, args) => window.__surfaceCallback(name, args),
    async setWidgetState(state) {
      widgetState = state ?? null;
      await record('widget_state', widgetState);
      window.dispatchEvent(new CustomEvent('openai:set_globals', {detail: {globals: {widgetState}}}));
    },
    async sendFollowUpMessage({prompt} = {}) { await record('message', {prompt: String(prompt ?? '')}); },
    async openExternal({href} = {}) { await record('open_link', {url: String(href ?? '')}); },
    async requestDisplayMode({mode} = {}) {
      await record('display_mode', {mode: String(mode ?? '')});
      return {mode: 'inline'};
    },
    async notifyIntrinsicHeight(height) { await record('size', {height: Number(height)}); },
  };
  for (const member of ['requestModal', 'requestClose', 'uploadFile', 'getFileDownloadUrl', 'setOpenInAppUrl',
    'callCompletion', 'streamCompletion']) api[member] = unsupported(member);
  const reported = new Set();
  window.openai = new Proxy(api, {get(target, key, receiver) {
    if (typeof key === 'string' && !(key in target) && !['then', 'toJSON'].includes(key) && !reported.has(key)) {
      reported.add(key);
      record('unsupported', {member: key}).catch(() => {});
    }
    return Reflect.get(target, key, receiver);
  }});
}

// What a view asked of this host, as bounded evidence; never authority.
function bounded(value) {
  let text;
  try { text = JSON.stringify(value ?? null); } catch { return {omitted: 'not serializable'}; }
  return text.length <= PAYLOAD_LIMIT ? value ?? null
    : {omitted: `larger than ${PAYLOAD_LIMIT} characters`, characters: text.length, sha256: hash(text)};
}
// ui/update-model-context replaces the view's previous context; an empty update clears it.
function modelContext(source, content, structured) {
  const value = {...(content.length ? {content} : {}),
    ...(object(structured) && Object.keys(structured).length ? {structuredContent: structured} : {})};
  if (!Object.keys(value).length) return {kind: 'ui_model_context', source, cleared: true};
  const kept = bounded(value);
  return {kind: 'ui_model_context', source, ...(kept === value ? value : {omitted: kept})};
}
function viewRecord(bridge, kind, payload) {
  const legacy = bridge === 'legacy-openai', text = value => clip(String(value ?? ''));
  // The coordinator decides the disposition: an agent run delivers it as an app message.
  if (kind === 'message') return {kind: 'ui_message', source: legacy ? 'openai.sendFollowUpMessage' : 'ui/message',
    role: legacy ? 'user' : text(payload?.role),
    content: bounded(legacy ? [{type: 'text', text: String(payload?.prompt ?? '')}] : payload?.content ?? [])};
  if (kind === 'model_context') return modelContext('ui/update-model-context',
    Array.isArray(payload?.content) ? payload.content : [], payload?.structuredContent);
  if (kind === 'widget_state' && legacy) return modelContext('openai.setWidgetState', [],
    object(payload) ? payload : payload === null || payload === undefined ? null : {value: payload});
  if (kind === 'open_link') return {kind: 'ui_open_link', source: legacy ? 'openai.openExternal' : 'ui/open-link',
    url: clip(String(payload?.url ?? ''), 2000), disposition: 'recorded; not opened'};
  if (kind === 'display_mode') return {kind: 'ui_display_mode',
    source: legacy ? 'openai.requestDisplayMode' : 'ui/request-display-mode', requested: text(payload?.mode),
    granted: 'inline'};
  if (kind === 'log' && !legacy) return {kind: 'ui_log', level: text(payload?.level),
    logger: payload?.logger === undefined ? null : text(payload.logger), data: shown(payload?.data, 2000)};
  if (kind === 'teardown_request' && !legacy) return {kind: 'ui_request_teardown',
    disposition: 'recorded; the UI step decides when the view closes'};
  if (kind === 'foreign_origin' && !legacy) return {kind: 'ui_message_dropped', origin: text(payload?.origin),
    reason: 'not from the app origin: the view frame navigated away from this host\'s document'};
  if (kind === 'unsupported' && legacy) return {kind: 'ui_bridge_unsupported', bridge,
    member: clip(String(payload?.member ?? ''), 100)};
  return null;
}
// ui/resource-teardown before the view goes away, for any reason (MCP Apps 2026-01-26); idempotent.
function lifecycle(target) {
  const state = {started: false, closing: null};
  state.close = (reason, wait = TEARDOWN_MS) => {
    state.closing ||= (async () => {
      let outcome = {status: 'not-initialized', tool_cancelled: false}, timer;
      if (state.started) {
        try {
          outcome = await Promise.race([
            target.evaluate(([text, time]) => window.__surfaceTeardown(text, time), [reason, wait]),
            new Promise(resolve => {
              timer = setTimeout(() => resolve({status: 'no-response', tool_cancelled: null}), wait + 1000);
            })]);
        } catch {
          outcome = {status: 'failed', tool_cancelled: null};
        } finally { clearTimeout(timer); }
      }
      observe({kind: 'ui_teardown', reason, ...outcome});
      return outcome;
    })();
    return state.closing;
  };
  return state;
}
async function render(args) {
  try { return await renderApp(args); }
  catch (error) {
    // Waiting out the app itself (readiness, initialization, actions) is an app failure.
    if (!error.category && renderPhase === 'app' && error.name === 'TimeoutError') error.category = 'interaction';
    throw error;
  }
  finally {
    // Stopped or closing: a responsive view answers at once, and the step's host budget is short.
    if (view) await view.close('The UI step stopped', STOP_TEARDOWN_MS);
    view = null;
    if (embedded) await embedded.finishContext();
    else if (context) {
      const finished = context;
      context = null;
      await finished.close().catch(() => {});
    }
  }
}
async function renderApp({ui, source, run}) {
  renderPhase = 'host';
  const tool = ui.source_tool ? catalog.get(ui.source_tool) : undefined;
  const linked = tool ? uiLink(tool) : null;
  const uri = ui.resource_uri || linked?.uri || resultLink(source);
  // Record which source supplied the URI; only the tool definition is what real hosts use.
  const link = {kind: 'ui_resource', uri: uri || null, tool: ui.source_tool || null,
    source: ui.resource_uri ? 'scenario' : linked?.uri ? linked.source : uri ? 'tool-result-meta' : null,
    descriptor_uri: linked?.uri || null, descriptor_source: linked ? linked.source : null,
    descriptor_invalid: linked?.invalid ?? null, bridge: ui.bridge};
  if (!link.descriptor_uri || link.descriptor_uri !== link.uri) link.reason = !ui.source_tool
    ? 'The UI source step is not a tool call'
    : linked?.reason || (linked?.uri ? `The tool definition links ${linked.uri}`
      : `Tool ${ui.source_tool} links no UI resource in tools/list (_meta.ui.resourceUri)`);
  if (!uri) {
    observe(link);
    // A broken link is the server's defect; no link at all is coverage this server lacks.
    if (link.descriptor_invalid !== null) throw failure(`UI resource link invalid: ${link.reason}`, 'tool/API');
    throw failure(`UI unavailable: ${link.reason}`, 'capability-unavailable');
  }
  const resource = await client.readResource({uri}).catch(serviceFailure);
  // MCP Apps HTML arrives as text or as a base64 blob.
  const content = resource.contents.find(item => item.mimeType === MIME
    && (typeof item.text === 'string' || typeof item.blob === 'string'));
  if (!content) throw failure('UI resource has no supported HTML content', 'tool/API');
  const original = typeof content.text === 'string' ? content.text : Buffer.from(content.blob, 'base64').toString('utf8');
  observe({...link, sha256: hash(original), encoding: typeof content.text === 'string' ? 'text' : 'blob'});
  observe({resource: 'passed'});
  let html = original;
  if (ui.resource_file) {
    if (scenario.treatment !== 'workaround') throw failure('Resource intervention requires workaround treatment', 'setup/infrastructure');
    html = fs.readFileSync(privatePath(path.resolve(root, ui.resource_file)), 'utf8');
  }
  // The view's sandbox: CSP and permissions from the resource's _meta.ui. The native MCP Apps
  // renderer wraps the view in a sandbox proxy on the app origin; the legacy bridge and the
  // isolated renderer keep one app iframe on that origin.
  const bridge = ui.bridge === 'mcp-app', proxied = bridge && !scenario.browser_worker;
  const declared = await resourceMetadata(content, uri, ui.bridge);
  const policy = contentPolicy(declared.csp);
  const permissions = grantedPermissions(declared.permissions);
  const allow = buildAllowAttribute(permissions);
  // Resources the view may read through this host (serverResources), and whether the native
  // renderer lets requests reach browser_origins at all (it then guards them; review M6).
  const readable = new Set(bridge ? ui.callback_resources ?? [] : []);
  const guarded = !scenario.browser_worker && scenario.browser_origins.length > 0;
  const sandbox = {renderer: scenario.browser_worker ? 'isolated' : 'native',
    proxy: proxied ? {origin: appOrigin, host_origin: hostOrigin} : null, view_origin: appOrigin,
    metadata: declared.source, csp: {source: policy.source, policy: policy.header, domains: policy.domains,
      rejected: policy.rejected},
    permissions: {granted: Object.keys(permissions), allow: allow || null},
    ...(declared.domain !== undefined ? {domain: {requested: shown(declared.domain), applied: false}} : {}),
    ...(declared.prefersBorder !== undefined ? {prefers_border: declared.prefersBorder === true} : {}),
    ...(scenario.browser_worker ? {} : {network: guarded ? {destinations: 'browser_origins',
      local_network_access: 'granted', redirects: 'rechecked', writes: 'unconsented; recorded as host actions'}
      : {destinations: 'none'}})};
  observe({kind: 'ui_sandbox', bridge: ui.bridge, ...sandbox});
  const observations = {}, checks = [], diagnostics = [];
  const temporary = path.join(root, '.cache', 'surface-host');
  fs.mkdirSync(temporary, {recursive: true});
  if (context) await context.close();
  let guard = null;
  if (scenario.browser_worker) {
    embedded ||= new EmbeddedBrowser(root, scenario, chromium, rpcCallback, observe);
    context = await embedded.open();
  } else {
    browser ||= await chromium.launch({headless: true,
      executablePath: process.env.CONSUMER_AGENT_TESTING_TOOL_BROWSER_EXECUTABLE,
      args: ['--disable-background-networking']});
    context = await browser.newContext({serviceWorkers: 'block'});
    if (guarded) {
      // Chromium's Local Network Access would silently drop requests to allowlisted loopback
      // origins. Granted to the host page, as a user accepting its prompt would; the route check
      // and the CSP still decide every request.
      await context.grantPermissions(['local-network-access'], {origin: hostOrigin});
      // Playwright routes only a redirect chain's first request: the shared response guard checks
      // each Location before Chromium follows it. A redirect leads only to browser_origins, and only
      // as a read, so no write reaches the network unrecorded.
      guard = new BrowserNetwork({context, observe, ruleFor: (url, method) =>
        originAllowed(url.href, scenario.browser_origins) && ['GET', 'HEAD'].includes(method)
          ? {effect: 'read'} : undefined});
    }
  }
  // The arguments the scenario or model supplied for the source call; host inputs never cross.
  const toolInput = ui.tool_input || {};
  if (ui.bridge === 'legacy-openai') {
    // Prepending a script changes the app's document mode. Install the bridge
    // before app scripts without rewriting HTML or initializing other frames.
    await context.addInitScript(openaiShim, {url: appURL, input: toolInput,
      output: source.structuredContent || {}, metadata: source._meta || {}, maxHeight: MAX_VIEW_HEIGHT});
  }
  context.setDefaultTimeout(scenario.timeout_seconds * 1000);
  await context.routeWebSocket('**/*', socket => {
    observe({kind: 'network_denied', enforcement: 'playwright-websocket-route',
      reason: 'Embedded WebSocket access is disabled'});
    socket.close();
  });
  let callbackCompleted = 0, interrupted = null, inflight = 0, legacySize = null;
  // Only this host's bridge may use its bindings: the host page for MCP Apps, and the app frame
  // itself for the legacy bridge. A view's own frames never can.
  const bridgeFrame = frame => ui.bridge === 'legacy-openai'
    ? frame.url() === appURL && frame.parentFrame() === page?.mainFrame()
    : frame === page?.mainFrame();
  await context.exposeBinding('__surfaceCallback', async (sourceFrame, name, args) => {
    if (!bridgeFrame(sourceFrame.frame) || !ui.callback_tools.includes(name)) {
      observe({kind: 'ui_callback_denied', tool: String(name)});
      throw Error('UI callback denied');
    }
    // MCP Apps: a host MUST reject a view's tools/call for a tool whose visibility lacks "app".
    if (!visibility(catalog.get(name)).app) {
      observe({kind: 'ui_callback_denied', tool: String(name), reason: 'visibility excludes app'});
      throw Error('UI callback denied: the tool is not visible to apps');
    }
    inflight++;
    let reply;
    try { reply = await rpcRequest(name, args); }
    finally { inflight--; }
    // The coordinator stops the step for a pause, host failure or rejected write.
    if (reply.abort) interrupted ||= reply.error || `${name} was rejected`;
    if (reply.error) throw Error(reply.error);
    // Polling reads are answered without completing a step that submits a write.
    if (!reply.rejected && reply.completion !== false) {
      callbackCompleted++;
      observe({completion: 'passed'});
    }
    // A rejected call still returns the service's CallToolResult (isError: true) to the view.
    return reply.result;
  });
  const requests = {recorded: 0, logs: 0, dropped: 0, kinds: {}};
  // View requests are bounded evidence: VIEW_EVENT_LIMIT per step, at most VIEW_LOG_LIMIT of them logs.
  const admitted = value => {
    if (requests.recorded >= VIEW_EVENT_LIMIT || (value.kind === 'ui_log' && requests.logs >= VIEW_LOG_LIMIT)) {
      requests.dropped++;
      return false;
    }
    requests.recorded++;
    if (value.kind === 'ui_log') requests.logs++;
    requests.kinds[value.kind] = (requests.kinds[value.kind] ?? 0) + 1;
    return true;
  };
  const recordView = value => { if (admitted(value)) observe(value); };
  await context.exposeBinding('__surfaceViewEvent', async (sourceFrame, kind, payload) => {
    if (!bridgeFrame(sourceFrame.frame) || typeof kind !== 'string') throw Error('View request denied');
    if (kind === 'size' && ui.bridge === 'legacy-openai') {
      // notifyIntrinsicHeight is the legacy size-changed: honored up to the maximum height.
      const height = Number(payload?.height);
      legacySize = {width: null, height: Number.isFinite(height) ? height : null, count: (legacySize?.count ?? 0) + 1,
        applied_height: legacySize?.applied_height ?? null};
      if (Number.isFinite(height) && height > 0) {
        legacySize.applied_height = Math.min(Math.ceil(height), MAX_VIEW_HEIGHT);
        await page.locator('iframe').evaluate((element, value) => { element.style.height = value + 'px'; },
          legacySize.applied_height);
      }
      return;
    }
    const value = viewRecord(ui.bridge, kind, payload);
    if (!value) throw Error('View request denied');
    if (value.kind !== 'ui_message') return void recordView(value);
    // The coordinator records a ui/message and decides whether a conversation gets it; one past the
    // step's evidence bound is neither recorded nor delivered.
    if (!admitted(value)) return {delivered: false};
    const reply = await rpcRequest('__surface_app_message', value).catch(error => ({error: error.message}));
    return {delivered: reply.result?.queued === true};
  });
  if (readable.size) await context.exposeBinding('__surfaceResource', async (sourceFrame, kind, params) => {
    if (!bridgeFrame(sourceFrame.frame) || !['read', 'list'].includes(kind)) throw Error('View request denied');
    return viewResource(kind, params, readable, recordView);
  });
  const stopIfInterrupted = () => {
    if (interrupted) throw failure(`UI step stopped by its callback: ${interrupted}`, 'callback-interrupted');
  };
  if (bridge) bridgeScript ||= await bundle();
  let proxyServed = false;
  await context.route('**/*', async route => {
    const request = route.request(), url = request.url();
    if ([hostURL, appURL, scriptURL, proxyURL].includes(url)) {
      if (request.method() !== 'GET') return route.abort();
      if (url === hostURL) return route.fulfill({contentType: 'text/html', body: hostDocument(bridge, allow)});
      if (url === scriptURL && bridge) return route.fulfill({contentType: 'text/javascript', body: bridgeScript});
      const csp = {'content-security-policy': policy.header};
      if (url === appURL && !proxied) return route.fulfill({contentType: 'text/html', headers: csp, body: html});
      // The proxy goes once, to this host's own iframe: one view per render.
      let owned = false;
      try { owned = request.frame().parentFrame() === page?.mainFrame(); } catch { /* not a frame request */ }
      if (url === proxyURL && proxied && owned && !proxyServed) {
        proxyServed = true;
        return route.fulfill({contentType: 'text/html', headers: csp, body: PROXY_HTML});
      }
      return route.abort();
    }
    if (embedded) return embedded.route(route);
    if (!originAllowed(url, scenario.browser_origins)) {
      diagnostics.push({type: 'denied-request', origin: new URL(url).origin});
      return route.abort();
    }
    const deny = reason => {
      observe({kind: 'network_denied', enforcement: 'native-network-policy', url: clip(url, 2000),
        method: request.method(), reason});
      return route.abort();
    };
    try {
      const frame = request.frame();
      if (!guard?.redirectGuardReady.has(frame.page())) throw Error('Response guard was not active before this request started');
      await guard.guardRequestFrame(frame);
    } catch (error) { return deny(error.message); }
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method())) {
      // No consent exists for a view's own HTTP write here: the coordinator records it as an
      // unconsented host action before it is sent, or the write is refused.
      const body = request.postDataBuffer() || Buffer.alloc(0);
      const reply = await rpcRequest('__surface_network_write', {url: clip(url, 2000), method: request.method(),
        body_sha256: crypto.createHash('sha256').update(body).digest('hex'), body_bytes: body.length})
        .catch(error => ({error: error.message}));
      if (reply.error || reply.result?.recorded !== true) return deny(`Write not recorded: ${reply.error || 'no record'}`);
    }
    return route.continue();
  });
  page = await context.newPage();
  if (embedded) await embedded.network.guardRedirects(page);
  else if (guard) await guard.guardRedirects(page);
  page.on('pageerror', error => diagnostics.push({type: 'browser-error', message: error.message}));
  await page.goto(hostURL, {waitUntil: 'domcontentloaded'});
  if (bridge) {
    await page.waitForFunction(() => typeof window.__surfaceStart === 'function');
    view = lifecycle(page);
    await page.evaluate(data => window.__surfaceStart(data), {tool: tool ?? null, toolInput, source,
      proxy: proxied, src: proxied ? proxyURL : appURL, origin: appOrigin, html: proxied ? html : null,
      csp: policy.domains, permissions, maxHeight: MAX_VIEW_HEIGHT, resources: readable.size > 0,
      capabilities: hostCapabilities(policy, permissions, readable.size > 0)});
    view.started = true;
  }
  renderPhase = 'app';
  let hostContext = null;
  if (bridge) {
    await page.waitForFunction(() => window.__surfaceInitialized === true);
    // The source call completed before rendering. Its result goes to the view unless a view call
    // already stopped the step; teardown then sends tool-cancelled instead.
    stopIfInterrupted();
    await page.evaluate(() => window.__surfaceDeliver());
    hostContext = await page.evaluate(() => {
      const {toolInfo, ...context} = window.__surfaceHostContext;
      return {...context, tool_info: Boolean(toolInfo)};
    });
  }
  const outer = page.locator('iframe').contentFrame();
  await (proxied ? outer.locator('iframe').contentFrame() : outer).locator(ui.ready_selector).waitFor({state: 'visible'});
  const container = page.mainFrame().childFrames().find(item => item.url() === (proxied ? proxyURL : appURL));
  const frame = proxied ? container?.childFrames()[0] : container;
  if (!frame) throw Error('UI rendering failed: embedded frame unavailable');
  if (embedded) await embedded.network.guardRequestFrame(frame);
  else if (guard) await guard.guardRequestFrame(frame);
  await frame.locator(ui.ready_selector).waitFor({state: 'visible'});
  observe({rendering: 'passed', bridge: ui.bridge});
  const selectFrame = url => {
    if (!url) return frame;
    if (!originAllowed(url, scenario.browser_origins)) throw Error('UI frame origin denied');
    const found = page.frame({url});
    if (!found) throw Error('UI interaction frame unavailable');
    return found;
  };
  for (const [name, property] of Object.entries(ui.observations)) {
    if (scenario.treatment === 'baseline') throw Error('Runtime property observation requires observation treatment');
    observations[name] = await frame.evaluate(property => {
      const parts = property.split('.');
      if (parts.some(part => ['__proto__', 'prototype', 'constructor'].includes(part))) throw Error('Invalid property');
      return parts.reduce((value, key) => value?.[key], window);
    }, property);
  }
  for (const action of ui.actions) {
    stopIfInterrupted();
    const locator = selectFrame(action.frame_url).locator(action.selector);
    if (action.kind === 'fill') await locator.fill(String(action.value));
    else if (action.kind === 'click') await clickEmbedded(locator, scenario.timeout_seconds * 1000);
    else {
      const until = Date.now() + scenario.timeout_seconds * 1000;
      let actual;
      do {
        stopIfInterrupted();
        actual = action.kind === 'expect_value' ? await locator.inputValue() : await locator.innerText();
        if (actual === String(action.value)) break;
        await new Promise(resolve => setTimeout(resolve, 25));
      } while (Date.now() < until);
      const passed = actual === String(action.value);
      checks.push({kind: action.kind, selector: action.selector, passed});
      if (!passed) throw failure(`UI expectation failed: ${action.selector}`, 'interaction');
    }
  }
  stopIfInterrupted();
  if (ui.actions.length) observe({interaction: 'passed'});
  if (ui.callback_required) {
    const until = Date.now() + scenario.timeout_seconds * 1000;
    while (!callbackCompleted && !interrupted && Date.now() < until)
      await new Promise(resolve => setTimeout(resolve, 50));
    stopIfInterrupted();
    if (!callbackCompleted) throw failure('UI completion callback timed out', 'interaction');
  }
  // A view call still in flight belongs to this step; never let it outlive the step.
  const settle = Date.now() + scenario.timeout_seconds * 1000;
  while (inflight && !interrupted && Date.now() < settle) await new Promise(resolve => setTimeout(resolve, 25));
  stopIfInterrupted();
  if (inflight) throw failure('UI tool call still in progress at the end of the UI step', 'integration-failure');
  const result = {capabilities: {resource: 'passed', rendering: 'passed',
    interaction: ui.actions.length ? 'passed' : 'not-exercised',
    completion: callbackCompleted ? 'passed' : 'not-exercised'}, bridge: ui.bridge,
    resource_uri: link.uri, resource_link: link, resource_encoding: typeof content.text === 'string' ? 'text' : 'blob',
    resource_sha256: hash(original), rendered_resource_sha256: hash(html),
    host_context: hostContext, tool_input_keys: Object.keys(toolInput), sandbox,
    view_size: bridge ? await page.evaluate(() => window.__surfaceViewSize ?? null) : legacySize,
    view_requests: {...requests.kinds, ...(requests.dropped ? {dropped: requests.dropped} : {})},
    treatment: scenario.treatment, observations, checks, diagnostics, callbacks: callbackCompleted};
  if (view) result.teardown = await view.close('The UI step completed');
  // No screenshots or field values are recorded automatically.
  fs.writeFileSync(path.join(privatePath(run), 'ui-resource-hashes.json'), JSON.stringify({
    original: hash(original), rendered: hash(html), bridge: ui.bridge, treatment: scenario.treatment}));
  return result;
}
async function dispatch(method, args) {
  if (method === 'browse_open') {
    if (restricted) throw Error('A browser task is already open');
    if (!fs.existsSync(path.join(tooling, 'node_modules', '@playwright', 'mcp', 'package.json')))
      throw Error('Missing pinned @playwright/mcp; install the optional Node tooling');
    const mcpRequire = createRequire(require.resolve('@playwright/mcp'));
    const mcp = new BrowserMCP({
      createConnection: require('@playwright/mcp').createConnection, Client, InMemoryTransport,
      version: require('@playwright/mcp/package.json').version,
      playwrightVersion: mcpRequire('playwright/package.json').version,
      // Page-registered WebMCP tools only when the browser policy opts in (P3.4; not Muse).
      webmcp: args.webmcp === true,
    }, observe);
    // The remote worker protocol must use the SDK version pinned with its image.
    // MCP keeps its own implementation dependency; it receives the provided context.
    restricted = new RestrictedBrowser(args.worker ? chromium : mcpRequire('playwright').chromium, rpcCallback, root,
      observe, mcp, process.env.CONSUMER_AGENT_TESTING_TOOL_BROWSER_EXECUTABLE || chromium.executablePath());
    return restricted.open(args);
  }
  if (method === 'browse_action') return restricted.action(args);
  if (method === 'browse_storage_state') return restricted.storageState();
  if (method === 'browse_task_storage_state') return restricted.taskStorageState();
  if (method === 'browse_diagnostics_capture')
    return restricted.diagnostics.capture(restricted, args.generation);
  if (method === 'browse_diagnostics_stop') return restricted.diagnostics.stop();
  if (method === 'browse_return_control') {restricted.returnControl(); return {};}
  if (method === 'connect') return connect(args);
  if (method === 'call') {
    if (!Object.hasOwn(scenario.tools, args.name)) throw failure('Tool denied', 'setup/infrastructure');
    return client.callTool(args).catch(serviceFailure);
  }
  if (method === 'render') return render(args);
  if (method === 'close') {
    // A render the coordinator abandoned still tears its view down before the browser closes.
    if (view) await view.close('The host closed', STOP_TEARDOWN_MS);
    if (restricted) await restricted.close();
    if (embedded) await embedded.close();
    if (browser) await browser.close();
    if (client) await client.close();
    return {};
  }
  throw Error('Unknown host operation');
}
const lines = readline.createInterface({input: process.stdin});
lines.on('line', async line => {
  const message = JSON.parse(line);
  if (message.callback_id) {
    const waiter = callbackWaiters.get(message.callback_id);
    callbackWaiters.delete(message.callback_id);
    if (waiter) waiter.resolve(message);
    return;
  }
  try { send({id: message.id, result: await dispatch(message.method, message.arguments)}); }
  catch (error) {
    if (message.method === 'render') observe({ui_failure: error.message});
    send({id: message.id, error: error.message, category: error.category || 'integration-failure'});
  }
});
lines.on('close', async () => {
  inputClosed = true;
  // The coordinator is gone: release every resource, then exit even if a library keeps a
  // handle open, so no orphaned browser outlives a crashed run.
  setTimeout(() => process.exit(0), 3000).unref();
  for (const waiter of callbackWaiters.values()) waiter.reject(Error('Coordinator channel closed'));
  callbackWaiters.clear();
  try { if (view) await view.close('The host closed', STOP_TEARDOWN_MS); } catch { /* best effort during shutdown */ }
  for (const resource of [restricted, embedded, browser, client]) {
    try { if (resource) await resource.close(); } catch { /* best effort during shutdown */ }
  }
  process.exit(0);
});
