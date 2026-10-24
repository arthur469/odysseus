"""Exercise the stop button's confirmation and failure paths without a server."""

import base64
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node is required")
def test_application_stop_ui_behaviour():
    module = Path(__file__).resolve().parents[1] / "static/js/settings/application.js"
    encoded = base64.b64encode(module.read_bytes()).decode("ascii")
    script = r'''
import assert from 'node:assert/strict';
const { initApplicationControls } = await import('data:text/javascript;base64,ENCODED');
async function scenario({ visible = true, accepted = true, success = true } = {}) {
  let hidden = true;
  const card = { classList: { remove: name => { if (name === 'hidden') hidden = false; } } };
  const button = { disabled: false, addEventListener: (_, fn) => { button.click = fn; } };
  const message = { textContent: '' };
  const elements = { 'settings-application-card': card, 'settings-application-stop': button, 'settings-application-message': message };
  const requests = [];
  let confirmCount = 0;
  initApplicationControls({
    root: { getElementById: id => elements[id] },
    confirm: async () => { confirmCount++; return accepted; },
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith('/status')) return { ok: true, json: async () => ({ can_stop: visible }) };
      return { ok: success };
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(hidden, !visible);
  if (!visible) return;
  await button.click();
  assert.equal(confirmCount, 1);
  const posts = requests.filter(request => request.options.method === 'POST');
  if (!accepted) {
    assert.equal(posts.length, 0);
    assert.equal(button.disabled, false);
  } else {
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, '/api/application/stop');
    assert.deepEqual(JSON.parse(posts[0].options.body), { confirmation: 'stop-odysseus' });
    assert.equal(button.disabled, success);
    assert(message.textContent.includes(success ? 'Stop requested' : 'refused'));
    if (success) { await button.click(); assert.equal(confirmCount, 1); }
  }
}
await scenario({ visible: false });
await scenario({ accepted: false });
await scenario({ success: false });
await scenario();
console.log('Application stop UI behaviour passed');
'''.replace("ENCODED", encoded)
    result = subprocess.run(["node", "--input-type=module"], input=script, text=True,
                            encoding="utf-8", capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
