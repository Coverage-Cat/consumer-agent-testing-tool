"""Pointer routing failures must never become premature or repeated presses."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {clickEmbedded} = await import(pathToFileURL(process.argv[1]).href);
const mode = process.argv[2];
let pointer = 'outer-iframe', moves = 0, presses = 0, effects = 0;
const locator = {
  async hover() {
    moves++;
    if (mode !== 'never-ready' && moves > 1) pointer = 'button';
  },
  async evaluate(probe) {
    return probe({matches: selector => {
      assert.equal(selector, ':scope:hover');
      return pointer === 'button';
    }});
  },
  async click() {
    presses++;
    assert.equal(pointer, 'button', 'mouse press delivered to the outer iframe');
    effects++;
    if (mode === 'lost-reply') throw Error('reply lost after committed effect');
  },
};
if (mode === 'never-ready') {
  await assert.rejects(clickEmbedded(locator, 60), /pointer readiness timed out/);
  assert.ok(moves > 0);
  assert.equal(presses, 0);
  assert.equal(effects, 0);
} else if (mode === 'lost-reply') {
  await assert.rejects(clickEmbedded(locator, 1000), /reply lost after committed effect/);
  assert.equal(presses, 1);
  assert.equal(effects, 1);
} else {
  await clickEmbedded(locator, 1000);
  assert.equal(presses, 1);
  assert.equal(effects, 1);
}
"""


@pytest.mark.parametrize("mode", ["frame-becomes-ready", "never-ready", "lost-reply"])
def test_pointer_delivery_before_press_without_click_retry(mode):
    root = Path.cwd()
    module = root / "src/consumer_agent_testing_tool/data/surface_host/embedded_click.mjs"
    result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, str(module), mode],
                            cwd=root, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
