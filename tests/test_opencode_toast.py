"""OpenCode activity notifications must not misattribute a model response."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for plugin behavior test")
@pytest.mark.parametrize("provider_id", ["anthropic", "freellmpool"])
def test_pool_activity_toast_is_scoped_and_labels_uncorrelated_status(provider_id):
    source = (
        Path(__file__).resolve().parents[1] / "integrations/opencode/freellmpool.js"
    ).read_text()
    # Replace only the SDK's tool/schema factories; execute the real plugin hook.
    source = source.replace(
        'import { tool } from "@opencode-ai/plugin";',
        "const chain = new Proxy({}, { get: () => () => chain }); "
        "const tool = x => x; tool.schema = chain;",
    )
    program = f"""
delete process.env.FREELLMPOOL_PROXY_KEY;
process.env.FREELLMPOOL_TOAST = "1";
const toasts = [];
let statusRequests = 0;
globalThis.fetch = async () => {{
  statusRequests += 1;
  return {{ok: true, text: async () => JSON.stringify({{
    recent: [{{provider: 'alpha', model: 'from-another-session', attempts: 2}}]
  }})}};
}};
const plugin = await import('data:text/javascript;base64,' +
  Buffer.from({json.dumps(source)}).toString('base64'));
const hooks = await plugin.default({{client: {{tui: {{showToast: async x => toasts.push(x)}}}}}});
const event = {{type: 'message.updated', properties: {{info: {{
  id: 'reply-1', role: 'assistant', providerID: {json.dumps(provider_id)}, finish: 'stop'
}}}}}};
await hooks.event({{event}});
await hooks.event({{event}});
console.log(JSON.stringify({{toasts, statusRequests}}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program],
        check=True,
        text=True,
        capture_output=True,
    )
    observed = json.loads(result.stdout)
    if provider_id != "freellmpool":
        assert observed == {"toasts": [], "statusRequests": 0}
    else:
        assert observed["statusRequests"] == 1
        assert len(observed["toasts"]) == 1
        assert observed["toasts"][0]["body"]["message"] == (
            "Latest pool request: alpha/from-another-session (2 attempts)"
        )
