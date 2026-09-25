"""Batch maint-close-reviewed-16: fingerprint-gated closure verdicts.

The owner reviewed issues #16-#115 in MAINTENANCE_REVIEW.md (policy rev6/rev7)
and closed 16 of them on Sept 18 as reviewed. Every one re-fired with changed
evidence (Sept 18-24 scheduled runs); the automation reopened each and rewrote
its body with a new fingerprint. Closing any of them now would suppress
changed evidence, so all 16 stay open.

This module pins that verdict: the recorded Sept-24 live fingerprints must
recompute under the repo's own fingerprint function, must differ from the
Sept-17 reviewed fingerprints, and the batch verifier must report `refired`
for all 16 against the Sept-24 public report.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from freellmpool import maintenance as m
from scripts import sync_maintenance_issues as issues
from scripts import verify_maint_close_batch as batch

BATCH_PATH = Path("maintenance/close-batch-reviewed-16.json")

# Recorded from freellmpool-public-report artifacts:
# reviewed_fp: run 35208104022 (2026-09-17, last scheduled run before the
#   Sept-18 review closures); live_*: run 35984815610 (2026-09-24T10:02:10Z).
# Live rows also match the current automation-written issue bodies exactly.
RECORDED: list[dict[str, str]] = [
    {"issue": "16", "provider": "opencode", "code": "source_changed", "subject": "terms",
     "reviewed_fp": "a243313168f9abf5da197f6715f6f7ab831c6fe0d0fe4c971ae4a6a287eba62e",
     "live_before": "bcdc42e1c73ade194b1d6ff17445d008407d5fe5a47bac2684ff6f0d76202c70",
     "live_after": "5db2c454a61494e2618d3bc7130fd6d312b05eaba276ea303a4c3d73fc51bd57",
     "live_fp": "80c4cdf6d0b5ad0a35c311af62c13de0204fe9c67fc64358ae8a579c95c7613b"},
    {"issue": "17", "provider": "vercel", "code": "source_changed", "subject": "terms",
     "reviewed_fp": "0171286796089ef55ddbfae49b5660042898e8eba639a4af67be42664f4e153e",
     "live_before": "19c8bba745493eb873178e2daef2be5706854fb190899f9eff41f2050cc211a2",
     "live_after": "d6a21ca32a397944163e00f7ed1c51529c0a441fad6c13a11d47b5056962bf50",
     "live_fp": "4fc1b59cd096eaa2ac73ef944cbf15cbe027343d7678d639c8f6da6bab79fac9"},
    {"issue": "21", "provider": "opencode", "code": "source_changed", "subject": "limiter",
     "reviewed_fp": "307d4a1a509e2d2175fdb22b100ad8107b3adbc15eb773b3c108432dc3bd21ec",
     "live_before": "581f2f6ba915d14fe4b1923548acf99b087eea36d3e3529cecdca460cd2cb1a7",
     "live_after": "4eb4ea9875bfb4a1e704724bc20f4abe677a9260fab3ca0e1df1d69fc506929e",
     "live_fp": "44d672ebd8213c261af5ac662c4c74ce1b3409e44d8fbf022147f299fb17ea4d"},
    {"issue": "22", "provider": "cloudflare", "code": "source_changed", "subject": "catalog",
     "reviewed_fp": "a77db87e1482ed88f32c37cbdc68f464eddea8de1264b979954a41cbbb1b1776",
     "live_before": "5079d33455ecad10d4a2968a7333a3430b3f5efffa2c2c2992a90835f254e0e4",
     "live_after": "3637e400da767e8a63c2709535c77f3fe42bb68edfa361c8efc9825961b4bc0f",
     "live_fp": "cfb5732ee98b71ea0bbe17ed309cb770562a90e069c01c7d741f5f3a1565ffc1"},
    {"issue": "23", "provider": "gemini", "code": "source_changed", "subject": "terms",
     "reviewed_fp": "4672ede6019857bc41232bb0c0364e8667b1f9c06258b4ce777ea72b04d2bc2c",
     "live_before": "561303312ab491ff4d78fe3b0fd04b401d6491be4c89ee735bc19e2a3e29b47a",
     "live_after": "64b59a3650b87328f9a676abecb7c4aa8fd20b013150646be356361a83bc6e72",
     "live_fp": "8c8223e966dec66d36befaf735dfb7b5ee353370f92c41d2e355618a02e9d1ec"},
    {"issue": "27", "provider": "ollama", "code": "source_changed", "subject": "terms",
     "reviewed_fp": "1e761922e03c6aaa7cd58ff58b699eb3c6cafe8558229da64b302b4ab18c555e",
     "live_before": "743d3cb5ba12eae33e02d019a7e532a3f4ae94133363841e2541078388ecc438",
     "live_after": "4383d28f4df3886215f81f483a3a8844be93aca43a8540b6dae514839fdeae9c",
     "live_fp": "d761e7b416a67863f5a1dbd787bd854eb21cec4eecac84041c595e1368967fc0"},
    {"issue": "30", "provider": "openrouter", "code": "source_changed", "subject": "pricing",
     "reviewed_fp": "275c442ee9c992a2205070d5baec4f40ccfdb1db1ec703c3c947e099a06a1f01",
     "live_before": "f8a9b9a377cc97a3aa6a33bba12723674f0d29bb30a94f6dd0cf31180b533869",
     "live_after": "61065c754e180b0e277388057e1b78aaa60e5bb257d629656536d663fc5474b2",
     "live_fp": "e9d6eee66a222459c944f0dca56f614e1a6604c63353dab3782a111567fdc7f8"},
    {"issue": "32", "provider": "cohere", "code": "source_changed", "subject": "terms",
     "reviewed_fp": "06d187df00b03ab5301d63414a4a9f9a235896348bc2ace4ec1d284483fb38db",
     "live_before": "fbe34f81914cd56a0163feeb1d2bc26070a8dbc5dde2e38227fa954e169e484a",
     "live_after": "935b86e808d63273928d33d3fe74f0e92d2f4a9c163119679ba8a90be290d0b9",
     "live_fp": "f2668348d9147d0828747f546e2d9fb45b4feb0b15042dbb3bfe55e54aac45b6"},
    {"issue": "37", "provider": "cohere", "code": "source_changed", "subject": "catalog",
     "reviewed_fp": "14e973f32de1b237cf9085245ba7d7724b1134da9893df4853d703354b87a144",
     "live_before": "787be5786f9c7771ce3fb568985655d140250bd1d4bcd12e307dc90d4a21f542",
     "live_after": "717011e35c61b661d60b74dcaf600a74baf24dbb6bba661795760c04fc0d665e",
     "live_fp": "b5cb075985b54507d342f33561875d9455ea858e04c34d8b463b5b5765722c0c"},
    {"issue": "44", "provider": "ovh", "code": "source_changed", "subject": "pricing",
     "reviewed_fp": "352f7ab5b1ce7868a5715a0432bd1ac4a05b068e4413e980a71ec9c28a335de3",
     "live_before": "b3bac801d888252a14ef9b33c3cd7aebaec5ec9876d9f8b1eeb4ec0b2159da1a",
     "live_after": "820b9c3233d1098c4173242d66ae41974069c20debc99b9f09a94345be03ae37",
     "live_fp": "24744f40e11df473e4becb74fbe05de6fb17849dcb295ba59df08c805b533b89"},
    {"issue": "45", "provider": "ovh", "code": "source_changed", "subject": "terms",
     "reviewed_fp": "fc98c88e2b3c749b3039be877846f5db6ac179bae22ef94d31002312d1e86731",
     "live_before": "17c3c792a2edb32e6d003cc88f68bac5d34d5be623028a4e97325c48354cd5c2",
     "live_after": "79a499f83fb863f9feff7729ba734311aa9b28d71ffbec86dc0182f3e457c548",
     "live_fp": "863ceb6fe8d5eb772d16499b424f2ea8c2ce4af589b6608a24d3f73a8ca87a48"},
    {"issue": "52", "provider": "kilo", "code": "source_changed", "subject": "terms",
     "reviewed_fp": "b2fcea14d0706ccfc7101252b9bbfdcf00449a03419fb249991912e585bee5a2",
     "live_before": "2ffcfcf9738e610e54a3d251273c329c6465b07e1832849a2ed245517c44f355",
     "live_after": "7929a3d278785805a7478b64d2624fbc91803c6b1b3f059c90299621e1e94c86",
     "live_fp": "4b668403a8f7a77a8d49d03e3fafac7078452886ab6b7d99f5c14d0eb2e2bcbf"},
    {"issue": "80", "provider": "groq", "code": "source_changed", "subject": "limits",
     "reviewed_fp": "eedd34ee64d1793e3ae3d2f3850ba3a69f387ba23ea80cd37e92b789de8281f3",
     "live_before": "6ea925ca546471b5f094b430e2344369bbd1eab5dc40062236e713b006de8fb7",
     "live_after": "d3862f3e4f047c34905760b463ace2ce40369872eb0159d1563743b64ab7eed2",
     "live_fp": "b0673dcb415f7c66e356aa3df0dbc5f1b7bcfe09c5e51f6a361ec5a56c05d8fc"},
    {"issue": "106", "provider": "gemini", "code": "source_changed", "subject": "limits",
     "reviewed_fp": "bd96cf501429e2e2af7be950822391a8cd8fcb59370b4c661f366ac3e5ef5765",
     "live_before": "b9bfe4986f3613b17da6952600f359d9a65e3515da208b0cae4211c6fd1d08a2",
     "live_after": "e8c425f16cb47e71d378c1c294019498b0079461137bbc6107ff7e6be7e07511",
     "live_fp": "f3beb6cf89a04f8b95a8b313e1b3a9d2c89d7ca741ea2a250eb382e1b08543a7"},
    {"issue": "107", "provider": "gemini", "code": "source_changed", "subject": "auth",
     "reviewed_fp": "8db14d38596827b1efae78de6f2aac3a8a90639280745b85db49c8ff0667f262",
     "live_before": "354178211285fca77b55e2066742d963b78001c4641dc71be4984c801e46b7d7",
     "live_after": "b4443b1399446eff6f72b23e9e7852b9d4e8734b0e060a70110d4f5a4f3aa243",
     "live_fp": "c90aa4aaaafeb38b5b4c95ff0d2f16b983e4e61ed3b403ba345181013ad643ba"},
    {"issue": "115", "provider": "mistral", "code": "source_changed", "subject": "limits",
     "reviewed_fp": "1c088bf01778afa460db34d1774cd9cd0ea5ea90b8baba1a025e9b10e15d7db0",
     "live_before": "08892b8f522260359ebfc7fca0afbcbe63ec92016d23bcbe97a5d6d3ee89cfde",
     "live_after": "db6c40c802e2f27532fbe7271dab3412b371c466989943e37d61b806d1a0c1fc",
     "live_fp": "d089c432f26c429c08844f7ff68ac7af520991c98c6a3fc998a9f424f2ea7461"},
]


def _live_report(rows: list[dict[str, str]] | None = None) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for row in rows if rows is not None else RECORDED:
        findings.append({
            "id": f"{row['provider']}:source_changed:subject",
            "provider": row["provider"], "kind": "incident",
            "code": row["code"], "subject": row["subject"],
            "fingerprint": row["live_fp"],
            "before": row["live_before"], "after": row["live_after"],
        })
    return {"findings": findings, "pending_changes": [], "resolutions": [],
            "providers": {}, "checked_at": "2026-09-24T10:02:10.139976+00:00"}


def test_recorded_live_fingerprints_recompute_under_repo_function() -> None:
    assert len(RECORDED) == 16
    for row in RECORDED:
        rebuilt = m._finding(row["provider"], row["code"], row["subject"],
                             before=row["live_before"], after=row["live_after"])
        assert rebuilt["fingerprint"] == row["live_fp"], f"issue #{row['issue']}"


def test_all_sixteen_live_fingerprints_differ_from_reviewed() -> None:
    for row in RECORDED:
        assert row["live_fp"] != row["reviewed_fp"], f"issue #{row['issue']}"


def test_batch_spec_lists_exactly_the_sixteen_reviewed_targets() -> None:
    spec = batch.load_batch(BATCH_PATH)
    assert [entry["issue"] for entry in spec["targets"]] == [
        16, 17, 21, 22, 23, 27, 30, 32, 37, 44, 45, 52, 80, 106, 107, 115]
    wanted = {row["issue"]: row["reviewed_fp"] for row in RECORDED}
    for entry in spec["targets"]:
        assert entry["reviewed_fingerprint"] == wanted[str(entry["issue"])]


def test_batch_verdicts_are_all_refired_against_sept24_report() -> None:
    spec = batch.load_batch(BATCH_PATH)
    verdicts = batch.verdicts(spec, _live_report())
    assert sorted(verdicts) == [16, 17, 21, 22, 23, 27, 30, 32, 37, 44, 45, 52, 80, 106, 107, 115]
    assert set(verdicts.values()) == {"refired"}


def test_unchanged_finding_is_closable_as_reviewed() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = copy.deepcopy(RECORDED)
    rows[0] = {**rows[0], "live_fp": rows[0]["reviewed_fp"]}
    verdicts = batch.verdicts(spec, _live_report(rows))
    assert verdicts[16] == "unchanged"
    assert verdicts[17] == "refired"


def test_absent_target_without_proof_needs_review() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [row for row in RECORDED if row["issue"] != "22"]
    verdicts = batch.verdicts(spec, _live_report(rows))
    assert verdicts[22] == "needs-review"


def test_matching_resolution_with_fresh_evidence_is_resolved() -> None:
    spec = batch.load_batch(BATCH_PATH)
    row = next(entry for entry in RECORDED if entry["issue"] == "22")
    rows = [entry for entry in RECORDED if entry["issue"] != "22"]
    report = _live_report(rows)
    report["resolutions"] = [{
        "id": "cloudflare:source_changed:subject", "provider": "cloudflare",
        "kind": "incident", "code": "source_changed", "subject": "catalog",
        "fingerprint": row["reviewed_fp"]}]
    report["providers"] = {"cloudflare": {"sources": [{
        "id": "catalog", "status": "unchanged",
        "checked_at": "2026-09-24T09:00:00+00:00",
        "expires_at": "2026-10-01T09:00:00+00:00"}]}}
    verdicts = batch.verdicts(spec, report)
    assert verdicts[22] == "resolved"


def test_mismatched_resolution_fingerprint_needs_review() -> None:
    spec = batch.load_batch(BATCH_PATH)
    rows = [entry for entry in RECORDED if entry["issue"] != "22"]
    report = _live_report(rows)
    report["resolutions"] = [{
        "id": "cloudflare:source_changed:subject", "provider": "cloudflare",
        "kind": "incident", "code": "source_changed", "subject": "catalog",
        "fingerprint": "f" * 64}]
    report["providers"] = {"cloudflare": {"sources": [{
        "id": "catalog", "status": "unchanged",
        "checked_at": "2026-09-24T09:00:00+00:00",
        "expires_at": "2026-10-01T09:00:00+00:00"}]}}
    verdicts = batch.verdicts(spec, report)
    assert verdicts[22] == "needs-review"


def test_cli_reports_refired_batch_with_distinct_exit(tmp_path: Path) -> None:
    report_path = tmp_path / "public-report.json"
    report_path.write_text(json.dumps(_live_report()))
    assert batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path)]) == 3
    quiet = batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path),
                        "--format", "json"])
    assert quiet == 3


def test_reconcile_reopens_manually_closed_issue_on_live_fingerprint() -> None:
    class MemoryAPI:
        repository = "pauljones0/freellmpool_sandbox"

        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []
            self.mutations = 0

        def json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
            if method == "GET" and "?" in path:
                return copy.deepcopy(self.rows)
            if method == "POST":
                result: dict[str, Any] = {"number": 1, "user": {"login": "github-actions[bot]"},
                                          "state": "open", **(payload or {})}
                self.rows.append(result)
                self.mutations += 1
                return copy.deepcopy(result)
            target = self.rows[0]
            if method == "PATCH":
                target.update(payload or {})
                self.mutations += 1
            return copy.deepcopy(target)

    row = next(entry for entry in RECORDED if entry["issue"] == "22")
    reviewed = {"id": "cloudflare:source_changed:subject", "provider": "cloudflare",
                "kind": "incident", "code": "source_changed", "subject": "catalog",
                "fingerprint": row["reviewed_fp"]}
    live = {"id": "cloudflare:source_changed:subject", "provider": "cloudflare",
            "kind": "incident", "code": "source_changed", "subject": "catalog",
            "fingerprint": row["live_fp"], "before": row["live_before"],
            "after": row["live_after"]}
    api = MemoryAPI()
    issues.reconcile_validated_report({"findings": [reviewed], "resolutions": [],
                                       "providers": {}, "source_revision": "a" * 40}, api)
    assert api.mutations == 1
    api.rows[0]["state"] = "closed"  # Sept-18 owner closure keeps the reviewed marker.
    api.mutations = 0
    stats = issues.reconcile_validated_report(
        {"findings": [live], "resolutions": [], "providers": {},
         "source_revision": "b" * 40}, api)
    assert stats["updated"] == 1
    assert api.rows[0]["state"] == "open"
    assert row["live_fp"] in api.rows[0]["body"]
