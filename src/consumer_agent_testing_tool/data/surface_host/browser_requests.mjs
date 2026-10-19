// Optional private request ledger for scenario execution. No headers or cookies cross it.
import crypto from 'node:crypto';

export class BrowserRequests {
  constructor(browser) {
    this.browser = browser;
    this.requests = new WeakMap();
    this.pending = new Set();
    browser.context.on('response', response => this.complete(response.request(), response));
    browser.context.on('requestfailed', request => this.complete(request, null));
  }

  async admit(route) {
    const request = route.request(), url = new URL(request.url());
    const row = {request_id: crypto.randomUUID(), url: request.url(), method: request.method(),
      body: request.postData() || ''};
    const state = {row};
    this.requests.set(request, state);
    try {
      const privateState = this.browser.policy.scenario_private_storage
        ? {storage_state: await this.browser.storageState()} : {};
      const result = await this.browser.authorize('scenario_browser_request', {...row, ...privateState});
      if (!result.allowed) {
        state.denied = true;
        return route.fulfill({status: 403, contentType: 'application/json',
          body: JSON.stringify({host_denial: true, error: result.reason || 'The host declined this request'})});
      }
      if (!['read', 'write'].includes(result.effect)) throw Error('Invalid request classification');
      if (!this.browser.ruleFor(url, request.method())) {
        this.browser.policy.rules.push({id: 'scenario:' + row.request_id, origin: url.origin,
          path: url.pathname, query: url.search, methods: [request.method()], effect: result.effect});
      }
      return await this.browser.continueRequest(route);
    } catch {
      state.denied = true;
      return route.abort();
    }
  }

  complete(request, response) {
    const state = this.requests.get(request);
    if (!state || state.completion) return;
    const completion = (async () => {
      let status = null, body = null;
      try {
        if (response) {
          status = response.status();
          if (response.headers()['content-type']?.includes('application/json')) {
            const bytes = await response.body();
            if (bytes.length > 1024 * 1024) throw Error('Response exceeded the scenario capture limit');
            body = JSON.parse(bytes.toString('utf8'));
          }
        }
      } catch { status = null; }
      await this.browser.authorize('scenario_browser_response', {
        request_id: state.row.request_id, status, body, host_denial: !!state.denied});
    })();
    state.completion = completion;
    this.pending.add(completion);
    completion.catch(() => { this.error = Error('Scenario response checkpoint failed'); })
      .finally(() => this.pending.delete(completion));
  }

  async drain() {
    while (this.pending.size) await Promise.allSettled([...this.pending]);
    if (this.error) throw this.error;
  }
}
