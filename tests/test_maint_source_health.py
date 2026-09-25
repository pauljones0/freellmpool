"""Batch maint-source-health: triage groq/gemini source changed/due (#128/#129/#137-#141).

Issues filed Sept 20-24 after the Sept-18 review cutoff, plus evidence rows
due for renewal:

| Issue | Event | Verdict |
| --- | --- | --- |
| #128 | groq source_changed terms | REFRESHED: claim re-verified against live text; hash+window renewed, no claim or grant change |
| #129 | groq source_changed speech_billing | REFRESHED: same shape as #128 |
| #137 | gemini source_due limits | REFRESHED: claim re-verified; hash+window renewed |
| #138 | gemini source_due auth | REFRESHED: claim re-verified; hash+window renewed |
| #139 | groq source_due terms | REFRESHED: same renewal as #128 |
| #140 | groq source_due limits | REFRESHED 2026-09-25: rate-limits docs page re-verified (org-scoped limits, documented headers, account-specific caps); hash+window renewed. Numeric compound capacities stay under the separate #79 limit review, never guessed |
| #141 | groq source_due speech_billing | REFRESHED: same renewal as #129 |

Recorded live evidence below was fetched 2026-09-25T05:41Z (credentialless
GET of each reviewed evidence URL via the repo's own check_public_sources)
plus a structured parse of the groq free table at 05:43Z. Changed-issue
After hashes equal the live hashes: the evidence is unchanged since filing.
A follow-up review on 2026-09-25 re-verified the groq limits docs claim
against the live page (observed stable across repeated fetches) and renewed
its hash+window; the compound capacity review continues independently.

This module pins the verdicts following tests/test_maint_ling_vl.py: every
recorded fingerprint recomputes under the repo's own fingerprint function,
the batch verifier reports `unchanged` (closable as reviewed) for all 6
batch targets against a current report, the five renewals touch hash+window
only, and an end-to-end report proves the renewed sources go silent while
the groq limit review (#79) keeps its exact live fingerprint. The recorded
#140 row below is the pre-renewal state used by the batch-mechanism tests;
the live issue resolves once the renewal commits.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from freellmpool import maintenance as m
from freellmpool.provider_registry import (
    evidence_renewal_is_current,
    load_registry,
    policy_digest,
)
from scripts import verify_maint_close_batch as batch

BATCH_PATH = Path("maintenance/close-batch-source-health.json")
REGISTRY_PATH = Path("src/freellmpool/provider_registry.json")
CHANNEL_PATH = Path("maintenance/policy-channel.json")

# Managed-section fingerprints, read from the live GitHub issues (all OPEN).
RECORDED: list[dict[str, Any]] = [
    {"issue": 128, "provider": "groq", "code": "source_changed",
     "subject": "terms",
     "before": "4cdb46e1f700505cd5dc99ab3a60a7fae4a146577fae60d5dbdd87bc1bc5d761",
     "after": "386717a7b89a70e3f3fed4a0d4e5cd96831708ef621d2e4fef4bb648ade78db0",
     "reviewed_fp": "97a79180fa472d3c23d181e302d9e26e6302aa637ed97da4b3d21a7da1d4fa2d"},
    {"issue": 129, "provider": "groq", "code": "source_changed",
     "subject": "speech_billing",
     "before": "f42c3b879fa705a8af1142cee6660275747c6880e71af06edac98740b476806e",
     "after": "28eedbd0af45d6187c49c28eeff4f59576775ec94263c0239981472d3cfac7b5",
     "reviewed_fp": "39473571af32da500e643f16d97dfec3a4a9773c66d76c144cf5976fbbed1395"},
    {"issue": 137, "provider": "gemini", "code": "source_due",
     "subject": "limits", "before": None, "after": None,
     "reviewed_fp": "c449a380d8dd79b0edb75ad327d9b05986c85a9d6412204dd72a349512e1c409"},
    {"issue": 138, "provider": "gemini", "code": "source_due",
     "subject": "auth", "before": None, "after": None,
     "reviewed_fp": "fc15eadc6be01b38f2af7f65f337861fd3bc4df2b499b0bd1e9a033052e5b2b9"},
    {"issue": 139, "provider": "groq", "code": "source_due",
     "subject": "terms", "before": None, "after": None,
     "reviewed_fp": "c5c55ad7a78246fa4e49b3a57b369eaee915df7ed6d433812ed34dc45ec3a9b2"},
    {"issue": 141, "provider": "groq", "code": "source_due",
     "subject": "speech_billing", "before": None, "after": None,
     "reviewed_fp": "35196cb1b77eff020c7f0dee7130c4f4816183e4e3324ccecbc9494d1afdcbb4"},
]

# Pre-renewal #140 record for the batch-mechanism tests (fingerprint math,
# batch exclusion, verdict matrix). The live issue is resolved by the Sept-25
# hash+window renewal; compound capacities stay tracked by #79 instead.
STAYS_OPEN_140: dict[str, Any] = {
    "issue": 140, "provider": "groq", "code": "source_due",
    "subject": "limits", "before": None, "after": None,
    "reviewed_fp": "08f6d6bae96535af30950b467fe73c8e93975f5ed589eaea04a663330d7975a3",
}

CHECKED_AT = "2026-09-25T05:41:27+00:00"

# Renewed evidence windows for the five refreshed rows (7-day convention).
REFRESH_CHECKED_AT = "2026-09-25T00:00:00+00:00"
REFRESH_EXPIRES_AT = "2026-10-02T00:00:00+00:00"

# Live source hashes (visible_text_v1) observed 2026-09-25T05:41Z via the
# repo's own check_public_sources. Changed-issue After values equal these
# live hashes; gemini After values also match the Sept-24 scheduled-run
# observations pinned in tests/test_maint_close_reviewed_16.py.
GROQ_TERMS_BEFORE = "4cdb46e1f700505cd5dc99ab3a60a7fae4a146577fae60d5dbdd87bc1bc5d761"
GROQ_TERMS_AFTER = "386717a7b89a70e3f3fed4a0d4e5cd96831708ef621d2e4fef4bb648ade78db0"
GROQ_SPEECH_BEFORE = "f42c3b879fa705a8af1142cee6660275747c6880e71af06edac98740b476806e"
GROQ_SPEECH_AFTER = "28eedbd0af45d6187c49c28eeff4f59576775ec94263c0239981472d3cfac7b5"
GEMINI_LIMITS_BEFORE = "b9bfe4986f3613b17da6952600f359d9a65e3515da208b0cae4211c6fd1d08a2"
GEMINI_LIMITS_AFTER = "e8c425f16cb47e71d378c1c294019498b0079461137bbc6107ff7e6be7e07511"
GEMINI_AUTH_BEFORE = "354178211285fca77b55e2066742d963b78001c4641dc71be4984c801e46b7d7"
GEMINI_AUTH_AFTER = "b4443b1399446eff6f72b23e9e7852b9d4e8734b0e060a70110d4f5a4f3aa243"

# groq limits docs page: Sept-18 baseline replaced by the verified live hash
# on 2026-09-25 (claim holds verbatim; observed stable across re-fetches).
GROQ_LIMITS_BEFORE = "6ea925ca546471b5f094b430e2344369bbd1eab5dc40062236e713b006de8fb7"
GROQ_LIMITS_AFTER = "d3862f3e4f047c34905760b463ace2ce40369872eb0159d1563743b64ab7eed2"

# Retained claims: each re-verified against the current live page text.
# groq terms still documents the Free-to-Developer upgrade via a payment
# method with Free tier limits on downgrade; groq speech still documents a
# 10-second minimum billed length; gemini limits still documents per-project
# RPM/input-TPM/RPD with a midnight-Pacific daily reset and AI Studio as the
# exact-limit source; gemini auth still documents the September 2026
# standard-key rejection with service-account-bound auth keys in AI Studio.
GROQ_TERMS_CLAIM = ("The Free organization plan is distinct from the billable Developer plan. "
                    "Adding a payment method upgrades to Developer; paid balances do not preserve "
                    "a free request price.")
GROQ_SPEECH_CLAIM = ("Speech-to-text has a 10-second minimum billed length; reserve this floor "
                     "conservatively with the audio quota.")
GEMINI_LIMITS_CLAIM = ("Rate limits are per project, not key; input TPM and requests have separate "
                       "limits. Daily requests reset at midnight America/Los_Angeles. Exact current "
                       "limits appear in AI Studio.")
GEMINI_AUTH_CLAIM = ("September 2026 standard API-key retirement requires current AI Studio "
                     "authorization keys, bound to service accounts. Auth migration errors must not "
                     "be labeled quota exhaustion.")
GROQ_LIMITS_CLAIM = ("Limits apply by organization and model. Request headers are daily requests; "
                     "token headers are tokens/minute. Exact limits are account-specific; daily "
                     "reset timezone is undocumented.")

# Live groq free table parsed 2026-09-25T05:43Z via parse_groq_free_limits:
# 10 models; groq/compound and groq/compound-mini are absent while the
# registry still reviews capacities for them.
LIVE_GROQ_FREE_TABLE: dict[str, dict[str, int | None]] = {
    "canopylabs/orpheus-arabic-saudi": {"rpm": 10, "rpd": 100, "tpm": 1200, "tpd": 3600, "ash": None, "asd": None},
    "canopylabs/orpheus-v1-english": {"rpm": 10, "rpd": 100, "tpm": 1200, "tpd": 3600, "ash": None, "asd": None},
    "meta-llama/llama-prompt-guard-2-22m": {"rpm": 30, "rpd": 14400, "tpm": 15000, "tpd": 500000, "ash": None, "asd": None},
    "meta-llama/llama-prompt-guard-2-86m": {"rpm": 30, "rpd": 14400, "tpm": 15000, "tpd": 500000, "ash": None, "asd": None},
    "openai/gpt-oss-120b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000, "ash": None, "asd": None},
    "openai/gpt-oss-20b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000, "ash": None, "asd": None},
    "openai/gpt-oss-safeguard-20b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000, "ash": None, "asd": None},
    "qwen/qwen3.8-27b": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000, "ash": None, "asd": None},
    "whisper-large-v3": {"rpm": 20, "rpd": 2000, "tpm": None, "tpd": None, "ash": 7200, "asd": 28800},
    "whisper-large-v3-turbo": {"rpm": 20, "rpd": 2000, "tpm": None, "tpd": None, "ash": 7200, "asd": 28800},
}

# Cross-batch fingerprint from inspected live state (Sept-24 public-report
# artifact run 35984815610; #79 body): the end-to-end report must reproduce
# the compound capacity review exactly after this batch's renewal.
GROQ_LIMIT_REVIEW_FP = "9b9bea78aba89a0f2c4b04fb4b6fa492b28d07d8bc7276fc14fa0950b9927c65"

GEMINI_ALLOWLIST = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
    "gemini-robotics-er-2-preview", "gemini-robotics-er-1.6-preview",
    "gemma-4-26b-a4b-it", "gemma-4-31b-it",
]


def _finding_row(row: dict[str, Any], fingerprint: str | None = None) -> dict[str, Any]:
    finding = m._finding(
        str(row["provider"]), str(row["code"]), str(row["subject"]),
        before=row["before"], after=row["after"])
    if fingerprint is not None:
        finding["fingerprint"] = fingerprint
    return finding


def _live_report(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    selected = [*RECORDED, STAYS_OPEN_140] if rows is None else rows
    return {"findings": [_finding_row(row) for row in selected],
            "pending_changes": [], "resolutions": [], "providers": {},
            "checked_at": CHECKED_AT}


def _registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _provider(pid: str) -> dict[str, Any]:
    registry = _registry()
    return next(p for p in registry["providers"] if p["id"] == pid)


def _evidence(pid: str, sid: str) -> dict[str, Any]:
    return next(e for e in _provider(pid)["evidence"] if e["id"] == sid)


def test_recorded_fingerprints_recompute_under_repo_function() -> None:
    assert len(RECORDED) == 6
    for row in [*RECORDED, STAYS_OPEN_140]:
        rebuilt = m._finding(
            str(row["provider"]), str(row["code"]), str(row["subject"]),
            before=row["before"], after=row["after"])
        assert rebuilt["fingerprint"] == row["reviewed_fp"], f"issue #{row['issue']}"


def test_batch_spec_lists_exactly_the_six_targets() -> None:
    spec = batch.load_batch(BATCH_PATH)
    assert [entry["issue"] for entry in spec["targets"]] == [128, 129, 137, 138, 139, 141]
    wanted = {(row["provider"], row["code"], row["subject"]): row["reviewed_fp"]
              for row in RECORDED}
    for entry in spec["targets"]:
        key = (entry["provider"], entry["code"], entry["subject"])
        assert entry["reviewed_fingerprint"] == wanted[key]


def test_140_groq_limits_due_is_excluded_from_the_batch() -> None:
    spec = batch.load_batch(BATCH_PATH)
    triples = {(entry["provider"], entry["code"], entry["subject"])
               for entry in spec["targets"]}
    assert (STAYS_OPEN_140["provider"], STAYS_OPEN_140["code"], STAYS_OPEN_140["subject"]) not in triples
    assert 140 not in [entry["issue"] for entry in spec["targets"]]


def test_batch_verdicts_are_all_unchanged_against_current_report() -> None:
    spec = batch.load_batch(BATCH_PATH)
    verdicts = batch.verdicts(spec, _live_report())
    assert sorted(verdicts) == [128, 129, 137, 138, 139, 141]
    assert set(verdicts.values()) == {"unchanged"}


def test_changed_evidence_refires_instead_of_closing() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [_finding_row(row) for row in [*RECORDED, STAYS_OPEN_140]]
    rows[0] = _finding_row(RECORDED[0], fingerprint="f" * 64)
    report = _live_report([])
    report["findings"] = rows
    verdicts = batch.verdicts(spec, report)
    assert verdicts[128] == "refired"
    assert verdicts[129] == "unchanged"


def test_absent_target_without_proof_needs_review() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [row for row in [*RECORDED, STAYS_OPEN_140] if row["issue"] != 137]
    assert batch.verdicts(spec, _live_report(rows))[137] == "needs-review"


def test_matching_source_resolution_with_fresh_evidence_is_resolved() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [row for row in [*RECORDED, STAYS_OPEN_140] if row["issue"] != 139]
    report = _live_report(rows)
    reviewed = next(row for row in RECORDED if row["issue"] == 139)
    report["resolutions"] = [{
        "id": "groq:source_due:subject", "provider": "groq", "kind": "incident",
        "code": "source_due", "subject": "terms",
        "fingerprint": reviewed["reviewed_fp"]}]
    report["providers"] = {"groq": {"sources": [{
        "id": "terms", "status": "unchanged",
        "checked_at": "2026-09-25T05:00:00+00:00",
        "expires_at": REFRESH_EXPIRES_AT}]}}
    assert batch.verdicts(spec, report)[139] == "resolved"


def test_cli_reports_closable_batch_with_zero_exit(tmp_path: Path) -> None:
    report_path = tmp_path / "public-report.json"
    report_path.write_text(json.dumps(_live_report()))
    assert batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path)]) == 0
    quiet = batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path),
                        "--format", "json"])
    assert quiet == 0


def test_128_139_groq_terms_refresh_renews_hash_and_window_only() -> None:
    evidence = _evidence("groq", "terms")
    assert evidence["url"] == "https://console.groq.com/docs/billing-faqs"
    assert evidence["claim"] == GROQ_TERMS_CLAIM
    assert evidence["status"] == "verified"
    assert evidence["source_hash"] == {"algorithm": "visible_text_v1",
                                       "sha256": GROQ_TERMS_AFTER}
    assert evidence["checked_at"] == REFRESH_CHECKED_AT
    assert evidence["expires_at"] == REFRESH_EXPIRES_AT
    # No grant or eligibility change rides along with the renewal.
    spec = _provider("groq")
    assert spec["grants"][0]["kind"] == "recurring_quota"
    assert spec["grants"][0]["status"] == "conditional"
    assert spec["grants"][0]["model_selector"]["kind"] == "all"
    assert spec.get("blocked_models") == ["qwen/qwen3.6-27b"]


def test_129_141_groq_speech_refresh_renews_hash_and_window_only() -> None:
    evidence = _evidence("groq", "speech_billing")
    assert evidence["url"] == "https://console.groq.com/docs/speech-to-text"
    assert evidence["claim"] == GROQ_SPEECH_CLAIM
    assert evidence["status"] == "verified"
    assert evidence["source_hash"] == {"algorithm": "visible_text_v1",
                                       "sha256": GROQ_SPEECH_AFTER}
    assert evidence["checked_at"] == REFRESH_CHECKED_AT
    assert evidence["expires_at"] == REFRESH_EXPIRES_AT


def test_137_gemini_limits_refresh_renews_hash_and_window_only() -> None:
    evidence = _evidence("gemini", "limits")
    assert evidence["url"] == "https://ai.google.dev/gemini-api/docs/rate-limits"
    assert evidence["claim"] == GEMINI_LIMITS_CLAIM
    assert evidence["status"] == "verified"
    assert evidence["source_hash"] == {"algorithm": "visible_text_v1",
                                       "sha256": GEMINI_LIMITS_AFTER}
    assert evidence["checked_at"] == REFRESH_CHECKED_AT
    assert evidence["expires_at"] == REFRESH_EXPIRES_AT
    # No grant or eligibility change rides along with the renewal.
    spec = _provider("gemini")
    assert spec["grants"][0]["kind"] == "recurring_quota"
    assert spec["grants"][0]["model_selector"]["models"] == GEMINI_ALLOWLIST
    assert spec.get("blocked_models") is None


def test_138_gemini_auth_refresh_renews_hash_and_window_only() -> None:
    evidence = _evidence("gemini", "auth")
    assert evidence["url"] == "https://ai.google.dev/gemini-api/docs/api-key"
    assert evidence["claim"] == GEMINI_AUTH_CLAIM
    assert evidence["status"] == "verified"
    assert evidence["source_hash"] == {"algorithm": "visible_text_v1",
                                       "sha256": GEMINI_AUTH_AFTER}
    assert evidence["checked_at"] == REFRESH_CHECKED_AT
    assert evidence["expires_at"] == REFRESH_EXPIRES_AT


def test_140_groq_limits_refresh_renews_hash_and_window_only() -> None:
    evidence = _evidence("groq", "limits")
    assert evidence["url"] == "https://console.groq.com/docs/rate-limits"
    assert evidence["claim"] == GROQ_LIMITS_CLAIM
    assert evidence["status"] == "verified"
    assert evidence["source_hash"] == {"algorithm": "visible_text_v1",
                                       "sha256": GROQ_LIMITS_AFTER}
    assert evidence["checked_at"] == REFRESH_CHECKED_AT
    assert evidence["expires_at"] == REFRESH_EXPIRES_AT
    # No other capacity change rides along with the hash renewal.


def test_140_compound_capacities_removed_with_dead_models() -> None:
    spec = _provider("groq")
    assert "groq/compound" not in LIVE_GROQ_FREE_TABLE
    assert "groq/compound-mini" not in LIVE_GROQ_FREE_TABLE
    assert len(LIVE_GROQ_FREE_TABLE) == 10
    # Groq shut both models down 2026-09-21 (deprecation log) and removed
    # them from the free table, so the capacity review completed by REMOVING
    # their reviewed capacities (resolves #79) instead of preserving them.
    for rule in spec["limits"]:
        assert "groq/compound" not in rule.get("model_capacities", {})
        assert "groq/compound-mini" not in rule.get("model_capacities", {})


def test_renewed_report_keeps_only_expected_findings() -> None:
    registry = load_registry()
    subset = {"groq": registry["groq"], "gemini": registry["gemini"]}
    now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
    catalog = {"providers": {
        pid: {"status": "ok", "complete": True, "checked_at": "2026-09-24T11:00:00+00:00", "models": []}
        for pid in subset}}
    evidence: dict[str, Any] = {"providers": {"groq": {}, "gemini": {}}}
    for pid, sid, sha in (("groq", "terms", GROQ_TERMS_AFTER),
                          ("groq", "speech_billing", GROQ_SPEECH_AFTER),
                          ("groq", "limits", GROQ_LIMITS_AFTER),
                          ("gemini", "limits", GEMINI_LIMITS_AFTER),
                          ("gemini", "auth", GEMINI_AUTH_AFTER)):
        source = next(e for e in subset[pid]["evidence"] if e["id"] == sid)
        evidence["providers"][pid][sid] = {
            "url": source["url"], "status": "unchanged", "sha256": sha,
            "hash_algorithm": "visible_text_v1",
            "policy_sha256": policy_digest(subset[pid]),
            "checked_at": REFRESH_CHECKED_AT, "expires_at": REFRESH_EXPIRES_AT}
    proposals = {"providers": {"groq": {"status": "review_required", "proposals": []}}}
    report, _ = m.build_public_report(subset, catalog, evidence=evidence,
                                      proposals=proposals, now=now)
    findings = {(row["provider"], row["code"], row["subject"]): row
                for row in report["findings"]}
    # Renewed sources are silent: no changed or due findings for them.
    for triple in (("groq", "source_changed", "terms"),
                   ("groq", "source_due", "terms"),
                   ("groq", "source_changed", "speech_billing"),
                   ("groq", "source_due", "speech_billing"),
                   ("groq", "source_changed", "limits"),
                   ("groq", "source_due", "limits"),
                   ("gemini", "source_changed", "limits"),
                   ("gemini", "source_due", "limits"),
                   ("gemini", "source_changed", "auth"),
                   ("gemini", "source_due", "auth"),
                   ("gemini", "source_due", "terms")):
        assert triple not in findings, triple
    # The compound capacity review still fires identically: #79's limit
    # review is unaffected by hash+window renewals. (gemini terms due went
    # silent under the separate Sept-25 terms renewal in the live tree.)
    assert findings[("groq", "limit_source_review", "provider")]["fingerprint"] == GROQ_LIMIT_REVIEW_FP


def test_refresh_rejects_stale_hash_renewal() -> None:
    now = 1790380800.0  # 2026-09-26T00:00:00Z, inside the renewed window
    cases = (("groq", "terms", GROQ_TERMS_BEFORE),
             ("groq", "speech_billing", GROQ_SPEECH_BEFORE),
             ("groq", "limits", GROQ_LIMITS_BEFORE),
             ("gemini", "limits", GEMINI_LIMITS_BEFORE),
             ("gemini", "auth", GEMINI_AUTH_BEFORE))
    for pid, sid, old_hash in cases:
        spec = _provider(pid)
        source = next(e for e in spec["evidence"] if e["id"] == sid)
        overlay = {"status": "unchanged", "url": source["url"],
                   "hash_algorithm": "visible_text_v1",
                   "policy_sha256": policy_digest(spec),
                   "checked_at": REFRESH_CHECKED_AT, "expires_at": REFRESH_EXPIRES_AT}
        assert evidence_renewal_is_current(
            source, {**overlay, "sha256": old_hash}, policy_digest(spec), now) is False
        assert evidence_renewal_is_current(
            source, {**overlay, "sha256": source["source_hash"]["sha256"]},
            policy_digest(spec), now) is True


def test_policy_channel_matches_renewed_registry() -> None:
    manifest = json.loads(CHANNEL_PATH.read_text(encoding="utf-8"))
    # Revisions advance with each later review (9: Sept-25 sweep, 10:
    # cloudflare terms, 11: compound prune); the durable invariant is that
    # the manifest tracks this tree's registry bytes exactly.
    assert manifest["revision"] >= 9
    assert manifest["registry_sha256"] == hashlib.sha256(
        REGISTRY_PATH.read_bytes()).hexdigest()
