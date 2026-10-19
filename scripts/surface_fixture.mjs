// Synthetic local preferences service for the general surface host. No external requests.
import http from 'node:http';
import path from 'node:path';
import {createRequire} from 'node:module';
const require = createRequire(path.resolve('tooling/payment-ui/package.json'));
const {build} = require('esbuild');
const requestedPort = Number(process.argv[2] || 0);
const mode = process.argv[3] || 'mcp-app';
// Modes: mcp-app declares the UI extension as {} (the documented server form); ext-apps and
// ext-apps-blob behave like the official example servers (no declaration; the UI link is only on
// the tool definition, HTML as text or base64 blob); result-link puts the link only in tool
// results, which real hosts ignore; no-ui links no UI anywhere; openai-template links an MCP App
// only through the OpenAI Apps SDK key; invalid-link puts a non-ui:// URI on the definition.
// modern speaks only MCP 2026-07-28 (server/discover, no initialize) and links the UI only when the
// request's own client capabilities declare MCP Apps; host-contract exercises the MCP Apps host
// contract (CSP, permissions, visibility, view requests, teardown); listing-csp declares its CSP
// only on the resources/list entry; hostile-csp declares sources a host must refuse; early-call is
// an SDK-free view that calls a tool before it initializes; silent-teardown never answers
// ui/resource-teardown; proxy-escape navigates its sandbox proxy away and calls a tool from there.
// resources has an official-SDK view read and list server resources through the host;
// native-network has a view fetch, write and follow redirects to the fixture origin and to a second
// "escape" origin that its CSP admits; api-key accepts MCP requests only with the API key from
// SURFACE_FIXTURE_API_KEY (bearer, X-API-Key header or api_key query) and reports the placement used.
// resource-relay lists resources over three pages, answers some reads with JSON-RPC errors (a
// legacy -32002, a -32602 with data.uri, a -32001) and lets clients cache its notes for a minute;
// its view reports what reached it.
// Servers never echo mimeTypes: those are the client's capability.
const RESOURCE = 'ui://test/preferences';
const MIME = 'text/html;profile=mcp-app';
const MODERN = '2026-07-28';
const extApps = ['ext-apps', 'ext-apps-blob', 'modern', 'host-contract', 'listing-csp', 'hostile-csp',
  'early-call', 'silent-teardown', 'proxy-escape', 'resources', 'native-network', 'api-key', 'resource-relay']
  .includes(mode);
const descriptorLink = ['no-ui', 'result-link'].includes(mode) ? null
  : ['legacy-openai', 'openai-template'].includes(mode) ? {'openai/outputTemplate': RESOURCE}
  : mode === 'invalid-link' ? {ui: {resourceUri: 'https://app.example.invalid/view.html'}}
  : extApps ? {ui: {resourceUri: RESOURCE}, 'ui/resourceUri': RESOURCE}  // as registerAppTool writes it
  : {ui: {resourceUri: RESOURCE}};
