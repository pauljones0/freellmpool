"""G25 U4: --resume/--no-resume flags, skipped-honor, resume recap."""

import argparse
import json
from datetime import UTC, datetime, timedelta

import pytest

from freellmpool.onboarding import _setup_ref, run_onboarding

HELP_RESUME = ("resume saved setup progress (default): pass over already-checked and skipped "
               "providers")
HELP_NO_RESUME = "re-check every provider, ignoring saved progress"
RECAP_BOTH = ("Resumed: passed over 1 already-checked and 1 skipped provider(s). Skipped: beta. "
              "Retry one: freellmpool setup --provider PROVIDER")
RECAP_CHECKED = "Resumed: passed over 2 already-checked provider(s)."
RECAP_SKIPPED = ("Resumed: passed over 2 skipped provider(s). Skipped: alpha, beta. "
                 "Retry one: freellmpool setup --provider PROVIDER")
NOW_ISO = datetime.now(UTC).isoformat()


def _registry(*names):
    def row(name):
        return {"id": name, "display_name": name.title(), "credential_env": name.upper() + "_KEY",
                "grants": [{"kind": "recurring_quota", "status": "verified"}],
                "setup": {"signup_url": "https://example.test/signup",
                          "key_url": "https://example.test/keys", "steps": ["Choose the Free plan."],
                          "required_env": [name.upper() + "_KEY"]}}
    return {name: row(name) for name in names}


def _env(tmp_path):
    return {"FREELLMPOOL_CONFIG_FILE": str(tmp_path / "config.toml"),
            "FREELLMPOOL_ACCOUNTS_FILE": str(tmp_path / "accounts.json")}


def test_setup_parsers_resume_flags(capsys):
    from freellmpool import managed_cli
    from freellmpool.onboarding import main as onboarding_main

    parser = argparse.ArgumentParser()
    managed_cli.add_commands(parser.add_subparsers())
    assert parser.parse_args(["setup"]).resume is True
    assert parser.parse_args(["setup", "--resume"]).resume is True
    assert parser.parse_args(["setup", "--no-resume"]).resume is False
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert HELP_RESUME in help_text
    assert HELP_NO_RESUME in help_text

    with pytest.raises(SystemExit):
        onboarding_main(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert HELP_RESUME in help_text
    assert HELP_NO_RESUME in help_text


def test_no_resume_rechecks_checked_provider(tmp_path):
    progress = tmp_path / "progress.json"
    calls = []
    kwargs = dict(registry=_registry("alpha"), env=_env(tmp_path), progress_path=progress,
                  input_fn=lambda _: "", secret_fn=lambda _: "private-value",
                  check=lambda *_: calls.append(True) or {"status": "ok"}, output=lambda _: None)
    assert run_onboarding(**kwargs) == 0
    assert len(calls) == 1
    assert run_onboarding(**kwargs, resume=False) == 0
    assert len(calls) == 2


def test_resume_skips_checked_and_skipped_with_recap(tmp_path):
    progress = tmp_path / "progress.json"
    env = _env(tmp_path)
    answers = iter(["", "", "s"])
    calls = []
    assert run_onboarding(registry=_registry("alpha", "beta"), env=env, progress_path=progress,
                          input_fn=lambda _: next(answers), secret_fn=lambda _: "private-value",
                          check=lambda *_: calls.append(True) or {"status": "ok"},
                          output=lambda _: None) == 0
    assert len(calls) == 1
    output = []
    assert run_onboarding(registry=_registry("alpha", "beta"), env=env, progress_path=progress,
                          input_fn=lambda prompt: pytest.fail(f"re-prompted: {prompt}"),
                          secret_fn=lambda _: pytest.fail("re-collected"),
                          check=lambda *_: pytest.fail("re-checked"),
                          output=output.append) == 0
    assert RECAP_BOTH in output
    state = json.loads(progress.read_text())
    assert state["providers"]["alpha"]["status"] == "checked"
    assert state["providers"]["beta"]["status"] == "skipped"


def test_resume_recap_branches(tmp_path):
    cases = [(["", "", "", ""], RECAP_CHECKED), (["s", "s"], RECAP_SKIPPED)]
    env = _env(tmp_path)
    for index, (first, expected) in enumerate(cases):
        progress = tmp_path / f"progress-{index}.json"

        def drive(first=first, progress=progress):
            answers = iter(first)
            return run_onboarding(registry=_registry("alpha", "beta"), env=env,
                                  progress_path=progress, input_fn=lambda _: next(answers),
                                  secret_fn=lambda _: "private-value",
                                  check=lambda *_: {"status": "ok"}, output=lambda _: None)

        assert drive() == 0
        output = []
        assert run_onboarding(registry=_registry("alpha", "beta"), env=env, progress_path=progress,
                              input_fn=lambda prompt: pytest.fail(f"re-prompted: {prompt}"),
                              secret_fn=lambda _: pytest.fail("re-collected"),
                              check=lambda *_: pytest.fail("re-checked"),
                              output=output.append) == 0
        assert expected in output

    output = []
    assert run_onboarding(registry=_registry("alpha"), env=_env(tmp_path),
                          progress_path=tmp_path / "fresh.json", input_fn=lambda _: "",
                          secret_fn=lambda _: "private-value",
                          check=lambda *_: {"status": "ok"}, output=output.append) == 0
    assert not [line for line in output if line.startswith("Resumed:")]


def test_resume_ignores_non_registry_progress_keys(tmp_path):
    from freellmpool.config import effective_env

    registry = _registry("alpha")
    env = _env(tmp_path)
    credentials = effective_env({**env, "ALPHA_KEY": "k"})
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({"schema": 1, "providers": {
        "alpha": {"status": "skipped", "note": "",
                  "updated_at": (datetime.now(UTC) - timedelta(days=90)).isoformat(),
                  "credential_ref": _setup_ref("alpha", registry["alpha"], credentials)},
        "ghost": {"status": "skipped", "note": "", "updated_at": "2020-01-01T00:00:00+00:00",
                  "credential_ref": "tampered"}}}))
    output = []
    assert run_onboarding(registry=registry, env={**env, "ALPHA_KEY": "k"},
                          progress_path=progress,
                          input_fn=lambda prompt: pytest.fail(f"re-prompted: {prompt}"),
                          secret_fn=lambda _: pytest.fail("re-collected"),
                          check=lambda *_: pytest.fail("re-checked"),
                          output=output.append) == 0
    assert ("Resumed: passed over 1 skipped provider(s). Skipped: alpha. "
            "Retry one: freellmpool setup --provider PROVIDER") in output
    assert "ghost" not in "\n".join(output)


