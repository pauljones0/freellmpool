"""Batch maint-ling-vl: triage the ling-3.0-flash-vl rotation (#142-#147).

Issues filed Sept 24 after the Sept-18 review cutoff, plus two evidence rows
due for renewal:

| Issue | Event | Verdict |
| --- | --- | --- |
| #142 | kilo model_removed inclusionai/ling-3.0-flash-vl:free | RETIRED: absent from the live 396-row catalog; paid sibling excluded by suffix+price gates; unpinned |
| #143 | kilo source_due terms | REFRESHED: page text moved but the retained claim re-verifies; hash+window renewed, no claim or grant change |
| #144 | mistral source_due limits | REFRESHED: same shape as #143 |
| #145 | openrouter model_removed inclusionai/ling-3.0-flash-vl:free | RETIRED: absent from the live 460-row catalog; unpinned |
| #146 | vercel model_removed inclusionai/ling-3.0-flash-vl-free | RETIRED: absent from the live 390-row catalog; -free siblings still listed; unpinned |
| #147 | vercel model_removed inclusionai/ling-3.0-flash-vl | STALE ARTIFACT: the paid id is still listed with nonzero pricing; stays open, excluded from the batch |

Recorded live catalog evidence below was fetched 2026-09-25T05:37Z
(credentialless GET of each provider's reviewed discovery URL). Row dicts
carry the exact fields the grant gates consume.

This module pins the verdicts following tests/test_maint_catalog_rotation.py:
every recorded fingerprint recomputes under the repo's own fingerprint
function, the batch verifier reports `unchanged` (closable as reviewed) for
all 5 batch targets against a current report, and per-issue admission proofs
show the retired free routes leave no grant, pin, or eligibility residue
while #147's live presence keeps it open.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from freellmpool import discovery as d
from freellmpool import free_policy as fp
from freellmpool import maintenance as m
from freellmpool.config import load_catalog
from freellmpool.provider_registry import evidence_renewal_is_current, policy_digest
from scripts import verify_maint_close_batch as batch

BATCH_PATH = Path("maintenance/close-batch-ling-vl.json")
REGISTRY_PATH = Path("src/freellmpool/provider_registry.json")
CHANNEL_PATH = Path("maintenance/policy-channel.json")

# Managed-section fingerprints, read from the live GitHub issues (all OPEN).
RECORDED: list[dict[str, Any]] = [
    {"issue": 142, "provider": "kilo", "code": "model_removed",
     "subject": "inclusionai/ling-3.0-flash-vl:free", "before": True, "after": False,
     "reviewed_fp": "dbac78b9da6b5a50863bc3a385e72006cb974f752da42f7dbcbc2cc483986600"},
    {"issue": 143, "provider": "kilo", "code": "source_due",
     "subject": "terms", "before": None, "after": None,
     "reviewed_fp": "b0205020b954c5e6a950cb1bb9921ac5799c745880a4394fc868e21d89d6650e"},
    {"issue": 144, "provider": "mistral", "code": "source_due",
     "subject": "limits", "before": None, "after": None,
     "reviewed_fp": "f449530d9e391c58950c67800664eb258596ff0086d8da1ab9e34e5ed716f400"},
    {"issue": 145, "provider": "openrouter", "code": "model_removed",
     "subject": "inclusionai/ling-3.0-flash-vl:free", "before": True, "after": False,
     "reviewed_fp": "5ee4901b03e9b4a2d9b057ff49e8ca80d2215b8358bba35cbbb8c96458afccbc"},
    {"issue": 146, "provider": "vercel", "code": "model_removed",
     "subject": "inclusionai/ling-3.0-flash-vl-free", "before": True, "after": False,
     "reviewed_fp": "0b51716d576739222b83aec3a49e294322380f0a7aa81079e808d80974c2bc59"},
]

# #147 stays open: the paid id is still in the live catalog, so the removal
# claim is a stale-source artifact, never a retirement.
STALE_147: dict[str, Any] = {
    "issue": 147, "provider": "vercel", "code": "model_removed",
    "subject": "inclusionai/ling-3.0-flash-vl", "before": True, "after": False,
    "reviewed_fp": "0d266b4e021a8accc9994e88fc7d72982f264e25d3f2b84647537bbd39144b4b",
}

CHECKED_AT = "2026-09-25T05:37:00+00:00"

# Renewed evidence windows for the two refreshed rows (7-day convention).
REFRESH_CHECKED_AT = "2026-09-25T00:00:00+00:00"
REFRESH_EXPIRES_AT = "2026-10-02T00:00:00+00:00"

# Live source hashes (visible_text_v1) observed 2026-09-25T05:37Z via the
# repo's own check_public_sources; both match the Sept-24 scheduled-run
# observations pinned in tests/test_maint_close_reviewed_16.py.
KILO_TERMS_BEFORE = "2ffcfcf9738e610e54a3d251273c329c6465b07e1832849a2ed245517c44f355"
KILO_TERMS_AFTER = "7929a3d278785805a7478b64d2624fbc91803c6b1b3f059c90299621e1e94c86"
MISTRAL_LIMITS_BEFORE = "08892b8f522260359ebfc7fca0afbcbe63ec92016d23bcbe97a5d6d3ee89cfde"
MISTRAL_LIMITS_AFTER = "db6c40c802e2f27532fbe7271dab3412b371c466989943e37d61b806d1a0c1fc"

# Retained claims: both re-verified against the current live page text. Kilo
# still documents anonymous access for free models only at 200 requests per
# hour per IP with :free-tagged ids; Mistral still documents the beta admin
# billing endpoints behind an admin API key.
KILO_TERMS_CLAIM = ("Anonymous free models are allowed; 200 requests/hour/IP applies "
                    "across free requests, not once per model or API key.")
MISTRAL_LIMITS_CLAIM = ("Optional admin-key GET /admin/rate-limit, /admin/spend-limit and "
                        "/admin/usage expose limits/usage. An inference key need not have admin "
                        "permissions; forbidden admin access leaves limits unknown.")

# Exact paid ling-vl rows (live kilo/openrouter): nonzero price, no :free suffix.
KILO_PAID_VL: dict[str, Any] = {
    "id": "inclusionai/ling-3.0-flash-vl",
    "architecture": {"input_modalities": ["text", "image", "video"],
                     "modality": "text+image+video->text",
                     "output_modalities": ["text"], "tokenizer": "Other"},
    "context_length": 262144,
    "pricing": {"prompt": "0.000000075000", "completion": "0.000000220000",
                "input_cache_read": "0.000000015000"},
}
KILO_FREE_CONTROL: dict[str, Any] = {
    "id": "qwen/qwen3.8-27b:free",
    "architecture": {"input_modalities": ["text", "image", "video"],
                     "modality": "text+image+video->text",
                     "output_modalities": ["text"], "tokenizer": "Qwen"},
    "context_length": 262144, "pricing": {"prompt": "0", "completion": "0"},
}
OR_PAID_VL: dict[str, Any] = {
    "id": "inclusionai/ling-3.0-flash-vl",
    "architecture": {"input_modalities": ["text", "image", "video"],
                     "modality": "text+image+video->text",
                     "output_modalities": ["text"], "tokenizer": "Other"},
    "context_length": 262144,
    "pricing": {"prompt": "0.00000006", "completion": "0.00000018",
                "input_cache_read": "0.000000012"},
}
OR_FREE_CONTROL: dict[str, Any] = {
    "id": "qwen/qwen3.8-27b:free",
    "architecture": {"input_modalities": ["text", "image", "video"],
                     "modality": "text+image+video->text",
                     "output_modalities": ["text"], "tokenizer": "Qwen"},
    "context_length": 262144, "pricing": {"prompt": "0", "completion": "0"},
}

# Exact vercel rows: paid vl is nonzero-priced; -free siblings are zero.
VERCEL_PAID_VL: dict[str, Any] = {
    "id": "inclusionai/ling-3.0-flash-vl", "object": "model",
    "owned_by": "inclusionai", "type": "language",
    "modalities": {"input": ["text", "image", "video"], "output": ["text"]},
    "pricing": {"input": "0.000000075", "output": "0.00000022",
                "input_cache_read": "0.000000015"},
}
VERCEL_FIN_FREE: dict[str, Any] = {
    "id": "inclusionai/ling-3.0-flash-fin-free", "object": "model",
    "owned_by": "inclusionai", "type": "language",
    "modalities": {"input": ["text"], "output": ["text"]},
    "pricing": {"input": "0", "output": "0"},
}
VERCEL_SANTE_FREE: dict[str, Any] = {
    "id": "inclusionai/ling-3.0-flash-sante-free", "object": "model",
    "owned_by": "inclusionai", "type": "language",
    "modalities": {"input": ["text"], "output": ["text"]},
    "pricing": {"input": "0", "output": "0"},
}

# Live :free id sets (kilo: 396 rows; openrouter: 460 rows; vercel: 390 rows).
KILO_FREE_IDS = [
    "cohere/north-mini-code:free", "dots-studio/dots-3-note-preview:free",
    "inclusionai/ling-3.0-flash-fin:free", "inclusionai/ling-3.0-flash-sante:free",
    "liquid/lfm-2.5-2.6b:free", "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3.5-lightning:free", "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free", "qwen/qwen3.8-27b:free",
    "stepfun/step-3.7-flash:free", "thinkingmachines/inkling-small:free",
    "z-ai/glm-5.2:free",
]
OR_FREE_IDS = [
    "cohere/north-mini-code:free", "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-26b-a4b-it:free", "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash-fin:free", "inclusionai/ling-3.0-flash-sante:free",
    "liquid/lfm-2.5-2.6b:free", "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3.5-lightning:free", "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free", "qwen/qwen3.8-27b:free",
    "thinkingmachines/inkling-small:free", "thinkingmachines/inkling:free",
    "z-ai/glm-5.2:free",
]
VERCEL_FREE_IDS = [
    "inclusionai/ling-3.0-flash-fin-free",
    "inclusionai/ling-3.0-flash-sante-free",
    "poolside/laguna-s-2.1-free",
]


def _finding_row(row: dict[str, Any], fingerprint: str | None = None) -> dict[str, Any]:
    finding = m._finding(
        str(row["provider"]), str(row["code"]), str(row["subject"]),
        before=row["before"], after=row["after"])
    if fingerprint is not None:
        finding["fingerprint"] = fingerprint
    return finding


def _live_report(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    selected = RECORDED if rows is None else rows
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
    assert len(RECORDED) == 5
    for row in [*RECORDED, STALE_147]:
        rebuilt = m._finding(
            str(row["provider"]), str(row["code"]), str(row["subject"]),
            before=row["before"], after=row["after"])
        assert rebuilt["fingerprint"] == row["reviewed_fp"], f"issue #{row['issue']}"


def test_batch_spec_lists_exactly_the_five_targets() -> None:
    spec = batch.load_batch(BATCH_PATH)
    assert [entry["issue"] for entry in spec["targets"]] == [142, 143, 144, 145, 146]
    wanted = {(row["provider"], row["code"], row["subject"]): row["reviewed_fp"]
              for row in RECORDED}
    for entry in spec["targets"]:
        key = (entry["provider"], entry["code"], entry["subject"])
        assert entry["reviewed_fingerprint"] == wanted[key]


def test_147_stale_artifact_is_excluded_from_the_batch() -> None:
    spec = batch.load_batch(BATCH_PATH)
    triples = {(entry["provider"], entry["code"], entry["subject"])
               for entry in spec["targets"]}
    assert (STALE_147["provider"], STALE_147["code"], STALE_147["subject"]) not in triples
    assert 147 not in [entry["issue"] for entry in spec["targets"]]


def test_batch_verdicts_are_all_unchanged_against_current_report() -> None:
    spec = batch.load_batch(BATCH_PATH)
    verdicts = batch.verdicts(spec, _live_report())
    assert sorted(verdicts) == [142, 143, 144, 145, 146]
    assert set(verdicts.values()) == {"unchanged"}


def test_changed_evidence_refires_instead_of_closing() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [_finding_row(row) for row in RECORDED]
    rows[0] = _finding_row(RECORDED[0], fingerprint="f" * 64)
    report = _live_report([])
    report["findings"] = rows
    verdicts = batch.verdicts(spec, report)
    assert verdicts[142] == "refired"
    assert verdicts[145] == "unchanged"


def test_absent_target_without_proof_needs_review() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [row for row in RECORDED if row["issue"] != 143]
    assert batch.verdicts(spec, _live_report(rows))[143] == "needs-review"


def test_matching_source_resolution_with_fresh_evidence_is_resolved() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [row for row in RECORDED if row["issue"] != 143]
    report = _live_report(rows)
    reviewed = next(row for row in RECORDED if row["issue"] == 143)
    report["resolutions"] = [{
        "id": "kilo:source_due:subject", "provider": "kilo", "kind": "incident",
        "code": "source_due", "subject": "terms",
        "fingerprint": reviewed["reviewed_fp"]}]
    report["providers"] = {"kilo": {"sources": [{
        "id": "terms", "status": "unchanged",
        "checked_at": "2026-09-25T05:00:00+00:00",
        "expires_at": REFRESH_EXPIRES_AT}]}}
    assert batch.verdicts(spec, report)[143] == "resolved"


def test_cli_reports_closable_batch_with_zero_exit(tmp_path: Path) -> None:
    report_path = tmp_path / "public-report.json"
    report_path.write_text(json.dumps(_live_report()))
    assert batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path)]) == 0
    quiet = batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path),
                        "--format", "json"])
    assert quiet == 0


def test_142_kilo_removed_free_vl_is_absent_and_unpinned() -> None:
    assert "inclusionai/ling-3.0-flash-vl:free" not in KILO_FREE_IDS
    assert "qwen/qwen3.8-27b:free" in KILO_FREE_IDS  # control: list is live, not empty
    assert "inclusionai/ling-3.0-flash-fin:free" in KILO_FREE_IDS  # sibling still live
    spec = _provider("kilo")
    assert spec["grants"][0]["model_selector"]["suffix"] == ":free"
    models = d.normalize_models("kilo", {"data": [KILO_PAID_VL, KILO_FREE_CONTROL]})
    by_id = {model["id"]: model for model in models}
    # The surviving paid sibling is excluded twice over: no :free suffix match
    # and nonzero price under the zero_price grant kind.
    assert fp.model_matches_grant(spec["grants"][0], by_id[KILO_PAID_VL["id"]]) is False
    assert fp.model_matches_grant(spec["grants"][0], by_id[KILO_FREE_CONTROL["id"]]) is True
    assert [model["id"] for model in d.free_catalog_models(spec, models)] == [
        KILO_FREE_CONTROL["id"]]
    names = [model.name for model in
             next(p for p in load_catalog() if p.id == "kilo").models]
    assert not [name for name in names if "ling-3.0-flash-vl" in name]


def test_145_openrouter_removed_free_vl_is_absent_and_unpinned() -> None:
    assert "inclusionai/ling-3.0-flash-vl:free" not in OR_FREE_IDS
    assert "qwen/qwen3.8-27b:free" in OR_FREE_IDS  # control: list is live, not empty
    assert "inclusionai/ling-3.0-flash-sante:free" in OR_FREE_IDS  # sibling still live
    spec = _provider("openrouter")
    assert spec["grants"][0]["model_selector"]["suffix"] == ":free"
    models = d.normalize_models("openrouter", {"data": [OR_PAID_VL, OR_FREE_CONTROL]})
    by_id = {model["id"]: model for model in models}
    assert fp.model_matches_grant(spec["grants"][0], by_id[OR_PAID_VL["id"]]) is False
    assert fp.model_matches_grant(spec["grants"][0], by_id[OR_FREE_CONTROL["id"]]) is True
    assert [model["id"] for model in d.free_catalog_models(spec, models)] == [
        OR_FREE_CONTROL["id"]]
    names = [model.name for model in
             next(p for p in load_catalog() if p.id == "openrouter").models]
    assert not [name for name in names if "ling-3.0-flash-vl" in name]


def test_146_vercel_free_vl_absent_while_priced_sibling_stays_out() -> None:
    assert "inclusionai/ling-3.0-flash-vl-free" not in VERCEL_FREE_IDS
    assert "inclusionai/ling-3.0-flash-fin-free" in VERCEL_FREE_IDS  # live control
    spec = _provider("vercel")
    assert spec["grants"][0]["kind"] == "zero_price"
    models = d.normalize_models(
        "vercel", {"data": [VERCEL_PAID_VL, VERCEL_FIN_FREE, VERCEL_SANTE_FREE]})
    by_id = {model["id"]: model for model in models}
    assert fp.model_matches_grant(spec["grants"][0], by_id[VERCEL_PAID_VL["id"]]) is False
    assert [model["id"] for model in d.free_catalog_models(spec, models)] == [
        VERCEL_FIN_FREE["id"], VERCEL_SANTE_FREE["id"]]
    names = [model.name for model in
             next(p for p in load_catalog() if p.id == "vercel").models]
    assert not [name for name in names if "ling-3.0-flash-vl" in name]


def test_147_paid_vl_still_listed_so_removal_stays_open() -> None:
    # The live catalog contradicts the removal: the paid id is present with
    # nonzero pricing, so #147 is a stale-source artifact, never a retirement.
    assert VERCEL_PAID_VL["id"] == STALE_147["subject"]
    assert any(float(amount) > 0 for amount in VERCEL_PAID_VL["pricing"].values())
    spec = _provider("vercel")
    (model,) = d.normalize_models("vercel", {"data": [VERCEL_PAID_VL]})
    assert fp.model_matches_grant(spec["grants"][0], model) is False
    assert d.free_catalog_models(spec, [model]) == []


def test_143_kilo_terms_refresh_renews_hash_and_window_only() -> None:
    evidence = _evidence("kilo", "terms")
    assert evidence["url"] == "https://kilo.ai/docs/gateway/authentication"
    assert evidence["claim"] == KILO_TERMS_CLAIM
    assert evidence["status"] == "verified"
    assert evidence["source_hash"] == {"algorithm": "visible_text_v1",
                                       "sha256": KILO_TERMS_AFTER}
    assert evidence["checked_at"] == REFRESH_CHECKED_AT
    assert evidence["expires_at"] == REFRESH_EXPIRES_AT
    # No grant or eligibility change rides along with the renewal.
    spec = _provider("kilo")
    assert spec["grants"][0]["kind"] == "zero_price"
    assert spec["grants"][0]["model_selector"]["suffix"] == ":free"
    assert spec.get("blocked_models") is None


def test_144_mistral_limits_refresh_renews_hash_and_window_only() -> None:
    evidence = _evidence("mistral", "limits")
    assert evidence["url"] == "https://docs.mistral.ai/api/endpoint/beta/admin/billing"
    assert evidence["claim"] == MISTRAL_LIMITS_CLAIM
    assert evidence["status"] == "verified"
    assert evidence["source_hash"] == {"algorithm": "visible_text_v1",
                                       "sha256": MISTRAL_LIMITS_AFTER}
    assert evidence["checked_at"] == REFRESH_CHECKED_AT
    assert evidence["expires_at"] == REFRESH_EXPIRES_AT
    spec = _provider("mistral")
    assert spec["grants"][0]["requires_account_evidence"] is True
    assert spec["grants"][1]["model_selector"]["models"] == ["mistral-embed"]


def test_refresh_rejects_stale_hash_renewal() -> None:
    now = 1790380800.0  # 2026-09-26T00:00:00Z, inside the renewed window
    for pid, sid, old_hash in (("kilo", "terms", KILO_TERMS_BEFORE),
                               ("mistral", "limits", MISTRAL_LIMITS_BEFORE)):
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
    assert manifest["revision"] == 9
    assert manifest["registry_sha256"] == hashlib.sha256(
        REGISTRY_PATH.read_bytes()).hexdigest()
