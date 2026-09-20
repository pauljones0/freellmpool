# Evidence artifacts

Verbatim supervisor reproductions referenced by the G24 discovery-bound design:

- `httpx-drip-bound-2026-09-20.json`: 12 chunks x 0.15s with 0.2s phase
  timeouts completed in 1.695s (> 0.8s sum) without timing out on httpx
  0.28.1. Proves phase sums cannot bound totals (sub-timeout drip);
  loopback synthetic only, no external network. Source:
  supervisor repro `httpx_drip_result.json`, 2026-09-20, copied verbatim.
- `httpx-async-dns-deadline-2026-09-20.json`: mocked `socket.getaddrinfo`
  sleeping 1.2s with `AsyncClient` and a 0.05s `wait_for`: timeout
  surfaced at ~0.051s, `asyncio.run` returned ~1.268s. Proves the
  documented v5.2 DNS residual (loop shutdown waits on the resolver
  thread). Unprivileged `unittest.mock`, no external requests. Source:
  supervisor repro `httpx_async_dns_deadline_result.json`, 2026-09-20,
  copied verbatim.
