"""G11: structured-output repair loop — validate, repair once, account honestly."""

from __future__ import annotations

import sqlite3

import pytest
from test_managed_runtime import make_pool, successful

from freellmpool import structured
from freellmpool.errors import AllProvidersExhausted, StructuredOutputError

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
FORMAT = {"type": "json_schema", "json_schema": {"name": "t", "strict": True, "schema": SCHEMA}}


def test_parse_valid_json_passthrough():
    value, error = structured.parse_json_output('{"ok": true}')
    assert (value, error) == ({"ok": True}, None)


def test_parse_is_strict_raw_json():
    for bad in ('```json\n{"ok": true}\n```', 'Sure! {"ok": true}', '{"ok": tru'):
        value, error = structured.parse_json_output(bad)
        assert value is None and error, bad
    assert len(structured.parse_json_output("x" * 5000)[1]) < 500


def test_validate_schema_names_violations():
    assert structured.validate_schema({"ok": True}, SCHEMA) == []
    assert structured.validate_schema({}, SCHEMA) == ["$: missing required property 'ok'"]
    assert structured.validate_schema({"ok": "yes"}, SCHEMA) == ["$.ok: expected boolean"]
    assert structured.validate_schema({"ok": True, "x": 1}, SCHEMA) == ["$: unexpected property 'x'"]


def test_validate_schema_nested_and_enum():
    schema = {"type": "object", "properties": {
        "n": {"type": "integer", "enum": [7]},
        "tags": {"type": "array", "items": {"type": "string"}}}, "required": ["n"]}
    assert structured.validate_schema({"n": 7.5}, schema) == ["$.n: expected integer"]
    assert structured.validate_schema({"n": 8}, schema) == ["$.n: not one of [7]"]
    assert structured.validate_schema({"n": 7, "tags": ["a", 1]}, schema) == ["$.tags[1]: expected string"]


def test_wants_json_request_shapes():
    assert structured.wants_json(None) == (False, None)
    assert structured.wants_json({"type": "json_object"}) == (True, None)
    assert structured.wants_json(FORMAT) == (True, SCHEMA)
    with pytest.raises(ValueError):
        structured.wants_json({"type": "bogus"})


def _json_pool(tmp_path, bodies):
    calls = []

    def post(url, headers, body, timeout):
        calls.append(body)
        content = bodies[min(len(calls) - 1, len(bodies) - 1)]
        return successful({"choices": [{"message": {"role": "assistant", "content": content}}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

    pool = make_pool(tmp_path, ids=("alpha",), post=post)
    route = pool.snapshot().routes[0]
    for feature in ("json", "json_schema"):
        pool.conformance.record(route.provider, route.model, feature,
                                status="pass", classification="verified")
    return pool, calls


def _reservations(tmp_path):
    db = sqlite3.connect(tmp_path / "allowances.db")
    try:
        return db.execute("SELECT COUNT(DISTINCT reservation) FROM charges").fetchone()[0]
    finally:
        db.close()


def test_chat_valid_passthrough_single_call(tmp_path):
    pool, calls = _json_pool(tmp_path, ['{"ok": true}'])
    reply = pool.chat([{"role": "user", "content": "hi"}], response_format=FORMAT)
    assert reply.text == '{"ok": true}'
    assert len(calls) == 1 and _reservations(tmp_path) == 1


def test_chat_repairs_once_and_charges_both_turns(tmp_path):
    pool, calls = _json_pool(tmp_path, ['Sure! {"ok": true}', '{"ok": true}'])
    reply = pool.chat([{"role": "user", "content": "hi"}], response_format=FORMAT)
    assert reply.text == '{"ok": true}'
    assert len(calls) == 2
    assert _reservations(tmp_path) == 2
    repair_user = calls[1]["messages"][-1]
    assert repair_user["role"] == "user"
    assert "not valid JSON" in repair_user["content"]
    assert calls[1]["messages"][-2]["content"] == 'Sure! {"ok": true}'


def test_chat_unrepairable_raises_after_one_repair(tmp_path):
    pool, calls = _json_pool(tmp_path, ["nope", "still nope"])
    with pytest.raises(StructuredOutputError) as excinfo:
        pool.chat([{"role": "user", "content": "hi"}], response_format=FORMAT)
    assert "max 1 repair" in (excinfo.value.client_message or "")
    assert len(calls) == 2
    assert _reservations(tmp_path) == 2


def test_chat_without_response_format_passes_invalid_through(tmp_path):
    pool, calls = _json_pool(tmp_path, ["nope"])
    assert pool.chat([{"role": "user", "content": "hi"}]).text == "nope"
    assert len(calls) == 1


def test_probe_bypasses_repair_for_raw_measurement(tmp_path):
    pool, calls = _json_pool(tmp_path, ["nope"])
    route = pool.snapshot().routes[0]
    reply = pool.probe_call(route.provider, route.model, [{"role": "user", "content": "hi"}],
                            response_format=FORMAT)
    assert reply.text == "nope"
    assert len(calls) == 1


def test_error_is_honest_exhaustion_with_502():
    err = StructuredOutputError([("a/m", "bad json")], client_message="m")
    assert isinstance(err, AllProvidersExhausted)
    assert err.client_status == 502


def test_bound_is_one_and_documented():
    from pathlib import Path

    assert structured.MAX_REPAIRS == 1
    doc = Path(__file__).resolve().parent.parent / "docs" / "STRUCTURED_OUTPUT.md"
    assert f"max {structured.MAX_REPAIRS} repair" in doc.read_text()
