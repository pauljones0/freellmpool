"""U6: container liveness tolerates first-run discovery (G24)."""

from pathlib import Path


def test_dockerfile_healthcheck_start_period():
    text = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert "start-period=90s" in text
    assert "<=40s" in text
