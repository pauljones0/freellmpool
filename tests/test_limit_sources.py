"""Only explicitly identified official free tables can propose capacities."""

import copy
import json

import httpx
import pytest

from freellmpool import limit_sources as s


def registry():
    return {"groq": {"id": "groq", "limits": [
        {"id": "rpm", "metric": "requests", "scope": "model", "window_seconds": 60,
         "capacity": None, "model_capacities": {"openai/gpt-oss-20b": 20}},
        {"id": "rpd", "metric": "requests", "scope": "model", "window_seconds": 86400,
         "capacity": None, "model_capacities": {"openai/gpt-oss-20b": 1000}},
        {"id": "tpm", "metric": "total_tokens", "scope": "model", "window_seconds": 60,
         "capacity": None, "model_capacities": {"openai/gpt-oss-20b": 6000}},
    ]}}


def document(free=None, *, headers=None, duplicate=False):
    titles = ["MODEL ID", "RPM", "RPD", "TPM", "TPD", "ASH", "ASD"]
    descriptions = [None, "Requests per minute", "Requests per day", "Tokens per minute",
                    "Tokens per day", "Audio seconds per hour", "Audio seconds per day"]
    props = {"freeRows": [["openai/gpt-oss-20b", "30", "1K", "8K", "200K", "-", "-"]]
             if free is None else free,
             "devRows": [["openai/gpt-oss-20b", "1K", "100K", "250K", "-", "-", "-"]],
             "headers": headers if headers is not None else [
                 {"title": title, **({"tooltip": desc} if desc else {})}
                 for title, desc in zip(titles, descriptions, strict=True)]}
    payload = "18:" + json.dumps(["$", "$L49", None, props]) + "\n"
    script = "<script>self.__next_f.push(" + json.dumps([1, payload]) + ")</script>"
    return "<html><body>Free Plan Limits Developer Plan Limits" + script * (2 if duplicate else 1) + "</body></html>"


def transport(monkeypatch, html):
    calls = []
    def respond(request):
        calls.append(request)
        assert str(request.url) == "https://console.groq.com/docs/rate-limits"
        assert request.method == "GET" and "authorization" not in request.headers
        return httpx.Response(200, text=html)
    monkeypatch.setattr(s, "_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    return calls


def test_structured_free_table_proposals_use_correct_plan_units_and_old_values(monkeypatch):
    calls = transport(monkeypatch, document())
    original = registry()
    before = copy.deepcopy(original)
    report = s.collect_proposals(original)
    row = report["providers"]["groq"]
    assert row["status"] == "ok" and len(calls) == 1
    assert len(row["source_sha256"]) == 64
    proposals = {p["rule_id"]: p for p in row["proposals"]}
    assert proposals["rpm"]["old_capacity"] == 20
    assert proposals["rpm"]["new_capacity"] == 30
    assert proposals["tpm"]["new_capacity"] == 8000
    assert "rpd" not in proposals
    assert all(p["scope"] == "model" and p["model_id"] == "openai/gpt-oss-20b"
               for p in proposals.values())
    assert before == original


@pytest.mark.parametrize("html", [
    "<table><tr><td>Free Plan Limits</td><td>1000</td></tr></table>",
    document(duplicate=True),
    document(free=[]),
    document(free=[["openai/gpt-oss-20b", "30 RPM", "1K", "8K", "200K", "-", "-"]]),
    document(free=[["openai/gpt-oss-20b", "NaN", "1K", "8K", "200K", "-", "-"]]),
    document(free=[["openai/gpt-oss-20b", "0.5", "1K", "8K", "200K", "-", "-"]]),
    document(free=[["openai/gpt-oss-20b", True, "1K", "8K", "200K", "-", "-"]]),
    document(free=[["@mention", "30", "1K", "8K", "200K", "-", "-"]]),
    document(free=[["openai/gpt-oss-20b", "30", "1K", "8K", "200K", "-", "-"]] * 2),
    document(headers=[{"title": "MODEL ID"}, {"title": "RPM", "tooltip": "Requests per month"}]),
])
def test_ambiguous_or_malformed_source_requires_review(monkeypatch, html):
    transport(monkeypatch, html)
    row = s.collect_proposals(registry())["providers"]["groq"]
    assert row["status"] == "review_required" and not row["proposals"]


def test_missing_reviewed_models_cannot_become_deleted_limits(monkeypatch):
    transport(monkeypatch, document(free=[["new/model", "30", "1K", "8K", "200K", "-", "-"]]))
    row = s.collect_proposals(registry())["providers"]["groq"]
    assert row["status"] == "review_required" and not row["proposals"]


def test_missing_numeric_value_does_not_mean_unlimited(monkeypatch):
    transport(monkeypatch, document(free=[["openai/gpt-oss-20b", "-", "1K", "8K", "200K", "-", "-"]]))
    row = s.collect_proposals(registry())["providers"]["groq"]
    assert row["status"] == "review_required" and not row["proposals"]


def test_schema_change_and_untrusted_registry_urls_cannot_redirect_fetch(monkeypatch):
    calls = transport(monkeypatch, document())
    specs = registry()
    specs["groq"]["evidence"] = [{"id": "limits", "url": "https://evil.example"}]
    specs["groq"]["limits"][0]["metric"] = "micro_usd"
    row = s.collect_proposals(specs)["providers"]["groq"]
    assert row["status"] == "review_required" and not row["proposals"]
    assert len(calls) == 1


@pytest.mark.parametrize("status", [302, 429, 500])
def test_failed_source_never_returns_proposals_or_response_body(monkeypatch, status):
    monkeypatch.setattr(s, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="malicious private content", headers={"Location": "https://evil.example"}))))
    row = s.collect_proposals(registry())["providers"]["groq"]
    assert row["status"] == "error" and not row["proposals"]
    assert "malicious" not in json.dumps(row)


def test_unsupported_providers_have_explicit_no_parser_status_without_network(monkeypatch):
    transport(monkeypatch, document())
    row = s.collect_proposals({"cohere": {"id": "cohere"}})["providers"]["cohere"]
    assert row["status"] == "unsupported" and not row["proposals"]
