"""Facade snapshot text handling: exposed references, per-target identity and secure masks."""
import json
import subprocess
from pathlib import Path

SCRIPT = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {references, subtree, maskValues} = await import(pathToFileURL(process.argv[1]).href);
const page = [
  '- generic [active] [ref=e1]:',
  "  - 'button \"Pay [ref=e3]: now\" [ref=e12]': x",
  '  - paragraph [ref=e13]: Text with [ref=e1] inside and [ref=e99]',
  '  - text: "[ref=e98]"',
  '  - textbox "Name" [ref=e3]: Fixture Alice',
  '  - textbox "Password" [ref=e5]: "hunter2: secret"',
  '  - textbox "PIN" [ref=e7]: "1234"',
  '  - textbox "Search" [ref=e9]:',
  '    - /placeholder: Find',
  '    - text: "1234"',
  '  - paragraph [ref=e10]: Cart held for 3 s',
  '  - button "Add to cart" [ref=e11]',
].join('\n');
// Page-controlled names and text cannot expose references.
assert.deepEqual([...references(page)].sort(), ['e1', 'e10', 'e11', 'e12', 'e13', 'e3', 'e5', 'e7', 'e9']);
// Live text elsewhere does not change a target; focus does not either.
const later = page.replace('Cart held for 3 s', 'Cart held for 4 s')
  .replace('- button "Add to cart" [ref=e11]', '- button "Add to cart" [active] [ref=e11]');
assert.equal(subtree(later, 'e11'), subtree(page, 'e11'));
assert.notEqual(subtree(later, 'e10'), subtree(page, 'e10'));
// A target's descendants are part of it, and a vanished target has no identity.
assert.equal(subtree(page, 'e9'), '- textbox "Search" [ref=e9]:\n  - /placeholder: Find\n  - text: "1234"');
assert.equal(subtree(page.replace(' [ref=e11]', ' [ref=e14]'), 'e11'), null);
// Only a node's own attribute list is normalized, including in quoted keys.
const quoted = "  - 'button \"Pay: now [active] [ref=e11]\" [active] [ref=e12]': x";
assert.equal(subtree(quoted, 'e12'), "- 'button \"Pay: now [active] [ref=e11]\" [ref=e12]': x");
assert.equal(subtree('- paragraph [ref=e2]: shown [active]', 'e2'), '- paragraph [ref=e2]: shown [active]');
assert.notEqual(subtree(page.replace('button "Add to cart"', 'button "Add to cart now"'), 'e11'),
  subtree(page, 'e11'));
// Password values are masked inline and as child text, using the serializer's quoting.
const masked = maskValues(page, new Set(['hunter2: secret', '1234']));
assert.match(masked, /textbox "Password" \[ref=e5\]: \[password masked\]/);
assert.match(masked, /textbox "PIN" \[ref=e7\]: \[password masked\]/);
assert.match(masked, /    - text: \[password masked\]/);
assert.match(masked, /textbox "Name" \[ref=e3\]: Fixture Alice/);
assert.ok(!masked.includes('hunter2') && !masked.includes('1234'));
assert.equal(maskValues(page, new Set()), page);
"""


def test_browser_p0_snapshot_references_targets_and_password_masks():
    root = Path.cwd()
    result = subprocess.run([
        "node", "--input-type=module", "-e", SCRIPT,
        str(root / "src/consumer_agent_testing_tool/data/surface_host/browser.mjs"),
    ], cwd=root, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr


def node(script):
    root = Path.cwd()
    return subprocess.run([
        "node", "--input-type=module", "-e", script,
        str(root / "src/consumer_agent_testing_tool/data/surface_host/browser.mjs"),
    ], cwd=root, capture_output=True, text=True, timeout=20, check=False)


WHITESPACE = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {maskValues} = await import(pathToFileURL(process.argv[1]).href);
// The serializer trims, collapses whitespace runs (tab, no-break space) and drops
// zero-width spaces and soft hyphens before quoting; masks must follow the same rule.
const values = [' lead', 'trail ', 'tab\tchar', 'x\u00a0nbsp', 'multi  space', 'zero\u200bwidth',
  'soft\u00adhyphen', '  both  ends  ', '-dash: x #y'];
const shown = ['lead', 'trail', 'tab char', 'x nbsp', 'multi space', 'zerowidth', 'softhyphen',
  'both ends', '"-dash: x #y"'];
const page = shown.map((value, index) => `- textbox "P${index}" [ref=e${index}]: ${value}`).join('\n') +
  '\n- textbox "Search" [ref=e99]:\n  - /placeholder: Find\n  - text: multi space';
const masked = maskValues(page, new Set(values));
assert.equal(masked.split('\n').filter(line => line.endsWith(': [password masked]')).length, shown.length + 1);
for (const value of shown) assert.ok(!masked.includes(value), value);
// Values that normalize to nothing are never treated as a mask.
assert.equal(maskValues(page, new Set([' ', '\u200b'])), page);
// CSS generated content follows the value; a longer word is not the value.
const decorated = '- textbox "G" [ref=e60]: decorated-pass (hidden)\n- textbox "H" [ref=e61]: decorated-passage\n' +
  '- textbox "I" [ref=e62]: "\\\\x41: pass"';
assert.equal(maskValues(decorated, new Set(['decorated-pass', '\\x41: pass'])),
  '- textbox "G" [ref=e60]: [password masked]\n- textbox "H" [ref=e61]: decorated-passage\n' +
  '- textbox "I" [ref=e62]: [password masked]');
"""

