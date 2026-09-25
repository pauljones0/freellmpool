"""Batch maint-fresh-baseline: fresh baseline scan gate for all close batches.

A complete fresh run anchors every close verdict in this batch series:

    freellmpool maintenance --public-only --refresh --json
    exit 0, checked_at 2026-09-25T06:05:58.714900+00:00

The full normalized report is captured at
maintenance/fresh-baseline-2026-09-25.json (37 findings, 3 resolutions,
16/16 providers, baseline_status ok, zero check failures). This module pins
that run as the close gate:

* the captured report is structurally complete (fresh baseline, every
  provider rechecked, no failed or errored checks);
* every captured finding fingerprint recomputes under the repo's own
  fingerprint function;
* the 16 pre-close checks diff: 12 still fire (7 byte-identical to the
  Sept-24 live evidence, 5 re-churned since Sept-24), 4 are silent because a
  sibling renewal batch refreshed their hash, and all 16 hold (refired or
  needs-review) -- none is closable on this evidence;
* issue #31 still fires with the same fingerprint as the 05:06Z run, so it
  stays open as refired;
* every other batch spec replays against the fresh report with the verdicts
  its owning batch claims, and every closable verdict rests on fresh backing
  evidence -- nothing closes on partial or unknown evidence.

The gate also hardens scripts/verify_maint_close_batch.py: carried findings
persist across runs even when their check was skipped, so an unchanged
fingerprint alone cannot prove a recheck happened. ``require_complete``
refuses closable verdicts unless the backing catalog or source check is
fresh in the same report, and --require-complete turns that refusal into a
distinct CLI exit instead of a close decision.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from freellmpool import maintenance as m
from scripts import verify_maint_close_batch as batch

REPORT_PATH = Path("maintenance/fresh-baseline-2026-09-25.json")
CHECKED_AT = "2026-09-25T06:05:58.714900+00:00"

EXPECTED_PROVIDERS = [
    "aion", "cloudflare", "cohere", "gemini", "groq", "kilo", "llm7",
    "mistral", "modelscope", "nvidia", "ollama", "opencode", "openrouter",
    "ovh", "vercel", "zhipu",
]

# The 12 pre-close checks still firing in the fresh run. befores are the
# stable reviewed hashes; afters are the live hashes observed at 06:05Z.
# Five re-churned after the Sept-24 live evidence (#21, #22, #23, #44, #45);
# seven are byte-identical to Sept-24 (#16, #17, #27, #30, #32, #37, #80).
FIRING: list[dict[str, str]] = [
    {"issue": "16", "provider": "opencode", "subject": "terms",
     "reviewed_fp": "a243313168f9abf5da197f6715f6f7ab831c6fe0d0fe4c971ae4a6a287eba62e",
     "before": "bcdc42e1c73ade194b1d6ff17445d008407d5fe5a47bac2684ff6f0d76202c70",
     "after": "5db2c454a61494e2618d3bc7130fd6d312b05eaba276ea303a4c3d73fc51bd57",
     "fresh_fp": "80c4cdf6d0b5ad0a35c311af62c13de0204fe9c67fc64358ae8a579c95c7613b"},
    {"issue": "17", "provider": "vercel", "subject": "terms",
     "reviewed_fp": "0171286796089ef55ddbfae49b5660042898e8eba639a4af67be42664f4e153e",
     "before": "19c8bba745493eb873178e2daef2be5706854fb190899f9eff41f2050cc211a2",
     "after": "d6a21ca32a397944163e00f7ed1c51529c0a441fad6c13a11d47b5056962bf50",
     "fresh_fp": "4fc1b59cd096eaa2ac73ef944cbf15cbe027343d7678d639c8f6da6bab79fac9"},
    {"issue": "21", "provider": "opencode", "subject": "limiter",
     "reviewed_fp": "307d4a1a509e2d2175fdb22b100ad8107b3adbc15eb773b3c108432dc3bd21ec",
     "before": "581f2f6ba915d14fe4b1923548acf99b087eea36d3e3529cecdca460cd2cb1a7",
     "after": "a698ea3a0c8b4ac87bd713d043419a7f9d4223d486c3391f30039bb8ed26e8ae",
     "fresh_fp": "29654f8f7a4e8495f0c0ade79454ef3a1b22c8ccb1b87933061a2ee72266c439"},
    {"issue": "22", "provider": "cloudflare", "subject": "catalog",
     "reviewed_fp": "a77db87e1482ed88f32c37cbdc68f464eddea8de1264b979954a41cbbb1b1776",
     "before": "5079d33455ecad10d4a2968a7333a3430b3f5efffa2c2c2992a90835f254e0e4",
     "after": "f67fbcd483b73a25748b52251709e48af3d88e090a4ebe0005527d886c0e5db5",
     "fresh_fp": "2f59e29f9a41bd0742959d78dfc08df45050097a8c38ada7e5c5990669e85020"},
    {"issue": "23", "provider": "gemini", "subject": "terms",
     "reviewed_fp": "4672ede6019857bc41232bb0c0364e8667b1f9c06258b4ce777ea72b04d2bc2c",
     "before": "561303312ab491ff4d78fe3b0fd04b401d6491be4c89ee735bc19e2a3e29b47a",
     "after": "b638c2e3c5cc0ed58b6c16d6656e9c5d52c3b1beb3738f45159f008ddecf0557",
     "fresh_fp": "f2506744f982b0eb5020d6d5a2ec02133057cca75936807d610a0dfde2f44fac"},
    {"issue": "27", "provider": "ollama", "subject": "terms",
     "reviewed_fp": "1e761922e03c6aaa7cd58ff58b699eb3c6cafe8558229da64b302b4ab18c555e",
     "before": "743d3cb5ba12eae33e02d019a7e532a3f4ae94133363841e2541078388ecc438",
     "after": "4383d28f4df3886215f81f483a3a8844be93aca43a8540b6dae514839fdeae9c",
     "fresh_fp": "d761e7b416a67863f5a1dbd787bd854eb21cec4eecac84041c595e1368967fc0"},
    {"issue": "30", "provider": "openrouter", "subject": "pricing",
     "reviewed_fp": "275c442ee9c992a2205070d5baec4f40ccfdb1db1ec703c3c947e099a06a1f01",
     "before": "f8a9b9a377cc97a3aa6a33bba12723674f0d29bb30a94f6dd0cf31180b533869",
     "after": "61065c754e180b0e277388057e1b78aaa60e5bb257d629656536d663fc5474b2",
     "fresh_fp": "e9d6eee66a222459c944f0dca56f614e1a6604c63353dab3782a111567fdc7f8"},
    {"issue": "32", "provider": "cohere", "subject": "terms",
     "reviewed_fp": "06d187df00b03ab5301d63414a4a9f9a235896348bc2ace4ec1d284483fb38db",
     "before": "fbe34f81914cd56a0163feeb1d2bc26070a8dbc5dde2e38227fa954e169e484a",
     "after": "935b86e808d63273928d33d3fe74f0e92d2f4a9c163119679ba8a90be290d0b9",
     "fresh_fp": "f2668348d9147d0828747f546e2d9fb45b4feb0b15042dbb3bfe55e54aac45b6"},
    {"issue": "37", "provider": "cohere", "subject": "catalog",
     "reviewed_fp": "14e973f32de1b237cf9085245ba7d7724b1134da9893df4853d703354b87a144",
     "before": "787be5786f9c7771ce3fb568985655d140250bd1d4bcd12e307dc90d4a21f542",
     "after": "717011e35c61b661d60b74dcaf600a74baf24dbb6bba661795760c04fc0d665e",
     "fresh_fp": "b5cb075985b54507d342f33561875d9455ea858e04c34d8b463b5b5765722c0c"},
    {"issue": "44", "provider": "ovh", "subject": "pricing",
     "reviewed_fp": "352f7ab5b1ce7868a5715a0432bd1ac4a05b068e4413e980a71ec9c28a335de3",
     "before": "b3bac801d888252a14ef9b33c3cd7aebaec5ec9876d9f8b1eeb4ec0b2159da1a",
     "after": "115744fd4e8ecdee0122f467c017b572fd920c015f93907084c3ac6d1ef8fb70",
     "fresh_fp": "88d93b2c815573b94141ca03f71e454137cd6e12acac65b1ccdeb84da4626dea"},
    {"issue": "45", "provider": "ovh", "subject": "terms",
     "reviewed_fp": "fc98c88e2b3c749b3039be877846f5db6ac179bae22ef94d31002312d1e86731",
     "before": "17c3c792a2edb32e6d003cc88f68bac5d34d5be623028a4e97325c48354cd5c2",
     "after": "5c3e9be77a973a9fba324203d57939b34c7efbec574380a21683676c0f6d0292",
     "fresh_fp": "8b9085f3a54cb40c632e4defc2d119360c78eb63858a0ce8b5f611e0ad3c7282"},
    {"issue": "80", "provider": "groq", "subject": "limits",
     "reviewed_fp": "eedd34ee64d1793e3ae3d2f3850ba3a69f387ba23ea80cd37e92b789de8281f3",
     "before": "6ea925ca546471b5f094b430e2344369bbd1eab5dc40062236e713b006de8fb7",
     "after": "d3862f3e4f047c34905760b463ace2ce40369872eb0159d1563743b64ab7eed2",
     "fresh_fp": "b0673dcb415f7c66e356aa3df0dbc5f1b7bcfe09c5e51f6a361ec5a56c05d8fc"},
]

# The 4 pre-close checks silent in the fresh run. Each went quiet because a
# sibling batch renewed its reviewed hash to the live value (hash+window
# only, no claim change); the renewed hash equals the recorded live hash.
SILENT: list[dict[str, str]] = [
    {"issue": "52", "provider": "kilo", "subject": "terms",
     "reviewed_fp": "b2fcea14d0706ccfc7101252b9bbfdcf00449a03419fb249991912e585bee5a2",
     "renewed_hash": "7929a3d278785805a7478b64d2624fbc91803c6b1b3f059c90299621e1e94c86",
     "owner": "maint-ling-vl (#143)"},
    {"issue": "106", "provider": "gemini", "subject": "limits",
     "reviewed_fp": "bd96cf501429e2e2af7be950822391a8cd8fcb59370b4c661f366ac3e5ef5765",
     "renewed_hash": "e8c425f16cb47e71d378c1c294019498b0079461137bbc6107ff7e6be7e07511",
     "owner": "maint-source-health (#137)"},
    {"issue": "107", "provider": "gemini", "subject": "auth",
     "reviewed_fp": "8db14d38596827b1efae78de6f2aac3a8a90639280745b85db49c8ff0667f262",
     "renewed_hash": "b4443b1399446eff6f72b23e9e7852b9d4e8734b0e060a70110d4f5a4f3aa243",
     "owner": "maint-source-health (#138)"},
    {"issue": "115", "provider": "mistral", "subject": "limits",
     "reviewed_fp": "1c088bf01778afa460db34d1774cd9cd0ea5ea90b8baba1a025e9b10e15d7db0",
     "renewed_hash": "db6c40c802e2f27532fbe7271dab3412b371c466989943e37d61b806d1a0c1fc",
     "owner": "maint-ling-vl (#144)"},
]

# Issue #31 (vercel catalog) at 06:05Z: byte-identical to the 05:06Z run, so
# the rev7 renewal basis still holds and the post-rev7 churn is unchanged.
ISSUE_31: dict[str, str] = {
    "before": "0cc3b4bcf7382cbb0b25196d890e3602df2a25582f9edea691e9461eee8bf1b1",
    "after": "1645c74670af7a5aa2dca541f327b357d38c2967075cf4c6499551d1f3e80c82",
    "reviewed_fp": "e9543f4147d5489ba27a1c0558e54f4946fa00b8a10fdcbe93837088b49f6dc0",
    "fresh_fp": "166eaff1f0e13f1425880a68baaa8eb501dff7866eac280ec018c2cda01db2d2",
}

# Expected replay verdicts for every batch spec against the fresh report.
REPLAY: dict[str, dict[int, str]] = {
    "maintenance/close-batch-reviewed-16.json": {
        16: "refired", 17: "refired", 21: "refired", 22: "refired",
        23: "refired", 27: "refired", 30: "refired", 32: "refired",
        37: "refired", 44: "refired", 45: "refired", 52: "needs-review",
        80: "refired", 106: "needs-review", 107: "needs-review",
        115: "needs-review"},
    "maintenance/close-batch-31.json": {31: "refired"},
    "maintenance/close-batch-catalog-rotation.json": {
        121: "unchanged", 125: "unchanged", 126: "unchanged",
        130: "unchanged", 132: "unchanged", 133: "unchanged",
        134: "unchanged", 135: "unchanged", 136: "unchanged"},
    "maintenance/close-batch-ling-vl.json": {
        142: "unchanged", 143: "needs-review", 144: "needs-review",
        145: "unchanged", 146: "unchanged"},
    "maintenance/close-batch-source-health.json": {
        128: "resolved", 129: "resolved", 137: "needs-review",
        138: "needs-review", 139: "needs-review", 141: "needs-review"},
}


def _report() -> dict[str, Any]:
    return dict(json.loads(REPORT_PATH.read_text(encoding="utf-8")))


def _finding_index(report: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = list(report.get("findings", [])) + list(report.get("pending_changes", []))
    return {(str(row["provider"]), str(row["code"]), str(row["subject"])): row
            for row in rows if isinstance(row, dict)}


def _source_row(report: dict[str, Any], provider: str, subject: str) -> dict[str, Any]:
    sources = report.get("providers", {}).get(provider, {}).get("sources", [])
    return next(row for row in sources if row.get("id") == subject)


def test_captured_run_matches_the_recorded_fresh_refresh() -> None:
    report = _report()
    assert report["checked_at"] == CHECKED_AT
    assert report["baseline_status"] == "ok"
    assert report["schema"] == 1
    assert report["visibility"] == "public"
    assert len(report["findings"]) == 37
    assert len(report["resolutions"]) == 3
    assert sorted(report["providers"]) == EXPECTED_PROVIDERS


def test_captured_run_is_complete_for_all_sixteen_providers() -> None:
    report = _report()
    assert batch.completeness_defects(report) == []
    batch.require_complete(report, expected_providers=EXPECTED_PROVIDERS)


def test_every_captured_finding_fingerprint_recomputes() -> None:
    report = _report()
    assert len(report["findings"]) == 37
    for row in report["findings"]:
        rebuilt = m._finding(str(row["provider"]), str(row["code"]),
                             str(row["subject"]), before=row.get("before"),
                             after=row.get("after"))
        assert rebuilt["fingerprint"] == row["fingerprint"], row["id"]


def test_firing_preclose_rows_match_capture_and_differ_from_reviewed() -> None:
    report = _report()
    live = _finding_index(report)
    assert len(FIRING) == 12
    for row in FIRING:
        key = (row["provider"], "source_changed", row["subject"])
        captured = live[key]
        assert captured["before"] == row["before"], f"issue #{row['issue']}"
        assert captured["after"] == row["after"], f"issue #{row['issue']}"
        assert captured["fingerprint"] == row["fresh_fp"], f"issue #{row['issue']}"
        rebuilt = m._finding(row["provider"], "source_changed", row["subject"],
                             before=row["before"], after=row["after"])
        assert rebuilt["fingerprint"] == row["fresh_fp"], f"issue #{row['issue']}"
        assert row["fresh_fp"] != row["reviewed_fp"], f"issue #{row['issue']}"


def test_silent_preclose_rows_are_renewed_not_unchecked() -> None:
    from freellmpool.provider_registry import load_registry

    report = _report()
    live = _finding_index(report)
    registry = load_registry(None)
    assert len(SILENT) == 4
    for row in SILENT:
        key = (row["provider"], "source_changed", row["subject"])
        assert key not in live, f"issue #{row['issue']}"
        reviewed = next(entry for entry in registry[row["provider"]]["evidence"]
                        if entry["id"] == row["subject"])
        assert reviewed["source_hash"]["sha256"] == row["renewed_hash"], (
            f"issue #{row['issue']} ({row['owner']})")
        source = _source_row(report, row["provider"], row["subject"])
        assert source["status"] == "unchanged", f"issue #{row['issue']}"
        assert source["checked_at"] is not None and source["expires_at"] is not None


def test_sixteen_plus_31_all_hold_against_fresh_report() -> None:
    report = _report()
    reviewed = batch.load_batch("maintenance/close-batch-reviewed-16.json")
    assert batch.verdicts(reviewed, report) == REPLAY[
        "maintenance/close-batch-reviewed-16.json"]
    single = batch.load_batch("maintenance/close-batch-31.json")
    assert batch.verdicts(single, report) == {31: "refired"}
    assert batch.main(["--batch", "maintenance/close-batch-reviewed-16.json",
                       "--report", str(REPORT_PATH)]) == 3
    assert batch.main(["--batch", "maintenance/close-batch-31.json",
                       "--report", str(REPORT_PATH)]) == 3


def test_issue31_firing_state_is_unchanged_since_0506z() -> None:
    report = _report()
    live = _finding_index(report)
    captured = live[("vercel", "source_changed", "catalog")]
    assert captured["before"] == ISSUE_31["before"]
    assert captured["after"] == ISSUE_31["after"]
    assert captured["fingerprint"] == ISSUE_31["fresh_fp"]
    rebuilt = m._finding("vercel", "source_changed", "catalog",
                         before=ISSUE_31["before"], after=ISSUE_31["after"])
    assert rebuilt["fingerprint"] == ISSUE_31["fresh_fp"]
    assert ISSUE_31["fresh_fp"] != ISSUE_31["reviewed_fp"]


def test_gate_replay_matches_every_owning_batch() -> None:
    report = _report()
    for name, expected in REPLAY.items():
        spec = batch.load_batch(name)
        assert batch.verdicts(spec, report) == expected, name


def test_gate_confirms_closable_verdicts_rest_on_fresh_evidence() -> None:
    report = _report()
    for name in REPLAY:
        spec = batch.load_batch(name)
        batch.require_complete(report, spec, expected_providers=EXPECTED_PROVIDERS)


def test_missing_baseline_is_partial() -> None:
    report = _report()
    report["baseline_status"] = "missing"
    assert batch.completeness_defects(report) != []
    with pytest.raises(ValueError, match="baseline_status"):
        batch.require_complete(report)


def test_failed_checks_are_partial() -> None:
    for code in ("catalog_failed", "source_check_failed", "limit_source_failed"):
        report = _report()
        report["findings"] = [*report["findings"],
                              m._finding("vercel", code, "catalog")]
        assert batch.completeness_defects(report) != [], code
        with pytest.raises(ValueError, match="partial"):
            batch.require_complete(report)


def test_errored_provider_sections_are_partial() -> None:
    report = _report()
    report["providers"]["kilo"]["catalog"]["status"] = "error"
    assert batch.completeness_defects(report) != []
    with pytest.raises(ValueError, match="partial"):
        batch.require_complete(report)
    report = _report()
    report["providers"]["kilo"]["sources"][0]["status"] = "check_failed"
    assert batch.completeness_defects(report) != []
    with pytest.raises(ValueError, match="partial"):
        batch.require_complete(report)


def test_missing_expected_provider_is_partial() -> None:
    report = _report()
    with pytest.raises(ValueError, match="nope"):
        batch.require_complete(report, expected_providers=[*EXPECTED_PROVIDERS, "nope"])
    pruned = _report()
    del pruned["providers"]["vercel"]
    with pytest.raises(ValueError, match="vercel"):
        batch.require_complete(pruned, expected_providers=EXPECTED_PROVIDERS)


def test_stale_carried_catalog_finding_cannot_close() -> None:
    report = _report()
    spec = batch.load_batch("maintenance/close-batch-catalog-rotation.json")
    assert batch.verdicts(spec, report)[121] == "unchanged"
    stale = copy.deepcopy(report)
    stale["providers"]["modelscope"]["catalog"]["status"] = "not_checked"
    with pytest.raises(ValueError, match="modelscope"):
        batch.require_complete(stale, spec)
    stale = copy.deepcopy(report)
    stale["providers"]["modelscope"]["catalog"]["checked_at"] = "2026-09-20T06:05:58+00:00"
    with pytest.raises(ValueError, match="modelscope"):
        batch.require_complete(stale, spec)


def test_unobserved_source_difference_cannot_close() -> None:
    report = _report()
    spec = {"batch": "synthetic", "review": "test",
            "targets": [{"issue": 16, "provider": "opencode", "code": "source_changed",
                         "subject": "terms",
                         "reviewed_fingerprint": "80c4cdf6d0b5ad0a35c311af62c13de0204fe9c67fc64358ae8a579c95c7613b"}]}
    assert batch.verdicts(spec, report) == {16: "unchanged"}
    batch.require_complete(report, spec)
    stale = copy.deepcopy(report)
    sources = stale["providers"]["opencode"]["sources"]
    terms = next(row for row in sources if row["id"] == "terms")
    terms["status"] = "not_checked"
    with pytest.raises(ValueError, match="opencode"):
        batch.require_complete(stale, spec)
    stale = copy.deepcopy(report)
    sources = stale["providers"]["opencode"]["sources"]
    terms = next(row for row in sources if row["id"] == "terms")
    terms["last_attempt_at"] = "2026-09-20T06:05:58+00:00"
    with pytest.raises(ValueError, match="opencode"):
        batch.require_complete(stale, spec)


def test_unknown_target_holds_even_on_a_complete_report() -> None:
    report = _report()
    spec = {"batch": "synthetic", "review": "test",
            "targets": [{"issue": 999, "provider": "vercel", "code": "source_changed",
                         "subject": "pricing",
                         "reviewed_fingerprint": "0" * 64}]}
    assert batch.verdicts(spec, report) == {999: "needs-review"}
    batch.require_complete(report, spec)


def test_cli_refuses_partial_report_with_distinct_exit(tmp_path: Path) -> None:
    partial_path = tmp_path / "partial-report.json"
    report = _report()
    report["baseline_status"] = "missing"
    partial_path.write_text(json.dumps(report))
    args = ["--batch", "maintenance/close-batch-catalog-rotation.json",
            "--report", str(partial_path), "--require-complete"]
    with pytest.raises(SystemExit) as exit_info:
        batch.main(args)
    assert exit_info.value.code == 1
    gated = batch.main(["--batch", "maintenance/close-batch-catalog-rotation.json",
                        "--report", str(REPORT_PATH), "--require-complete"])
    assert gated == 0