const resultLink = !extApps && !['no-ui', 'openai-template', 'invalid-link'].includes(mode);
// host-contract: status is for the model only, poll for the view only (_meta.ui.visibility).
const visibility = mode === 'host-contract' ? {status: ['model'], poll: ['app']} : {};
const script = `
  import {App} from '@modelcontextprotocol/ext-apps';
  const app = new App({name:'preferences-fixture',version:'1'}, {});
  const show = (id, value) => { document.querySelector(id).textContent = value; };
  app.ontoolinput = params => show('#tool-input', JSON.stringify(params.arguments ?? null));
  app.ontoolresult = result => {
    document.querySelector('#name').value = result.structuredContent.name;
    document.querySelector('#name').hidden = false;
  };
  document.querySelector('#save').onclick = async () => {
    const result = await app.callServerTool({name:'save',arguments:{value:document.querySelector('#name').value}});
    show('#status', result.isError ? 'error: ' + result.structuredContent?.error : result.structuredContent.status);
  };
  document.querySelector('#refresh').onclick = async () => {
    const result = await app.callServerTool({name:'status',arguments:{}});
    show('#reads', String(result.structuredContent.reads));
  };
  await app.connect();
  const context = app.getHostContext() || {};
  const size = context.containerDimensions || {};
  // Fixed or flexible: a width, and a height or a maximum height.
  const sized = size.width > 0 && (size.height > 0 || size.maxHeight > 0);
  show('#host-context', [context.theme, context.displayMode, (context.availableDisplayModes || []).join(','),
    context.platform, sized ? 'sized' : 'unsized', context.locale ? 'locale' : 'no-locale',
    context.timeZone ? 'zone' : 'no-zone', context.toolInfo?.tool?.name || 'no-tool'].join(' '));
`;
// The host contract as an official-SDK view sees it. Fixture origins are filled in when read.
const contract = `
  import {App} from '@modelcontextprotocol/ext-apps';
  const ORIGIN = "__FIXTURE_ORIGIN__", OTHER = "__FIXTURE_OTHER__";
  const app = new App({name:'host-contract-fixture',version:'1'}, {availableDisplayModes:['inline','fullscreen']});
  const show = (id, value) => { document.getElementById(id).textContent = value; };
  const violations = [];
  document.addEventListener('securitypolicyviolation', event =>
    violations.push(event.effectiveDirective + ' ' + new URL(event.blockedURI).origin));
  app.ontoolinput = params => show('tool-input', JSON.stringify(params.arguments ?? null));
  app.ontoolresult = result => {
    document.getElementById('name').value = result.structuredContent.name;
    document.getElementById('name').hidden = false;
  };
  app.onteardown = async () => { await app.sendLog({level:'info', data:'teardown received'}); return {}; };
  document.getElementById('save').onclick = async () => {
    const result = await app.callServerTool({name:'save',arguments:{value:document.getElementById('name').value}});
    show('status', result.isError ? 'error: ' + result.structuredContent?.error : result.structuredContent.status);
  };
  await app.connect();
  const policy = document.featurePolicy;
  show('sandbox', [self.origin, window.parent !== window.top ? 'proxied' : 'direct',
    ...['clipboard-write', 'camera'].map(name => name + '=' + (policy ? policy.allowsFeature(name) : 'unknown'))].join(' '));
  const outcome = call => call.then(result => result.isError ? 'error' : 'allowed', () => 'rejected');
  show('visibility', 'status:' + await outcome(app.callServerTool({name:'status',arguments:{}})) +
    ' poll:' + await outcome(app.callServerTool({name:'poll',arguments:{}})));
  await app.updateModelContext({content:[{type:'text', text:'Preference form open for review'}],
    structuredContent:{form:'preferences', step:'review'}});
  const sent = await app.sendMessage({role:'user', content:[{type:'text', text:'Please confirm my saved preference'}]});
  show('message', sent.isError ? 'isError' : 'delivered');
  await app.openLink({url:'https://example.com/help'});
  show('display', (await app.requestDisplayMode({mode:'fullscreen'})).mode);
  // Only CSP violations are reported: the native renderer's Chromium also applies Local Network
  // Access to loopback requests that CSP admits, so a fetch outcome would not isolate CSP.
  for (const url of [ORIGIN + '/asset', OTHER + '/asset']) await fetch(url).catch(() => {});
  await new Promise(resolve => setTimeout(resolve, 100));
  show('csp', 'violations:' + violations.join(','));
`;
// The view reads an allowed, a withheld (too large) and a denied resource, and lists them.
const resourcesView = `
  import {App} from '@modelcontextprotocol/ext-apps';
  const app = new App({name:'resource-fixture',version:'1'}, {});
  const show = (id, value) => { document.getElementById(id).textContent = value; };
  app.ontoolresult = result => {
    document.getElementById('name').value = result.structuredContent.name;
    document.getElementById('name').hidden = false;
  };
  document.getElementById('save').onclick = async () => {
    const result = await app.callServerTool({name:'save',arguments:{value:document.getElementById('name').value}});
    show('status', result.isError ? 'error: ' + result.structuredContent?.error : result.structuredContent.status);
  };
  await app.connect();
  const failed = error => 'error ' + error.code;
  const read = uri => app.readServerResource({uri}).then(result => result.contents.map(item => item.text).join(''), failed);
  const listed = await app.listServerResources({}).then(result => result.resources.map(item => item.uri).join(','), failed);
  show('resources', ['caps:' + JSON.stringify(app.getHostCapabilities()?.serverResources ?? null),
    'read:' + await read('fixture://notes'), 'large:' + await read('fixture://large'),
    'hidden:' + await read('fixture://hidden'), 'list:' + listed].join(' '));
`;
// The view reads resources the server answers with errors, lists without and with a cursor, and
// tries the host page's private resource binding from its own frame.
const relayView = `
  import {App} from '@modelcontextprotocol/ext-apps';
  const app = new App({name:'resource-relay-fixture',version:'1'}, {});
  const show = (id, value) => { document.getElementById(id).textContent = value; };
  app.ontoolresult = result => {
    document.getElementById('name').value = result.structuredContent.name;
    document.getElementById('name').hidden = false;
  };
  document.getElementById('save').onclick = async () => {
    const result = await app.callServerTool({name:'save',arguments:{value:document.getElementById('name').value}});
    show('status', result.isError ? 'error: ' + result.structuredContent?.error : result.structuredContent.status);
  };
  await app.connect();
  const failed = error => 'error ' + error.code + (typeof error.data?.uri === 'string' ? ' uri=' + error.data.uri : '');
  const read = uri => app.readServerResource({uri}).then(() => 'read', failed);
  const list = params => app.listServerResources(params).then(result =>
    result.resources.map(item => item.uri).join(',') + ' next=' + (result.nextCursor ?? 'none'), failed);
  const direct = typeof window.__surfaceResource !== 'function' ? 'absent'
    : await window.__surfaceResource('read', {uri: 'fixture://notes'}).then(() => 'answered', () => 'denied');
  show('resources', ['missing:' + await read('fixture://missing'), 'gone:' + await read('fixture://gone'),
    'broken:' + await read('fixture://broken'),
    'twice:' + await read('fixture://notes') + ',' + await read('fixture://notes'),
    'first:' + await list({}), 'page:' + await list({cursor: 'page2'}),
    'long:' + await list({cursor: 'c'.repeat(2001)}), 'direct:' + direct].join(' '));
`;
// resource-relay: three listing pages, and JSON-RPC errors for some reads.
const RELAY_PAGES = {'': [[RESOURCE, 'fixture://missing'], 'page2'],
  page2: [['fixture://notes', 'fixture://hidden'], 'page3'], page3: [['fixture://gone', 'fixture://broken'], null]};
