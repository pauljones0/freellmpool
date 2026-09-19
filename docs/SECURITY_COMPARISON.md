# Security comparison: freellmpool vs LiteLLM's 2026 advisories

LiteLLM published 12 advisories in 2026, including pre-auth RCE chains.
This doc maps the verified representative ones to freellmpool's surface,
with proof links per claim (TRUST.md convention) and explicit residual
risks. No security theater: anything we cannot prove is in Residual risks.

Advisory sources (all verified in the GitHub Advisory Database / press):

- CVE-2026-42208 / GHSA-r75f-5x8p-qvmc — pre-auth SQL injection, exploited
  within 36h ([The Hacker News](https://thehackernews.com/2026/04/litellm-cve-2026-42208-sql-injection.html)).
- CVE-2026-42271 / GHSA-v4p8-mg3p-g94g — authenticated command execution
  via MCP stdio test endpoints, chained to unauthenticated RCE
  ([advisory](https://github.com/advisories/GHSA-v4p8-mg3p-g94g)).
- CVE-2026-49468 / GHSA-4xpc-pv4p-pm3w — auth bypass via Host header
  injection ([GBHackers](https://gbhackers.com/critical-litellm-flaw/)).
- CVE-2026-42203 — SSTI chained with stolen keys to RCE (reported
  alongside CVE-2026-42208).
- Feb-2026 RCE + sandbox escape (fixed v1.82.0); CVE-2026-59821 (RCE) /
  CVE-2026-59822 (MCP auth bypass), Jul-2026 ([Huntaegis timeline](https://huntaegis.com/article/cc0eb4c74cffdc46a8c30c789a2bf279)).

## Class-by-class posture

### 1. SQL injection (CVE-2026-42208)

LiteLLM centralizes credentials in Postgres behind an HTTP API; one
unparameterized query gave pre-auth attackers the credential tables.

freellmpool: there is no network-reachable database. Request state is
in-memory; the only SQL is local sqlite (quota ledger, allowances) using
bound parameters, never reachable from a request. The class is eliminated
for remote attackers, not merely patched.
Verify: [`src/freellmpool/allowances.py`](../src/freellmpool/allowances.py)
(parameterized queries), [`src/freellmpool/quota.py`](../src/freellmpool/quota.py).

### 2. Host-header auth bypass (CVE-2026-49468)

LiteLLM trusted the Host header in an auth decision.

freellmpool: the Host allowlist is built from the socket's own
`getsockname()` — never from attacker input — and unknown authorities get
403 before auth is even evaluated. Browsers get an additional Origin check;
forwarded headers are never trusted. Live-probed: `Host: evil.com` → 403.
Verify: [`src/freellmpool/request_security.py`](../src/freellmpool/request_security.py),
[`tests/test_request_security.py`](../tests/test_request_security.py).

### 3. Command execution via test/debug endpoints (CVE-2026-42271)

LiteLLM shipped endpoints that spawn MCP stdio servers from request input.

freellmpool: no proxy or MCP request path reaches `subprocess`, `eval`,
`exec`, or a shell. The only spawns in the tree take operator-local argv
(agent launcher, tailscale/gh CLIs, operator-configured MCP wrapper) and
none is reachable from HTTP or MCP tool input.
Verify: `grep -rn "subprocess" src/freellmpool/` (launcher.py,
tailnet.py, maintenance.py, onboarding.py, mcp_diet.py — all CLI-local);
[`src/freellmpool/mcp_server.py`](../src/freellmpool/mcp_server.py) exposes
LLM tools only.

### 4. SSTI → RCE (CVE-2026-42203)

LiteLLM rendered attacker-influenced data through server-side templates.

freellmpool: there is no template engine. The browser shell interpolates
constants only, under a hash-pinned CSP; SVG badges escape every dynamic
value (`_esc`) and the `metric` parameter is allowlisted, never
interpolated; the dashboard renders exclusively via `textContent`.
Verify: [`src/freellmpool/proxy.py`](../src/freellmpool/proxy.py)
(`_browser_shell_csp`, `_browser_shell_html`), [`src/freellmpool/svg.py`](../src/freellmpool/svg.py).

### 5. Credential echo in errors/logs (LiteLLM credential-table targeting)

Attackers went straight for `litellm_credentials`. Our analogue: a
provider echoing a key in an error body would previously flow verbatim
into proxy/MCP client errors via `client_message` (found in the G19
adversarial review, 2026-09-19). Now upstream error text is capped at 400
chars and passed through the G16 redactor at construction, so echoed
secrets arrive as `[REDACTED_*]`.
Verify: [`src/freellmpool/client.py`](../src/freellmpool/client.py)
(`_err_message`), `test_upstream_error_redacts_echoed_secrets` and
`test_proxy_error_never_echoes_upstream_key`.

### 6. Supply chain (Mar-2026 PyPI incident chatter)

LiteLLM's blast radius included its install base. freellmpool pins every
dependency (`uv.lock`), gates releases on `pip-audit --strict` plus
bandit/zizmor/CodeQL, and ships SPDX SBOMs with Sigstore attestations per
release artifact.
Verify: [`uv.lock`](../uv.lock),
[security.yml](../.github/workflows/security.yml),
[release-evidence.yml](../.github/workflows/release-evidence.yml), and the
v0.14.3 run:
<https://github.com/pauljones0/freellmpool/actions/runs/35425357340>.

## What the G19 adversarial review probed (live, key-locked proxy)

No-auth → 401, wrong key → 401, forged Host → 403, duplicate
Content-Length → 400, 17 MB body → rejected at the 16 MB cap, chunked
framing → 400, `/readyz` without key → 401, public shell → 200 with zero
pool/credential bytes. One flaw found and fixed (§5 above); everything else
held.

## Residual risks (honest)

1. **Prompt injection is inherent.** MCP/LLM tools process untrusted text by
   design; a malicious tool result or page can steer a model. We scope tools
   narrowly but cannot eliminate the class.
2. **Local file access is total.** Provider keys live in env/config files by
   design; anyone who can read the operator's files owns the keys. OS-level
   protection (chmod 600, full-disk encryption) is the operator's job.
3. **Pre-auth route table is visible.** Unknown POST routes return 404 before
   the 401 check, so scanners can enumerate route names. Names are public API
   and carry no data; accepted.
4. **Upstream trust.** We forward prompts to third-party free tiers; a
   malicious provider sees request content. `private=True` routing plus
   redaction (G16) narrows this but cannot remove it for the serving route.
5. **No external audit (yet).** Everything above is self-review plus scanners.
   Until an independent party audits the proxy, treat "boring and safe" as a
   tested claim, not a certified one.
6. **Fork/canonical drift.** Security releases cut on this fork (e.g.
   v0.14.2) do not automatically reach `0xzr/freellmpool`; a user sync step
   remains (see GOALS.md G17/G19 evidence).
