// Trusted developer capture only. No capture operation is in the agent dispatcher.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const hash = value => crypto.createHash('sha256').update(value).digest('hex');

export class BrowserDiagnostics {
  constructor(root, options, directory) {
    this.options = options;
    this.count = 0;
    this.bytes = 0;
    this.stopped = false;
    if (!options.enabled) return;
    const actual = fs.realpathSync(directory);
    if (!actual.startsWith(root + path.sep) || path.basename(actual) !== 'diagnostics.private')
      throw Error('Diagnostics must use the task private directory');
    this.directory = actual;
    fs.chmodSync(actual, 0o700);
    if (options.retain_raw_trace) {
      this.work = path.join(actual, '.work');
      fs.mkdirSync(this.work, {mode: 0o700});
    }
  }

  async start(context) {
    this.context = context;
    if (this.options.enabled && this.options.retain_raw_trace) {
      try {
        await context.tracing.start({screenshots: true, snapshots: true, sources: false});
        this.tracing = true;
      } catch {
        this.startFailed = true;
      }
    }
  }

  save(buffer, extension) {
    const sha256 = hash(buffer), filename = `${sha256}.${extension}`;
    const target = path.join(this.directory, filename);
    if (fs.existsSync(target)) {
      if (!fs.lstatSync(target).isFile() || hash(fs.readFileSync(target)) !== sha256)
        throw Error('Private diagnostic artifact changed');
    } else {
      if (this.bytes + buffer.length > this.options.max_bytes)
        return {status: 'limit-exceeded', reason: 'retained-artifact-byte-limit'};
      fs.writeFileSync(target, buffer, {flag: 'wx', mode: 0o600});
      this.bytes += buffer.length;
    }
    return {status: 'captured', path: filename, sha256, bytes: buffer.length};
  }

  async capture(browser, generation) {
    if (!this.options.enabled) return {status: 'disabled'};
    if (this.stopped) throw Error('Diagnostics collection has stopped');
    if (browser.busy) throw Error('Browser operation is in progress');
    if (generation !== browser.generation) throw Error('Diagnostic snapshot generation changed');
    if (this.count >= this.options.max_screenshots)
      return {status: 'limit-exceeded', reason: 'screenshot-count-limit', generation};
    this.count++;
    // The same masked page text the facade emitted, so password values never decide pairing.
    const before = await browser.pageText();
    if (before !== browser.lastText)
      return {status: 'observation-gap', reason: 'page-changed-before-capture', generation};
    const mask = [];
    for (const frame of browser.page.frames()) {
      // Masks cross frame boundaries and editable shadow-DOM controls. Additional
      // developer selectors can mask non-form content, including canvases.
      mask.push(frame.locator('input, textarea, select, [contenteditable], [data-sensitive]'));
      for (const selector of this.options.mask_selectors) mask.push(frame.locator(selector));
    }
    const buffer = await browser.page.screenshot({
      type: 'png', fullPage: false, animations: 'allow', caret: 'initial',
      mask, maskColor: '#FF00FF', timeout: browser.policy.timeout_seconds * 1000,
    });
    const after = await browser.pageText();
    if (after !== before || generation !== browser.generation)
      return {status: 'observation-gap', reason: 'page-changed-during-capture', generation};
    return {...this.save(buffer, 'png'), generation, pairing: 'matching-accessibility-snapshots',
      redaction: 'form-and-declared-selector-masks; not certified for export',
      audience: 'developer-only'};
  }

  async stop() {
    if (this.stopped) return this.result;
    this.stopped = true;
    this.result = {status: 'disabled'};
    if (this.startFailed) this.result = {status: 'failed', reason: 'trace-start-failed'};
    if (!this.tracing) {
      if (this.work) fs.rmSync(this.work, {recursive: true, force: true});
      return this.result;
    }
    const temporary = path.join(this.directory, 'trace.pending.zip');
    try {
      await this.context.tracing.stop({path: temporary});
      if (fs.statSync(temporary).size + this.bytes > this.options.max_bytes)
        this.result = {status: 'limit-exceeded', reason: 'retained-artifact-byte-limit'};
      else this.result = {...this.save(fs.readFileSync(temporary), 'zip'),
        audience: 'developer-only', redaction: 'raw-restricted'};
      return this.result;
    } finally {
      fs.rmSync(temporary, {force: true});
      if (this.work) fs.rmSync(this.work, {recursive: true, force: true});
    }
  }
}
