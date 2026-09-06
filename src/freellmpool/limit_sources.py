"""Public, structured limit proposals; never activate or renew policy.

Groq's server-rendered rate-limit table contains separately named freeRows and
devRows with unit-labelled headers. Parse those JSON literals without executing
JavaScript. Changed, missing or ambiguous structure requires review.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

import httpx

from .http_read import ACCEPT_ENCODING, bounded_response_bytes

JSON = dict[str, Any]
_URL = "https://console.groq.com/docs/rate-limits"
_MAX_BYTES = 2 * 1024 * 1024
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_NUMERIC = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?[KM]?\Z")
_COLUMNS = [
    ("RPM", "Requests per minute", "rpm", "requests", 60),
    ("RPD", "Requests per day", "rpd", "requests", 86400),
    ("TPM", "Tokens per minute", "tpm", "total_tokens", 60),
    ("TPD", "Tokens per day", "tpd", "total_tokens", 86400),
    ("ASH", "Audio seconds per hour", "ash", "audio_seconds", 3600),
    ("ASD", "Audio seconds per day", "asd", "audio_seconds", 86400),
]


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20, connect=10), follow_redirects=False, trust_env=False)


def _unique(pairs: list[tuple[str, Any]]) -> JSON:
    result: JSON = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate source JSON field")
        result[key] = value
    return result


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.active = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.active = True
            self.parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.active:
            self.scripts.append("".join(self.parts))
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active:
            self.parts.append(data)


def _quantity(value: Any) -> int | None:
    if value == "-":
        return None  # Not documented; never interpreted as an unlimited cap.
    if not isinstance(value, str) or len(value) > 20 or not _NUMERIC.fullmatch(value):
        raise ValueError("Unrecognized capacity notation")
    suffix = value[-1]
    number = Decimal(value[:-1]) * (1000 if suffix == "K" else 1000000) if suffix in "KM" else Decimal(value)
    if number != number.to_integral_value() or not 0 <= number <= 10**15:
        raise ValueError("Non-integral or unsupported capacity")
    return int(number)


def parse_groq_free_limits(html: str) -> dict[str, dict[str, int | None]]:
    """Read only one explicitly labelled first-party free-table data object."""
    if len(html.encode()) > _MAX_BYTES:
        raise ValueError("Source exceeded budget")
    parser = _Scripts()
    parser.feed(html)
    candidates: list[JSON] = []
    for script in parser.scripts:
        match = re.fullmatch(r"\s*self\.__next_f\.push\((\[.*\])\)\s*;?\s*", script, re.DOTALL)
        if match is None:
            continue
        envelope = json.loads(match[1], object_pairs_hook=_unique)
        if not isinstance(envelope, list) or len(envelope) != 2 or envelope[0] != 1 or not isinstance(envelope[1], str):
            continue
        for line in envelope[1].splitlines():
            if '"freeRows"' not in line:
                continue
            prefix, separator, payload = line.partition(":")
            if not separator or not re.fullmatch(r"[0-9a-f]+", prefix):
                raise ValueError("Unknown table record framing")
            record = json.loads(payload, object_pairs_hook=_unique)
            if (not isinstance(record, list) or len(record) != 4 or record[0] != "$"
                    or not isinstance(record[3], dict) or not {"freeRows", "devRows", "headers"} <= set(record[3])):
                raise ValueError("Unknown table record")
            candidates.append(record[3])
    if len(candidates) != 1:
        raise ValueError("Missing or ambiguous free-plan table")
    table = candidates[0]
    headers = table["headers"]
    if (not isinstance(headers, list) or len(headers) != 7 or not isinstance(headers[0], dict)
            or headers[0].get("title") != "MODEL ID"):
        raise ValueError("Unknown table columns")
    for header, column in zip(headers[1:], _COLUMNS, strict=True):
        if not isinstance(header, dict) or (header.get("title"), header.get("tooltip")) != column[:2]:
            raise ValueError("Changed table units")
    rows = table["freeRows"]
    if not isinstance(rows, list) or not 0 < len(rows) <= 1000 or not isinstance(table["devRows"], list):
        raise ValueError("Missing free-plan model rows")
    result = {}
    for row in rows:
        if (not isinstance(row, list) or len(row) != 7 or not isinstance(row[0], str)
                or not _MODEL_ID.fullmatch(row[0]) or row[0] in result):
            raise ValueError("Invalid or duplicate free-plan model")
        result[row[0]] = {column[2]: _quantity(value) for value, column in zip(row[1:], _COLUMNS, strict=True)}
    return result


def _proposals(spec: Mapping[str, Any], observed: dict[str, dict[str, int | None]]) -> list[JSON]:
    rules = spec.get("limits")
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Unsupported reviewed limits")
    known: set[str] = set()
    by_id = {}
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or rule["id"] in by_id:
            raise ValueError("Invalid reviewed limits")
        by_id[rule["id"]] = rule
        capacities, models = rule.get("model_capacities", {}), rule.get("model_ids", [])
        if not isinstance(capacities, dict) or not isinstance(models, list):
            raise ValueError("Invalid model scope")
        if any(not isinstance(model, str) or not _MODEL_ID.fullmatch(model) for model in [*capacities, *models]):
            raise ValueError("Unsupported model identity")
        known.update(capacities)
        known.update(models)
    if not known or known - observed.keys():
        raise ValueError("Reviewed models absent from the free table")
    result = []
    for _, _, rule_id, metric, seconds in _COLUMNS:
        rule = by_id.get(rule_id)
        if rule is None:
            continue
        if (rule.get("scope") != "model" or rule.get("metric") != metric
                or rule.get("window_seconds") != seconds):
            raise ValueError("Reviewed limit scope or units changed")
        model_ids = rule.get("model_ids") or sorted(observed)
        for model_id in model_ids:
            value = observed[model_id][rule_id]
            old = rule.get("model_capacities", {}).get(model_id, rule.get("capacity"))
            if old is not None and (type(old) is not int or not 0 <= old <= 10**15):
                raise ValueError("Invalid prior capacity")
            if value is None:
                if old is not None:
                    raise ValueError("A reviewed capacity is no longer documented")
                continue
            if old != value:
                result.append({"rule_id": rule_id, "model_id": model_id, "metric": metric,
                               "scope": "model", "window_seconds": seconds,
                               "old_capacity": old, "new_capacity": value})
    return result


def collect_proposals(registry: Mapping[str, Mapping[str, Any]]) -> JSON:
    """Fetch public facts and propose reviewed rule edits without mutating them."""
    now = datetime.now(UTC).isoformat()
    result: JSON = {"schema": 1, "checked_at": now, "providers": {}}
    for pid in registry:
        row: JSON = {"status": "unsupported", "checked_at": now, "source_url": None,
                     "source_sha256": None, "parser": None, "proposals": [],
                     "note": "No reviewed structured limit parser is available for this provider."}
        if pid != "groq":
            result["providers"][pid] = row
            continue
        row.update(source_url=_URL, parser="groq_free_rows_v1")
        try:
            with _client() as client, client.stream("GET", _URL, headers={"Accept": "text/html", "Accept-Encoding": ACCEPT_ENCODING}) as response:
                if response.status_code != 200:
                    raise httpx.HTTPStatusError("Public source failed", request=response.request, response=response)
                body = bounded_response_bytes(response, _MAX_BYTES)
            row["source_sha256"] = hashlib.sha256(body).hexdigest()
            observed = parse_groq_free_limits(body.decode("utf-8"))
            row.update(status="ok", proposals=_proposals(registry[pid], observed), model_count=len(observed),
                       note="Parsed the official free-plan table; proposals require review and do not activate policy.")
        except httpx.HTTPError:
            row.update(status="error", note="Official limit source could not be read; no proposals generated.")
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            row.update(status="review_required", proposals=[],
                       note="Official free-plan table or reviewed rule mapping changed; inspect the source before editing limits.")
        result["providers"][pid] = row
    return result
