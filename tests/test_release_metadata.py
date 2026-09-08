from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from xml.etree import ElementTree

from freellmpool import __version__, client
from scripts.catalog_counts import catalog_counts

ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_versions_match_package() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]
    server = json.loads((ROOT / "server.json").read_text())
    docs = (ROOT / "docs" / "index.html").read_text()
    demo = (ROOT / "assets" / "demo.svg").read_text()
    legacy = (ROOT / "docs/legacy-0.13-guide.md").read_text()

    assert version == "0.14.1"
    assert __version__ == version
    legacy_version = "0.13.0"
    assert server["version"] == legacy_version
    assert server["packages"][0]["version"] == legacy_version
    counts = catalog_counts(ROOT)
    assert f"{counts.providers} LLM providers" in server["description"]
    assert f"{counts.providers} LLM providers" in legacy
    assert f"{counts.cataloged_chat_models} cataloged" in legacy
    assert f"{counts.enabled_chat_models} enabled chat routes" in docs
    assert f"{counts.cataloged_chat_models} cataloged" in docs
    assert f"Latest release: {legacy_version}" in docs
    assert f'"softwareVersion": "{legacy_version}"' in docs
    assert "installed from current checkout" in demo.lower()
    assert f"{counts.providers} cataloged providers" in demo


def test_client_user_agent_uses_package_version() -> None:
    assert f"freellmpool/{__version__}" in client._USER_AGENT