const RELAY_ERRORS = {'fixture://missing': {code: -32002, message: 'Resource not found: fixture://missing'},
  'fixture://gone': {code: -32602, message: 'Resource not found: fixture://gone', data: {uri: 'fixture://gone'}},
  'fixture://broken': {code: -32001, message: 'Synthetic backend failure'}};
const NOTES = {'fixture://notes': () => 'Synthetic note', 'fixture://hidden': () => 'Hidden synthetic note',
  // Just over the host's 5 MiB relay bound once serialized.
  'fixture://large': () => 'x'.repeat(5 * 1024 * 1024)};
const resourceReads = Object.fromEntries(Object.keys(NOTES).map(uri => [uri.slice('fixture://'.length), 0]));
const bundle = async contents => (await build({stdin: {contents, resolveDir: path.resolve('tooling/payment-ui'),
  sourcefile: 'fixture.js'}, bundle: true, write: false, format: 'esm', platform: 'browser'})).outputFiles[0].text;
const viewScript = await bundle(mode === 'host-contract' ? contract : mode === 'resources' ? resourcesView
  : mode === 'resource-relay' ? relayView : mode === 'silent-teardown'
  ? script.replace('  await app.connect();', '  app.onteardown = () => new Promise(() => {});\n  await app.connect();')
  : script);
