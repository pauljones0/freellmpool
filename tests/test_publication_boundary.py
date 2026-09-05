"""The maintained fork must not publish private state or upstream releases."""

import fnmatch
import subprocess
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_WORKFLOWS = (
    "docker.yml", "pages.yml", "promote-latest.yml", "publish-llm-plugin.yml",
    "publish-mcp.yml", "publish-opencode.yml", "release-evidence.yml",
)
PRIVATE_PATHS = (
    "config.toml", "config.toml.bak", "keys.toml", "keys.toml.before-update",
    "accounts.json", "accounts.json.bak", "account-observations.json", "proxy.key.bak",
    "allowances.sqlite3", "allowances.sqlite3-wal", "allowances.sqlite3-shm",
    "quota.json", "stats.json", "route_health.json", "run_records.jsonl", "conformance.json",
    "discovery.json", "evidence.json", "GOAL.md", "AUDIT.md", "audit/2026-09-05/local-catalog.toml",
    ".beads/issues.jsonl", ".codegraph/daemon.pid", "maintenance-attention.txt",
    "policy-update.json", "policy-bundle.json", "workflow-health.json", "maintenance-proposals.json",
    "public-baseline.json", "public-report.json", "public-evidence.json",
)


def test_inherited_release_jobs_are_restricted_to_original_owner():
    for filename in UPSTREAM_WORKFLOWS:
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        for name, job in workflow["jobs"].items():
            assert "github.repository == '0xzr/freellmpool'" in job.get("if", ""), (filename, name)


def test_private_state_is_ignored_even_if_copied_into_checkout():
    result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                            input="\n".join(PRIVATE_PATHS) + "\n", text=True,
                            cwd=ROOT, capture_output=True, check=False)
    ignored = set(result.stdout.splitlines())
    assert ignored == set(PRIVATE_PATHS)
    public = ["config.toml.example", ".env.example", "src/freellmpool/providers.toml",
              "src/freellmpool/provider_registry.json", "MAINTENANCE_GOAL.md"]
    result = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                            input="\n".join(public) + "\n", text=True,
                            cwd=ROOT, capture_output=True, check=False)
    assert result.returncode == 1
    assert not result.stdout


def test_docker_context_excludes_private_files_and_state_directories():
    patterns = [line.lstrip("/").rstrip("/") for line in (ROOT / ".dockerignore").read_text().splitlines()
                if line and not line.startswith(("#", "!"))]
    for path in PRIVATE_PATHS:
        parts = path.split("/")
        candidates = ["/".join(parts[:end]) for end in range(1, len(parts) + 1)]
        assert any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates for pattern in patterns), path


def test_public_readme_and_metadata_identify_source_fork_without_capacity_marketing():
    readme = (ROOT / "README.md").read_text()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    repository = "https://github.com/pauljones0/freellmpool"
    assert f"git clone {repository}.git" in readme
    assert "sh integrations/setup/bootstrap.sh" in readme
    assert "freellmpool maintenance" in readme
    assert "docs/maintenance.md" in readme
    assert "https://github.com/0xzr/freellmpool" in readme
    assert "MIT" in readme and "finite" in readme.lower()
    assert "GOAL.md](GOAL.md)" not in readme and "AUDIT.md" not in readme
    assert "mcp-name: io.github.0xzr" not in readme
    assert len(readme.splitlines()) <= 100
    assert not any(character.isdigit() for character in project["description"])
    assert project["urls"]["Repository"] == repository
    assert all("pauljones0/freellmpool" in url for url in project["urls"].values())
