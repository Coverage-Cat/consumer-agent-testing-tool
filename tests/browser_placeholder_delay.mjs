// Test-only scheduling treatment, injected by the test into the host process: a new tab's held
// first navigation commits its placeholder late, as on a loaded machine.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
const tooling = createRequire(path.resolve('tooling/payment-ui/package.json'));
// The restricted browser launches through @playwright/mcp's copy of Playwright.
const {chromium} = createRequire(tooling.resolve('@playwright/mcp'))('playwright');
const launch = chromium.launch.bind(chromium);
chromium.launch = async (...args) => {
  const browser = await launch(...args);
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async (...args) => {
    const context = await newContext(...args);
    const route = context.route.bind(context);
    context.route = (url, handler, ...rest) => route(url, (request, ...more) => {
      const fulfill = request.fulfill.bind(request);
      request.fulfill = async options => {
        if (options?.body === '<!doctype html><title></title>') {
          await new Promise(resolve => setTimeout(resolve, 1200));
          fs.appendFileSync(process.env.SURFACE_TEST_PLACEHOLDER_RECEIPT, 'delayed\n');
        }
        return fulfill(options);
      };
      return handler(request, ...more);
    }, ...rest);
    return context;
  };
  return browser;
};
