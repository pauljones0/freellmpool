# Privacy: redaction + private routing

Free tiers are convenient and surveilled: several train on your
prompts by default. Two independent defenses, usable together:

```python
reply = pool.chat(messages, redact=True, private=True)
```

Or over HTTP: `{"redact": true, "private": true}` on
`/v1/chat/completions` (booleans only; anything else is a 400).

## `redact`: pre-flight scrubbing (defense in depth)

`redact=True` scrubs API keys, bearer tokens, emails, phones, SSNs,
Luhn-valid card numbers, private-key blocks, and `password=`-style
assignments from chat messages before any provider sees them.
`reply.redactions` reports which kinds were hit.

Honest limits: regex-based, single-line, best-effort — it is not a
leak-proof guarantee. Novel secret shapes and split/obfuscated
values can pass through. Redaction is never silent about what it
did (see `redactions`), but absence of hits is not proof of
absence of secrets.

## `private`: strict no-train routing (the guarantee)

`private=True` admits only providers labeled `api-no-train` in
`src/freellmpool/data_policies.json` and refuses otherwise:

```
Private mode admits only api-no-train providers; no eligible route
remains. Relax filters or run freellmpool status.
```

Labels fail closed: `unknown` and `trains-by-default` providers
are excluded. Current labels (reviewed 2026-09-19):

| Provider | Training label |
|---|---|
| groq, cohere, cloudflare | `api-no-train` (sourced) |
| ollama | `api-no-train` (local; no egress) |
| gemini | `trains-by-default` (free-tier AI Studio terms) |
| all others | `unknown` (unverified; excluded in strict mode) |

Each entry carries `as_of`, an `https` source where a primary was
verified (else `null` + note), and a retention note. Re-verify
before trusting a label with real secrets — provider terms change.

## Which to use

- Casual prompts, no secrets: neither flag.
- Prompts with incidental secrets: `redact=True`.
- Sensitive work (proprietary code, PII, customer data):
  `redact=True, private=True` — scrubbed *and* confined to
  no-train routes.
