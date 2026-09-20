"""G26 U1/U2: drip-bounded sync body reads + five-site wiring."""

import inspect
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from freellmpool import (
    account_observations,
    discovery,
    http_read,
    limit_sources,
    policy_updates,
    workflow_health,
)
from freellmpool.http_read import bounded_response_bytes

MESSAGE = "HTTP response exceeded its total read deadline"


class SyncStream(httpx.SyncByteStream):
    def __init__(self, chunks, *, delay=0.0):
        self._chunks = chunks
        self._delay = delay
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        for chunk in self._chunks:
            if self._delay:
                time.sleep(self._delay)
            self.consumed += 1
            yield chunk

    def close(self):
        self.closed = True


def test_deadline_error_type_and_message():
    assert issubclass(http_read.ReadDeadlineExceeded, ValueError)
    assert http_read._SOURCE_TOTAL_SECONDS == 30


def test_overdue_first_chunk_fails_fast():
    stream = SyncStream([b"a", b"b"])
    response = httpx.Response(200, stream=stream)
    with pytest.raises(http_read.ReadDeadlineExceeded) as caught:
        bounded_response_bytes(response, 1024, deadline=time.monotonic() - 1)
    assert str(caught.value) == MESSAGE
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert stream.consumed == 0
    assert response.is_closed and stream.closed


def test_mid_drip_abort_closes_and_carries_no_body():
    stream = SyncStream([b"a", b"b", b"c"], delay=0.15)
    response = httpx.Response(200, stream=stream)
    with pytest.raises(http_read.ReadDeadlineExceeded) as caught:
        bounded_response_bytes(response, 1024, deadline=time.monotonic() + 0.05)
    assert str(caught.value) == MESSAGE
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert response.is_closed and stream.closed


def test_consumed_path_exempt_from_deadline():
    response = httpx.Response(200, content=b"abc")
    assert response.is_stream_consumed
    assert bounded_response_bytes(response, 1024, deadline=time.monotonic() - 1) == b"abc"


def test_sub_chunk_drip_still_hits_deadline():
    # 1-byte arrivals must surface per arrival: 64KB chunk assembly would
    # buffer this whole drip and starve the chunk-top check past deadline.
    stream = SyncStream([b"x"] * 10, delay=0.02)
    response = httpx.Response(200, stream=stream)
    with pytest.raises(http_read.ReadDeadlineExceeded):
        bounded_response_bytes(response, 1024, deadline=time.monotonic() + 0.05)
    assert stream.consumed < 10


def test_legacy_none_deadline_unchanged():
    assert bounded_response_bytes(httpx.Response(200, stream=SyncStream([b"a", b"b"])), 1024) == b"ab"
    assert bounded_response_bytes(httpx.Response(200, stream=SyncStream([b"a", b"b"])), 1024,
                                  deadline=None) == b"ab"


# --- G26 U2: five-site wiring + mappings ---


def _raising_burn(monkeypatch, module):
    seen = {}

    def fail(response, max_bytes, *, deadline=None):
        seen["deadline"] = deadline
        raise http_read.ReadDeadlineExceeded(MESSAGE)

    monkeypatch.setattr(module, "bounded_response_bytes", fail)
    return seen


def _deadline_is_source_start_plus_30(seen):
    assert 29 < seen["deadline"] - time.monotonic() <= 30


def _two_url_registry():
    return {"p": {"evidence": [{"url": "https://example.com/a"},
                               {"url": "https://example.com/b"}]}}


