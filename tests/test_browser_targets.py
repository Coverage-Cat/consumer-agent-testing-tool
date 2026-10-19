"""Iframe execution is withheld until guards are ready; failures never resume it."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = r"""
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {pathToFileURL} from 'node:url';
const {guardChildTargets} = await import(pathToFileURL(process.argv[1]).href);
const mode = process.argv[2], parent = new EventEmitter(), commands = [], failures = [];
let child, pending;
const flush = () => new Promise(resolve => setImmediate(resolve));
const reply = (id, result = {}) => parent.emit('Target.receivedMessageFromTarget', {
  sessionId:'child', message:JSON.stringify({id, result}),
});
parent.send = async (method, params) => {
  if (method === 'Target.setAutoAttach') {
    assert.equal(params.waitForDebuggerOnStart, true);
    assert.deepEqual(params.filter, [{type:'iframe',exclude:false},{exclude:true}]);
    return {};
  }
  assert.equal(method, 'Target.sendMessageToTarget');
  const message = JSON.parse(params.message);
  commands.push(message.method);
  if (message.method === 'Fetch.enable') {
    pending = message.id;
    if (mode === 'transport-error') throw Error('transport lost');
  } else queueMicrotask(() => reply(message.id));
  return {};
};
await guardChildTargets(parent, async session => {
  child = session;
  await session.send('Fetch.enable', {patterns:[]});
}, error => failures.push(error));
parent.emit('Target.attachedToTarget', {
  sessionId:'child', targetInfo:{type:'iframe'}, waitingForDebugger:mode !== 'unpaused',
});
await flush();
assert.ok(!commands.includes('Runtime.runIfWaitingForDebugger'));
if (mode === 'ready') {
  assert.deepEqual(commands, ['Fetch.enable']);
  reply(pending);
  await flush();
  assert.deepEqual(commands, [
    'Fetch.enable', 'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger',
  ]);
  assert.equal(failures.length, 0);
} else if (mode === 'protocol-error') {
  parent.emit('Target.receivedMessageFromTarget', {sessionId:'child',
    message:JSON.stringify({id:pending, error:{code:-1, message:'fixture failure'}})});
  await flush();
  assert.equal(failures.length, 1);
} else if (mode === 'detached' || mode === 'parent-closed') {
  if (mode === 'detached') parent.emit('Target.detachedFromTarget', {sessionId:'child'});
  else parent.emit('close');
  await flush();
  assert.equal(failures.length, 0);
  await assert.rejects(child.send('Runtime.runIfWaitingForDebugger'), /detached/);
} else {
  assert.equal(failures.length, 1);
}
if (mode !== 'ready') assert.ok(!commands.includes('Runtime.runIfWaitingForDebugger'));
parent.emit('close');
await flush();
"""


@pytest.mark.parametrize("mode", [
    "ready", "unpaused", "protocol-error", "transport-error", "detached", "parent-closed",
])
def test_child_target_requires_completed_guard_before_execution(mode):
    root = Path.cwd()
    result = subprocess.run([
        "node", "--input-type=module", "-e", SCRIPT,
        str(root / "src/consumer_agent_testing_tool/data/surface_host/browser_targets.mjs"), mode,
    ], cwd=root, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
