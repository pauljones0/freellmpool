"""G21 run survivor: checkpoint/resume for fan-out runs (no live calls)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from freellmpool import run_checkpoint as rc
from freellmpool.panel import PanelAnswer
from freellmpool.tokenmax import fan_out


def _target(pid: str, model: str):
    return SimpleNamespace(provider=SimpleNamespace(id=pid), model=model)


def _pool(script: dict, counter: dict):
    """Fake pool: script maps 'pid/model' -> text or Exception to raise."""

    def chat(messages, *, model=None, providers=None, **kwargs):
        label = f"{providers[0]}/{model}"
        counter[label] = counter.get(label, 0) + 1
        outcome = script[label]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(provider_id=providers[0], model=model, text=outcome)

    return SimpleNamespace(chat=chat)


def test_checkpoint_roundtrip_and_missing_file(tmp_path) -> None:
    path = tmp_path / "r1.json"
    cp = rc.RunCheckpoint(run_id="r1", kind="tokenmax", max_tokens=64,
                          targets=["a/m", "b/m"], path=path)
    cp.record("a/m", text="hello", fresh=True)
    cp.record("b/m", error="boom", fresh=True)
    cp.save()
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = rc.RunCheckpoint.load(path)
    assert loaded.answered == {"a/m": loaded.results["a/m"]}
    assert loaded.results["a/m"]["fresh"] is True
    assert loaded.results["b/m"]["error"] == "boom"
    with pytest.raises(FileNotFoundError):
        rc.RunCheckpoint.load(tmp_path / "nope.json")


def test_checkpoint_path_lives_under_config_runs_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = rc.checkpoint_path("abc123")
    assert path.parent == tmp_path / "freellmpool" / "runs"
    assert path.name == "abc123.json"


def test_fan_out_records_incrementally_and_resume_only_runs_missing(tmp_path) -> None:
    path = tmp_path / "run.json"
    cp = rc.RunCheckpoint(run_id="run", kind="tokenmax", max_tokens=64,
                          targets=["a/m", "b/m", "c/m"], path=path)
    picks = [_target("a", "m"), _target("b", "m"), _target("c", "m")]
    counter: dict = {}
    flaky = _pool({"a/m": "A1", "b/m": RuntimeError("429"), "c/m": RuntimeError("down")},
                  counter)
    answered, failed = fan_out(flaky, [], picks, max_tokens=64, checkpoint=cp)
    assert [lbl for lbl, _ in answered] == ["a/m"]
    assert sorted(failed) == ["b/m", "c/m"]
    assert counter == {"a/m": 1, "b/m": 1, "c/m": 1}
    assert json.loads(path.read_text())["results"]["a/m"]["text"] == "A1"

    # Resume: the bench recovered. Only missing labels hit the pool again.
    resumed = rc.RunCheckpoint.load(path)
    replay, todo = rc.resume_plan(resumed)
    assert [lbl for lbl, _ in replay] == ["a/m"]
    assert sorted(f"{t.provider.id}/{t.model}" for t in todo) == ["b/m", "c/m"]
    healed = _pool({"a/m": "A2-should-not-run", "b/m": "B2", "c/m": "C2"}, counter)
    answered2, failed2 = fan_out(healed, [], todo, max_tokens=64, checkpoint=resumed)
    assert sorted(lbl for lbl, _ in answered2) == ["b/m", "c/m"]
    assert failed2 == []
    assert counter == {"a/m": 1, "b/m": 2, "c/m": 2}  # replay consumed no quota
    final = rc.RunCheckpoint.load(path)
    assert final.results["a/m"]["fresh"] is True  # first attempt was live
    assert final.results["b/m"] == {"label": "b/m", "text": "B2", "error": None,
                                    "fresh": True,
                                    "at": final.results["b/m"]["at"]}
    merged = rc.merge_answers(replay, answered2)
    assert [(lbl, fresh) for lbl, _, fresh in merged] == [
        ("a/m", False), ("b/m", True), ("c/m", True)]


def test_resume_plan_replays_original_targets_only() -> None:
    cp = rc.RunCheckpoint(
        run_id="r", kind="tokenmax", max_tokens=64,
        targets=["a/m", "b/deep/model", "c/m"])
    cp.record("a/m", text="A1")
    cp.record("b/deep/model", error="429")
    replay, todo = rc.resume_plan(cp)
    assert replay == [("a/m", "A1")]
    # Failed b/... retries and never-attempted c/m runs; models containing
    # "/" survive the label split.
    assert [(t.provider.id, t.model) for t in todo] == [
        ("b", "deep/model"), ("c", "m")]


def test_run_panel_checkpoints_and_resumes_missing_only(tmp_path) -> None:
    from freellmpool.panel import run_panel

    targets = [_target("p1", "m1"), _target("p2", "m2")]
    counter: dict = {}
    flaky = _pool({"p1/m1": "one", "p2/m2": RuntimeError("429")}, counter)
    flaky.rank_targets = lambda *a, **k: targets
    path = tmp_path / "p.json"
    cp = rc.RunCheckpoint(run_id="p", kind="panel", max_tokens=64, targets=[],
                          path=path)
    phase1 = run_panel(flaky, prompt="Q?", n=2, checkpoint=cp)
    assert [a.text for a in phase1.answers if a.ok] == ["one"]
    assert sum(1 for a in phase1.answers if not a.ok) == 1
    assert all(a.replayed is False for a in phase1.answers)

    healed = _pool({"p1/m1": "SHOULD-NOT-RUN", "p2/m2": "two"}, counter)
    healed.rank_targets = lambda *a, **k: targets
    resumed = rc.RunCheckpoint.load(path)
    phase2 = run_panel(healed, prompt="Q?", n=2, checkpoint=resumed)
    by_label = {a.label: a for a in phase2.answers}
    assert by_label["p1/m1"].replayed is True
    assert by_label["p1/m1"].text == "one"
    assert by_label["p2/m2"].replayed is False
    assert by_label["p2/m2"].text == "two"
    assert counter == {"p1/m1": 1, "p2/m2": 2}
    fresh = PanelAnswer(provider_id="p", model="m", label="p/m", family=None,
                        text="t", latency_ms=1)
    assert fresh.replayed is False


def test_checkpoint_path_rejects_traversal(tmp_path) -> None:
    with pytest.raises(ValueError, match="invalid run id"):
        rc.checkpoint_path("../../etc/evil", runs_dir=tmp_path)
    with pytest.raises(ValueError, match="invalid run id"):
        rc.checkpoint_path("has space", runs_dir=tmp_path)
    assert rc.checkpoint_path("ok-id_1.2", runs_dir=tmp_path).name == "ok-id_1.2.json"


def test_new_run_id_is_filesafe_and_unique() -> None:
    ids = {rc.new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(set(i) <= set("0123456789TZ-abcdef") for i in ids)


def test_fan_out_normalizes_missing_text_for_merge() -> None:
    """CI strict-mypy contract: answered texts are str (never None) so the
    checkpoint merge path type-checks; a textless success normalizes to ''."""
    picks = [_target("a", "m")]
    pool = _pool({"a/m": None}, {})
    answered, failed = fan_out(pool, [], picks, max_tokens=64)
    assert failed == []
    assert answered == [("a/m", "")]
    merged = rc.merge_answers([], answered)
    assert merged == [("a/m", "", True)]


def test_replay_panel_answer_tolerates_corrupt_latency() -> None:
    import freellmpool.run_checkpoint as rc

    answered = rc.replay_panel_answer({
        "provider_id": "p", "model": "m", "label": "p/m",
        "text": "hi", "latency_ms": "abc",
    })
    assert answered.latency_ms == 0
    assert answered.text == "hi"
    assert answered.replayed is True
