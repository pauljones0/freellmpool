"""Source monitoring may renew identical reviewed policy, never rewrite it."""

import copy
import json
from datetime import UTC, datetime, timedelta

import httpx

from freellmpool import discovery as d
from freellmpool import provider_registry as registry_module


def fixture_registry(monkeypatch, tmp_path):
    content = "<html><main>Free: 10 requests per day.</main></html>"
    now = datetime.now(UTC)
    provider = {"id": "example", "grants": [{"capacity": 10}], "evidence": [{
        "id": "terms", "url": "https://provider.example/pricing", "status": "official",
        "checked_at": (now - timedelta(days=8)).isoformat(),
        "expires_at": (now - timedelta(days=1)).isoformat(),
        "source_hash": {"algorithm": "visible_text_v1", "sha256": d.source_digest(content.encode(), "text/html")}}]}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"schema": 1, "providers": [provider]}))
    monkeypatch.setattr(registry_module, "REGISTRY_PATH", path)
    env = {"FREELLMPOOL_EVIDENCE_FILE": str(tmp_path / "evidence.json")}
    return provider, content, env


def mock_source(monkeypatch, content, status=200):
    def responder(request):
        assert request.method == "GET"
        assert "authorization" not in request.headers
        return httpx.Response(status, text=content, headers={"content-type": "text/html"})
    monkeypatch.setattr(d, "_client", lambda: httpx.Client(transport=httpx.MockTransport(responder)))


def test_unchanged_reviewed_source_renews_only_evidence_dates(monkeypatch, tmp_path):
    provider, content, env = fixture_registry(monkeypatch, tmp_path)
    mock_source(monkeypatch, content)
    result = d.refresh_evidence(env)
    assert result["providers"]["example"]["terms"]["status"] == "unchanged"
    updated = registry_module.load_registry(env)["example"]
    assert updated["grants"] == provider["grants"]
    assert updated["evidence"][0]["checked_at"] > provider["evidence"][0]["checked_at"]
    assert registry_module.load_registry()["example"] == provider


def test_changed_failed_unreviewed_sources_cannot_renew(monkeypatch, tmp_path):
    provider, content, env = fixture_registry(monkeypatch, tmp_path)
    for new_content, status in [(content.replace("10", "100"), 200), (content, 503)]:
        mock_source(monkeypatch, new_content, status)
        d.refresh_evidence(env)
        assert registry_module.load_registry(env)["example"] == provider
    provider["evidence"][0].pop("source_hash")
    registry_module.REGISTRY_PATH.write_text(json.dumps({"schema": 1, "providers": [provider]}))
    mock_source(monkeypatch, content)
    d.refresh_evidence(env)
    assert registry_module.load_registry(env)["example"] == provider


def test_changed_packaged_grant_invalidates_prior_renewal(monkeypatch, tmp_path):
    provider, content, env = fixture_registry(monkeypatch, tmp_path)
    mock_source(monkeypatch, content)
    d.refresh_evidence(env)
    changed = copy.deepcopy(provider)
    changed["grants"][0]["capacity"] = 100
    registry_module.REGISTRY_PATH.write_text(json.dumps({"schema": 1, "providers": [changed]}))
    assert registry_module.load_registry(env)["example"] == changed


def test_failed_check_preserves_last_good_age_without_renewal(monkeypatch, tmp_path):
    _, content, env = fixture_registry(monkeypatch, tmp_path)
    mock_source(monkeypatch, content)
    original = d.refresh_evidence(env)["providers"]["example"]["terms"]
    mock_source(monkeypatch, "Unavailable", 503)
    row = d.refresh_evidence(env)["providers"]["example"]["terms"]
    assert row["checked_at"] == original["checked_at"]
    assert row["expires_at"] == original["expires_at"]
    assert row["last_status"] == "check_failed"
    assert registry_module.load_registry(env)["example"]["evidence"][0]["checked_at"] == original["checked_at"]


def test_malformed_future_or_overlong_renewals_are_ignored(monkeypatch, tmp_path):
    provider, content, env = fixture_registry(monkeypatch, tmp_path)
    mock_source(monkeypatch, content)
    original = d.refresh_evidence(env)
    for days, future in [(365, False), (7, True)]:
        value = copy.deepcopy(original)
        row = value["providers"]["example"]["terms"]
        now = datetime.now(UTC) + timedelta(days=1 if future else 0)
        row["checked_at"] = now.isoformat()
        row["expires_at"] = (now + timedelta(days=days)).isoformat()
        from pathlib import Path
        Path(env["FREELLMPOOL_EVIDENCE_FILE"]).write_text(json.dumps(value))
        assert registry_module.load_registry(env)["example"] == provider


def test_source_digest_ignores_scripts_but_detects_visible_prices():
    a = b"<html><script>nonce=1</script><main> Free: $0 </main></html>"
    b = b"<html><script>nonce=2</script><main>Free:  $0</main></html>"
    assert d.source_digest(a, "text/html") == d.source_digest(b, "text/html")
    assert d.source_digest(a, "text/html") != d.source_digest(b.replace(b"$0", b"$1"), "text/html")


