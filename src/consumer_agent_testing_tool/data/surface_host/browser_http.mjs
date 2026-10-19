// Host-only HTTP transport. Chromium has no network route to any destination.
// Addresses are explicitly pinned by the developer; DNS and automatic redirects
// cannot change the destination after request authorization.
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';

export class BrowserHTTP {
  constructor(policy, observe = () => {}) {
    this.policy = policy;
    this.observe = observe;
    this.active = new Set();
    this.closed = false;
    this.exhausted = false;
    this.requestCount = 0;
    this.requestBytes = 0;
    this.responseBytes = 0;
    const worker = policy.worker;
    this.pins = worker.destinations;
    this.limit = worker.max_response_bytes ?? 10 * 1024 * 1024;
    this.requestLimit = worker.max_request_bytes ?? 10 * 1024 * 1024;
    this.concurrentLimit = worker.max_concurrent_requests ?? 8;
    this.countLimit = worker.max_requests ?? 256;
    this.totalRequestLimit = worker.max_total_request_bytes ?? 32 * 1024 * 1024;
    this.totalResponseLimit = worker.max_total_response_bytes ?? 64 * 1024 * 1024;
    for (const value of [this.limit, this.requestLimit, this.concurrentLimit,
      this.countLimit, this.totalRequestLimit, this.totalResponseLimit])
      if (!Number.isSafeInteger(value) || value < 1) throw Error('Invalid HTTP broker resource limit');
    for (const origin of policy.origins)
      if (!net.isIP(this.pins[origin] || '')) throw Error('Every worker origin needs a literal IP pin');
    if (Object.keys(this.pins).some(origin => !policy.origins.includes(origin)))
      throw Error('A worker destination is outside the browser policy');
  }
  usage() {
    return {requests_admitted: this.requestCount, request_bytes_reserved: this.requestBytes,
      response_bytes_received: this.responseBytes, active_requests: this.active.size};
  }
  deny(reason, request) {
    this.observe({kind: 'network_denied', enforcement: 'host-http-budget',
      reason, url: request.url, method: request.method, usage: this.usage()});
    return Object.assign(Error(reason), {category: 'budget-exceeded'});
  }
  async fetch({url: value, method, headers = {}, body = null}) {
    if (this.closed) throw Error('Browser HTTP broker is closed');
    if (this.exhausted || this.responseBytes >= this.totalResponseLimit)
      throw this.deny('Total HTTP response budget is exhausted', {url: value, method});
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
        !this.policy.origins.includes(url.origin) || !this.policy.rules.some(rule =>
          rule.origin === url.origin && rule.path === url.pathname &&
          (rule.query || '') === url.search && rule.methods.includes(method)))
      throw Error('HTTP broker destination has not been authorized');
    const bytes = body === null ? null : Buffer.isBuffer(body) ? body : Buffer.from(body);
    if (bytes && bytes.length > this.requestLimit) throw Error('HTTP request exceeds the byte limit');
    const admission = {url: value, method};
    if (this.active.size >= this.concurrentLimit)
      throw this.deny('HTTP concurrency budget is exhausted', admission);
    if (this.requestCount >= this.countLimit)
      throw this.deny('HTTP request-count budget is exhausted', admission);
    if (this.requestBytes + (bytes?.length || 0) > this.totalRequestLimit)
      throw this.deny('Total HTTP request-byte budget is exhausted', admission);
    // Failed admissions never reach the target. Once admitted, attempts and
    // reserved request bytes are not refunded after errors or interruption.
    this.requestCount++;
    this.requestBytes += bytes?.length || 0;
    const outgoing = {};
    const hopHeaders = new Set(['host', 'connection', 'content-length', 'transfer-encoding',
      'upgrade', 'proxy-authorization', 'proxy-connection', 'keep-alive', 'te', 'trailer']);
    for (const [key, value] of Object.entries(headers))
      if (!hopHeaders.has(key.toLowerCase())) outgoing[key.toLowerCase()] = value;
    outgoing.host = url.host;
    outgoing.connection = 'close';
    if (bytes) outgoing['content-length'] = String(bytes.length);
    const transport = url.protocol === 'https:' ? https : http;
    let current;
    try { return await new Promise((resolve, reject) => {
      const request = transport.request({
        hostname: this.pins[url.origin], port: url.port || (url.protocol === 'https:' ? 443 : 80),
        servername: url.hostname.replace(/^\[|\]$/g, ''), method,
        path: url.pathname + url.search, headers: outgoing, agent: false,
        rejectUnauthorized: true, maxHeaderSize: 32768,
      }, response => {
        let size = 0;
        const chunks = [];
        response.on('data', bytes => {
          size += bytes.length;
          this.responseBytes += bytes.length;
          if (this.responseBytes > this.totalResponseLimit) {
            this.exhausted = true;
            const error = this.deny('Total HTTP response budget is exhausted', admission);
            for (const active of this.active) active.destroy(error);
          } else if (size > this.limit) request.destroy(Error('HTTP response exceeds the byte limit'));
          else chunks.push(bytes);
        });
        response.on('error', reject);
        response.on('aborted', () => reject(Error('HTTP response was interrupted')));
        response.on('end', () => {
          if (size > this.limit || this.exhausted) return;
          const headers = {};
          for (const [key, value] of Object.entries(response.headers))
            if (!['connection', 'transfer-encoding', 'keep-alive', 'trailer', 'upgrade'].includes(key))
              headers[key] = Array.isArray(value) ? value.join('\n') : value;
          this.observe({kind: 'browser_http', enforcement: 'host-pinned-http-broker',
            url: url.href, method, status: response.statusCode, response_bytes: size,
            usage: this.usage()});
          resolve({status: response.statusCode, headers, body: Buffer.concat(chunks)});
        });
      });
      current = request;
      this.active.add(request);
      const timer = setTimeout(() => request.destroy(Error('HTTP broker deadline exceeded')),
        this.policy.timeout_seconds * 1000);
      request.on('close', () => {clearTimeout(timer); this.active.delete(request);});
      request.on('error', reject);
      request.end(bytes);
    }); } finally {
      if (current) this.active.delete(current);
    }
  }
  async fulfill(route) {
    const request = route.request();
    const response = await this.fetch({url: request.url(), method: request.method(),
      headers: await request.allHeaders(), body: request.postDataBuffer()});
    await route.fulfill(response);
  }
  close() {
    this.closed = true;
    for (const request of this.active) request.destroy(Error('Browser HTTP broker closed'));
  }
}
