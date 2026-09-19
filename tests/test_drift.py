"""G10: drift radar — diff classification, snapshots, emit."""

from __future__ import annotations

import json

from freellmpool import drift


def _snap(rows):
    """rows: {target: {feature: status}} -> stored snapshot shape."""
    return {
        "schema": 1,
        "generated_at": "2026-09-10T00:00:00Z",
        "freellmpool": "0.0-test",
        "targets": {
            target: {feat: {"status": st, "verified_at": "2026-09-10T00:00:00Z",
                             "classification": "verified" if st == "pass" else "x"}
                     for feat, st in feats.items()}
            for target, feats in rows.items()
        },
    }


def test_classify_died_recovered_changed():
    old = _snap({"a/m": {"tools": "pass", "vision": "fail", "chat": "pass", "streaming": "pass"}})
    new = _snap({"a/m": {"tools": "fail", "vision": "pass", "chat": "unavailable", "streaming": "pass"}})
    kinds = {(c["target"], c["feature"]): c["kind"] for c in drift.classify_changes(old, new)}
    assert kinds == {("a/m", "tools"): "died", ("a/m", "vision"): "recovered",
                     ("a/m", "chat"): "changed"}


def test_vanished_pass_is_died_new_observations_skipped():
    old = _snap({"a/m": {"tools": "pass"}, "b/m": {"tools": "fail"}})
    new = _snap({"c/m": {"tools": "pass"}})
    kinds = {(c["target"], c["feature"]): c["kind"] for c in drift.classify_changes(old, new)}
    assert kinds == {("a/m", "tools"): "died"}


def test_identical_status_is_silent_even_when_refreshed():
    old = _snap({"a/m": {"tools": "pass"}})
    new = json.loads(json.dumps(old))
    new["generated_at"] = "2026-09-19T00:00:00Z"
    assert drift.classify_changes(old, new) == []


def test_report_names_each_change_with_dates():
    old = _snap({"a/m": {"tools": "pass"}})
    new = _snap({"a/m": {"tools": "unsupported"}})
    new["generated_at"] = "2026-09-19T00:00:00Z"
    out = drift.render_report(drift.classify_changes(old, new))
    assert "died" in out and "a/m" in out and "tools" in out and "2026-09-19" in out


def test_take_snapshot_sanitizes_store_state():
    store = {"version": 1, "updated_at": "x",
             "targets": {"a/m": {"fingerprint": "fp", "features": {
                 "tools": {"status": "pass", "verified_at": "t", "classification": "verified",
                           "extra": "dropped"}}}}}
    snap = drift.take_snapshot(store, freellmpool_version="9.9")
    assert snap["schema"] == 1 and snap["freellmpool"] == "9.9"
    assert snap["targets"] == {"a/m": {"tools": {"status": "pass", "verified_at": "t",
                                                       "classification": "verified"}}}


def test_cli_baseline_then_diff(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from test_managed_runtime import make_pool

    from freellmpool import managed_cli
    from freellmpool.conformance import ConformanceStore

    store = ConformanceStore(tmp_path / "conf.json")
    pool = make_pool(tmp_path, conformance=store)
    monkeypatch.setattr(managed_cli.ManagedPool, "from_default_config", lambda: pool)
    monkeypatch.setenv("FREELLMPOOL_DRIFT_DIR", str(tmp_path / "drift"))
    route = pool.snapshot().routes[0]
    store.record(route.provider, route.model, "tools",
                 status="pass", classification="verified")
    args = SimpleNamespace(probe=False, limit=8, features="tools", provider=None,
                           timeout=30, json=False, emit=None)
    assert managed_cli.cmd_drift(args) == 0
    assert "baseline" in capsys.readouterr().out.lower()
    store.record(route.provider, route.model, "tools",
                 status="fail", classification="semantic_mismatch")
    assert managed_cli.cmd_drift(args) == 0
    out = capsys.readouterr().out
    assert "died" in out and route.provider.id in out


def test_emit_writes_valid_snapshot(tmp_path):
    from scripts.check_drift_snapshot import validate_snapshot

    snap = _snap({"a/m": {"tools": "pass"}})
    path = tmp_path / "snap.json"
    drift.write_snapshot(snap, path)
    assert validate_snapshot(path) == []


def test_schema_doc_example_validates(tmp_path):
    import re
    from pathlib import Path

    from scripts.check_drift_snapshot import validate_snapshot

    doc = Path(__file__).resolve().parent.parent / "docs" / "DRIFT_SNAPSHOT.md"
    block = re.search(r"```json\n(.*?)```", doc.read_text(), re.S)
    assert block, "schema doc must carry a normative JSON example"
    example = tmp_path / "doc-example.json"
    example.write_text(block.group(1))
    assert validate_snapshot(example) == []
