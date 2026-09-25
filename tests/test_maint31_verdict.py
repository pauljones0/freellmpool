"""Batch maint-31-verdict: issue #31 (vercel catalog) stays open.

Policy revision 7 renewed the vercel catalog hash after Vercel added
paid-only zai/glm-5.3-flashx, expecting #31 to resolve on the next run.
A fresh public-only refresh (2026-09-25T05:06:42Z) still fires
vercel:source_changed:catalog with a new fingerprint: the catalog changed
again after the rev7 snapshot, so the finding carries changed, unreviewed
evidence and must stay open. The renewal basis itself is intact
(glm-5.3-flashx still present at exactly $0.37/$1.25 per M), so the delta
is new post-rev7 churn elsewhere in the 390-model catalog.

This module pins that verdict following tests/test_maint_close_reviewed_16.py:
the recorded Sept-25 live fingerprint recomputes under the repo's own
fingerprint function, differs from the rev7 reviewed fingerprint, and the
batch verifier reports `refired` against the fresh report. It also pins the
rev7 safety claim on the fresh catalog shape: paid-only glm-5.3-flashx stays
excluded from the vercel zero-price grant while the four fresh zero-price
chat routes are admitted.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from freellmpool import discovery as d
from freellmpool import maintenance as m
from freellmpool.provider_registry import load_registry
from scripts import verify_maint_close_batch as batch

BATCH_PATH = Path("maintenance/close-batch-31.json")

# Recorded from a fresh public-only refresh (2026-09-25T05:06:42Z, exit 0):
# REV6/REV7 from commit 02e7809 (policy revision 7); LIVE_AFTER observed on
# four byte-identical fetches of https://ai-gateway.vercel.sh/v1/models.
ISSUE = 31
PROVIDER = "vercel"
CODE = "source_changed"
SUBJECT = "catalog"
REV6 = "b8f74dc3aa54c6dd385c06000b9528d2653f00e3f4f6960c48afcb8b38dbc2c9"
REV7 = "0cc3b4bcf7382cbb0b25196d890e3602df2a25582f9edea691e9461eee8bf1b1"
LIVE_AFTER = "1645c74670af7a5aa2dca541f327b357d38c2967075cf4c6499551d1f3e80c82"
REVIEWED_FP = "e9543f4147d5489ba27a1c0558e54f4946fa00b8a10fdcbe93837088b49f6dc0"
LIVE_FP = "166eaff1f0e13f1425880a68baaa8eb501dff7866eac280ec018c2cda01db2d2"
CHECKED_AT = "2026-09-25T05:06:42.668252+00:00"

# Exact rows from the Sept-25 live catalog body (only id/type/pricing shown;
# the live body carries context_window/max_tokens alongside, which the
# zero-price gate does not consume).
FRESH_ROWS: list[dict[str, Any]] = [
    {"id": "zai/glm-5.3-flashx", "type": "language",
     "pricing": {"input": "0.00000037", "input_cache_read": "0.000000075",
                 "output": "0.00000125"}},
    {"id": "inclusionai/ling-3.0-flash-fin", "type": "language",
     "pricing": {"input": "0", "output": "0", "varies_by_provider": True}},
    {"id": "inclusionai/ling-3.0-flash-fin-free", "type": "language",
     "pricing": {"input": "0", "output": "0"}},
    {"id": "inclusionai/ling-3.0-flash-sante", "type": "language",
     "pricing": {"input": "0", "output": "0"}},
    {"id": "inclusionai/ling-3.0-flash-sante-free", "type": "language",
     "pricing": {"input": "0", "output": "0"}},
    {"id": "poolside/laguna-s-2.1-free", "type": "language",
     "pricing": {"input": "0", "output": "0"}},
    {"id": "zai/glm-5.2", "type": "language",
     "pricing": {"input": "0.0000008", "input_cache_read": "0.00000016",
                 "output": "0.00000255", "varies_by_provider": True,
                 "fast": {"input": "0.0000028", "input_cache_read": "0.00000056",
                          "output": "0.0000088"}}},
]
FRESH_ZERO_IDS = [
    "inclusionai/ling-3.0-flash-fin-free",
    "inclusionai/ling-3.0-flash-sante",
    "inclusionai/ling-3.0-flash-sante-free",
    "poolside/laguna-s-2.1-free",
]


def _live_report() -> dict[str, Any]:
    return {"findings": [{
        "id": f"{PROVIDER}:{CODE}:subject", "provider": PROVIDER,
        "kind": "review", "code": CODE, "subject": SUBJECT,
        "fingerprint": LIVE_FP, "before": REV7, "after": LIVE_AFTER}],
        "pending_changes": [], "resolutions": [], "providers": {},
        "checked_at": CHECKED_AT}


def _vercel_spec() -> dict[str, Any]:
    return copy.deepcopy(load_registry(None)["vercel"])


def test_live_fingerprint_recomputes_under_repo_function() -> None:
    rebuilt = m._finding(PROVIDER, CODE, SUBJECT, before=REV7, after=LIVE_AFTER)
    assert rebuilt["fingerprint"] == LIVE_FP
    assert rebuilt["id"] == "vercel:source_changed:652f55016243bf1b"


def test_reviewed_fingerprint_recomputes_from_rev6_rev7_hashes() -> None:
    rebuilt = m._finding(PROVIDER, CODE, SUBJECT, before=REV6, after=REV7)
    assert rebuilt["fingerprint"] == REVIEWED_FP


def test_live_fingerprint_differs_from_reviewed() -> None:
    assert LIVE_FP != REVIEWED_FP
    assert LIVE_AFTER != REV7


def test_batch_spec_lists_exactly_the_31_target() -> None:
    spec = batch.load_batch(BATCH_PATH)
    assert [entry["issue"] for entry in spec["targets"]] == [31]
    entry = spec["targets"][0]
    assert (entry["provider"], entry["code"], entry["subject"]) == (
        PROVIDER, CODE, SUBJECT)
    assert entry["reviewed_fingerprint"] == REVIEWED_FP


def test_verdict_is_refired_against_fresh_report() -> None:
    spec = batch.load_batch(BATCH_PATH)
    assert batch.verdicts(spec, _live_report()) == {31: "refired"}


def test_matching_resolution_with_fresh_evidence_is_resolved() -> None:
    spec = batch.load_batch(BATCH_PATH)
    report = _live_report()
    report["findings"] = []
    report["resolutions"] = [{
        "id": f"{PROVIDER}:{CODE}:subject", "provider": PROVIDER,
        "kind": "review", "code": CODE, "subject": SUBJECT,
        "fingerprint": REVIEWED_FP}]
    report["providers"] = {PROVIDER: {"sources": [{
        "id": SUBJECT, "status": "unchanged",
        "checked_at": "2026-09-25T05:00:00+00:00",
        "expires_at": "2026-10-02T05:00:00+00:00"}]}}
    assert batch.verdicts(spec, report) == {31: "resolved"}


def test_absent_target_without_proof_needs_review() -> None:
    spec = batch.load_batch(BATCH_PATH)
    report = _live_report()
    report["findings"] = []
    assert batch.verdicts(spec, report) == {31: "needs-review"}


def test_cli_reports_refired_batch_with_distinct_exit(tmp_path: Path) -> None:
    report_path = tmp_path / "public-report.json"
    report_path.write_text(json.dumps(_live_report()))
    assert batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path)]) == 3
    quiet = batch.main(["--batch", str(BATCH_PATH), "--report", str(report_path),
                        "--format", "json"])
    assert quiet == 3


def test_paid_only_glm_53_flashx_excluded_from_zero_price_grant() -> None:
    admitted = [row["id"] for row in d.free_catalog_models(
        _vercel_spec(), d.normalize_models(PROVIDER, {"data": FRESH_ROWS}))]
    assert "zai/glm-5.3-flashx" not in admitted
    assert "zai/glm-5.2" not in admitted


def test_fresh_zero_price_chat_routes_admitted_without_expansion() -> None:
    admitted = [row["id"] for row in d.free_catalog_models(
        _vercel_spec(), d.normalize_models(PROVIDER, {"data": FRESH_ROWS}))]
    assert admitted == FRESH_ZERO_IDS
    # Zero input/output alone is not enough: per-provider variance keeps the
    # non-free ling-fin route out of the grant.
    assert "inclusionai/ling-3.0-flash-fin" not in admitted
