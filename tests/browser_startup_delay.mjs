// Test-only scheduling treatment, injected by the test into the host process.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
const require = createRequire(path.resolve('tooling/payment-ui/package.json'));
const {chromium} = require('playwright');
const connect = chromium.connect.bind(chromium);
chromium.connect = async (...args) => {
  const browser = await connect(...args);
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async (...args) => {
    const context = await newContext(...args);
    const cdp = context.newCDPSession.bind(context);
    context.newCDPSession = async target => {
      const session = await cdp(target);
      const send = session.send.bind(session);
      session.send = async (method, params) => {
        let nested = {method, params};
        while (nested.method === 'Target.sendMessageToTarget')
          nested = JSON.parse(nested.params.message);
        if (nested.method === 'Fetch.enable' &&
            (method === 'Target.sendMessageToTarget' || target.parentFrame)) {
          await new Promise(resolve => setTimeout(resolve, 100));
          fs.appendFileSync(process.env.SURFACE_TEST_STARTUP_RECEIPT, 'delayed\n');
        }
        return send(method, params);
      };
      return session;
    };
    return context;
  };
  return browser;
};
