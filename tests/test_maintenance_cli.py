"""Public command execution must never obtain the operator environment."""

import json

from test_maintenance import SHA, public

from freellmpool import maintenance_cli as cli


def test_default_command_is_read_only_and_prints_recovery(monkeypatch, capsys):
    report, _ = public()
    report["visibility"] = "private"
    monkeypatch.setattr(cli, "effective_env", lambda: {})
    monkeypatch.setattr(cli, "status_report", lambda env: report)
    monkeypatch.setattr(cli, "run_maintenance", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("refresh")))
    assert cli.main([]) == 0
    assert "No actionable" in capsys.readouterr().out


def test_public_cli_ignores_private_environment_and_writes_valid_json(tmp_path, monkeypatch):
    report, _ = public()
    def forbidden():
        raise AssertionError("private environment was loaded")
    def refresh(env, **kwargs):
        assert env == {}
        assert kwargs["public_only"] is True
        assert kwargs["source_revision"] == SHA
        return report
    monkeypatch.setattr(cli, "effective_env", forbidden)
    monkeypatch.setattr(cli, "run_maintenance", refresh)
    output = tmp_path / "report.json"
    assert cli.main(["--public-only", "--refresh", "--source-revision", SHA, "--output", str(output)]) == 0
    assert json.loads(output.read_text()) == report


def test_total_failure_does_not_echo_upstream_body(monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise ValueError("KEY-SECRET @everyone https://attacker.invalid")
    monkeypatch.setattr(cli, "run_maintenance", broken)
    assert cli.main(["--public", "--refresh"]) == 2
    output = capsys.readouterr()
    assert "SECRET" not in output.out + output.err
    assert "valid report" in output.err


def test_public_output_revalidates_returned_object(monkeypatch, tmp_path):
    report, _ = public()
    report["account_state"] = {"key": "SECRET"}
    monkeypatch.setattr(cli, "run_maintenance", lambda *args, **kwargs: report)
    path = tmp_path / "bad.json"
    assert cli.main(["--public", "--refresh", "--output", str(path)]) == 2
    assert not path.exists()
