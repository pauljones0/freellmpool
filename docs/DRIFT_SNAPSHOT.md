# Drift Snapshot Schema (v1)

`freellmpool drift --emit PATH` writes a dated, machine-readable snapshot of
free-route conformance evidence. Third parties may consume it to track which
free tiers still work without running probes themselves.

## Cadence

Snapshots are generated on demand; operators SHOULD publish one weekly
(`freellmpool drift --probe --emit drift-snapshot.json`). `generated_at`
records when the snapshot was taken, so stale snapshots are detectable.

## Schema

Top-level object:

| Field | Type | Meaning |
|---|---|---|
| `schema` | integer | Schema version, always `1` for this document. |
| `generated_at` | string | ISO-8601 UTC timestamp of snapshot creation. |
| `freellmpool` | string | freellmpool version that produced the snapshot. |
| `targets` | object | Map of `provider-id/model` to feature evidence. |

Each target maps feature names (`chat`, `tools`, `streaming`, `vision`) to:

| Field | Type | Meaning |
|---|---|---|
| `status` | string | One of `pass`, `fail`, `unsupported`, `unavailable`. |
| `verified_at` | string | ISO-8601 UTC timestamp of the probe that set the status. |
| `classification` | string | Machine label for the outcome (e.g. `verified`, `semantic_mismatch`, `http_429`). |

## Guarantees

- No key material: snapshots carry only statuses, timestamps, and
  classifications — never API keys, request bodies, or model outputs.
- `pass` means a live probe succeeded within the freshness window; any other
  status names the observed failure mode honestly.

## Validation

```sh
python scripts/check_drift_snapshot.py drift-snapshot.json
```

## Example

```json
{
  "freellmpool": "0.5.0",
  "generated_at": "2026-09-19T00:00:00Z",
  "schema": 1,
  "targets": {
    "groq/llama-3.3-70b": {
      "chat": {"classification": "verified", "status": "pass",
               "verified_at": "2026-09-18T11:00:00Z"}
    }
  }
}
```