def test_skipped_honor_is_rotation_sensitive_and_schema_compatible(tmp_path):
    from freellmpool.config import effective_env

    registry = _registry("alpha")
    env = _env(tmp_path)
    old_credentials = effective_env({**env, "ALPHA_KEY": "old-key"})
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({"schema": 1, "providers": {
        "alpha": {"status": "skipped", "note": "",
                  "updated_at": (datetime.now(UTC) - timedelta(days=90)).isoformat(),
                  "credential_ref": _setup_ref("alpha", registry["alpha"], old_credentials)}}}))
    calls = []
    assert run_onboarding(registry=registry, env={**env, "ALPHA_KEY": "new-key"},
                          progress_path=progress, input_fn=lambda _: "q",
                          secret_fn=lambda _: pytest.fail("must not collect before quit"),
                          check=lambda *_: calls.append(True) or {"status": "ok"},
                          output=lambda _: None) == 1
    assert calls == []

    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"schema": 1, "providers": {
        "alpha": {"status": "skipped", "note": "",
                  "updated_at": (datetime.now(UTC) - timedelta(days=90)).isoformat()}}}))
    prompts = []
    assert run_onboarding(registry=registry, env={**env, "ALPHA_KEY": "new-key"},
                          progress_path=legacy,
                          input_fn=lambda prompt: (prompts.append(prompt), "q")[1],
                          secret_fn=lambda _: pytest.fail("must not collect before quit"),
                          check=lambda *_: calls.append(True) or {"status": "ok"},
                          output=lambda _: None) == 1
    assert prompts and calls == []


def test_provider_bypass_reaches_skipped_provider(tmp_path):
    from freellmpool.config import effective_env

    registry = _registry("alpha")
    env = _env(tmp_path)
    credentials = effective_env({**env, "ALPHA_KEY": "k"})
    progress = tmp_path / "progress.json"
    progress.write_text(json.dumps({"schema": 1, "providers": {
        "alpha": {"status": "skipped", "note": "", "updated_at": NOW_ISO,
                  "credential_ref": _setup_ref("alpha", registry["alpha"], credentials)}}}))
    calls = []
    assert run_onboarding(provider="alpha", registry=registry, env={**env, "ALPHA_KEY": "k"},
                          progress_path=progress, input_fn=lambda _: "",
                          secret_fn=lambda _: pytest.fail("key already present"),
                          check=lambda *_: calls.append(True) or {"status": "ok"},
                          output=lambda _: None) == 0
    assert len(calls) == 1
