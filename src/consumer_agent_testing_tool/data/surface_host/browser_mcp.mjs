// Private Playwright MCP connection. The upstream catalog is never an agent toolset.
import fs from 'node:fs';
import path from 'node:path';

const fields = {
  browser_snapshot: [],
  browser_navigate: ['url'],
  browser_tabs: ['action', 'index'],
  browser_click: ['target'],
  browser_type: ['target', 'text'],
  browser_select_option: ['target', 'values'],
  browser_press_key: ['key'],
  // Answers the open JavaScript dialog. Clearing a file chooser never names a path:
  // approved files are set by the trusted facade, not through the upstream tool.
  browser_handle_dialog: ['accept'],
  browser_file_upload: [],
};
const optional = {browser_handle_dialog: ['promptText']};
// P3.4: page-registered WebMCP tools, a generic WebMCP host lane (basis: assumed). Meta documents a WebMCP host
// for web apps on its AI glasses (Meta AI on Ray-Ban Display, developer preview), a Meta product that is not
// Muse; this lane does not reproduce that host's own limits, so the label names no product.
export const WEBMCP_LABEL = 'generic WebMCP page tools, not Muse';
const WEBMCP_BASIS = {label: 'assumed', source: 'review 2026-10-15 P3.4; Playwright MCP 0.0.83 ' +
  '"webmcp": collect and expose the tools that a page registers through the experimental WebMCP API',
  product: {label: 'documented', note: 'Meta AI on Ray-Ban Display consumes WebMCP tools of web apps (developer ' +
    "preview); not Muse, and its host limits are not reproduced here", sources: [
    'https://wearables.developer.meta.com/docs/develop/webapps/agent-tools (retrieved 2026-10-15)',
    'https://github.com/webmachinelearning/webmcp/blob/main/implementation-status.md (retrieved 2026-10-15)']}};
// The pinned upstream prefixes a raw snapshot with page-provided tool lines that can imitate
// snapshot nodes. The aria part never contains a blank line, so the last one ends the prefix.
const UPSTREAM_LISTING = '- webmcp tools (page-provided, untrusted):\n';
const UPSTREAM_NOTE = /^\[UNTRUSTED: [^\]]*\] /;
// Shown in place of the upstream prefix: one JSON line whose key no snapshot node can match. It is
// page state; the lane label is run metadata, kept in evidence (backend identity, call events).
const LISTING_KEY = 'WebMCP page tools (page-provided, untrusted)';
const PAGE_TOOL_LIMIT = 50, DESCRIPTION_LIMIT = 2000, SCHEMA_LIMIT = 8000, ARGUMENT_LIMIT = 64 * 1024;

export class BrowserMCP {
  constructor({createConnection, Client, InMemoryTransport, version, playwrightVersion, webmcp = false}, observe) {
    if (typeof webmcp !== 'boolean') throw Error('Invalid WebMCP setting');
    Object.assign(this, {createConnection, Client, InMemoryTransport, version, playwrightVersion, webmcp, observe});
    this.pageToolNames = new Set();
  }

  async connect(context, directory, timeout) {
    this.directory = fs.mkdtempSync(path.join(directory, 'mcp-'));
    this.server = await this.createConnection({
      browser: {browserName: 'chromium', isolated: false},
      capabilities: [], webmcp: this.webmcp, saveSession: false,
      imageResponses: 'omit', codegen: 'none', snapshot: {mode: 'none', boxes: false},
      outputDir: this.directory, outputMaxSize: 10 * 1024 * 1024,
      timeouts: {action: timeout, navigation: timeout},
    }, async () => context);
    // Official SDK transport, with no TCP listener, browser debug port, or child
    // MCP endpoint. Both ends stay inside the trusted Node sidecar.
    const [clientTransport, serverTransport] = this.InMemoryTransport.createLinkedPair();
    const reply = serverTransport.send.bind(serverTransport);
    serverTransport.send = async (message, options) => {
      if (message.result?.protocolVersion) this.protocolVersion = message.result.protocolVersion;
      return reply(message, options);
    };
    const send = clientTransport.send.bind(clientTransport);
    clientTransport.send = async (message, options) => {
      if (message.method === 'tools/call') this.validate(message.params);
      else if (message.method && !['initialize', 'notifications/initialized', 'ping',
        'tools/list', 'notifications/cancelled'].includes(message.method))
        throw Error('MCP method is outside the restricted browser backend');
      return send(message, options);
    };
    this.client = new this.Client({name: 'consumer-agent-testing-tool-browser', version: '1.0.0'}, {capabilities: {}});
    await this.server.connect(serverTransport);
    await this.client.connect(clientTransport);
    const {tools} = await this.client.listTools();
    for (const name of Object.keys(fields)) {
      if (!tools.some(tool => tool.name === name))
        throw Error(`Pinned Playwright MCP is missing required tool ${name}`);
    }
    this.info = {name: '@playwright/mcp', version: this.version,
      playwright_version: this.playwrightVersion, transport: 'private-in-memory',
      protocol_version: this.protocolVersion, server: this.client.getServerVersion(),
      extensions: ['reference-focus', 'reference-scroll', 'reference-wait', 'approved-files',
        'dialog-response', 'file-chooser-release', 'password-masking',
        ...(this.webmcp ? ['webmcp-page-tools'] : [])],
      // Only an opted-in policy changes the backend identity, so default runs keep their lock.
      ...(this.webmcp ? {webmcp: {enabled: true, lane: WEBMCP_LABEL, basis: WEBMCP_BASIS,
        content: 'page-provided, untrusted'}} : {})};
    this.observe({kind: 'browser_backend', ...this.info});
  }

