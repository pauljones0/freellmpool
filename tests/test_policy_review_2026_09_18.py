"""Regression coverage for the September 18 upstream policy review."""
import json
from datetime import datetime
from pathlib import Path

import httpx

from freellmpool import discovery as d
from freellmpool.free_policy import admit

REGISTRY_PATH = Path(__file__).parents[1] / "src/freellmpool/provider_registry.json"
QWEN_36 = "qwen/qwen3.6-27b"
QWEN_38 = "qwen/qwen3.8-27b"


def packaged_registry():
    return json.loads(REGISTRY_PATH.read_text())


def test_modelscope_evidence_uses_canonical_posts_url():
    registry = packaged_registry()
    provider = next(p for p in registry["providers"] if p["id"] == "modelscope")
    urls = [source["url"] for source in provider["evidence"]]
    assert urls == ["https://modelscope.ai/posts/434362"]


def test_modelscope_article_digest_computed_for_posts_url(monkeypatch):
    article = {"Title": "Free allowance", "Content": "2,000 calls", "Click": 1, "Status": 1}
    value = json.dumps(json.dumps({"Articles": [article]}))
    body = f"<main>ModelScope guide</main><script>window.__detail_data__ = {value};</script>"

    def responder(request):
        assert request.url.path == "/posts/434362"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    monkeypatch.setattr(d, "_client", lambda: httpx.Client(transport=httpx.MockTransport(responder)))
    registry = {"modelscope": {"evidence": [{"id": "terms", "url": "https://modelscope.ai/posts/434362"}]}}
    record = d.check_public_sources(["modelscope"], registry=registry)["sources"][0]
    assert record["status"] == "ok"
    assert record["article_sha256"] == d.source_digest(body.encode(), "text/html", "modelscope_article_v1")


def test_groq_blocks_qwen36_absent_from_free_table():
    registry = packaged_registry()
    provider = next(p for p in registry["providers"] if p["id"] == "groq")
    assert QWEN_36 in provider["blocked_models"]
    assert QWEN_36 in provider["grants"][0]["model_selector"]["exclude"]
    for rule in provider["limits"]:
        assert QWEN_36 not in rule.get("model_capacities", {})
    assert QWEN_38 not in provider["blocked_models"]


def _fresh_account(provider):
    from datetime import timedelta

    expires = min(datetime.fromisoformat(row["expires_at"]) for row in provider["evidence"])
    moment = expires - timedelta(days=1)
    return {"tier": "free",
            "verified_at": (moment - timedelta(hours=1)).isoformat(),
            "expires_at": (moment + timedelta(hours=1)).isoformat()}, moment.timestamp()


def test_groq_qwen36_denied_even_with_free_account():
    from freellmpool.provider_registry import load_registry

    provider = load_registry()["groq"]
    account, now = _fresh_account(provider)
    model = {"id": QWEN_36, "modalities": ["chat"]}
    decision = admit(provider, model, account, now=now)
    assert not decision.allowed
    assert decision.reason == "model requires paid access"


def test_groq_qwen36_excluded_from_free_catalog():
    from freellmpool.provider_registry import load_registry

    provider = load_registry()["groq"]
    rows = [{"id": QWEN_36, "modalities": ["chat"]}, {"id": QWEN_38, "modalities": ["chat"]}]
    assert [row["id"] for row in d.free_catalog_models(provider, rows)] == [QWEN_38]
