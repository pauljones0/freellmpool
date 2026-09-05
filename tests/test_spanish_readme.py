from __future__ import annotations

from pathlib import Path

from scripts.catalog_counts import catalog_counts

ROOT = Path(__file__).resolve().parents[1]

FEATURED_URLS = (
    "https://www.youtube.com/watch?v=1UfIlWoedho",
    "https://www.youtube.com/watch?v=oaM_E92WVGQ",
    "https://mcpmarket.com/server/freellm-pool",
)


def test_historical_featured_section_remains_in_the_archived_guide():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/legacy-0.13-guide.md" in readme
    legacy = (ROOT / "docs/legacy-0.13-guide.md").read_text(encoding="utf-8")
    section = legacy.split("## Featured in", 1)[1].split("## Contributing", 1)[0]
    lines = [line for line in section.splitlines() if line.strip()]

    assert len(lines) <= 3
    for url in FEATURED_URLS:
        assert url in section


def test_spanish_readme_tracks_current_launch_surface():
    spanish = (ROOT / "README.es.md").read_text(encoding="utf-8")
    counts = catalog_counts(ROOT)

    assert "Puede quedar por detrás" in spanish
    assert "![demostración de freellmpool tokenmax en terminal](assets/demo.svg)" in spanish
    assert f"{counts.providers} proveedores" in spanish
    assert f"{counts.enabled_chat_models} rutas de chat" in spanish
    assert f"{counts.cataloged_chat_models} modelos de chat" in spanish
    assert "Última versión: 0.13.0" in spanish
    assert "main contiene cambios aún no publicados" not in spanish
    assert "0.11.4" not in spanish
    assert "Estado de publicación en npm: pendiente" in spanish
    assert "opencode-freellmpool" in spanish
    assert "opencode-freellmpool-tui" in spanish
    assert "freellmpool/spread" in spanish
    assert "hermes" in spanish
    assert "[plugins/llm-freellmpool](plugins/llm-freellmpool/)" in spanish
    assert "https://github.com/0xzr/llm-freellmpool" not in spanish
    assert "solo en main" not in spanish
    assert "ruta experimental compatible con Anthropic" in spanish
    assert "habla tanto la API de OpenAI como la de Anthropic" not in spanish

    for heading in (
        "## Inicio rápido en 30 segundos",
        "## Ejecuta un agente de código con modelos gratuitos",
        "## Como proxy",
        "## Como biblioteca",
        "## Capacidad y salud de proveedores",
        "## Cómo se compara",
        "## Preguntas frecuentes",
        "## Destacado en",
    ):
        assert heading in spanish

    for url in FEATURED_URLS:
        assert url in spanish
