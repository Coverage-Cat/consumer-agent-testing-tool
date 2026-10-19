// Private, pinned Chromium protocol transport for paused child targets.
// Playwright's public CDPSession sends on one session. Child sessions are
// multiplexed through the parent's non-flat Target protocol, never agent tools.
import {EventEmitter} from 'node:events';

class ChildSession extends EventEmitter {
  constructor(parent, id) {
    super();
    this.parent = parent;
    this.id = id;
    this.serial = 0;
    this.pending = new Map();
    this.closed = false;
  }
  send(method, params = {}) {
    if (this.closed) return Promise.reject(Error('Browser target detached'));
    const id = ++this.serial;
    return new Promise((resolve, reject) => {
      this.pending.set(id, {resolve, reject});
      this.parent.send('Target.sendMessageToTarget', {
        sessionId: this.id, message: JSON.stringify({id, method, params}),
      }).catch(error => {
        this.pending.delete(id);
        reject(error);
      });
    });
  }
  receive(message) {
    const value = JSON.parse(message);
    if (value.id !== undefined) {
      const pending = this.pending.get(value.id);
      this.pending.delete(value.id);
      if (value.error) pending?.reject(Error('Child browser protocol command failed'));
      else pending?.resolve(value.result);
    } else if (value.method) this.emit(value.method, value.params);
  }
  close() {
    this.closed = true;
    for (const pending of this.pending.values()) pending.reject(Error('Browser target detached'));
    this.pending.clear();
    this.emit('close');
    this.removeAllListeners();
  }
}

export async function guardChildTargets(parent, install, failed) {
  const children = new Map();
  parent.on('Target.receivedMessageFromTarget', event => {
    try { children.get(event.sessionId)?.receive(event.message); }
    catch (error) { failed(error); }
  });
  parent.on('Target.detachedFromTarget', event => {
    children.get(event.sessionId)?.close();
    children.delete(event.sessionId);
  });
  parent.on('close', () => {
    for (const child of children.values()) child.close();
    children.clear();
  });
  parent.on('Target.attachedToTarget', event => {
    const child = new ChildSession(parent, event.sessionId);
    children.set(event.sessionId, child);
    (async () => {
      if (event.targetInfo.type !== 'iframe' || !event.waitingForDebugger)
        throw Error('Iframe started without its network guard');
      await install(child);
      await guardChildTargets(child, install, failed);
      await child.send('Runtime.runIfWaitingForDebugger');
    })().catch(error => { if (!child.closed) failed(error); });
  });
  await parent.send('Target.setAutoAttach', {
    autoAttach: true, waitForDebuggerOnStart: true, flatten: false,
    filter: [{type: 'iframe', exclude: false}, {exclude: true}],
  });
}