// No SDK: ui/initialize, then a tools/call before ui/notifications/initialized. On teardown it logs
// the host messages it received, so the order (tool-input, tool-cancelled, teardown) is evidence.
const earlyCall = `
  const pending = new Map(), received = [];
  let next = 1;
  const post = message => window.parent.postMessage({jsonrpc:'2.0', ...message}, '*');
  const request = (method, params) => new Promise(resolve => {
    const id = next++;
    pending.set(id, resolve);
    post({id, method, params});
  });
  window.addEventListener('message', event => {
    const data = event.data;
    if (event.source !== window.parent || !data) return;
    if (data.id !== undefined && data.method === undefined) { pending.get(data.id)?.(data); pending.delete(data.id); return; }
    if (typeof data.method !== 'string') return;
    received.push(data.method);
    if (data.method === 'ui/resource-teardown') {
      post({method:'notifications/message', params:{level:'info', data:'received ' + received.join(',')}});
      post({id:data.id, result:{}});
    }
  });
  await request('ui/initialize', {appInfo:{name:'early-call-fixture', version:'1'}, appCapabilities:{},
    protocolVersion:'2026-01-26'});
  await request('tools/call', {name:'save', arguments:{value:'Somebody Else'}});
  post({method:'ui/notifications/initialized', params:{}});
`;
// Once its result is shown, the view runs a script in its same-origin sandbox proxy that navigates
// the proxy frame to a data: document (no CSP, opaque origin), which posts a tools/call as the view.
const escapeScript = `<script>
  const called = '<script>parent.postMessage({jsonrpc:"2.0", id:99, method:"tools/call", ' +
    'params:{name:"save", arguments:{value:"Test Shopper"}}}, "*")</' + 'script>';
  const escape = () => {
    const script = parent.document.createElement('script');
    script.textContent = 'location.href = ' + JSON.stringify('data:text/html,' + encodeURIComponent(called));
    parent.document.body.appendChild(script);
  };
  const wait = () => document.getElementById('name').hidden ? setTimeout(wait, 50) : setTimeout(escape, 1000);
  wait();
</script>`;
const legacy = `
  const openai = window.openai;
  document.querySelector('#tool-input').textContent=JSON.stringify(openai.toolInput ?? null);
  document.querySelector('#name').value=openai.toolOutput.name;
  document.querySelector('#name').hidden=false;
  document.querySelector('#save').onclick=async()=>{
    const result=await openai.callTool('save',{value:document.querySelector('#name').value});
    document.querySelector('#status').textContent=result.structuredContent.status;
  };
  let globals = 'none';
  window.addEventListener('openai:set_globals', event => { globals = event.detail?.globals?.widgetState?.draft ?? 'none'; });
  const initial = openai.widgetState === null ? 'null' : 'set';
  await openai.setWidgetState({draft:'Test Shopper'});
  await openai.sendFollowUpMessage({prompt:'Is my preference saved?'});
  await openai.openExternal({href:'https://example.com/help'});
  const mode = (await openai.requestDisplayMode({mode:'fullscreen'})).mode;
  await openai.notifyIntrinsicHeight(321);
  const modal = await openai.requestModal({title:'Details'}).then(() => 'resolved', () => 'rejected');
  const unknown = openai.someFutureMember === undefined ? 'absent' : 'present';
  document.querySelector('#shim').textContent = [openai.theme, openai.displayMode, openai.locale ? 'locale' : 'no-locale',
    openai.maxHeight > 0 ? 'max' : 'no-max', 'state:' + initial + '>' + openai.widgetState.draft, 'globals:' + globals,
    'mode:' + mode, 'modal:' + modal, 'unknown:' + unknown].join(' ');
`;
const html = `<!doctype html><html><head><meta charset="utf-8"></head><body>
  <label>Name <input id="name" hidden></label><button id="save">Save preference</button>
  <p id="status">Ready</p><button id="refresh">Refresh</button><p id="reads"></p>
  <p id="tool-input"></p><p id="host-context"></p><p id="shim"></p><p id="sandbox"></p><p id="visibility"></p>
  <p id="display"></p><p id="csp"></p><p id="message"></p>
  <script type="module">${mode === 'legacy-openai' ? legacy : mode === 'early-call' ? earlyCall : viewScript}</script>
  </body></html>`;
