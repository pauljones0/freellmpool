"""G18: live free-tier status page publisher (snapshot + staleness + history)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freellmpool.healthcheck import HealthRow
from freellmpool.status_page import (
    HISTORY_LIMIT,
    append_history,
    assert_no_key_material,
    build_snapshot,
    publish_status,
    render_status_html,
    validate_published,
)

ROWS = [
    HealthRow("groq/llama-3", "ok", 412.0, "12 tok"),
    HealthRow("gemini/flash", "rate_limited", None, "429 quota exceeded"),
]

HOSTILE_SECRET = "sk-live-abcdefghijklmnop1234"


def _snapshot() -> dict:
    return build_snapshot(ROWS, generated_at="2026-09-19T23:00:00Z", version="0.13.0")


def test_build_snapshot_projects_only_safe_fields() -> None:
    snap = _snapshot()
    assert snap["schema"] == 1
    assert snap["generated_at"] == "2026-09-19T23:00:00Z"
    assert snap["rows"] == [
        {"target": "groq/llama-3", "status": "ok", "latency_ms": 412.0, "note": "12 tok"},
        {"target": "gemini/flash", "status": "rate_limited", "latency_ms": None,
         "note": "429 quota exceeded"},
    ]
    assert set(snap) == {"schema", "generated_at", "freellmpool", "rows"}


def test_append_history_caps_at_limit_newest_last() -> None:
    history: list = []
    for i in range(HISTORY_LIMIT + 5):
        snap = build_snapshot([], generated_at=f"2026-09-{10 + i:02d}T00:00:00Z",
                              version="0.13.0")
        history = append_history(history, snap)
    assert len(history) == HISTORY_LIMIT
    assert history[-1]["generated_at"] == "2026-09-26T00:00:00Z"
    assert history[0]["generated_at"] == "2026-09-15T00:00:00Z"


def test_render_escapes_html_and_labels_staleness() -> None:
    rows = [HealthRow("<b>evil</b>", "ok", 1.0, "<script>alert(1)</script>")]
    snap = build_snapshot(rows, generated_at="2026-09-19T23:00:00Z", version="0.13.0")
    html = render_status_html(snap, [snap])
    assert "<script>alert(1)</script>" not in html and "<b>evil" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "2026-09-19T23:00:00Z" in html
    assert "stale" in html.lower() or "old" in html.lower()
    assert "1/1" in html  # ok/total summary


def test_render_empty_rows_is_honest_not_green() -> None:
    snap = build_snapshot([], generated_at="2026-09-19T23:00:00Z", version="0.13.0")
    html = render_status_html(snap, [])
    assert "No configured providers" in html
    assert "0/0" in html


def test_secret_scanner_passes_clean_and_redacted_text() -> None:
    assert_no_key_material("ok 12 tok, latency 412 ms")
    assert_no_key_material("note with [REDACTED_SECRET] marker is safe")


@pytest.mark.parametrize("secret", [
    "sk-live-abcdefghijklmnop1234",
    "sk-proj-abcdefghijklmnop1234",
    "ghp_abcdefghijklmnop1234567890",
    "AKIAIOSFODNN7EXAMPLE",
    "AIzaSyA-abcdefghijklmnopqrstuvw",
    "api_key=supersecretvalue123",
])
def test_secret_scanner_rejects_key_material(secret: str) -> None:
    with pytest.raises(ValueError, match="key material"):
        assert_no_key_material(f"probe note leaked {secret} oops")


def test_publish_redacts_hostile_notes_and_merges_history(tmp_path: Path) -> None:
    hostile = [HealthRow("evil/p", "error", None, f"failed: key {HOSTILE_SECRET} denied")]
    page, history_file = publish_status(tmp_path, hostile,
                                        generated_at="2026-09-19T23:00:00Z",
                                        version="0.13.0")
    assert page.name == "free-tier-status.html"
    assert history_file.name == "status-history.json"
    for path in (page, history_file):
        text = path.read_text()
        assert HOSTILE_SECRET not in text
        assert "REDACTED" in text
    assert validate_published(tmp_path) == []
    # Second publish appends history rather than replacing it.
    publish_status(tmp_path, ROWS, generated_at="2026-09-20T00:00:00Z", version="0.13.0")
    history = json.loads(history_file.read_text())
    assert [h["generated_at"] for h in history] == ["2026-09-19T23:00:00Z",
                                                   "2026-09-20T00:00:00Z"]


def test_cli_publish_from_rows_file_and_check(tmp_path: Path, capsys) -> None:
    from freellmpool.cli import main

    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps([
        {"target": "groq/llama-3", "status": "ok", "latency_ms": 100.0, "note": "8 tok"},
    ]))
    docs_dir = tmp_path / "docs"
    assert main(["status-page", "publish", "--docs-dir", str(docs_dir),
                 "--rows-file", str(rows_file)]) == 0
    out = capsys.readouterr().out
    assert "1/1 ok" in out
    assert (docs_dir / "free-tier-status.html").is_file()
    assert main(["status-page", "check", "--docs-dir", str(docs_dir)]) == 0
    assert "valid" in capsys.readouterr().out


def test_cli_check_fails_on_missing_files(tmp_path: Path) -> None:
    from freellmpool.cli import main

    assert main(["status-page", "check", "--docs-dir", str(tmp_path)]) == 1


def test_validate_published_detects_shape_and_secret_problems(tmp_path: Path) -> None:
    assert validate_published(tmp_path)  # missing files
    (tmp_path / "free-tier-status.html").write_text("<html>no stamp</html>")
    (tmp_path / "status-history.json").write_text(json.dumps([{"schema": 999}]))
    errors = validate_published(tmp_path)
    assert any("generated_at" in e for e in errors)
    assert any("schema" in e for e in errors)
    (tmp_path / "status-history.json").write_text(
        json.dumps([{"schema": 1, "generated_at": "t", "note": HOSTILE_SECRET}]))
    assert any("key material" in e for e in validate_published(tmp_path))
