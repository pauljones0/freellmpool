from __future__ import annotations

import re
import tomllib
import xml.dom.minidom
from pathlib import Path

from freellmpool.config import load_catalog
from scripts.catalog_counts import catalog_counts

ROOT = Path(__file__).resolve().parents[1]


def test_faq_lists_every_builtin_chat_provider():
    with (ROOT / "src/freellmpool/providers.toml").open("rb") as fh:
        providers = tomllib.load(fh)["provider"]

    faq = (ROOT / "FAQ.md").read_text(encoding="utf-8")

    assert providers
    for provider in providers:
        assert f"`{provider['id']}`" in faq


def test_readme_links_faq_prominently():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    first_screen = readme.split("## Install and set up", 1)[0]

    assert "[FAQ](FAQ.md)" in first_screen


def test_archived_comparison_table_preserves_its_dated_sources():
    readme = (ROOT / "docs/legacy-0.13-guide.md").read_text(encoding="utf-8")
    section = readme.split("## How it compares", 1)[1].split("## FAQ", 1)[0]

    for column in (
        "Keyless start",
        "# providers",
        "Failover",
        "MCP server",
        "CLI",
        "Transcription",
        "Local/self-hosted",
        "License",
    ):
        assert column in section

    for row in ("**freellmpool**", "OpenRouter free models", "LiteLLM", "FreeLLMAPI"):
        assert row in section

    assert "FreeLLMAPI predates this project" in section
    assert "independent convergence" in section


def test_readme_opens_with_the_current_setup_and_labels_legacy_demos():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "sh integrations/setup/bootstrap.sh" in readme
    assert "freellmpool setup --resume" in readme
    assert "docs/legacy-0.13-guide.md" in readme
    assert "## 30-second quickstart" not in readme
    archive = (ROOT / "docs/legacy-0.13-guide.md").read_text(encoding="utf-8")
    assert "assets/demo.svg" in archive
    assert "assets/tokenmax-results.svg" in archive


def test_demo_assets_are_well_formed_and_current():
    counts = catalog_counts(ROOT)
    demo = (ROOT / "assets/demo.svg").read_text(encoding="utf-8")
    results = (ROOT / "assets/tokenmax-results.svg").read_text(encoding="utf-8")

    xml.dom.minidom.parseString(demo)
    xml.dom.minidom.parseString(results)
    assert "TOKENMAXXING" in demo
    assert "--animation-duration: 8500ms" in demo
    assert "installed from current checkout" in demo.lower()
    assert f"{counts.providers} cataloged providers, {counts.enabled_chat_models} enabled chat routes" in demo
    assert "current checkout catalog" in demo.lower()
    assert "keyless start when available" in demo
    assert f">{counts.enabled_chat_models}</text>" in results
    assert f"{counts.cataloged_chat_models} cataloged" in results
    assert "cataloged providers" in results
    assert "$0" in results

    provider_ids = {provider.id for provider in load_catalog()}
    displayed_routes = {
        value
        for value in re.findall(
            r'<tspan class="(?:cyan|purple|yellow|green)">([^<]+/[^<]+)</tspan>', demo
        )
        if value.split("/", 1)[0] in provider_ids
    }
    automatic_routes = {
        f"{provider.id}/{model.name}"
        for provider in load_catalog()
        for model in provider.models
        if model.enabled and model.auto
    }
    assert displayed_routes
    assert displayed_routes <= automatic_routes

    assert (ROOT / "docs/assets/demo.png").read_bytes() == (
        ROOT / "assets/demo.png"
    ).read_bytes()