  validate(params) {
    if (this.webmcp && typeof params.name === 'string' && this.pageToolNames.has(params.name)) {
      // A tool the current page registered, as last shown: its input is the page's own schema.
      const args = params.arguments;
      if (!args || typeof args !== 'object' || Array.isArray(args) || JSON.stringify(args._meta) !== '{"raw":true}')
        throw Error('MCP tool or argument is outside the restricted browser backend');
      if (JSON.stringify(args).length > ARGUMENT_LIMIT) throw Error('WebMCP tool arguments exceed 64 KiB');
      return;
    }
    const allowed = Object.hasOwn(fields, params.name) ? fields[params.name] : null;
    const extra = Object.hasOwn(optional, params.name) ? optional[params.name] : [];
    const args = params.arguments;
    if (!allowed || !args || Object.keys(args).some(key => !allowed.includes(key) && !extra.includes(key)
        && key !== '_meta') ||
        allowed.some(key => !Object.hasOwn(args, key)) ||
        JSON.stringify(args._meta) !== '{"raw":true}')
      throw Error('MCP tool or argument is outside the restricted browser backend');
    if (params.name === 'browser_tabs' &&
        (args.action !== 'select' || !Number.isSafeInteger(args.index) || args.index < 0))
      throw Error('Only task-owned tab selection is supported');
    if (params.name === 'browser_handle_dialog' && (typeof args.accept !== 'boolean' ||
        (Object.hasOwn(args, 'promptText') && typeof args.promptText !== 'string')))
      throw Error('Dialog responses need a boolean and optional prompt text');
    if (Object.hasOwn(args, 'target') &&
        (typeof args.target !== 'string' || !/^(?:e\d+|f[\w-]+e\d+)$/.test(args.target)))
      throw Error('MCP target must be an emitted reference, never a selector');
  }

  async call(name, args = {}) {
    const result = await this.client.callTool({name, arguments: {...args, _meta: {raw: true}}});
    const page = this.pageToolNames.has(name);
    this.observe({kind: 'browser_backend_call', tool: name,
      status: result.isError ? 'failed' : 'passed', ...(page ? {lane: WEBMCP_LABEL} : {})});
    if (result.isError) {
      // Upstream errors can contain generated code, paths, or diagnostics.
      // Keep them out of the model's snapshot-only observation.
      this.observe({kind: 'browser_backend_error', tool: name, content: result.content});
      throw Error(`Playwright MCP ${name} failed; obtain a fresh snapshot before retrying`);
    }
    return result;
  }

  async snapshot() {
    const result = await this.call('browser_snapshot');
    if (result.content?.length !== 1 || result.content[0].type !== 'text')
      throw Error('Playwright MCP returned an unsupported snapshot representation');
    // Pinned upstream raw response excludes code, page headers, logs, and images.
    let text = result.content[0].text;
    if (text.startsWith(UPSTREAM_LISTING)) {
      // Page-defined tools enter the snapshot only when the policy enabled WebMCP.
      const boundary = text.lastIndexOf('\n\n');
      if (!this.webmcp || boundary < 0) throw Error('Playwright MCP returned an unsupported WebMCP listing');
      text = text.slice(boundary + 2);
    }
    if (!this.webmcp) return text;
    const tools = await this.pageTools();
    return tools.length ? `- ${LISTING_KEY}: ${JSON.stringify(tools)}\n${text}` : text;
  }

  // The current page's WebMCP tools, collected with the latest snapshot, as callable names
  // (upstream webmcp_<name>) with their page-provided titles, descriptions and schemas.
  async pageTools() {
    if (!this.webmcp) return [];
    const {tools} = await this.client.listTools();
    const page = tools.filter(tool => tool.name.startsWith('webmcp_')).slice(0, PAGE_TOOL_LIMIT);
    this.pageToolNames = new Set(page.map(tool => tool.name));
    return page.map(tool => {
      const description = String(tool.description ?? '').replace(UPSTREAM_NOTE, '');
      const schema = JSON.stringify(tool.inputSchema ?? {});
      return {name: tool.name, title: tool.annotations?.title ?? null,
        description: description.length > DESCRIPTION_LIMIT ? description.slice(0, DESCRIPTION_LIMIT - 3) + '...' : description,
        inputSchema: schema.length <= SCHEMA_LIMIT ? tool.inputSchema ?? {} : {omitted: `larger than ${SCHEMA_LIMIT} characters`},
        read_only: tool.annotations?.readOnlyHint === true};
    });
  }

  // Calls one tool the current page registered, as shown in the latest snapshot. The output is the
  // page's own, labeled untrusted by upstream; a page-reported error is an ordinary failure.
  async callPageTool(name, params = {}) {
    if (!this.webmcp) throw Error('WebMCP page tools are disabled for this browser task');
    if (typeof name !== 'string' || !this.pageToolNames.has(name))
      throw Error('WebMCP tool is not registered by the current page; take a fresh snapshot');
    if (!params || typeof params !== 'object' || Array.isArray(params) || Object.hasOwn(params, '_meta'))
      throw Error('WebMCP tool parameters must be a JSON object');
    const result = await this.call(name, params);
    return {lane: WEBMCP_LABEL, text: (result.content ?? []).filter(item => item.type === 'text')
      .map(item => item.text).join('\n')};
  }

  async close() {
    try { await this.client?.close(); }
    finally {
      try { await this.server?.close(); }
      finally {
        // Only the fresh private directory created by this instance is removed.
        if (this.directory) fs.rmSync(this.directory, {recursive: true, force: true});
      }
    }
  }
}
