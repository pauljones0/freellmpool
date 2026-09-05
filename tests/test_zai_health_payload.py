"""Offline health-only payload tests; real dispatch with an injected transport."""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from freellmpool.client import HTTPResult
from freellmpool.metrics import Metrics
from freellmpool.models import Model, Provider

bench = importlib.import_module("freellmpool.benchmark")
health = importlib.import_module("freellmpool.healthcheck")
FREE_FLASH = ("glm-4.5-flash", "glm-4.7-flash", "glm-4.6v-flash")


class ZaiHealthThinkingRegression(unittest.TestCase):
    def setUp(self):
        self.requests = []
        discovery = patch.object(bench, "discover_openai_models", return_value=[])
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)

    def post(self, url, headers, body, timeout):
        self.requests.append(body)
        return HTTPResult(200, {"choices": [{"message": {"content": "ok"}}]}, "")

    def pool(self, model, *, provider_id="zhipu", enabled=True):
        provider = Provider(
            provider_id, "Offline", "openai", "https://invalid.example/v1",
            (Model(model, enabled=enabled),),
        )
        return SimpleNamespace(providers=[provider], env={}, _post=self.post, metrics=Metrics())

    def test_health_disables_thinking_and_preserves_64_budget_for_each_free_flash(self):
        for model in FREE_FLASH:
            with self.subTest(model=model):
                rows = health.run_healthcheck(self.pool(model))
                self.assertEqual(rows[0].status, "ok")
                self.assertEqual(self.requests[-1]["model"], model)
                self.assertEqual(self.requests[-1]["max_tokens"], 64)
                self.assertEqual(self.requests[-1]["thinking"], {"type": "disabled"})

    def test_ordinary_benchmark_retains_thinking_headroom_for_all_free_flash(self):
        for model in FREE_FLASH:
            with self.subTest(model=model):
                self.assertTrue(bench.benchmark(self.pool(model), max_tokens=64)[0].ok)
                self.assertEqual(self.requests[-1]["max_tokens"], 4096)
                self.assertNotIn("thinking", self.requests[-1])

    def test_other_provider_model_with_same_name_is_untouched(self):
        health.run_healthcheck(self.pool("glm-4.5-flash", provider_id="other"))
        self.assertEqual(self.requests[-1]["max_tokens"], 4096)
        self.assertNotIn("thinking", self.requests[-1])

    def test_other_zai_models_are_untouched(self):
        health.run_healthcheck(self.pool("plain-model"))
        self.assertEqual(self.requests[-1]["max_tokens"], 64)
        self.assertNotIn("thinking", self.requests[-1])

    def test_health_does_not_mutate_transport_or_following_benchmark(self):
        pool = self.pool("glm-4.5-flash")
        transport = pool._post
        health.run_healthcheck(pool)
        self.assertIs(pool._post, transport)
        bench.benchmark(pool, max_tokens=64)
        self.assertIs(pool._post, transport)
        self.assertEqual(self.requests[0]["max_tokens"], 64)
        self.assertEqual(self.requests[0]["thinking"], {"type": "disabled"})
        self.assertEqual(self.requests[1]["max_tokens"], 4096)
        self.assertNotIn("thinking", self.requests[1])

    def test_paid_discovery_does_not_change_free_selection(self):
        self.discovery.return_value = ["glm-4.5", "unconfigured-paid-model"]
        health.run_healthcheck(self.pool("glm-4.5-flash"))
        self.assertEqual(self.requests[-1]["model"], "glm-4.5-flash")

    def test_no_enabled_model_does_not_make_any_request(self):
        rows = health.run_healthcheck(self.pool("glm-4.5", enabled=False))
        self.assertEqual(rows[0].status, "skipped")
        self.assertEqual(self.requests, [])
        self.discovery.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
