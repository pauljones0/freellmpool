"""The benchmark sweep: per-provider timing, sorting, and metrics capture."""

from __future__ import annotations

from helpers import gemini_body, make_post

from freellmpool.benchmark import benchmark, render_table
from freellmpool.router import Pool


def test_benchmark_reports_each_provider(providers, env, quota):
    # beta fails (500); gee needs a gemini-shaped body to succeed.
    post = make_post({"beta.test": (500, {"error": "boom"}), "gee.test": (200, gemini_body("ok"))})
    pool = Pool(providers, quota=quota, env=env, post=post)
    rows = benchmark(pool, timeout=5.0)
    by_target = {r.target.split("/")[0]: r for r in rows}
    assert by_target["alpha"].ok is True
    assert by_target["beta"].ok is False
    assert by_target["gee"].ok is True
    # successes are sorted ahead of failures
    ok_flags = [r.ok for r in rows]
    assert ok_flags == sorted(ok_flags, reverse=True)


def test_benchmark_feeds_metrics(providers, env, quota):
    pool = Pool(
        providers, quota=quota, env=env, post=make_post({"gee.test": (200, gemini_body("ok"))})
    )
    benchmark(pool, timeout=5.0)
    # alpha's first enabled model was measured
    assert pool.metrics.get("alpha/alpha-small").ok == 1


def test_benchmark_provider_filter(providers, env, quota):
    pool = Pool(providers, quota=quota, env=env, post=make_post({}))
    rows = benchmark(pool, providers=["alpha"], timeout=5.0)
    assert {r.target.split("/")[0] for r in rows} == {"alpha"}


def test_render_table_empty():
    assert "No configured providers" in render_table([])


def test_render_table_has_header_and_rows(providers, env, quota):
    pool = Pool(
        providers, quota=quota, env=env, post=make_post({"gee.test": (200, gemini_body("ok"))})
    )
    out = render_table(benchmark(pool, timeout=5.0))
    assert "provider/model" in out
    assert "providers responded" in out