def test_runtime_dependencies_guard_stdlib_first_contract() -> None:
    """The stdlib-first contract only allows httpx as a required runtime dependency."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == ["httpx>=0.27"]


def test_build_backend_is_reproducibly_pinned() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert pyproject["build-system"]["requires"] == ["hatchling==1.32.0"]


def test_readme_has_copy_pastable_tailnet_and_metaswarm_paths() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/legacy-0.13-guide.md" in readme
    legacy = (ROOT / "docs/legacy-0.13-guide.md").read_text(encoding="utf-8")
    assert "freellmpool tailnet serve --port 8080" in legacy
    assert "freellmpool tailnet connect <tailnet-ip> --port 8080" in legacy
    assert "freellmpool init --yes --agent metaswarm --tailnet" in legacy
    assert "freellmpool profile doctor metaswarm --dry-run" in legacy


def test_readme_has_current_adoption_paths_and_pinned_comparison_sources() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    integrations = (ROOT / "docs" / "INTEGRATIONS.md").read_text(encoding="utf-8")

    assert "sh integrations/setup/bootstrap.sh" in readme
    assert "freellmpool setup-clients" in readme
    assert "freellmpool update" in readme
    assert "freellmpool verify --limit 4" in readme
    readme = (ROOT / "docs/legacy-0.13-guide.md").read_text(encoding="utf-8")
    assert "Comparison snapshot: 2026-07-19" in readme
    assert (
        "tashfeenahmed/freellmapi/blob/"
        "759de8e7ed1edc1cd513c9777cd0a807fb5ceee3/README.md"
    ) in readme
    assert (
        "diegosouzapw/OmniRoute/blob/"
        "d8ff51874c8add566d43225988b9bc67e0542d65/README.md"
    ) in readme
    assert "28 free LLM providers" in readme
    assert "MCP (Streamable HTTP)" in readme
    assert "OmniRoute" in readme

    for text in (readme, integrations):
        assert "freellmpool profile install hermes" in text
        assert "provider: custom" in text
        assert "default: quality" in text
        assert "http://localhost:8080/v1" in text
        assert "/livez" in text
        assert "/readyz" in text
        assert "/v1/providers" in text
        assert "/v1/models?ready=true" in text


def test_public_docs_distinguish_current_source_from_archived_release() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    agents = (ROOT / "docs" / "AGENTS.md").read_text(encoding="utf-8")
    integrations = (ROOT / "docs" / "INTEGRATIONS.md").read_text(encoding="utf-8")
    index = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")

    legacy_version = "0.13.0"
    assert version in readme
    source_docs = (
        (ROOT / "README.es.md").read_text(encoding="utf-8"),
        agents,
        integrations,
        index,
        (ROOT / "docs" / "llms.txt").read_text(encoding="utf-8"),
        (ROOT / "docs" / "run-coding-agents-on-free-models.html").read_text(
            encoding="utf-8"
        ),
        (ROOT / "docs" / "run-opencode-on-free-models.html").read_text(
            encoding="utf-8"
        ),
    )

    for text in source_docs:
        normalized = text.casefold().replace("`", "")
        assert legacy_version in text
        assert "pip install" in text
        assert "unreleased" not in normalized
        assert re.search(r"\bcurrent[- ]+main\b", normalized) is None
        assert "0.11.4" not in text

    for text in (integrations, index):
        assert f"Latest release: {legacy_version}" in text
    for text in (readme, agents):
        assert "freellmpool setup" in text
        assert "compatibility" in text

    assert f'"softwareVersion": "{legacy_version}"' in index

    for text in (
        (ROOT / "docs" / "INTEGRATIONS.md").read_text(encoding="utf-8"),
        index,
        (ROOT / "docs" / "llms.txt").read_text(encoding="utf-8"),
        (ROOT / "docs" / "run-opencode-on-free-models.html").read_text(
            encoding="utf-8"
        ),
    ):
        assert "registry-readiness hardening" in text


def test_vercel_free_catalog_keeps_acceptance_claims_bound_to_recorded_evidence() -> None:
    catalog = tomllib.loads((ROOT / "src/freellmpool/providers.toml").read_text())
    provider = next(row for row in catalog["provider"] if row["id"] == "vercel")
    assert [model["name"] for model in provider["models"]] == ["poolside/laguna-s-2.1-free"]

    acceptance = (ROOT / "docs/VERCEL_ACCEPTANCE_2026-08-23.md").read_text()
    assert acceptance.startswith("# Vercel AI Gateway acceptance audit — 2026-08-23\n")
    assert "Updated through 2026-08-29" in acceptance
    evidence = acceptance.split("## Credentialed evidence", 1)[1].split("## Terms and privacy", 1)[0]
    compact = " ".join(evidence.replace("`", "").split())
    assert "All three failed with HTTP 403" in compact
    assert "customer_verification_required" in compact
    assert "No completion was returned and this is not acceptance evidence" in compact

    accounts = (ROOT / "docs/ACCOUNTS.md").read_text()
    guidance = accounts.split("### Vercel AI Gateway", 1)[1].split("### OVHcloud", 1)[0]
    assert "customer verification" in guidance.casefold()
    assert "After Vercel has cleared" in guidance
    assert "Only the currently price-verified zero-price" in guidance
    assert "--attest-credit-spend" not in guidance
    env = (ROOT / ".env.example").read_text()
    assert "Only routes with verified zero prices are admitted" in env

    page = (ROOT / "docs/free-llm-api-providers-list.html").read_text()
    row = next(line for line in page.splitlines() if 'data-provider="vercel"' in line)
    assert "Only a publicly price-verified zero-price route is retained" in row
    for surface in (guidance, row, env):
        normalized = " ".join(surface.split()).casefold()
        assert re.search(r"\bcompletion[- ]verified\b", normalized) is None
        assert re.search(r"\bcredentialed (?:completion|canary) (?:succeeded|passed|was successful)\b", normalized) is None


def test_pages_describe_current_main_agent_and_operations_surfaces() -> None:
    index = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    agents = (ROOT / "docs" / "run-coding-agents-on-free-models.html").read_text(
        encoding="utf-8"
    )
    opencode = (ROOT / "docs" / "run-opencode-on-free-models.html").read_text(
        encoding="utf-8"
    )
    llms = (ROOT / "docs" / "llms.txt").read_text(encoding="utf-8")

    for marker in ("Hermes", "/livez", "/readyz", "/v1/providers", "/v1/models?ready=true"):
        assert marker in index
        assert marker in agents

    for text in (index, opencode, llms):
        assert "freellmpool/agent" in text
        assert "spread" in text

    for text in (index, opencode):
        assert "Registry publication status: pending" in text
        assert "opencode-freellmpool" in text
        assert "opencode-freellmpool-tui" in text


def test_pages_dates_match_current_documentation_pass() -> None:
    sitemap = (ROOT / "docs" / "sitemap.xml").read_text(encoding="utf-8")
    index = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    agent_guide = (ROOT / "docs" / "run-coding-agents-on-free-models.html").read_text(
        encoding="utf-8"
    )
    opencode_guide = (ROOT / "docs" / "run-opencode-on-free-models.html").read_text(
        encoding="utf-8"
    )
    assert "Page updated 2026-08-29" in index
    assert "Updated 2026-08-29" in agent_guide
    assert "Updated 2026-08-29" in opencode_guide
    assert (
        "<loc>https://0xzr.github.io/freellmpool/</loc>"
        "<lastmod>2026-08-29</lastmod>"
    ) in sitemap
    assert (
        "<loc>https://0xzr.github.io/freellmpool/run-coding-agents-on-free-models.html</loc>"
        "<lastmod>2026-08-29</lastmod>"
    ) in sitemap
    assert (
        "<loc>https://0xzr.github.io/freellmpool/run-opencode-on-free-models.html</loc>"
        "<lastmod>2026-08-29</lastmod>"
    ) in sitemap
    assert (
        "<loc>https://0xzr.github.io/freellmpool/free-llm-api-providers-list.html</loc>"
        "<lastmod>2026-08-29</lastmod>"
    ) in sitemap


def test_every_sitemap_lastmod_is_visible_on_its_page() -> None:
    sitemap_path = ROOT / "docs" / "sitemap.xml"
    root = ElementTree.fromstring(sitemap_path.read_text(encoding="utf-8"))
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    base = "https://0xzr.github.io/freellmpool/"

    for entry in root.findall("s:url", namespace):
        location = entry.findtext("s:loc", namespaces=namespace)
        lastmod = entry.findtext("s:lastmod", namespaces=namespace)
        assert location is not None and location.startswith(base)
        assert lastmod is not None
        relative = location.removeprefix(base)
        page = ROOT / "docs" / (relative or "index.html")
        assert lastmod in page.read_text(encoding="utf-8"), page


def test_compose_matches_catalog_credentials_and_persists_runtime_state() -> None:
    catalog = tomllib.loads((ROOT / "src" / "freellmpool" / "providers.toml").read_text())
    credential_names = {
        row["key_env"]
        for section in ("provider", "embedder", "transcriber")
        for row in catalog.get(section, [])
        if row.get("key_env")
    }
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    for name in credential_names:
        assert f"{name}: ${{{name}:-}}" in compose
    for retired in ("GITHUB_TOKEN", "LONGCAT_API_KEY"):
        assert retired not in compose
    assert "freellmpool-data:/home/freellmpool/.config/freellmpool" in compose
    assert "condition: service_healthy" in compose


def test_pages_expose_search_and_llm_discovery_metadata() -> None:
    index = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="index,follow,max-image-preview:large">' in index
    assert '<link rel="alternate" type="text/plain" href="llms.txt"' in index
    assert '<meta name="twitter:title"' in index
    assert '"@type": "WebSite"' in index


def test_roadmap_reflects_kimi_m3_addendum() -> None:
    roadmap = (ROOT / "docs/ROADMAP.md").read_text(encoding="utf-8")
    assert "Top 10 feature map" in roadmap
    assert "Kimi/M3 Top-10 Planning Addendum" in roadmap
    assert "PYTHONPATH=src" in roadmap
    assert "No rate-limit bypass" in roadmap


def test_pypi_metadata_has_launch_surfaces() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]
    discovery = (ROOT / "docs/GITHUB_DISCOVERY.md").read_text(encoding="utf-8")
    promotion = (ROOT / "docs/promotion/README.md").read_text(encoding="utf-8")
    counts = catalog_counts(ROOT)

    assert len(project["description"]) <= 120
    assert f"> {project['description']}" in discovery
    assert (
        f"Released catalog: {counts.providers} cataloged providers, {counts.enabled_chat_models} enabled chat routes, {counts.cataloged_chat_models}"
        in promotion
    )

    urls = project["urls"]
    for name in ("Docs", "Changelog", "Issues", "Repository"):
        assert name in urls

    for keyword in (
        "anthropic",
        "claude",
        "cursor",
        "mcp",
        "model-context-protocol",
        "rate-limiting",
        "speech-to-text",
    ):
        assert keyword in project["keywords"]

    for classifier in (
        "Framework :: AsyncIO",
        "Operating System :: OS Independent",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ):
        assert classifier in project["classifiers"]

    dev_deps = project["optional-dependencies"]["dev"]
    assert any(dep.startswith("build>=") for dep in dev_deps)
    assert "twine==7.0.0" in dev_deps
