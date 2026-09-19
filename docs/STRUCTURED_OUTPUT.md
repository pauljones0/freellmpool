# Structured output (`response_format`)

`response_format: {"type": "json_object"}` and
`response_format: {"type": "json_schema", ...}` work on any route with
fresh `json` / `json_schema` conformance evidence. The gateway validates
every JSON-mode reply locally:

1. The reply must be strict raw JSON (no prose, no markdown fences).
2. For `json_schema`, the parsed value must satisfy the schema subset
   the gateway checks: `type`, `properties`, `required`, `items`,
   `enum`, `additionalProperties`.

## Repair bound

On validation failure the gateway re-asks **max 1 repair** turn: the
original messages plus the bad output plus a user turn carrying the
validation error, demanding exactly one raw JSON value. If the repair
turn also fails validation, the request fails honestly with
`StructuredOutputError` (HTTP 502 through the proxy:
"JSON-mode reply failed validation after max 1 repair turn(s)").

There is no silent salvage and no unbounded loop — one repair, then an
honest error naming the last validation failure.

## Allowance accounting

The repair turn is a normal second request through the accounted path:
it reserves and settles its own allowance charges (no free
double-calls). Both turns appear as separate reservations in the
allowance ledger.

## Raw measurement stays raw

Conformance canaries (`freellmpool verify --features json_schema`)
probe the model's unrepaired output, so routing evidence is never
inflated by the repair loop. Probing a route's raw JSON ability and
serving a repaired reply are deliberately different paths.
