"""Batch maint-catalog-rotation: triage 9 post-cutoff catalog rotations.

Issues #121-#136 (filed Sept 18-24, after the Sept-18 review cutoff at #120):

| Issue | Event | Verdict |
| --- | --- | --- |
| #121 | modelscope model_added Qwen-Ambassador/Qwen3.8-Omni-Flash | BENIGN: discovery-handled under the reviewed all-chat grant; no pin; runtime account gate still holds |
| #125 | ovh model_added Qwen3-Embedding-8B | BENIGN: already allowlisted in the free-embedding grant and bootstrap embedder; embedding-only, no chat-grant match |
| #126 | ovh model_removed Qwen3-32B | MATERIAL: stale bootstrap pin in providers.toml (CLI/capability still list it); fixed in this batch by removing the pin |
| #130 | openrouter model_removed deepseek/deepseek-v4-flash-0731:free | BENIGN: upstream absence; paid sibling excluded by suffix+price gates; unpinned |
| #132 | nvidia model_removed deepseek-ai/deepseek-v4-flash-0731 | BENIGN: upstream absence; unpinned |
| #133 | kilo model_removed deepseek/deepseek-v4-flash-0731:free | BENIGN: upstream absence; paid sibling excluded by suffix+price gates; unpinned |
| #134 | nvidia model_added deepseek-ai/deepseek-v4.1-flash | BENIGN: all-grant auto-admit; runtime account gate still holds; unpinned |
| #135 | aion model_added aion-labs/aion-3.5-mini | BENIGN: recurring_quota auto-admit despite nonzero list price; account gate holds; unpinned |
| #136 | aion model_added aion-labs/aion-3.5 | BENIGN: same as #135 |

Recorded live catalog evidence below was fetched 2026-09-25T05:12Z (credentialless
GET of each provider's reviewed discovery URL). Row dicts carry the exact fields
the grant gates consume; live bodies carry descriptions/dates/benchmarks
alongside, which admission does not consume.

This module pins the verdicts following tests/test_maint_close_reviewed_16.py:
every recorded fingerprint recomputes under the repo's own fingerprint
function, the batch verifier reports `unchanged` (closable as reviewed) for
all 9 against a current report, and per-issue admission proofs show discovery
already handles each rotation with no grant change and no eligibility expansion.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from freellmpool import discovery as d
from freellmpool import free_policy as fp
from freellmpool import maintenance as m
from freellmpool.config import load_catalog, load_embedders
from freellmpool.provider_registry import load_registry
from scripts import verify_maint_close_batch as batch

BATCH_PATH = Path("maintenance/close-batch-catalog-rotation.json")

# Managed-section fingerprints, read from the live GitHub issues (all OPEN).
RECORDED: list[dict[str, Any]] = [
    {"issue": 121, "provider": "modelscope", "code": "model_added",
     "subject": "Qwen-Ambassador/Qwen3.8-Omni-Flash", "before": False, "after": True,
     "reviewed_fp": "a835a2b72be459e5aa4c62372929f7586885eba07c880ef7628f87642a36e6e0"},
    {"issue": 125, "provider": "ovh", "code": "model_added",
     "subject": "Qwen3-Embedding-8B", "before": False, "after": True,
     "reviewed_fp": "f3f34b8b77128fff4358cc07f3b43f271bafce5c5c24b75fcd7d672cf94a83bf"},
    {"issue": 126, "provider": "ovh", "code": "model_removed",
     "subject": "Qwen3-32B", "before": True, "after": False,
     "reviewed_fp": "27ac8a4a56f14de5ef7fac6ce580c119070e41e857a84c6485e57f2019803721"},
    {"issue": 130, "provider": "openrouter", "code": "model_removed",
     "subject": "deepseek/deepseek-v4-flash-0731:free", "before": True, "after": False,
     "reviewed_fp": "14430156a3144f12bdd55a5fce614fc95f4ffe1ab1d115d7eb456b0de9af5216"},
    {"issue": 132, "provider": "nvidia", "code": "model_removed",
     "subject": "deepseek-ai/deepseek-v4-flash-0731", "before": True, "after": False,
     "reviewed_fp": "f8431979d3a0417b0bc5a7de4fcab95ab295f8e578bef0e840b1a8e339c0230a"},
    {"issue": 133, "provider": "kilo", "code": "model_removed",
     "subject": "deepseek/deepseek-v4-flash-0731:free", "before": True, "after": False,
     "reviewed_fp": "af3e45e3d478e2a77cc8339b82a0f9af850009194bccb84947c6e8797f87acd9"},
    {"issue": 134, "provider": "nvidia", "code": "model_added",
     "subject": "deepseek-ai/deepseek-v4.1-flash", "before": False, "after": True,
     "reviewed_fp": "937046f5e1be2023d8dd856f0a8c2fe616623762ccf92d6df599eb2b8d6293d2"},
    {"issue": 135, "provider": "aion", "code": "model_added",
     "subject": "aion-labs/aion-3.5-mini", "before": False, "after": True,
     "reviewed_fp": "d052bd155f830a9204ba359eb07dae22b9fdde72dd9e652da5fb17595a9cf1c3"},
    {"issue": 136, "provider": "aion", "code": "model_added",
     "subject": "aion-labs/aion-3.5", "before": False, "after": True,
     "reviewed_fp": "bab2a7f9bb4624ff252385a4be3b1bb8b2ceb7b38a403aa0ef92992c96a881a7"},
]

CHECKED_AT = "2026-09-25T05:12:00+00:00"

# All 24 ids in the live OVH catalog body (oai.endpoints.kepler.ai.cloud.ovh.net).
OVH_LIVE_IDS = [
    "Meta-Llama-3_3-70B-Instruct", "Qwen3.5-397B-A17B", "Qwen3-Coder-30B-A3B-Instruct",
    "Qwen3.6-27B", "gpt-oss-20b", "Mistral-Small-3.2-24B-Instruct-2506",
    "Mistral-7B-Instruct-v0.3", "gpt-oss-120b", "Qwen3.5-9B", "Qwen3Guard-Gen-0.6B",
    "Qwen2.5-VL-72B-Instruct", "Qwen3Guard-Gen-8B", "Mistral-Nemo-Instruct-2407",
    "Qwen3.8-27B", "whisper-large-v3-turbo", "whisper-large-v3", "Qwen3-Embedding-8B",
    "bge-multilingual-gemma2", "bge-m3", "stable-diffusion-xl-base-v10", "nvr-tts-it-it",
    "nvr-tts-de-de", "nvr-tts-en-us", "nvr-tts-es-es",
]

# Exact OVH rows for the embedding admission proof plus chat/embedding controls.
OVH_ROWS: list[dict[str, Any]] = [
    {"id": "Qwen3-Embedding-8B", "created": 1774533326, "object": "model",
     "owned_by": "Qwen",
     "pricing": {"currency_unit": "USD", "completion": "0", "image": "0",
                 "prompt": "0.00000012", "request": "0", "input_cache_reads": "0",
                 "input_cache_writes": "0"},
     "context_length": 32768, "max_completion_tokens": 0},
    {"id": "bge-m3", "created": 1731944050, "object": "model", "owned_by": "BAAI",
     "pricing": {"currency_unit": "USD", "completion": "0", "image": "0",
                 "prompt": "0.00000001", "request": "0", "input_cache_reads": "0",
                 "input_cache_writes": "0"},
     "context_length": 8192, "max_completion_tokens": 0},
    {"id": "Qwen3.6-27B", "created": 1780302734, "object": "model", "owned_by": "Qwen",
     "pricing": {"currency_unit": "USD", "completion": "0.00000319", "image": "0",
                 "prompt": "0.00000047", "request": "0", "input_cache_reads": "0",
                 "input_cache_writes": "0"},
     "context_length": 262144, "max_completion_tokens": 262144},
]

# The 11 OVH chat models the packaged bootstrap must list after the #126 fix:
# every one is present in the live body above; removed Qwen3-32B is gone.
OVH_BOOTSTRAP_CHAT = [
    "Meta-Llama-3_3-70B-Instruct", "Qwen3.5-397B-A17B", "gpt-oss-120b",
    "Mistral-Small-3.2-24B-Instruct-2506", "Mistral-Nemo-Instruct-2407",
    "Qwen3.6-27B", "Qwen3.5-9B", "Qwen2.5-VL-72B-Instruct",
    "Mistral-7B-Instruct-v0.3", "gpt-oss-20b", "Qwen3-Coder-30B-A3B-Instruct",
]

MS_ROW_OMNI: dict[str, Any] = {
    "id": "Qwen-Ambassador/Qwen3.8-Omni-Flash", "object": "", "owned_by": "system",
    "created": 1789744694,
}

AION_ROWS: list[dict[str, Any]] = [
    {"id": "aion-labs/aion-3.5", "context_length": 262144, "max_completion_tokens": 32768,
     "architecture": {"modality": "text->text"},
     "pricing": {"prompt": "0.0000030000", "completion": "0.0000060000",
                 "input_cache_read": "0.0000007500"}},
    {"id": "aion-labs/aion-3.5-mini", "context_length": 262144,
     "max_completion_tokens": 32768, "architecture": {"modality": "text->text"},
     "pricing": {"prompt": "0.0000007000", "completion": "0.0000014000",
                 "input_cache_read": "0.0000001800"}},
]

NVIDIA_ROWS: list[dict[str, Any]] = [
    {"id": "deepseek-ai/deepseek-v4.1-flash", "object": "model",
     "owned_by": "deepseek-ai", "created": 735790403},
    {"id": "deepseek-ai/deepseek-coder-6.7b-instruct", "object": "model",
     "owned_by": "deepseek-ai", "created": 735790403},
]

# Exact paid deepseek rows (live kilo/openrouter): nonzero price, no :free suffix.
KILO_PAID_DEEPSEEK: dict[str, Any] = {
    "id": "deepseek/deepseek-v4-flash-0731",
    "architecture": {"input_modalities": ["text"], "modality": "text->text",
                     "output_modalities": ["text"], "tokenizer": "DeepSeek"},
    "context_length": 1310720,
    "pricing": {"completion": "0.000001320000", "input_cache_read": "0.000000028000",
                "prompt": "0.000000440000"},
}
KILO_FREE_CONTROL: dict[str, Any] = {
    "id": "qwen/qwen3.8-27b:free",
    "architecture": {"input_modalities": ["text", "image", "video"],
                     "modality": "text+image+video->text",
                     "output_modalities": ["text"], "tokenizer": "Qwen"},
    "context_length": 262144, "pricing": {"completion": "0", "prompt": "0"},
}
OR_PAID_DEEPSEEK: dict[str, Any] = {
    "id": "deepseek/deepseek-v4-flash-0731",
    "architecture": {"input_modalities": ["text"], "modality": "text->text",
                     "output_modalities": ["text"], "tokenizer": "DeepSeek"},
    "context_length": 1310720,
    "pricing": {"completion": "0.00000032", "input_cache_read": "0.000000016",
                "prompt": "0.00000003"},
}
OR_FREE_CONTROL: dict[str, Any] = {
    "id": "qwen/qwen3.8-27b:free",
    "architecture": {"input_modalities": ["text", "image", "video"],
                     "modality": "text+image+video->text",
                     "output_modalities": ["text"], "tokenizer": "Qwen"},
    "context_length": 262144, "pricing": {"completion": "0", "prompt": "0"},
}

# Live deepseek and :free id sets (kilo: 396 rows; openrouter: 460 rows).
KILO_DEEPSEEK_IDS = [
    "deepseek/deepseek-v4.1-flash", "~deepseek/deepseek-pro-latest",
    "~deepseek/deepseek-flash-latest", "deepseek/deepseek-v4-flash-vision-exp",
    "deepseek/deepseek-v4-pro-0813", "~deepseek/deepseek-v4-flash-latest",
    "deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash", "deepseek/deepseek-v3.2",
    "deepseek/deepseek-v3.2-exp", "deepseek/deepseek-v3.1-terminus",
    "deepseek/deepseek-chat-v3.1", "deepseek/deepseek-r1-0528",
    "deepseek/deepseek-chat-v3-0324", "deepseek/deepseek-r1-distill-llama-70b",
    "deepseek/deepseek-r1", "deepseek/deepseek-chat",
]
KILO_FREE_IDS = [
    "poolside/laguna-s-2.1:free", "nvidia/nemotron-3-ultra-550b-a55b:free",
    "dots-studio/dots-3-note-preview:free", "nex-agi/nex-n2.5-pro:free",
    "nex-agi/nex-n2.5-mini:free", "inclusionai/ling-3.0-flash-sante:free",
    "inclusionai/ling-3.0-flash-fin:free", "qwen/qwen3.8-27b:free",
    "liquid/lfm-2.5-2.6b:free", "nvidia/nemotron-3.5-lightning:free",
    "thinkingmachines/inkling-small:free", "poolside/laguna-xs-2.1:free",
    "cohere/north-mini-code:free", "z-ai/glm-5.2:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free", "stepfun/step-3.7-flash:free",
]
OR_DEEPSEEK_IDS = [
    "~deepseek/deepseek-pro-latest", "~deepseek/deepseek-flash-latest",
    "deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash:batch",
    "deepseek/deepseek-v4-flash-vision-exp", "deepseek/deepseek-v4-pro-0813",
    "~deepseek/deepseek-v4-flash-latest", "deepseek/deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v3.2", "deepseek/deepseek-v3.2-exp",
    "deepseek/deepseek-v3.1-terminus", "deepseek/deepseek-chat-v3.1",
    "deepseek/deepseek-r1-0528", "deepseek/deepseek-chat-v3-0324",
    "deepseek/deepseek-r1-distill-llama-70b", "deepseek/deepseek-r1",
    "deepseek/deepseek-chat",
]
OR_FREE_IDS = [
    "nex-agi/nex-n2.5-mini:free", "nex-agi/nex-n2.5-pro:free",
    "inclusionai/ling-3.0-flash-sante:free", "inclusionai/ling-3.0-flash-fin:free",
    "qwen/qwen3.8-27b:free", "dots-studio/dots-3-note-preview:free",
    "liquid/lfm-2.5-2.6b:free", "nvidia/nemotron-3.5-lightning:free",
    "thinkingmachines/inkling-small:free", "poolside/laguna-s-2.1:free",
    "thinkingmachines/inkling:free", "poolside/laguna-xs-2.1:free",
    "cohere/north-mini-code:free", "z-ai/glm-5.2:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-26b-a4b-it:free", "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
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


def _registry(pid: str) -> dict[str, Any]:
    return dict(load_registry(None)[pid])


def _in_window_now(pid: str) -> float:
    checked = _registry(pid)["evidence"][0]["checked_at"]
    return datetime.fromisoformat(str(checked)).timestamp() + 3600


def test_recorded_fingerprints_recompute_under_repo_function() -> None:
    assert len(RECORDED) == 9
    for row in RECORDED:
        rebuilt = m._finding(
            str(row["provider"]), str(row["code"]), str(row["subject"]),
            before=row["before"], after=row["after"])
        assert rebuilt["fingerprint"] == row["reviewed_fp"], f"issue #{row['issue']}"


def test_batch_spec_lists_exactly_the_nine_targets() -> None:
    spec = batch.load_batch(BATCH_PATH)
    assert [entry["issue"] for entry in spec["targets"]] == [
        121, 125, 126, 130, 132, 133, 134, 135, 136]
    wanted = {(row["provider"], row["code"], row["subject"]): row["reviewed_fp"]
              for row in RECORDED}
    for entry in spec["targets"]:
        key = (entry["provider"], entry["code"], entry["subject"])
        assert entry["reviewed_fingerprint"] == wanted[key]


def test_batch_verdicts_are_all_unchanged_against_current_report() -> None:
    spec = batch.load_batch(BATCH_PATH)
    verdicts = batch.verdicts(spec, _live_report())
    assert sorted(verdicts) == [121, 125, 126, 130, 132, 133, 134, 135, 136]
    assert set(verdicts.values()) == {"unchanged"}


def test_changed_evidence_refires_instead_of_closing() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [_finding_row(row) for row in RECORDED]
    rows[0] = _finding_row(RECORDED[0], fingerprint="f" * 64)
    report = _live_report([])
    report["findings"] = rows
    verdicts = batch.verdicts(spec, report)
    assert verdicts[121] == "refired"
    assert verdicts[125] == "unchanged"


def test_absent_target_without_proof_needs_review() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [row for row in RECORDED if row["issue"] != 126]
    assert batch.verdicts(spec, _live_report(rows))[126] == "needs-review"


def test_matching_resolution_with_fresh_catalog_is_resolved() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [row for row in RECORDED if row["issue"] != 126]
    report = _live_report(rows)
    reviewed = next(row for row in RECORDED if row["issue"] == 126)
    report["resolutions"] = [{
        "id": "ovh:model_removed:subject", "provider": "ovh", "kind": "review",
        "code": "model_removed", "subject": "Qwen3-32B",
        "fingerprint": reviewed["reviewed_fp"]}]
    report["providers"] = {"ovh": {"catalog": {
        "status": "ok", "complete": True,
        "checked_at": "2026-09-25T05:00:00+00:00",
        "expires_at": "2026-09-26T05:00:00+00:00"}}}
    assert batch.verdicts(spec, report)[126] == "resolved"


def test_cli_reports_closable_batch_with_zero_exit(tmp_path: Path) -> None:
    report_path = tmp_path / "public-report.json"
    report_path.write_text(json.dumps(_live_report()))
    assert batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path)]) == 0
    quiet = batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path),
                        "--format", "json"])
    assert quiet == 0


def test_121_modelscope_omni_flash_admitted_under_reviewed_all_grant() -> None:
    spec = _registry("modelscope")
    assert spec["grants"][0]["model_selector"] == {"kind": "all", "models": []}
    (model,) = d.normalize_models("modelscope", {"data": [MS_ROW_OMNI]})
    assert model["modalities"] == ["chat"]
    assert model["pricing"] == {}
    assert fp.model_matches_grant(spec["grants"][0], model) is True
    assert d.free_catalog_models(spec, [model]) == [model]
    # Catalog visibility is not runtime eligibility: the conditional grant still
    # demands verified bound_free account evidence before inference.
    denied = fp.admit(spec, model, {}, modality="chat", now=_in_window_now("modelscope"))
    assert denied.allowed is False
    assert "needs verification" in denied.reason


def test_125_ovh_embedding_matches_only_the_free_embedding_grant() -> None:
    spec = _registry("ovh")
    assert spec["grants"][1]["model_selector"]["models"] == ["Qwen3-Embedding-8B"]
    models = d.normalize_models("ovh", {"data": OVH_ROWS})
    by_id = {model["id"]: model for model in models}
    assert by_id["Qwen3-Embedding-8B"]["modalities"] == ["embedding"]
    assert fp.model_matches_grant(spec["grants"][0], by_id["Qwen3-Embedding-8B"]) is False
    assert fp.model_matches_grant(spec["grants"][1], by_id["Qwen3-Embedding-8B"]) is True
    # Controls: unlisted bge stays out of every grant; chat neighbor stays in chat.
    assert all(fp.model_matches_grant(grant, by_id["bge-m3"]) is False
               for grant in spec["grants"])
    assert fp.model_matches_grant(spec["grants"][0], by_id["Qwen3.6-27B"]) is True
    admitted = {model["id"] for model in d.free_catalog_models(spec, models)}
    assert admitted == {"Qwen3-Embedding-8B", "Qwen3.6-27B"}
    # The bootstrap embedder already pins the reviewed route: no change needed.
    assert [model.name for model in
            next(e for e in load_embedders() if e.id == "ovh").models] == [
        "Qwen3-Embedding-8B"]


def test_126_ovh_bootstrap_drops_removed_qwen3_32b() -> None:
    assert "Qwen3-32B" not in OVH_LIVE_IDS
    assert len(OVH_LIVE_IDS) == 24
    names = [model.name for model in
             next(p for p in load_catalog() if p.id == "ovh").models]
    assert "Qwen3-32B" not in names
    assert sorted(names) == sorted(OVH_BOOTSTRAP_CHAT)


def test_130_openrouter_removed_free_route_is_absent_and_unpinned() -> None:
    assert "deepseek/deepseek-v4-flash-0731:free" not in OR_FREE_IDS
    assert "deepseek/deepseek-v4-flash-0731:free" not in OR_DEEPSEEK_IDS
    assert "qwen/qwen3.8-27b:free" in OR_FREE_IDS  # control: list is live, not empty
    spec = _registry("openrouter")
    models = d.normalize_models("openrouter", {"data": [OR_PAID_DEEPSEEK, OR_FREE_CONTROL]})
    by_id = {model["id"]: model for model in models}
    # The surviving paid sibling is excluded twice over: no :free suffix match
    # and nonzero price under the zero_price grant kind.
    assert fp.model_matches_grant(spec["grants"][0], by_id[OR_PAID_DEEPSEEK["id"]]) is False
    assert fp.model_matches_grant(spec["grants"][0], by_id[OR_FREE_CONTROL["id"]]) is True
    assert [model["id"] for model in d.free_catalog_models(spec, models)] == [
        OR_FREE_CONTROL["id"]]
    names = [model.name for model in
             next(p for p in load_catalog() if p.id == "openrouter").models]
    assert not [name for name in names if "deepseek" in name.lower()]


def test_133_kilo_removed_free_route_is_absent_and_unpinned() -> None:
    assert "deepseek/deepseek-v4-flash-0731:free" not in KILO_FREE_IDS
    assert "deepseek/deepseek-v4-flash-0731:free" not in KILO_DEEPSEEK_IDS
    assert "qwen/qwen3.8-27b:free" in KILO_FREE_IDS  # control: list is live, not empty
    spec = _registry("kilo")
    models = d.normalize_models("kilo", {"data": [KILO_PAID_DEEPSEEK, KILO_FREE_CONTROL]})
    by_id = {model["id"]: model for model in models}
    assert fp.model_matches_grant(spec["grants"][0], by_id[KILO_PAID_DEEPSEEK["id"]]) is False
    assert fp.model_matches_grant(spec["grants"][0], by_id[KILO_FREE_CONTROL["id"]]) is True
    assert [model["id"] for model in d.free_catalog_models(spec, models)] == [
        KILO_FREE_CONTROL["id"]]
    names = [model.name for model in
             next(p for p in load_catalog() if p.id == "kilo").models]
    assert not [name for name in names if "deepseek" in name.lower()]


def test_132_134_nvidia_flash_rotation_is_discovery_handled() -> None:
    spec = _registry("nvidia")
    assert spec["grants"][0]["model_selector"] == {"kind": "all", "models": []}
    models = d.normalize_models("nvidia", {"data": NVIDIA_ROWS})
    by_id = {model["id"]: model for model in models}
    assert "deepseek-ai/deepseek-v4-flash-0731" not in by_id
    added = by_id["deepseek-ai/deepseek-v4.1-flash"]
    assert added["modalities"] == ["chat"]
    assert fp.model_matches_grant(spec["grants"][0], added) is True
    assert {model["id"] for model in d.free_catalog_models(spec, models)} == set(by_id)
    denied = fp.admit(spec, added, {}, modality="chat", now=_in_window_now("nvidia"))
    assert denied.allowed is False
    assert "needs verification" in denied.reason
    names = [model.name for model in
             next(p for p in load_catalog() if p.id == "nvidia").models]
    assert not [name for name in names if "deepseek" in name.lower()]


def test_135_136_aion_35_models_admitted_with_account_gate_intact() -> None:
    spec = _registry("aion")
    assert spec["grants"][0]["model_selector"] == {"kind": "all", "models": []}
    assert spec["grants"][0]["requires_account_evidence"] is True
    models = d.normalize_models("aion", {"models": AION_ROWS})
    assert {model["id"] for model in models} == {
        "aion-labs/aion-3.5", "aion-labs/aion-3.5-mini"}
    for model in models:
        assert model["modalities"] == ["chat"]
        # Nonzero list prices do not block a recurring_quota grant: the free
        # allowance is account-scoped, not price-scoped.
        assert fp.model_matches_grant(spec["grants"][0], model) is True
    assert len(d.free_catalog_models(spec, models)) == 2
    now = _in_window_now("aion")
    for model in models:
        denied = fp.admit(spec, model, {}, modality="chat", now=now)
        assert denied.allowed is False
        assert "needs verification" in denied.reason


def test_cloudflare_deepseek_exclusions_survive_the_rotation() -> None:
    spec = _registry("cloudflare")
    pinned = "@cf/deepseek-ai/deepseek-v4-flash-0731"
    assert pinned in (spec.get("blocked_models") or [])
    assert pinned in (spec.get("paid_required_models") or [])
    assert pinned in (spec["grants"][0]["model_selector"].get("exclude") or [])
    listed = d.normalize_models("cloudflare", {"result": [
        {"name": pinned, "task": {"name": "text generation"}},
        {"name": "@cf/meta/llama-3.2-1b-instruct",
         "task": {"name": "text generation"}}]})
    assert [model["id"] for model in d.free_catalog_models(spec, listed)] == [
        "@cf/meta/llama-3.2-1b-instruct"]
