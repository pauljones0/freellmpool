"""Catalog loading + configured-provider filtering."""

from __future__ import annotations

from pathlib import Path

from freellmpool.config import (
    configured_providers,
    known_aliases,
    load_catalog,
    load_embedders,
    load_transcribers,
    resolve_alias,
)


def test_alias_default_maps_to_auto():
    assert resolve_alias("gpt-4o-mini", {}) == "auto"
    assert resolve_alias("claude-3-5-sonnet-latest", {}) == "auto"


def test_alias_unknown_passthrough():
    assert resolve_alias("groq/llama-3.1-8b-instant", {}) == "groq/llama-3.1-8b-instant"
    assert resolve_alias("auto", {}) == "auto"


def test_alias_env_override():
    env = {"FREELLMPOOL_ALIAS_GPT_4O_MINI": "groq/llama-3.3-70b-versatile"}
    assert resolve_alias("gpt-4o-mini", env) == "groq/llama-3.3-70b-versatile"


def test_known_aliases_include_env_alias():
    env = {"FREELLMPOOL_ALIAS_MY_MODEL": "groq/llama-3.3-70b-versatile"}
    assert "MY_MODEL" in known_aliases(env)


def test_packaged_catalog_loads():
    catalog = load_catalog()
    ids = {p.id for p in catalog}
    assert {"groq", "openrouter", "gemini"} <= ids
    for p in catalog:
        assert p.models  # every provider ships at least one model
        assert p.base_url.startswith("https://")


def test_packaged_models_are_available_and_have_a_reviewed_free_provider():
    from freellmpool.free_policy import _matches
    from freellmpool.provider_registry import load_registry

    registry = load_registry()
    for providers, modality in ((load_catalog(), "chat"), (load_embedders(), "embedding"), (load_transcribers(), "transcription")):
        for provider in providers:
            assert provider.models
            assert all(model.enabled for model in provider.models)
            assert provider.id in registry
            assert all(grant["hard_free_boundary"] is True for grant in registry[provider.id]["grants"])
            for model in provider.models:
                assert any(
                    modality in grant["allowed_modalities"]
                    and _matches(grant["model_selector"], {"id": model.name})
                    for grant in registry[provider.id]["grants"]
                ), f"Bundled route lacks reviewed free coverage: {provider.id}/{model.name} ({modality})"


def test_static_context_and_free_gateway_identity_remain_precise():
    providers = {provider.id: provider for provider in load_catalog()}
    assert providers["cloudflare"].model("@cf/qwen/qwen3.8-27b").context == 262_144
    assert {model.name for model in providers["llm7"].models} == {"codestral-latest"}
    assert {model.name for model in providers["vercel"].models} == {"poolside/laguna-s-2.1-free"}
    assert providers["vercel"].is_configured({"AI_GATEWAY_API_KEY": "free-route-key"})


def test_packaged_catalog_omits_retired_github_models():
    assert "github" not in {provider.id for provider in load_catalog()}
    assert "github" not in {embedder.id for embedder in load_embedders()}


def test_keyless_providers_always_configured():
    # OVH (auth=none) and LLM7 (key_optional) are usable with an empty env.
    catalog = load_catalog()
    ids = {p.id for p in configured_providers(catalog, {})}
    assert "ovh" in ids  # keyless
    assert "llm7" in ids  # key optional
    assert "groq" not in ids  # needs a key


def test_unknown_provider_does_not_appear_without_user_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("FREELLMPOOL_CONFIG", str(tmp_path / "absent.toml"))
    assert "unknown-fixture-provider" not in {provider.id for provider in load_catalog()}


def test_env_example_documents_keyless_providers():
    """Verify .env.example lists all default-enabled keyless/key-optional providers."""
    catalog = load_catalog()
    default_enabled_keyless_ids = {
        p.id for p in catalog if p.keyless and any(model.enabled for model in p.models)
    }
    disabled_keyless_ids = {
        p.id for p in catalog if p.keyless and not any(model.enabled for model in p.models)
    }

    env_content = (Path(__file__).parent.parent / ".env.example").read_text()
    start = env_content.find("# Zero-setup providers")
    end = env_content.find("# So freellmpool works")
    zero_setup_section = env_content[start:end]
    zero_setup_lower = zero_setup_section.lower()

    for provider_id in default_enabled_keyless_ids:
        assert provider_id.lower() in zero_setup_lower, (
            f"Keyless provider '{provider_id}' must be documented in .env.example zero-setup section"
        )
    for provider_id in disabled_keyless_ids:
        assert provider_id.lower() not in zero_setup_lower, (
            f"Disabled keyless provider '{provider_id}' must not be documented as zero-setup"
        )


def test_configured_filter_by_env():
    catalog = load_catalog()
    ids = {p.id for p in configured_providers(catalog, {"GROQ_API_KEY": "x"})}
    assert "groq" in ids
    assert "gemini" not in ids  # no key → excluded
    assert "ovh" in ids  # keyless → always present


def test_cloudflare_requires_extra_env():
    catalog = load_catalog()
    # token alone is not enough; account id is also required
    with_token = {p.id for p in configured_providers(catalog, {"CLOUDFLARE_API_TOKEN": "t"})}
    assert "cloudflare" not in with_token
    with_both = {
        p.id
        for p in configured_providers(
            catalog, {"CLOUDFLARE_API_TOKEN": "t", "CLOUDFLARE_ACCOUNT_ID": "acc"}
        )
    }
    assert "cloudflare" in with_both


