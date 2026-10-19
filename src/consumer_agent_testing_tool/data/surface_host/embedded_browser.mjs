// Embedded apps use the same offline worker and host HTTP boundary as browse.
// Network writes require an exact one-use host grant; resource metadata is not authority.
import crypto from 'node:crypto';
import {ContainerBrowser} from './browser_worker.mjs';
import {BrowserHTTP} from './browser_http.mjs';
import {BrowserNetwork} from './browser_network.mjs';

export class EmbeddedBrowser {
  constructor(root, scenario, chromium, authorize, observe) {
    this.authorize = authorize;
    this.observe = observe;
    this.chromium = chromium;
    this.policy = {worker: scenario.browser_worker, origins: scenario.browser_origins,
      rules: scenario.browser_worker.rules, timeout_seconds: scenario.timeout_seconds};
    this.http = new BrowserHTTP(this.policy, observe);
    this.worker = new ContainerBrowser(root, scenario.browser_worker, value => {
      this.identity = value;
      observe(value);
    });
  }
  async open() {
    if (!this.browser) this.browser = await this.worker.launch(this.chromium);
    this.context = await this.browser.newContext({serviceWorkers: 'block'});
    const {kind, ...identity} = this.identity;
    const response = await this.authorize('__surface_browser_identity', {
      ...identity, browser_version: this.browser.version()});
    if (response.accepted !== true) throw Error('Host did not accept the browser runtime identity');
    this.network = new BrowserNetwork({policy: this.policy, context: this.context,
      http: this.http, observe: this.observe});
    this.context.on('page', page => {
      this.network.watchFrames(page);
      this.network.guardRedirects(page).catch(() => page.close().catch(() => {}));
    });
    return this.context;
  }
  async route(route) {
    const request = route.request(), url = new URL(request.url());
    let enforcement = 'embedded-network-policy';
    try {
      const rule = this.network.ruleFor(url, request.method());
      if (!this.policy.origins.includes(url.origin) || url.username || url.password || !rule)
        throw Error('Embedded request has no exact destination rule');
      if (!this.network.redirectGuardReady.has(request.frame().page()))
        throw Error('Embedded redirect guard is not ready');
      await this.network.guardRequestFrame(request.frame());
      if (rule.effect === 'write') {
        enforcement = 'host-ui-network-grant';
        const result = await this.authorize('__surface_browser_request', {
          url: request.url(), method: request.method(),
          body_sha256: crypto.createHash('sha256').update(request.postDataBuffer() || Buffer.alloc(0)).digest('hex'),
        });
        if (result.authorized !== true) throw Error('No host authority for browser request');
      }
      enforcement = 'host-pinned-http-broker';
      await this.http.fulfill(route);
    } catch (error) {
      this.observe({kind: 'network_denied', enforcement, url: request.url(),
        method: request.method(), reason: error.message});
      await route.abort().catch(() => {});
    }
  }
  async finishContext() {
    await this.context?.close();
    const until = Date.now() + this.policy.timeout_seconds * 1000;
    while (this.http.active.size && Date.now() < until)
      await new Promise(resolve => setTimeout(resolve, 25));
    if (this.http.active.size) throw Error('Embedded HTTP requests remain unresolved');
  }
  async close() {
    this.http.close();
    await this.worker.close();
  }
}
