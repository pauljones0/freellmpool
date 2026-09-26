# One-command launch (draft, G1)

Fork launch copy for the one-command install. Post only after G1's audit
transcript exists; full relaunch waits for G6.

## Show HN draft

**Show HN: freellmpool fork — free LLM tokens in one command, no signup**

I maintain a fork of freellmpool (a local gateway that pools legitimate
free LLM tiers) focused on one thing: easy setup that just works.

```sh
uvx --from https://github.com/pauljones0/freellmpool/archive/refs/heads/main.tar.gz freellmpool ask --max-tokens 32 "Reply with one short sentence: freellmpool is ready."
```

No checkout, no API keys when a keyless provider is up — the first run
discovers free routes automatically. 14 reviewed providers, 132 chat
routes, strict-free routing (never a silent paid fallback), and every
allowance traces to an official source ([trust notes](../TRUST.md)).

Upstream `0xzr/freellmpool` did the original work (MIT); this fork is the
maintained, evidence-reviewed line. Happy to answer hard questions about
free-tier ToS posture — the FAQ doesn't dodge them.

## Checklist

- [ ] G1 audit transcript captured (fresh container, one command, live reply)
- [ ] Trust page accurate against the release commit
- [ ] Post at a weekday morning; reply-bank rules in `reply-bank.md` apply
