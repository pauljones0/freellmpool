"""Admission evidence must expire, and probes must demonstrate a complete protocol."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from freellmpool import conformance as c
from freellmpool.errors import ProviderHTTPError
from freellmpool.models import Model, Provider, Reply
from freellmpool.router import Target


def provider(name="alpha"):
    return Provider(
        id=name, label=name, adapter="openai", base_url=f"https://{name}.test/v1",
        auth="none", models=(Model("model-1"),),
    )


def stamp(age):
    return (datetime.now(UTC) - age).isoformat()


@pytest.mark.parametrize("age", [timedelta(days=7), timedelta(days=30)])
def test_expired_evidence_is_not_admissible_even_from_supplied_snapshot(tmp_path, age):
    p = provider()
    store = c.ConformanceStore(tmp_path / "evidence.json")
    store.record(p, "model-1", c.FEATURE_TOOLS, status="pass", classification="verified",
                 verified_at=stamp(age))
    snapshot = store.snapshot()
    assert store.evidence(p, "model-1", snapshot=snapshot) == {}
    assert not store.passes(p, "model-1", [c.FEATURE_TOOLS], snapshot=snapshot)
    assert snapshot["targets"]["alpha/model-1"]["features"]  # kept for audit/rotation


@pytest.mark.parametrize("timestamp", ["", "yesterday", "2026-99-99T00:00:00Z",
                                        "2026-09-01T00:00:00", "9999-12-31T00:00:00Z"])
def test_invalid_or_future_record_timestamps_are_rejected(tmp_path, timestamp):
    store = c.ConformanceStore(tmp_path / "evidence.json")
    with pytest.raises(ValueError, match="timestamp"):
        store.record(provider(), "model-1", c.FEATURE_CHAT, status="pass",
                     classification="verified", verified_at=timestamp)
    assert not store.path.exists()


@pytest.mark.parametrize("timestamp", ["bogus", "9999-12-31T00:00:00Z"])
def test_tampered_snapshot_cannot_bypass_timestamp_validation(tmp_path, timestamp):
    p = provider()
    store = c.ConformanceStore(tmp_path / "evidence.json")
    snapshot = {"targets": {"alpha/model-1": {
        "fingerprint": c.target_fingerprint(p, "model-1"), "features": {
            "tools": {"status": "pass", "classification": "verified",
                      "verified_at": timestamp, "verification_count": 1}}}}}
    assert not store.passes(p, "model-1", ["tools"], snapshot=snapshot)


def test_offset_timestamp_has_the_same_freshness_as_utc(tmp_path):
    p = provider()
    store = c.ConformanceStore(tmp_path / "evidence.json")
    store.record(p, "model-1", "chat", status="pass", classification="verified",
                 verified_at=stamp(timedelta(minutes=2)))
    assert store.passes(p, "model-1", ["chat"])


@pytest.mark.parametrize("message", [
    {"role": "tool", "tool_call_id": "call-1", "content": "7"},
    {"role": "function", "name": "record_number", "content": "7"},
    {"role": "assistant", "tool_calls": [{"id": "call-1"}], "content": None},
    {"role": "assistant", "function_call": {"name": "record_number"}},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1"}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1"}]},
    {"type": "function_call_output", "call_id": "call-1", "output": "7"},
])
def test_tool_history_requires_tool_conformance_without_new_tool_definitions(message):
    assert c.FEATURE_TOOLS in c.required_features(iter([message]))


def tool_reply(p, tool_id="call-1", arguments='{"number":7}'):
    return Reply(text="", provider_id=p.id, model="model-1", raw={}, message={
        "role": "assistant", "content": None, "tool_calls": [{
            "id": tool_id, "type": "function",
            "function": {"name": "record_number", "arguments": arguments}}]})


@pytest.mark.parametrize("tool_id", [None, "", "   ", 7])
def test_tool_canary_requires_usable_id(tool_id):
    assert c.validate_canary_result("tools", tool_reply(provider(), tool_id)) == "malformed_tool_call"


@pytest.mark.parametrize("arguments", ['{"number":7.0}', '{"number":true}',
                                         '{"number":7,"extra":0}', '{"number":0,"number":7}'])
def test_tool_canary_validates_exact_schema_and_duplicate_fields(arguments):
    assert c.validate_canary_result("tools", tool_reply(provider(), arguments=arguments)) != "verified"


def test_tools_probe_executes_complete_tool_result_round_trip():
    p = provider()
    calls = []

    def call_fn(_provider, model, messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            return tool_reply(p)
        assert messages[1]["tool_calls"][0]["id"] == "call-1"
        assert messages[2]["role"] == "tool"
        assert messages[2]["tool_call_id"] == "call-1"
        assert json.loads(messages[2]["content"]) == {"recorded": 7}
        assert kwargs.get("tool_choice") != {"type": "function", "function": {"name": "record_number"}}
        return Reply(text="OK", provider_id=p.id, model=model, raw={})

    result = c.run_target_canaries(p, "model-1", env={}, features=["tools"], call_fn=call_fn)
    assert result["tools"] == {"status": "pass", "classification": "verified"}
    assert len(calls) == 2
    assert all(256 <= kwargs["max_tokens"] <= 4096 for _, kwargs in calls)


def test_tool_canary_works_when_compatibility_api_rejects_optional_choice_parameter():
    p = provider("cohere")
    calls = []

    def call_fn(_provider, model, messages, **kwargs):
        calls.append(messages)
        # Cohere's compatibility tool examples omit tool_choice. Basic tools
        # support must not depend on optional forced-choice parameter support.
        if "tool_choice" in kwargs:
            raise ProviderHTTPError(400, "unsupported tool_choice parameter", retryable=False)
        if len(calls) == 1:
            return tool_reply(p)
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call-1"
        return Reply(text="OK", provider_id=p.id, model=model, raw={})

    result = c.run_target_canaries(p, "model-1", env={}, features=["tools"], call_fn=call_fn)
    assert result["tools"] == {"status": "pass", "classification": "verified"}
    assert len(calls) == 2


def test_tools_probe_does_not_pass_when_tool_followup_is_rejected():
    p = provider()
    calls = 0

    def call_fn(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_reply(p)
        return Reply(text="cannot accept tool result", provider_id=p.id, model="model-1", raw={})

    result = c.run_target_canaries(p, "model-1", env={}, features=["tools"], call_fn=call_fn)
    assert result["tools"]["status"] == "fail"
    assert calls == 2


def test_output_exhaustion_is_inconclusive_instead_of_protocol_failure():
    p = provider()

    def call_fn(*args, **kwargs):
        assert kwargs["max_tokens"] >= 256
        return Reply(text="", provider_id=p.id, model="model-1",
                     raw={"choices": [{"finish_reason": "length"}]})

    result = c.run_target_canaries(p, "model-1", env={}, features=["chat"], call_fn=call_fn)
    assert result["chat"] == {"status": "unavailable", "classification": "output_limit"}


@pytest.mark.parametrize("raw", [
    {"choices": None, "candidates": None, "incomplete_details": None},
    {"candidates": [{"finishReason": "MAX_TOKENS"}]},
    {"stop_reason": "max_tokens"},
    {"incomplete_details": {"reason": "max_output_tokens"}},
])
def test_empty_or_exhausted_output_is_inconclusive_across_protocols(raw):
    reply = Reply(text="", provider_id="alpha", model="model-1", raw=raw)
    assert c.validate_canary_result("chat", reply) in {"empty_output", "output_limit"}


def test_empty_stream_is_inconclusive_without_completion_metadata():
    p = provider()
    result = c.run_target_canaries(p, "model-1", env={}, features=["streaming"],
                                  stream_fn=lambda *a, **kw: iter(()))
    assert result["streaming"] == {"status": "unavailable", "classification": "empty_output"}


def test_tools_canary_does_not_follow_up_a_malformed_call():
    p = provider()
    calls = []

    def call_fn(*args, **kwargs):
        calls.append(args)
        return tool_reply(p, "")

    result = c.run_target_canaries(p, "model-1", env={}, features=["tools"], call_fn=call_fn)
    assert result["tools"]["status"] == "fail"
    assert len(calls) == 1


def test_rotation_prioritizes_unseen_then_oldest_attempt_including_failed_attempts(tmp_path):
    store = c.ConformanceStore(tmp_path / "evidence.json")
    targets = [Target(provider(name), "model-1", 100) for name in ("alpha", "beta", "gamma")]
    for target, age, status in [(targets[0], 4, "pass"), (targets[1], 2, "unavailable")]:
        store.record(target.provider, target.model, "chat", status=status, classification="verified",
                     verified_at=stamp(timedelta(days=age)))
    chosen = c.rotating_targets(targets, store, 2)
    assert [target.provider.id for target in chosen] == ["gamma", "alpha"]
    assert c.rotating_targets(targets, store, 0) == []
    with pytest.raises(ValueError):
        c.rotating_targets(targets, store, -1)


@pytest.mark.parametrize("classification", ["rate_limit", "timeout", "output_limit", "availability", "auth", "billing", "model_not_found"])
def test_inconclusive_canary_keeps_fresh_strong_proof_without_renewing_it(tmp_path, classification):
    p = provider()
    store = c.ConformanceStore(tmp_path / "evidence.json")
    store.record(p, "model-1", "tools", status="pass", classification="verified",
                 verified_at=stamp(timedelta(days=2)))
    previous = store.evidence(p, "model-1")["tools"]
    store.record(p, "model-1", "tools", status="unavailable", classification=classification,
                 verified_at=stamp(timedelta(hours=1)))
    fresh = c.ConformanceStore(store.path)
    assert fresh.passes(p, "model-1", ["tools"])
    row = fresh.evidence(p, "model-1")["tools"]
    assert row["verified_at"] == previous["verified_at"]
    assert row["classification"] == "verified"
    assert row["last_attempt_status"] == "unavailable"
    assert row["last_attempt_classification"] == classification
    assert row["last_attempt_at"] > row["verified_at"]
    assert row["verification_count"] == 2


@pytest.mark.parametrize("status,classification", [("fail", "semantic_mismatch"), ("unsupported", "unsupported")])
def test_definitive_canary_failure_replaces_previous_pass(tmp_path, status, classification):
    p = provider()
    store = c.ConformanceStore(tmp_path / "evidence.json")
    store.record(p, "model-1", "tools", status="pass", classification="verified",
                 verified_at=stamp(timedelta(days=2)))
    store.record(p, "model-1", "tools", status=status, classification=classification)
    assert not store.passes(p, "model-1", ["tools"])
    assert store.evidence(p, "model-1")["tools"]["status"] == status


def test_unavailable_canary_cannot_resurrect_expired_or_weak_tool_proof(tmp_path):
    for old_kind in ("expired", "weak"):
        p = provider()
        store = c.ConformanceStore(tmp_path / f"{old_kind}.json")
        age = timedelta(days=8 if old_kind == "expired" else 2)
        store.record(p, "model-1", "tools", status="pass", classification="verified", verified_at=stamp(age))
        if old_kind == "weak":
            data = json.loads(store.path.read_text())
            data["targets"]["alpha/model-1"]["features"]["tools"].pop("probe_version")
            store.path.write_text(json.dumps(data))
        store.record(p, "model-1", "tools", status="unavailable", classification="rate_limit")
        assert not store.passes(p, "model-1", ["tools"])
        assert store.evidence(p, "model-1")["tools"]["status"] == "unavailable"


def test_recent_unavailable_attempt_moves_target_to_back_of_rotation(tmp_path):
    store = c.ConformanceStore(tmp_path / "evidence.json")
    targets = [Target(provider(name), "model-1", 100) for name in ("alpha", "beta")]
    for target, age in zip(targets, (6, 2), strict=True):
        store.record(target.provider, target.model, "chat", status="pass", classification="verified",
                     verified_at=stamp(timedelta(days=age)))
    assert c.rotating_targets(targets, store, 1)[0].provider.id == "alpha"
    store.record(targets[0].provider, "model-1", "chat", status="unavailable", classification="timeout")
    assert store.passes(targets[0].provider, "model-1", ["chat"])
    assert c.rotating_targets(targets, store, 1)[0].provider.id == "beta"


def test_retained_proof_still_expires_from_its_original_timestamp(tmp_path, monkeypatch):
    p = provider()
    store = c.ConformanceStore(tmp_path / "evidence.json")
    store.record(p, "model-1", "tools", status="pass", classification="verified",
                 verified_at=stamp(timedelta(days=2)))
    store.record(p, "model-1", "tools", status="unavailable", classification="timeout")
    assert store.passes(p, "model-1", ["tools"])
    monkeypatch.setattr(c, "EVIDENCE_MAX_AGE", timedelta(days=1))
    assert not store.passes(p, "model-1", ["tools"])
    assert store.snapshot()["targets"]["alpha/model-1"]["features"]["tools"]["last_attempt_status"] == "unavailable"


def test_late_out_of_order_attempt_cannot_replace_newer_evidence(tmp_path):
    p = provider()
    store = c.ConformanceStore(tmp_path / "evidence.json")
    store.record(p, "model-1", "tools", status="pass", classification="verified",
                 verified_at=stamp(timedelta(hours=1)))
    before = store.snapshot()
    store.record(p, "model-1", "tools", status="fail", classification="semantic_mismatch",
                 verified_at=stamp(timedelta(days=2)))
    assert store.snapshot() == before


@pytest.mark.parametrize("bad_attempt", [
    {"last_attempt_status": "unavailable", "last_attempt_classification": "secret with spaces", "last_attempt_at": "9999-12-31T00:00:00Z"},
    {"last_attempt_status": "unknown", "last_attempt_classification": "timeout", "last_attempt_at": "yesterday"},
    {"last_attempt_status": "unavailable", "last_attempt_classification": "timeout", "last_attempt_at": "2000-01-01T00:00:00Z"},
])
def test_invalid_attempt_metadata_cannot_hide_or_renew_valid_proof(tmp_path, bad_attempt):
    p = provider()
    store = c.ConformanceStore(tmp_path / "evidence.json")
    store.record(p, "model-1", "tools", status="pass", classification="verified")
    data = json.loads(store.path.read_text())
    data["targets"]["alpha/model-1"]["features"]["tools"].update(bad_attempt)
    store.path.write_text(json.dumps(data))
    row = store.evidence(p, "model-1")["tools"]
    assert row["status"] == "pass"
    assert "last_attempt_status" not in row
    assert "secret" not in json.dumps(store.snapshot())


def test_evidence_byte_budget_evicts_oldest_target_and_keeps_new_checks_working(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "_MAX_BYTES", 1_000)
    store = c.ConformanceStore(tmp_path / "bounded.json")
    for name, age in (("alpha", 3), ("beta", 2), ("gamma", 1)):
        store.record(provider(name), "model-1", "chat", status="pass", classification="verified",
                     verified_at=stamp(timedelta(hours=age)))
    assert store.path.stat().st_size <= 1_000
    assert store.passes(provider("gamma"), "model-1", ["chat"])
    assert "alpha/model-1" not in store.snapshot()["targets"]
