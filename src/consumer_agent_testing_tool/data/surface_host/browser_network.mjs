// Shared private Chromium redirect enforcement; never an agent tool.
import {guardChildTargets} from './browser_targets.mjs';

export class BrowserNetwork {
  constructor(options = {}) {
    Object.assign(this, options);
    this.redirectGuards = new WeakMap();
    this.redirectGuardReady = new WeakSet();
  }
  async guardRequestFrame(frame) {
    if (!frame) throw Error('Browser request frame unavailable');
    // Same-process frames share the page interceptor. Separate targets remain
    // paused until their own interceptor and recursive attachment are ready.
    return this.guardRedirects(frame.page());
  }
  watchFrames(page) {
    page.on('framenavigated', frame => {
      this.guardRequestFrame(frame).catch(async () => {
        this.observe({kind: 'network_denied', enforcement: 'chromium-response-interceptor',
          reason: 'Frame redirect guard unavailable; browser context closed'});
        await this.context.close().catch(() => {});
      });
    });
  }
  ruleFor(url, method) {
    return this.policy.rules.find(rule => rule.origin === url.origin &&
      rule.path === url.pathname && (rule.query || '') === url.search &&
      rule.methods.includes(method));
  }
  guardRedirects(page) {
    if (this.redirectGuards.has(page)) return this.redirectGuards.get(page);
    // Playwright routing handles only the first request of a redirect chain.
    // This trusted response interceptor validates each Location before Chromium follows it.
    // CDP is never exposed in the agent action dispatcher.
    const ready = (async () => {
      const session = await this.context.newCDPSession(page);
      await this.installInterceptor(session, page);
      await guardChildTargets(session, child => this.installInterceptor(child, page), async () => {
        this.observe({kind: 'network_denied', enforcement: 'chromium-response-interceptor',
          reason: 'Iframe startup guard failed; browser context closed'});
        await this.context.close().catch(() => {});
      });
      this.redirectGuardReady.add(page);
    })();
    this.redirectGuards.set(page, ready);
    return ready;
  }
  async installInterceptor(session, page) {
    session.on('Fetch.requestPaused', async event => {
      try {
        if (event.responseErrorReason) {
          await session.send('Fetch.failRequest', {requestId: event.requestId,
            errorReason: event.responseErrorReason});
          return;
        }
        if (this.http && event.responseStatusCode === undefined) {
          if (!event.redirectedRequestId) {
            await session.send('Fetch.continueRequest', {requestId: event.requestId});
            return;
          }
          // Playwright does not route subsequent redirect requests. Service
          // those here, but never let a redirect consume another write grant.
          const destination = new URL(event.request.url);
          const rule = this.ruleFor(destination, event.request.method);
          if (!rule || rule.effect !== 'read') throw Error('Redirect is not an allowed read');
          const result = await this.http.fetch({url: event.request.url,
            method: event.request.method, headers: event.request.headers,
            body: event.request.postData === undefined ? null : Buffer.from(event.request.postData)});
          const responseHeaders = Object.entries(result.headers).flatMap(([name, value]) =>
            (name === 'set-cookie' ? value.split('\n') : [value]).map(value => ({name, value})));
          await session.send('Fetch.fulfillRequest', {requestId: event.requestId,
            responseCode: result.status, responseHeaders, body: result.body.toString('base64')});
          return;
        }
        const locations = event.responseHeaders?.filter(header => header.name.toLowerCase() === 'location') || [];
        const location = locations[0];
        if ([301,302,303,307,308].includes(event.responseStatusCode) && location) {
          const destination = new URL(location.value, event.request.url);
          let method = event.request.method;
          if ((event.responseStatusCode === 303 && method !== 'HEAD') ||
              ([301,302].includes(event.responseStatusCode) && method === 'POST')) method = 'GET';
          const rule = this.ruleFor(destination, method);
          // A redirect is not authority to consume another write grant. Require a new action.
          if (locations.length !== 1 || !rule || rule.effect !== 'read' ||
              destination.username || destination.password) {
            this.observe({kind:'network_denied', enforcement:'chromium-response-interceptor',
              reason:'Redirect destination is not an explicitly allowed read',
              url:destination.href, method});
            // Lets an owner label a tab that shows the resulting error page as a policy block.
            if (page && event.resourceType === 'Document') this.redirectDenied?.(page, destination.href);
            await session.send('Fetch.failRequest', {requestId:event.requestId, errorReason:'BlockedByClient'});
            return;
          }
        }
        await session.send('Fetch.continueResponse', {requestId:event.requestId});
      } catch (error) {
        this.observe({kind:'network_denied', enforcement:'chromium-response-interceptor',
          reason:'Redirect guard failed; browser context closed'});
        await this.context.close().catch(() => {});
      }
    });
    await session.send('Fetch.enable', {patterns:[
      ...(this.http ? [{urlPattern:'*', requestStage:'Request'}] : []),
      {urlPattern:'*', requestStage:'Response'}]});
  }
}