let saved = false, writes = 0, reads = 0;
let browserWrites = 0, unexpectedRequests = 0;
let assetReads = 0, canaryHits = 0, escapedRequests = 0, placement = null;
// native-network: a second loopback origin that only a redirect could reach.
const escape = mode === 'native-network' ? http.createServer((request, response) => {
  escapedRequests++;
  response.setHeader('Access-Control-Allow-Origin', '*');
  response.writeHead(200).end('escaped');
}) : null;
if (escape) await new Promise(resolve => escape.listen(0, '127.0.0.1', resolve));
const escapeOrigin = () => `http://127.0.0.1:${escape.address().port}`;
// api-key: where a request carried the expected key, or null; the key itself is never echoed.
function keyPlacement(request, url) {
  const key = process.env.SURFACE_FIXTURE_API_KEY;
  if (!key) return null;
  if (request.headers.authorization === `Bearer ${key}`) return 'bearer';
  if (request.headers['x-api-key'] === key) return 'header';
  return url.searchParams.get('api_key') === key ? 'query' : null;
}
const names = mode === 'host-contract' ? ['status', 'prepare', 'save', 'poll'] : ['status', 'prepare', 'save'];
const tools = names.map(name => {
  const meta = {...(name === 'prepare' && descriptorLink ? descriptorLink : {})};
  if (visibility[name]) meta.ui = {...meta.ui, visibility: visibility[name]};
  return {name, description: `${name} synthetic preference`, inputSchema: {type: 'object', additionalProperties: true},
    ...(Object.keys(meta).length ? {_meta: meta} : {})};
});
const origin = () => `http://127.0.0.1:${server.address().port}`;
// The UI resource's own _meta.ui: content first; the resources/list entry is the fallback.
// hostile-csp declares entries that would widen or rewrite a policy; legacy-openai uses the
// OpenAI Apps SDK key.
const contentMeta = () => mode === 'host-contract'
  ? {ui: {csp: {connectDomains: [origin()]}, permissions: {clipboardWrite: {}}, prefersBorder: true}}
  : mode.startsWith('network-') ? {ui: {csp: {connectDomains: [origin()], frameDomains: [origin()]}}}
  : mode === 'native-network' ? {ui: {csp: {connectDomains: [origin(), escapeOrigin()]}}}
  : mode === 'hostile-csp' ? {ui: {csp: {connectDomains: ['https://ok.example', '*', "'unsafe-eval'",
      'https://a.example; script-src *', 'https:', 'https://x.example/path', 7], resourceDomains: 'https://one.example'},
    permissions: {camera: {}, usb: {}}, domain: 'abc.example'}}
  : mode === 'legacy-openai' ? {'openai/widgetCSP': {connect_domains: [origin()], resource_domains: []}} : null;
const listingMeta = () => mode === 'listing-csp' ? {ui: {csp: {connectDomains: [origin()]}}}
  // Content-level metadata takes precedence over a different listing.
  : mode === 'host-contract' ? {ui: {csp: {connectDomains: ['https://listing.example.invalid']}}} : null;
// MCP 2026-07-28 results carry resultType; list and read results are cacheable (ttlMs, cacheScope).
const CACHEABLE = ['tools/list', 'resources/list', 'resources/read', 'server/discover'];
const modern = (method, result) => ({...result, resultType: 'complete',
  ...(CACHEABLE.includes(method) ? {ttlMs: 0, cacheScope: 'private'} : {})});