def test_user_override(tmp_path):
    override = tmp_path / "providers.toml"
    override.write_text(
        "[[provider]]\n"
        'id = "groq"\n'
        'label = "My Groq"\n'
        'adapter = "openai"\n'
        'base_url = "https://example.test/v1"\n'
        'key_env = "GROQ_API_KEY"\n'
        'models = [{ name = "custom-model", rpd = 42 }]\n'
    )
    catalog = load_catalog(path=override)
    groq = next(p for p in catalog if p.id == "groq")
    assert groq.label == "My Groq"
    assert groq.models[0].name == "custom-model"


def test_split_provider_model_guards_against_slash_model_names():
    from freellmpool.config import split_provider_model

    pids = {"groq", "fixture-provider", "kilo", "openrouter"}
    # real provider prefix → split
    assert split_provider_model("groq/llama-3.1-8b", pids) == (["groq"], "llama-3.1-8b")
    # slash-bearing model on a real provider → only first slash is the provider boundary
    assert split_provider_model("fixture-provider/Qwen/Qwen3-Coder-30B-A3B-Instruct", pids) == (
        ["fixture-provider"],
        "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    )
    # bare slash-model (no valid provider prefix) → kept whole, NOT mis-split into "Qwen"
    assert split_provider_model("Qwen/Qwen3-Coder-30B-A3B-Instruct", pids) == (
        None,
        "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    )
    assert split_provider_model("deepseek-ai/DeepSeek-R1", pids) == (None, "deepseek-ai/DeepSeek-R1")
    # no slash, or no provider set → unchanged
    assert split_provider_model("gpt-4o-mini", pids) == (None, "gpt-4o-mini")
    assert split_provider_model("groq/x", None) == (None, "groq/x")


def test_custom_provider_rejects_unsafe_environment_variable_names():
    from freellmpool.config import _parse_rows

    def row(provider_id, **fields):
        return {
            "id": provider_id,
            "base_url": "https://provider.example/v1",
            "models": [{"name": "model"}],
            **fields,
        }

    providers = _parse_rows(
        [
            row("valid", key_env="PROVIDER_KEY", extra_env=["ACCOUNT_ID"]),
            row("newline", key_env="BAD\nKEY"),
            row("space", extra_env=["BAD NAME"]),
            row("scalar-extra", extra_env="ACCOUNT_ID"),
        ]
    )

    assert [provider.id for provider in providers] == ["valid"]
    assert providers[0].key_env == "PROVIDER_KEY"
    assert providers[0].extra_env == ("ACCOUNT_ID",)


def test_catalog_toml_is_parsed_once_across_surfaces(tmp_path, monkeypatch):
    import freellmpool.config as config_module

    path = tmp_path / "all.toml"
    path.write_text(
        "[[provider]]\n"
        'id = "chat"\nbase_url = "https://chat.test/v1"\n'
        'models = [{ name = "chat-model" }]\n'
        "[[embedder]]\n"
        'id = "embed"\nbase_url = "https://embed.test/v1"\n'
        'models = [{ name = "embed-model" }]\n'
        "[[transcriber]]\n"
        'id = "audio"\nbase_url = "https://audio.test/v1"\n'
        'models = [{ name = "audio-model" }]\n'
    )
    original = config_module.tomllib.load
    calls = 0

    def counted(handle):
        nonlocal calls
        calls += 1
        return original(handle)

    monkeypatch.setattr(config_module.tomllib, "load", counted)

    assert load_catalog(path)[0].id == "chat"
    assert load_embedders(path)[0].id == "embed"
    assert config_module.load_transcribers(path)[0].id == "audio"
    assert calls == 1

    first = load_catalog(path)
    first.clear()
    assert load_catalog(path)[0].id == "chat"


def test_local_catalog_marker_only_accepts_canonical_literal_loopback(monkeypatch):
    from freellmpool.config import _parse_rows

    monkeypatch.delenv("FREELLMPOOL_ALLOW_LOCAL_PROVIDERS", raising=False)

    def row(provider_id, base_url):
        return {
            "id": provider_id,
            "base_url": base_url,
            "local": True,
            "models": [{"name": "model"}],
        }

    parsed = _parse_rows(
        [
            row("v4", "http://127.0.0.2:11434/v1"),
            row("v6", "http://[::1]:1234/v1"),
            row("lan", "http://192.168.1.2:11434/v1"),
            row("localhost", "http://localhost:11434/v1"),
            row("short", "http://127.1:11434/v1"),
            row("mapped", "http://[::ffff:127.0.0.1]:11434/v1"),
            row("public", "https://api.example.test/v1"),
        ]
    )

    assert [provider.id for provider in parsed] == ["v4", "v6"]


def test_catalog_cache_invalidates_on_local_opt_in_and_same_size_replace(
    tmp_path, monkeypatch
):
    import os

    path = tmp_path / "providers.toml"
    first = (
        "[[provider]]\n"
        'id = "local"\nbase_url = "http://192.168.1.2:1234/v1"\n'
        'models = [{ name = "model-a" }]\n'
    )
    second = first.replace("model-a", "model-b")
    path.write_text(first)
    original_stat = path.stat()

    monkeypatch.delenv("FREELLMPOOL_ALLOW_LOCAL_PROVIDERS", raising=False)
    assert load_catalog(path) == []
    monkeypatch.setenv("FREELLMPOOL_ALLOW_LOCAL_PROVIDERS", "1")
    assert load_catalog(path)[0].models[0].name == "model-a"

    path.write_text(second)
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert load_catalog(path)[0].models[0].name == "model-b"