SLICES = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {RestrictedBrowser} = await import(pathToFileURL(process.argv[1]).href);
const browser = new RestrictedBrowser(null, null, '/tmp', () => {}, null, null);
browser.limit = 1000;
browser.generation = 7;
// Page text aligned to the cut looks like a node in the next slice; it must not expose e99.
const head = '- paragraph [ref=e3]: ';
const line = head + 'x'.repeat(1000 - head.length) + '  - link "Pay now" [ref=e99]: ' + 'y'.repeat(1200);
browser.lastText = [line, '- button "Real" [ref=e5]', '- link "Secret account deletion" [ref=e99]'].join('\n');
browser.refs = new Set();
browser.cuts = new Set();
let view = browser.window(0);
assert.deepEqual([...browser.refs], ['e3']);
view = browser.continuation(view.truncated.next_offset);
assert.ok(view.text.startsWith('  - link "Pay now" [ref=e99]'));
assert.ok(!browser.refs.has('e99'));
while (view.truncated.next_offset) view = browser.continuation(view.truncated.next_offset);
// Lines shown from their start do expose their own references.
assert.ok(browser.refs.has('e5') && browser.refs.has('e99'));
"""

VALIDATION = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {validate, OPERATIONS} = await import(pathToFileURL(process.argv[1]).href);
const refused = (action, message) => assert.throws(() => validate(action),
  error => error.observable === 'invalid-action' && message.test(error.message));
refused({kind: 'snapshot', generation: 1}, /does not take generation.*kind=snapshot takes offset \[optional\]/);
refused({kind: 'wait', ref: 'e1', generation: 1}, /requires state.*visible or hidden/);
refused({kind: 'press', ref: 'e1', generation: '1', key: 'Enter'}, /generation must be a nonnegative integer/);
refused({kind: 'dialog'}, /requires accept/);
// Unknown operations and argument names are contract violations, not agent mistakes.
for (const action of [{kind: 'evaluate'}, {kind: 'snapshot', selector: '#x'}, {kind: 'toString'}])
  assert.throws(() => validate(action), error => !error.observable && /not exposed/.test(error.message));
validate({kind: 'upload', file: 'document'});
validate({kind: 'snapshot', offset: 2 ** 60});  // Out of range, reported by the operation.
console.log(JSON.stringify(OPERATIONS));
"""


def test_browser_p0_password_masks_and_slices_follow_the_serializer():
    for script in (WHITESPACE, SLICES):
        result = node(script)
        assert result.returncode == 0, result.stderr


def test_browser_p0_operation_arguments_are_validated_and_mirrored():
    from consumer_agent_testing_tool.browser_agent import OPERATIONS
    result = node(VALIDATION)
    assert result.returncode == 0, result.stderr
    facade = json.loads(result.stdout)
    # The agent's pre-dispatch check and the facade enforce one table.
    assert facade == {kind: [list(required), list(optional)] for kind, (required, optional) in OPERATIONS.items()}