def test_modelscope_article_digest_ignores_view_counts_and_tracks_full_content():
    def page(content, views):
        article = {"Title": "Free allowance", "Content": content, "Click": views, "Status": 1}
        value = json.dumps(json.dumps({"Articles": [article]}))
        return f"<script>window.__detail_data__ = {value};</script>".encode()
    digest = d.source_digest(page("2,000 calls", 1), "text/html", "modelscope_article_v1")
    assert digest == d.source_digest(page("2,000 calls", 2), "text/html", "modelscope_article_v1")
    assert digest != d.source_digest(page("Paid calls only", 2), "text/html", "modelscope_article_v1")


def test_discourse_policy_post_hash_ignores_related_topic_counts_and_detects_rule_changes():
    def page(rule, activity):
        return (f'<div class="post" itemprop="text"><p>{rule}</p><div><p>Evaluation only.</p></div></div>'
                f'<aside>{activity} replies</aside><div class="post" itemprop="text">Comment {activity}</div>').encode()
    digest = d.source_digest(page("Free hosted evaluation", 3), "text/html", "discourse_first_post_v1")
    assert digest == d.source_digest(page("Free hosted evaluation", 4), "text/html", "discourse_first_post_v1")
    assert digest != d.source_digest(page("Paid hosted evaluation", 4), "text/html", "discourse_first_post_v1")
    import pytest
    for invalid in (b"<p>No post</p>", b'<div class="post" itemprop="text">Unclosed'):
        with pytest.raises(ValueError):
            d.source_digest(invalid, "text/html", "discourse_first_post_v1")


def test_visible_text_v1_vectors_stay_pinned():
    pinned = b"<html><main>Free: 10 requests per day. (built 2026-09-25T05:54:54.356Z)</main></html>"
    assert d.source_digest(pinned, "text/html") == "38b3714e81e1abd4c72cb9ec7e3ad72c970ae9a59430920e6bfc0e6f1e2421ef"


def test_visible_text_v2_ignores_build_stamps_but_detects_policy_changes():
    def page(stamp, allowance):
        return (f"<html><main>Free: {allowance} requests per day. "
                f"From the docs graph (built {stamp}), spanning docs.</main></html>").encode()
    digest = d.source_digest(page("2026-09-25T05:54:54.356Z", "10"), "text/html", "visible_text_v2")
    assert digest == d.source_digest(page("2026-09-25T15:15:34.887Z", "10"), "text/html", "visible_text_v2")
    assert digest != d.source_digest(page("2026-09-25T15:15:34.887Z", "100"), "text/html", "visible_text_v2")


def test_visible_text_v2_ignores_last_updated_dates_and_relative_times():
    def page(updated, activity):
        return (f"<html><main>Free tier. Last updated {updated}. Deployed {activity}.</main></html>").encode()
    digest = d.source_digest(page("September 8, 2026", "3 hours ago"), "text/html", "visible_text_v2")
    assert digest == d.source_digest(page("September 25, 2026", "10 hours ago"), "text/html", "visible_text_v2")
    assert digest == d.source_digest(page("2026-09-25", "just now"), "text/html", "visible_text_v2")
    assert digest != d.source_digest(page("2026-09-25", "just now").replace(b"Free tier", b"Paid tier"),
                                     "text/html", "visible_text_v2")


def test_visible_text_v2_unknown_algorithm_still_rejected():
    import pytest
    with pytest.raises(ValueError):
        d.source_digest(b"<main>Free</main>", "text/html", "visible_text_v9")


def test_refresh_evidence_v2_ignores_stamp_drift_but_blocks_policy_change(monkeypatch, tmp_path):
    before = "<html><main>Free: 10 requests per day. (built 2026-09-25T05:54:54.356Z)</main></html>"
    now = datetime.now(UTC)
    provider = {"id": "example", "grants": [{"capacity": 10}], "evidence": [{
        "id": "terms", "url": "https://provider.example/pricing", "status": "official",
        "checked_at": (now - timedelta(days=8)).isoformat(),
        "expires_at": (now - timedelta(days=1)).isoformat(),
        "source_hash": {"algorithm": "visible_text_v2",
                        "sha256": d.source_digest(before.encode(), "text/html", "visible_text_v2")}}]}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"schema": 1, "providers": [provider]}))
    monkeypatch.setattr(registry_module, "REGISTRY_PATH", path)
    env = {"FREELLMPOOL_EVIDENCE_FILE": str(tmp_path / "evidence.json")}
    stamp_only = before.replace("2026-09-25T05:54:54.356Z", "2026-09-26T01:02:03.004Z")
    mock_source(monkeypatch, stamp_only)
    result = d.refresh_evidence(env)
    assert result["providers"]["example"]["terms"]["status"] == "unchanged"
    # Separate evidence file: a changed page after a good renewal keeps the
    # last good row visible with last_status set, by design.
    env2 = {"FREELLMPOOL_EVIDENCE_FILE": str(tmp_path / "evidence2.json")}
    mock_source(monkeypatch, stamp_only.replace("10 requests", "100 requests"))
    result = d.refresh_evidence(env2)
    assert result["providers"]["example"]["terms"]["status"] == "review_required"
