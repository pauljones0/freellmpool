"""Offline request-payload regressions for free Flash reasoning headroom."""

import asyncio
import unittest
from types import SimpleNamespace

from freellmpool import client
from freellmpool.aio import AsyncPool
from freellmpool.models import Model, Provider

FLASH_IDS = ("glm-4.5-flash", "glm-4.6v-flash")


class FlashHeadroomRegression(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.provider = Provider(
            "offline", "Offline", "openai", "https://invalid.example/v1",
            tuple(Model(model) for model in FLASH_IDS),
        )

    def post(self, url, headers, body, timeout):
        self.requests.append(body)
        return client.HTTPResult(200, {"choices": [{"message": {"content": "ok"}}]}, "")

    def dispatch(self, model, **kwargs):
        return client.call(
            self.provider, model, [{"role": "user", "content": "Say ok"}],
            api_key=None, env={}, max_tokens=kwargs.pop("max_tokens", 64),
            post=self.post, **kwargs,
        )

    def test_sync_free_flash_requests_have_reasoning_headroom(self):
        for model in FLASH_IDS:
            with self.subTest(model=model):
                self.assertEqual(self.dispatch(model).text, "ok")
                self.assertEqual(self.requests[-1]["max_tokens"], 4096)
                self.assertEqual(self.requests[-1]["model"], model)

    def test_strict_probe_opt_out_preserves_budget(self):
        for model in FLASH_IDS:
            with self.subTest(model=model):
                self.dispatch(model, enforce_thinking_floor=False)
                self.assertEqual(self.requests[-1]["max_tokens"], 64)

    def test_other_model_retains_requested_budget(self):
        self.dispatch("plain-chat-model")
        self.assertEqual(self.requests[-1]["max_tokens"], 64)

    def test_above_floor_budget_is_preserved(self):
        for model in FLASH_IDS:
            with self.subTest(model=model):
                self.dispatch(model, max_tokens=8192)
                self.assertEqual(self.requests[-1]["max_tokens"], 8192)

    def test_existing_glm_47_behavior_is_preserved(self):
        self.dispatch("glm-4.7-flash")
        self.assertEqual(self.requests[-1]["max_tokens"], 4096)

    def test_async_dispatch_uses_shared_reasoning_hints(self):
        async def apost(url, headers, body, timeout):
            return self.post(url, headers, body, timeout)

        async def run():
            pool = AsyncPool(SimpleNamespace(env={}), apost=apost)
            for model in (*FLASH_IDS, "plain-chat-model"):
                reply = await pool._acall(
                    self.provider, model, [{"role": "user", "content": "Say ok"}],
                    max_tokens=64, temperature=0.0, timeout=1.0,
                    tools=None, tool_choice=None, response_format=None,
                )
                self.assertEqual(reply.text, "ok")
                self.assertEqual(self.requests[-1]["max_tokens"], 4096 if model in FLASH_IDS else 64)
            self.assertIsNone(pool._aclient, "Offline transport must not create a network client")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