def _html_client(monkeypatch, module):
    def respond(request):
        return httpx.Response(200, text="<html>x</html>",
                              headers={"content-type": "text/html"})

    monkeypatch.setattr(module, "_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(respond)))


def test_discovery_drip_maps_to_error_record(monkeypatch):
    seen = _raising_burn(monkeypatch, discovery)
    _html_client(monkeypatch, discovery)
    result = discovery.check_public_sources(
        ["p"], registry={"p": {"evidence": [{"url": "https://example.com/a"}]}})
    (record,) = result["sources"]
    assert record["status"] == "error"
    _deadline_is_source_start_plus_30(seen)
    assert MESSAGE not in json.dumps(result)


def test_discovery_source_deadline_ignores_overall_between_url_only(monkeypatch):
    seen = {}

    def capture(response, max_bytes, *, deadline=None):
        seen.setdefault("deadlines", []).append(deadline)
        return b"<html>x</html>"

    monkeypatch.setattr(discovery, "bounded_response_bytes", capture)
    _html_client(monkeypatch, discovery)
    ticks = iter([1000.0, 1000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 1000.0))
    result = discovery.check_public_sources(
        ["p"], registry={"p": {"evidence": [{"url": "https://example.com/a"}]}},
        time_budget_seconds=10)
    assert result["sources"][0]["status"] == "ok"
    assert seen["deadlines"] == [1030.0]


def test_discovery_overall_budget_fails_fast_second_url(monkeypatch):
    _html_client(monkeypatch, discovery)
    ticks = iter([1000.0, 1000.0, 1061.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 1061.0))
    result = discovery.check_public_sources(["p"], registry=_two_url_registry(),
                                            time_budget_seconds=60)
    first, second = result["sources"]
    assert first["status"] == "ok" and second["status"] == "error"
    assert MESSAGE not in json.dumps(result)


def test_discovery_none_budget_disables_overall(monkeypatch):
    _html_client(monkeypatch, discovery)
    ticks = iter([1000.0, 1000.0, 99999.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 99999.0))
    result = discovery.check_public_sources(["p"], registry=_two_url_registry(),
                                            time_budget_seconds=None)
    assert [row["status"] for row in result["sources"]] == ["ok", "ok"]


def test_discovery_default_budget_mirrored_at_both_layers(monkeypatch, tmp_path):
    assert discovery._EVIDENCE_OVERALL_SECONDS == 120
    params = inspect.signature(discovery.check_public_sources).parameters
    assert params["time_budget_seconds"].default == 120
    params = inspect.signature(discovery.refresh_evidence).parameters
    assert params["time_budget_seconds"].default == 120
    seen = {}

    def fake_check(provider_ids, *, registry=None, time_budget_seconds=None):
        seen["budget"] = time_budget_seconds
        return {"schema": 1, "checked_at": "2026-01-01T00:00:00+00:00", "sources": []}

    monkeypatch.setattr(discovery, "check_public_sources", fake_check)
    env = {"XDG_STATE_HOME": str(tmp_path)}
    discovery.refresh_evidence(env, [], path=tmp_path / "ev.json")
    assert seen["budget"] == 120
    discovery.refresh_evidence(env, [], path=tmp_path / "ev.json", time_budget_seconds=None)
    assert seen["budget"] is None


def test_workflow_drip_maps_to_unknown(monkeypatch, tmp_path):
    seen = _raising_burn(monkeypatch, workflow_health)

    def respond(request):
        return httpx.Response(200, json={"state": "active"})

    monkeypatch.setattr(workflow_health, "_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    env = {"XDG_STATE_HOME": str(tmp_path), "GITHUB_TOKEN": "SECRET"}
    result = workflow_health.refresh_workflow(env)
    assert result["status"] == "unknown"
    _deadline_is_source_start_plus_30(seen)
    assert MESSAGE not in json.dumps(result)


def test_limits_drip_maps_to_error_not_review_required(monkeypatch):
    seen = _raising_burn(monkeypatch, limit_sources)

    def respond(request):
        return httpx.Response(200, text="<html>limits</html>")

    monkeypatch.setattr(limit_sources, "_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    original = {"groq": {"id": "groq", "limits": []}}
    report = limit_sources.collect_proposals(original)
    row = report["providers"]["groq"]
    assert row["status"] == "error"
    assert row["note"] == "Official limit source could not be read; no proposals generated."
    _deadline_is_source_start_plus_30(seen)
    assert MESSAGE not in json.dumps(report)


def test_account_drip_maps_to_error_not_malformed(monkeypatch, tmp_path):
    seen = _raising_burn(monkeypatch, account_observations)

    def respond(request):
        return httpx.Response(200, json={"data": {}})

    monkeypatch.setattr(account_observations, "_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(respond),
                                             follow_redirects=False))
    monkeypatch.setattr(account_observations, "_now",
                        lambda: datetime(2026, 9, 5, 12, tzinfo=UTC))
    env = {"FREELLMPOOL_OBSERVATIONS_FILE": str(tmp_path / "private" / "observations.json"),
           "OPENROUTER_API_KEY": "test-observation-secret"}
    result = account_observations.refresh_accounts(env, ["openrouter"])
    row = result["providers"]["openrouter"]
    assert row["status"] == "error"
    assert row["note"] == ("Account observation network or HTTP failure;"
                           " last-good evidence age is unchanged.")
    _deadline_is_source_start_plus_30(seen)
    assert MESSAGE not in json.dumps(result)


def test_policy_drip_maps_to_error_with_fixed_reason(monkeypatch, tmp_path):
    seen = _raising_burn(monkeypatch, policy_updates)

    def respond(request):
        return httpx.Response(200, json={"sha": "a" * 40})

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    packaged = json.loads((Path(__file__).parents[1]
                           / "src/freellmpool/provider_registry.json").read_text())
    env = {"FREELLMPOOL_POLICY_BUNDLE_FILE": str(tmp_path / "bundle.json"),
           "FREELLMPOOL_POLICY_STATUS_FILE": str(tmp_path / "status.json")}
    result = policy_updates.refresh_policy(env, client=client, packaged=packaged)
    assert result["status"] == "error"
    assert result["reason"] == ("Policy update failed validation or could not be fetched;"
                                " prior rules retained.")
    _deadline_is_source_start_plus_30(seen)
    assert MESSAGE not in json.dumps(result)
