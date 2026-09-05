"""Offline regression tests for enabled-default health probes and billing errors.

Run with ~/.local/share/uv/tools/freellmpool/bin/python <this file>.
Discovery and completion calls are mocked; no credentials or network are used.
"""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from freellmpool.errors import ProviderHTTPError
from freellmpool.metrics import Metrics
from freellmpool.models import Model, Provider, Reply

bench = importlib.import_module("freellmpool.benchmark")
health = importlib.import_module("freellmpool.healthcheck")


class HealthSelectionRegression(unittest.TestCase):
    def setUp(self):
        self.discovery_patch = patch.object(bench, "discover_openai_models", return_value=[])
        self.discovery = self.discovery_patch.start()
        self.addCleanup(self.discovery_patch.stop)
        self.call_patch = patch.object(bench._client, "call", side_effect=self.reply)
        self.call = self.call_patch.start()
        self.addCleanup(self.call_patch.stop)

    @staticmethod
    def reply(provider, name, messages, **kwargs):
        return Reply("ok", provider.id, name, {}, completion_tokens=1)

    def pool(self, *models, adapter="openai"):
        provider = Provider("test", "Test", adapter, "https://invalid.example/v1", tuple(models))
        return SimpleNamespace(providers=[provider], env={}, _post=None, metrics=Metrics())

    def selected(self, pool, *, model=None):
        rows = bench.benchmark(pool, model=model)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].ok, rows[0].error)
        self.assertEqual(self.call.call_count, 1)
        return self.call.call_args.args[1]

    def test_default_omitted_free_model_does_not_probe_discovered_paid_model(self):
        pool = self.pool(Model("glm-4.5", enabled=False), Model("glm-4.7-flash"))
        self.discovery.return_value = ["glm-4.5", "unconfigured-paid-model"]
        self.assertEqual(self.selected(pool), "glm-4.7-flash")

    def test_default_prefers_enabled_discovered_model(self):
        pool = self.pool(Model("retired"), Model("paid", enabled=False), Model("free-listed"))
        self.discovery.return_value = ["paid", "free-listed", "unconfigured"]
        self.assertEqual(self.selected(pool), "free-listed")

    def test_no_discovery_uses_enabled_model(self):
        pool = self.pool(Model("paid", enabled=False), Model("free"))
        self.discovery.side_effect = ValueError("discovery unavailable")
        self.assertEqual(self.selected(pool), "free")

    def test_empty_discovery_uses_enabled_model(self):
        self.assertEqual(self.selected(self.pool(Model("free"))), "free")

    def test_aggregator_provider_suffix_matches_discovered_base(self):
        pool = self.pool(Model("retired"), Model("org/free:upstream"))
        self.discovery.return_value = ["org/free"]
        self.assertEqual(self.selected(pool), "org/free:upstream")

    def test_explicit_configured_pin_works_despite_discovery_omission(self):
        self.discovery.return_value = ["paid"]
        pool = self.pool(Model("glm-4.7-flash"))
        self.assertEqual(self.selected(pool, model="glm-4.7-flash"), "glm-4.7-flash")
        self.discovery.assert_not_called()

    def test_explicit_disabled_pin_preserves_intentional_opt_in(self):
        pool = self.pool(Model("disabled", enabled=False))
        self.assertEqual(self.selected(pool, model="disabled"), "disabled")

    def test_explicit_unconfigured_discovered_pin_preserves_opt_in(self):
        self.discovery.return_value = ["new-free"]
        self.assertEqual(self.selected(self.pool(), model="new-free"), "new-free")

    def test_explicit_unconfigured_aggregator_pin_keeps_suffix_match(self):
        self.discovery.return_value = ["org/new-free"]
        self.assertEqual(self.selected(self.pool(), model="org/new-free:provider"), "org/new-free:provider")

    def test_explicit_unconfigured_absent_pin_skips_inference(self):
        self.discovery.return_value = ["some-other-model"]
        rows = health.run_healthcheck(self.pool(), model="unknown")
        self.assertEqual(rows[0].status, "skipped")
        self.assertIn("model not listed", rows[0].note)
        self.call.assert_not_called()

    def test_explicit_unconfigured_pin_preserves_missing_discovery_behavior(self):
        self.discovery.side_effect = ValueError("unavailable")
        self.assertEqual(self.selected(self.pool(), model="intentional"), "intentional")

    def test_all_disabled_default_skips_without_any_network_calls(self):
        pool = self.pool(Model("paid", enabled=False))
        self.discovery.return_value = ["paid", "other-paid"]
        rows = health.run_healthcheck(pool)
        self.assertEqual(rows[0].target, "test")
        self.assertEqual(rows[0].status, "skipped")
        self.assertIn("no enabled configured models", rows[0].note)
        self.assertEqual(pool.metrics.snapshot(), {})
        self.assertIn("SKIP", bench.render_table(bench.benchmark(pool)))
        self.discovery.assert_not_called()
        self.call.assert_not_called()

    def test_empty_default_catalog_skips_without_network_calls(self):
        rows = health.run_healthcheck(self.pool())
        self.assertEqual(rows[0].status, "skipped")
        self.discovery.assert_not_called()
        self.call.assert_not_called()

    def test_auto_false_enabled_model_remains_health_testable(self):
        self.assertEqual(self.selected(self.pool(Model("manual", auto=False))), "manual")

    def test_non_openai_disabled_default_skips(self):
        rows = health.run_healthcheck(self.pool(Model("paid", enabled=False), adapter="gemini"))
        self.assertEqual(rows[0].status, "skipped")
        self.discovery.assert_not_called()
        self.call.assert_not_called()

    def test_health_preserves_64_token_reasoning_canary(self):
        rows = health.run_healthcheck(self.pool(Model("free")))
        self.assertEqual(rows[0].status, "ok")
        self.assertEqual(self.call.call_args.kwargs["max_tokens"], 64)

    def test_billing_429_uses_full_error_beyond_short_note(self):
        message = '{"request_id":"' + "x" * 100 + '","error":{"code":"1113",' \
            '"message":"Insufficient balance or no resource package. Please recharge."}}'
        self.call.side_effect = ProviderHTTPError(429, message, retryable=True)
        rows = health.run_healthcheck(self.pool(Model("free")))
        self.assertEqual(rows[0].status, "billing_blocked")
        self.assertNotIn("balance", rows[0].note)
        self.assertEqual(len(rows[0].note), 80)

    def test_429_resource_package_and_402_are_billing_blocked(self):
        for error in ("HTTP 429: No resource package", "HTTP 402: payment required"):
            with self.subTest(error=error):
                self.assertEqual(health._failure_status(error), "billing_blocked")

    def test_actual_rate_limit_remains_rate_limited(self):
        self.call.side_effect = ProviderHTTPError(429, "Rate limit reached for requests", retryable=True)
        rows = health.run_healthcheck(self.pool(Model("free")))
        self.assertEqual(rows[0].status, "rate_limited")

    def test_unrelated_429_text_is_not_a_rate_limit(self):
        self.assertEqual(health._failure_status("ValueError: unexpected model 429"), "fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)