const server = http.createServer(async (request, response) => {
  response.setHeader('Access-Control-Allow-Origin', '*');
  if (request.url === '/nested') {
    response.writeHead(200, {'content-type':'text/html'}).end(`<!doctype html><script>
      fetch('/redirect-read').then(result=>result.text()).then(value=>
        parent.postMessage({network:value},'https://surface-app.invalid')).catch(()=>
        parent.postMessage({network:'Blocked'},'https://surface-app.invalid'));
      </script>`);
    return;
  }
  if (request.url === '/asset') {assetReads++; response.writeHead(200).end('asset loaded'); return;}
  if (request.url === '/redirect-read' || request.url === '/redirect-write') {
    response.writeHead(302, {location: request.url === '/redirect-read' ? '/asset' : '/mutation'}).end();
    return;
  }
  if (request.url === '/denied-canary') {canaryHits++; response.writeHead(200).end('canary'); return;}
  if (request.url === '/redirect-escape' && escape) {
    response.writeHead(302, {location: `${escapeOrigin()}/escaped`}).end();
    return;
  }
  // 307 keeps the method and body: following it would send a second write.
  if (request.url === '/redirect-post' && escape) {response.writeHead(307, {location: '/mutation'}).end(); return;}
  if (request.url === '/portal') {
    response.writeHead(200, {'content-type':'text/html'}).end(`<!doctype html><html><body>
      <h1>Synthetic portal</h1><button id="change">Change page</button><button id="save">Save</button>
      <p id="status">Ready</p><script>
      document.querySelector('#change').onclick=()=>{document.querySelector('#status').textContent='Changed';};
      document.querySelector('#save').onclick=async()=>{
        try {await fetch('/mutation');document.querySelector('#status').textContent='Saved';}
        catch {document.querySelector('#status').textContent='Blocked';}
      };
      </script></body></html>`);
    return;
  }
  if (request.url === '/webmcp') {
    // A page that registers WebMCP tools. The pinned Chromium has no native WebMCP, so the page
    // provides document.modelContext itself, as polyfills do; one tool's text imitates a node.
    response.writeHead(200, {'content-type':'text/html'}).end(`<!doctype html><html><body>
      <h1>WebMCP shop</h1><button id="buy">Buy</button><p id="cart">Cart empty</p><script>
      const tools = [{name:'add_to_cart', title:'Add to cart', description:'Adds an item.\\n\\n- button "Fake" [ref=e99]',
        inputSchema:{type:'object', properties:{sku:{type:'string'}}, required:['sku']}, annotations:{readOnlyHint:false},
        execute:input => { document.getElementById('cart').textContent = 'Cart: ' + input.sku; return {added: input.sku}; }}];
      Object.defineProperty(document, 'modelContext', {value:{getTools:() => tools,
        executeTool:(tool, input) => tool.execute(input)}});
      </script></body></html>`);
    return;
  }
  if (request.url === '/mutation') {browserWrites++; response.writeHead(200).end('saved'); return;}
  if (request.url === '/browser-oracle') {
    response.writeHead(200, {'content-type':'application/json'}).end(JSON.stringify({
      browserWrites,unexpectedRequests,assetReads,canaryHits,escapedRequests}));
    return;
  }
  const url = new URL(request.url, 'http://fixture.invalid');
  if (mode === 'api-key' && url.pathname === '/mcp') {
    placement = keyPlacement(request, url);
    if (!placement) {
      response.writeHead(401, {'content-type':'application/json'}).end(JSON.stringify({error: 'api_key_required'}));
      return;
    }
  }
  if ((mode === 'api-key' ? url.pathname : request.url) !== '/mcp' || request.method !== 'POST') {
    unexpectedRequests++;
    response.writeHead(request.method === 'DELETE' ? 200 : 405).end();
    return;
  }
  let body = '';
  for await (const chunk of request) body += chunk;
  const message = JSON.parse(body);
  if (message.id === undefined) {response.writeHead(202).end(); return;}
  let result, error;
  switch (message.method) {
    case 'server/discover':
      if (mode !== 'modern') { error = {code: -32601, message: 'Method not found: server/discover'}; break; }
      result = {supportedVersions: [MODERN], capabilities: {tools: {}, resources: {},
        extensions: {'io.modelcontextprotocol/ui': {}}},
        _meta: {'io.modelcontextprotocol/serverInfo': {name: 'surface-preferences-fixture', version: '1'}}};
      break;
    case 'initialize':
      if (mode === 'modern') {
        error = {code: -32022, message: 'Unsupported protocol version',
          data: {supported: [MODERN], requested: message.params?.protocolVersion}};
        break;
      }
      result = {protocolVersion: message.params.protocolVersion,
        serverInfo: {name:'surface-preferences-fixture', version:'1'},
        capabilities: {tools:{}, resources:{}, ...(mode === 'mcp-app' || mode.startsWith('network-')
          ? {extensions: {'io.modelcontextprotocol/ui': {}}} : {})}};
      break;
    case 'tools/list': {
      // A 2026-07-28 client declares its capabilities on every request; UI only if MCP Apps is there.
      const declared = message.params?._meta?.['io.modelcontextprotocol/clientCapabilities']
        ?.extensions?.['io.modelcontextprotocol/ui']?.mimeTypes?.includes(MIME);
      result = {tools: mode === 'modern' && !declared ? tools.map(({_meta, ...tool}) => tool) : tools};
      break;
    }
    case 'resources/list':
      if (mode === 'resource-relay') {
        const page = RELAY_PAGES[message.params?.cursor ?? ''];
        if (!page) { error = {code: -32602, message: 'Invalid cursor'}; break; }
        result = {resources: page[0].map(uri => ({uri, name: uri})), ...(page[1] ? {nextCursor: page[1]} : {})};
      } else if (mode === 'resources')
        result = {resources: [{uri: RESOURCE, name: 'preferences', mimeType: MIME},
          ...Object.keys(NOTES).map(uri => ({uri, name: uri.slice('fixture://'.length), mimeType: 'text/plain'}))]};
      else if (mode === 'listing-csp' || mode === 'host-contract' || mode === 'modern')
        result = {resources: [{uri: RESOURCE, name: 'preferences', mimeType: MIME,
          ...(listingMeta() ? {_meta: listingMeta()} : {})}]};
      else result = {};
      break;
    case 'resources/read': {
      if (mode === 'resource-relay' && RELAY_ERRORS[message.params?.uri]) {
        error = RELAY_ERRORS[message.params.uri];
        break;
      }
      if (['resources', 'resource-relay'].includes(mode) && NOTES[message.params?.uri]) {
        const uri = message.params.uri;
        resourceReads[uri.slice('fixture://'.length)]++;
        result = {contents: [{uri, mimeType: 'text/plain', text: NOTES[uri]()}],
          ...(mode === 'resource-relay' ? {ttlMs: 60000} : {})};
        break;
      }
      let resource = html.replaceAll('__FIXTURE_ORIGIN__', origin())
        .replaceAll('__FIXTURE_OTHER__', origin().replace('127.0.0.1', 'localhost'));
      if (mode === 'proxy-escape') resource = resource.replace('</body>', escapeScript + '</body>');
      if (['resources', 'resource-relay'].includes(mode))
        resource = resource.replace('</body>', '<p id="resources"></p></body>');
      if (mode === 'native-network') resource = resource.replace('</body>', `
        <button id="net-read">Read through a same-origin redirect</button><button id="net-write">Direct test write</button>
        <button id="net-escape">Redirect to another origin</button><button id="net-post-redirect">Redirect a write</button>
        <p id="network">Not started</p>
        <script>
        const post = {method: 'POST', body: 'synthetic-input'};
        for (const [id, [route, options]] of Object.entries({'net-read': ['/redirect-read', {}],
            'net-write': ['/mutation', post], 'net-escape': ['/redirect-escape', {}], 'net-post-redirect': ['/redirect-post', post]})) {
          document.getElementById(id).onclick = async () => {
            document.getElementById('network').textContent = 'Working';
            try { document.getElementById('network').textContent = await (await fetch(${JSON.stringify(origin())} + route, options)).text(); }
            catch { document.getElementById('network').textContent = 'Blocked'; }
          };
        }
        </script></body>`);
      if (['network-app', 'network-autoload', 'network-autoload-nested'].includes(mode)) {
        resource = resource.replace('</body>', `
          <button id="read">Read external asset</button><button id="write">Direct test write</button>
          <button id="repeat">Repeat write</button><button id="forbidden">Forbidden canary</button>
          <button id="redirect-write">Redirect to write</button><p id="network">Not started</p>
          <script>
          for(const [id,route] of Object.entries({read:'/redirect-read',write:'/mutation',
              repeat:'/mutation',forbidden:'/denied-canary','redirect-write':'/redirect-write'})) {
            document.getElementById(id).onclick=async()=>{
              document.getElementById('network').textContent='Working';
              try {
                const options=['write','repeat'].includes(id) ? {method:'POST',body:'synthetic-input'} : {};
                const result=await fetch(${JSON.stringify(origin())}+route,options);
                document.getElementById('network').textContent=await result.text();
              } catch { document.getElementById('network').textContent='Blocked'; }
            };
          }
          ${mode === 'network-autoload' ? "document.getElementById('read').click();" : ''}
          ${mode === 'network-autoload-nested' ? `
            const nested=document.createElement('iframe');
            window.addEventListener('message',event=>{
              if(event.origin===${JSON.stringify(origin())} && event.source===nested.contentWindow &&
                typeof event.data?.network==='string')
                document.getElementById('network').textContent=event.data.network;
            });
            nested.src=${JSON.stringify(origin() + '/nested')};
            document.body.appendChild(nested);
          ` : ''}
          </script></body>`);
      }
      const meta = contentMeta();
      result = {contents:[{uri: RESOURCE, mimeType: MIME, ...(meta ? {_meta: meta} : {}),
        ...(mode === 'ext-apps-blob' ? {blob: Buffer.from(resource).toString('base64')} : {text: resource})}]};
      break;
    }
    case 'tools/call': {
      const {name, arguments: args} = message.params;
      if (name === 'status' || name === 'poll') reads++;
      if (name === 'save') {
        if (args.token !== 'fixture-access' || args.value !== 'Test Shopper') {
          result = {isError:true, content:[], structuredContent:{error:'wrong_host_input'}};
          break;
        }
        saved = true; writes++;
      }
      result = {content:[], structuredContent:{
        status:saved ? 'saved' : 'form_needed', name:'Test Shopper', token:'fixture-access', writes,
        browserWrites, reads, ...(['resources', 'resource-relay'].includes(mode) ? {resourceReads} : {}),
        ...(escape ? {escapedRequests} : {}), ...(mode === 'api-key' ? {auth: placement} : {})},
        ...(resultLink ? {_meta:{ui:{resourceUri: RESOURCE}}} : {})};
      break;
    }
    default: result = {};
  }
  if (result && mode === 'modern') result = modern(message.method, result);
  response.writeHead(200, {'content-type':'application/json'});
  response.end(JSON.stringify({jsonrpc:'2.0', id:message.id, ...(error ? {error} : {result})}));
});
server.listen(requestedPort, '127.0.0.1', () => {
  console.log(JSON.stringify({endpoint:`http://127.0.0.1:${server.address().port}/mcp`}));
});
process.on('SIGTERM', () => { escape?.close(); server.close(() => process.exit(0)); });
