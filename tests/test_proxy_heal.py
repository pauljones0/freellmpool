"""G33 proxy demand-driven heal (spike v2).

Terminal tools-429s record demand ticks (memory-only); an AUTOHEAL-gated
out-of-band executor heals when demand passes threshold. All tests
offline; tick/request paths verified side-effect-free.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from freellmpool import heal as h
from freellmpool.errors import AllProvidersExhausted


def exhausted_429() -> AllProvidersExhausted:
    return AllProvidersExhausted(
        attempts=[("groq:probe-free", "429")], client_status=429,
        client_message="all providers exhausted", retry_after=None,
        upstream_status=429)


def tick_env(tmp_path: Path, autoheal: bool = True) -> dict[str, str]:
    env = {"FREELLMPOOL_HEAL_TICKS_PATH": str(tmp_path / "ticks.json"),
           "FREELLMPOOL_HEAL_PATH": str(tmp_path / "heal.json")}
    if autoheal:
        env["FREELLMPOOL_AUTOHEAL"] = "1"
    return env


# --- T1: tick rule ---

@pytest.mark.parametrize(("had_tools", "status", "want"), [
    (True, 429, True),
    (True, 502, False),
    (True, 400, False),
    (True, None, False),
    (False, 429, False),
    (False, 502, False),
])
def test_should_tick_matrix(had_tools, status, want):
    exc = AllProvidersExhausted(
        attempts=[("groq:probe-free", "x")], client_status=status,
        client_message="m", retry_after=None, upstream_status=status)
    assert h._should_tick(had_tools, exc) is want


def test_should_tick_ignores_retry_after():
    exc = exhausted_429()
    exc.retry_after = 900.0
    assert h._should_tick(True, exc) is True
    assert h._should_tick(False, exc) is False


# --- T2: store ---

def test_store_coalesces_in_window(tmp_path):
    ticks = h.TickStore(h.default_ticks_path(tick_env(tmp_path)))
    assert ticks.demand(now=1000.0) is False
    for _ in range(4):
        ticks.record(now=1000.0)
    assert ticks.demand(now=1000.0) is False
    ticks.record(now=1000.0)
    assert ticks.demand(now=1000.0) is True


def test_store_window_rollover_resets(tmp_path):
    ticks = h.TickStore(h.default_ticks_path(tick_env(tmp_path)))
    for _ in range(5):
        ticks.record(now=1000.0)
    assert ticks.demand(now=1000.0) is True
    # Aligned window floor(t/600): 1200 is a new window.
    assert ticks.demand(now=1200.0) is False
    ticks.record(now=1200.0)
    assert ticks.demand(now=1200.0) is False


def test_store_flush_moves_without_double_count(tmp_path):
    path = h.default_ticks_path(tick_env(tmp_path))
    ticks = h.TickStore(path)
    for _ in range(3):
        ticks.record(now=1000.0)
    assert ticks.flush(now=1000.0) is True
    assert ticks.demand(now=1000.0) is False  # moved out of memory
    on_disk = json.loads(path.read_text())
    assert on_disk["count"] == 3
    # Second flush with no new ticks changes nothing (no double-count).
    assert ticks.flush(now=1000.0) is True
    assert json.loads(path.read_text())["count"] == 3


def test_store_merge_same_window_adds(tmp_path):
    path = h.default_ticks_path(tick_env(tmp_path))
    first = h.TickStore(path)
    for _ in range(2):
        first.record(now=1000.0)
    first.flush(now=1000.0)
    second = h.TickStore(path)
    for _ in range(3):
        second.record(now=1000.0)
    second.flush(now=1000.0)
    assert json.loads(path.read_text())["count"] == 5


def test_store_merge_later_window_wins_as_expiry(tmp_path):
    path = h.default_ticks_path(tick_env(tmp_path))
    old = h.TickStore(path)
    for _ in range(5):
        old.record(now=1000.0)
    old.flush(now=1000.0)
    new = h.TickStore(path)
    new.record(now=2000.0)
    new.flush(now=2000.0)
    on_disk = json.loads(path.read_text())
    assert on_disk["count"] == 1  # older window discarded as expiry, not loss


def test_store_torn_file_degrades_without_writing(tmp_path):
    path = h.default_ticks_path(tick_env(tmp_path))
    path.write_text("{torn")
    before = path.read_bytes()
    ticks = h.TickStore(path)
    assert ticks.demand(now=1000.0) is False
    ticks.record(now=1000.0)
    assert ticks.flush(now=1000.0) is False  # backed off, kept memory
    assert path.read_bytes() == before  # never blank-overwrites good data
    assert ticks.demand(now=1000.0) is False  # 1 tick kept in memory
    for _ in range(4):
        ticks.record(now=1000.0)
    assert ticks.demand(now=1000.0) is True


def test_store_unknown_schema_preserved_and_ignored(tmp_path):
    path = h.default_ticks_path(tick_env(tmp_path))
    path.write_text(json.dumps({"schema": 999, "window": 1, "count": 99}))
    ticks = h.TickStore(path)
    ticks.record(now=1000.0)
    assert ticks.flush(now=1000.0) is False
    assert json.loads(path.read_text())["schema"] == 999


def test_store_unwritable_dir_retains_and_retries(tmp_path):
    path = h.default_ticks_path(tick_env(tmp_path))
    ticks = h.TickStore(path)
    ticks.record(now=1000.0)
    path.parent.chmod(0o500)
    try:
        assert ticks.flush(now=1000.0) is False
    finally:
        path.parent.chmod(0o700)
    assert ticks.flush(now=1000.0) is True
    assert json.loads(path.read_text())["count"] == 1


def test_fresh_store_sees_file_demand_without_seeding(tmp_path):
    """Restart demand works through demand() alone: no seed step exists."""
    path = h.default_ticks_path(tick_env(tmp_path))
    first = h.TickStore(path)
    for _ in range(5):
        first.record(now=1000.0)
    first.flush(now=1000.0)
    second = h.TickStore(path)  # fresh process: empty memory
    assert second.demand(now=1000.0) is True  # file demand, no seed call
    assert second.demand(now=5000.0) is False  # old window expired


# --- T3: executor iteration ---

class _StubPool:
    def __init__(self, env, tools_ready=0):
        self.env = env
        self._ready = tools_ready

    def managed_status(self):
        return {"tools_ready": self._ready}

    def probe_call(self, *args, **kwargs):  # legacy-gate surface only
        raise AssertionError("must be mocked")


def _executor(pool, tmp_path, **kwargs):
    env = tick_env(tmp_path)
    ticks = h.TickStore(h.default_ticks_path(env))
    store = h.HealStore(h.default_heal_path(env))
    return h.HealExecutor(pool, ticks, store, **kwargs), ticks, store


def test_iteration_below_threshold_never_runs(tmp_path):
    pool = _StubPool(tick_env(tmp_path))
    calls = []
    ex, ticks, _ = _executor(pool, tmp_path,
                             run_fn=lambda p, s: calls.append(1) or {"ran": True})
    for _ in range(4):
        ticks.record(now=1000.0)
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is False
    assert calls == []


def test_iteration_threshold_thin_runs_and_consumes(tmp_path):
    pool = _StubPool(tick_env(tmp_path), tools_ready=1)
    calls = []
    ex, ticks, _ = _executor(
        pool, tmp_path,
        run_fn=lambda p, s: calls.append(1) or {"ran": True, "reason": "ok",
                                                "passes": 2})
    for _ in range(5):
        ticks.record(now=1000.0)
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is True
    assert calls == [1]
    assert ticks.demand(now=1000.0) is False  # consumed on ran


def test_iteration_healthy_bench_skips_and_consumes(tmp_path):
    pool = _StubPool(tick_env(tmp_path), tools_ready=9)
    calls = []
    ex, ticks, _ = _executor(pool, tmp_path,
                             run_fn=lambda p, s: calls.append(1))
    for _ in range(5):
        ticks.record(now=1000.0)
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is False
    assert out["reason"] == "healthy"
    assert calls == []  # pre-check skipped the lease entirely
    assert ticks.demand(now=1000.0) is False  # stale demand consumed


def test_iteration_cooldown_skips_without_lease(tmp_path):
    pool = _StubPool(tick_env(tmp_path), tools_ready=1)
    ex, ticks, store = _executor(pool, tmp_path,
                                 run_fn=lambda p, s: (_ for _ in ()).throw(
                                     AssertionError("must not run")))
    state = store.load()
    state["cooldown_until"] = "2999-01-01T00:00:00+00:00"
    store.save(state)
    for _ in range(5):
        ticks.record(now=1000.0)
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is False
    assert out["reason"] == "cooldown"
    assert ticks.demand(now=1000.0) is False


def test_iteration_busy_keeps_demand(tmp_path):
    pool = _StubPool(tick_env(tmp_path), tools_ready=1)
    ex, ticks, _ = _executor(pool, tmp_path,
                             run_fn=lambda p, s: {"ran": False, "reason": "busy"})
    for _ in range(5):
        ticks.record(now=1000.0)
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is False
    assert out["reason"] == "busy"
    assert ticks.demand(now=1000.0) is True  # transient: retry next round


def test_iteration_failed_run_consumes_and_logs(tmp_path, capsys):
    pool = _StubPool(tick_env(tmp_path), tools_ready=1)
    ex, ticks, _ = _executor(pool, tmp_path,
                             run_fn=lambda p, s: {"ran": True, "reason": "io-error",
                                                  "passes": 0})
    for _ in range(5):
        ticks.record(now=1000.0)
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is True
    assert ticks.demand(now=1000.0) is False  # fresh ticks re-arm; no hot loop


def test_iteration_survives_exploding_run(tmp_path, capsys):
    pool = _StubPool(tick_env(tmp_path), tools_ready=1)

    def boom(p, s):
        raise RuntimeError("heal boom")

    ex, ticks, _ = _executor(pool, tmp_path, run_fn=boom)
    for _ in range(5):
        ticks.record(now=1000.0)
    out = ex.iterate_once(now=1000.0)  # never raises (COMP G9)
    assert out["acted"] is False
    assert "heal boom" in capsys.readouterr().err


# --- T4: executor lifecycle (ordering pins live with proxy tests) ---

def test_maybe_start_none_without_autoheal(tmp_path):
    pool = _StubPool(tick_env(tmp_path, autoheal=False))
    assert h.maybe_start_heal_executor(pool) is None


def test_maybe_start_none_for_legacy_pool(tmp_path):
    assert h.maybe_start_heal_executor(object()) is None


def test_maybe_start_idempotent_and_stops(tmp_path):
    pool = _StubPool(tick_env(tmp_path))
    first = h.maybe_start_heal_executor(pool, interval=60.0)
    assert first is not None
    try:
        assert h.maybe_start_heal_executor(pool) is first
        thread = first._thread
        assert thread is not None and thread.is_alive()
    finally:
        first.stop()
    assert thread is not None and not thread.is_alive()


def test_default_run_uses_proxy_tick_trigger(monkeypatch, tmp_path):
    pool = _StubPool(tick_env(tmp_path), tools_ready=1)
    seen: dict = {}

    def spy(pool_arg, store_arg, **kwargs):
        seen.update(kwargs)
        return {"ran": True, "reason": "ok", "passes": 1}

    monkeypatch.setattr(h, "run_heal", spy)
    ex, ticks, _ = _executor(pool, tmp_path)  # default run_fn
    for _ in range(5):
        ticks.record(now=1000.0)
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is True
    assert seen.get("trigger") == "proxy-tick"


def test_iteration_honors_consent_flip(tmp_path):
    env = tick_env(tmp_path)
    pool = _StubPool(env, tools_ready=1)
    calls = []
    ex, ticks, _ = _executor(pool, tmp_path,
                             run_fn=lambda p, s: calls.append(1))
    for _ in range(5):
        ticks.record(now=1000.0)
    env.pop("FREELLMPOOL_AUTOHEAL")  # flipped off mid-process
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is False
    assert out["reason"] == "disabled"
    assert calls == []


def test_start_seeds_and_evaluates_immediately(tmp_path):
    import queue

    env = tick_env(tmp_path)
    path = h.default_ticks_path(env)
    old = h.TickStore(path)
    now = time.time()
    for _ in range(5):
        old.record(now=now)
    old.flush(now=now)
    pool = _StubPool(env, tools_ready=1)
    calls: queue.Queue = queue.Queue()
    ex = h.HealExecutor(pool, h.TickStore(path),
                        h.HealStore(h.default_heal_path(env)),
                        interval=3600.0,
                        run_fn=lambda p, s: calls.put(1) or {"ran": True,
                                                             "reason": "ok"})
    try:
        ex.start()
        calls.get(timeout=10.0)  # iteration 0: no 60 s wait
    finally:
        ex.stop()


# --- 020: concurrency counterexamples (ported from frozen-source probes) ---

def test_020_demand_flush_race_counts_once(tmp_path):
    """020#1: demand spanning a flush must see 3 ticks once, not 3+3."""
    ticks = h.TickStore(h.default_ticks_path(tick_env(tmp_path)))
    for _ in range(3):
        ticks.record(now=1000.0)
    entered, release = threading.Event(), threading.Event()
    original = ticks._read_raw

    def controlled_read():
        if threading.current_thread().name == "demand-thread":
            entered.set()
            assert release.wait(5)
        return original()

    ticks._read_raw = controlled_read
    result: list = []
    thread = threading.Thread(
        target=lambda: result.append(ticks.demand(now=1000.0)),
        name="demand-thread")
    thread.start()
    assert entered.wait(5)
    assert ticks.flush(now=1000.0) is True
    release.set()
    thread.join(5)
    assert json.loads(ticks.path.read_text())["count"] == 3
    assert result == [False]  # 3 ticks < threshold 5: no false heal


def test_020_stop_lifecycle_no_false_heal(tmp_path):
    """020#1 via the executor/stop lifecycle: 3 real ticks never heal."""

    class Pool:
        def __init__(self):
            self.env = {"FREELLMPOOL_AUTOHEAL": "1"}

        def managed_status(self):
            return {"tools_ready": 0}

    ticks = h.TickStore(tmp_path / "ticks.json")
    store = h.HealStore(tmp_path / "heal.json")
    entered, release, injected = (threading.Event() for _ in range(3))
    original_flush, original_read = ticks.flush, ticks._read_raw

    def lifecycle_flush(now=None):
        ok = original_flush(now)
        if threading.current_thread().name == "heal-executor" \
                and not injected.is_set():
            injected.set()
            for _ in range(3):
                ticks.record(now=now)
        return ok

    def lifecycle_read():
        if threading.current_thread().name == "heal-executor" \
                and injected.is_set() and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original_read()

    ticks.flush, ticks._read_raw = lifecycle_flush, lifecycle_read
    calls: list = []
    ex = h.HealExecutor(
        Pool(), ticks, store, interval=3600,
        run_fn=lambda p, s: calls.append("HEAL") or {"ran": True,
                                                    "reason": "ok"})
    old = h.HEAL_EXECUTOR_STOP_JOIN_SECONDS
    h.HEAL_EXECUTOR_STOP_JOIN_SECONDS = 0  # acceleration only (probe note)
    try:
        ex.start()
        assert entered.wait(5)
        ex.stop()
        disk = json.loads(ticks.path.read_text())["count"]
        release.set()
        time.sleep(0.1)
        assert disk == 3 and calls == []  # no false heal on 3 ticks
    finally:
        h.HEAL_EXECUTOR_STOP_JOIN_SECONDS = old
        release.set()


def test_020_boundary_restore_preserves_fresh(tmp_path):
    """020#2: failed flush across a window keeps the new tick live."""
    ticks = h.TickStore(h.default_ticks_path(tick_env(tmp_path)))
    for _ in range(4):
        ticks.record(now=1199.0)
    entered, release = threading.Event(), threading.Event()

    def failing_write(bucket, count):
        entered.set()
        assert release.wait(5)
        raise OSError("synthetic write failure")

    ticks._write = failing_write
    result: list = []
    thread = threading.Thread(
        target=lambda: result.append(ticks.flush(now=1199.0)))
    thread.start()
    assert entered.wait(5)
    ticks.record(now=1200.0)  # new-window tick lands mid-write
    release.set()
    thread.join(5)
    assert result == [False]
    assert (ticks._bucket, ticks._count) == (2, 1)  # fresh preserved, old expired
    assert ticks.demand(now=1200.0) is False  # 1 live tick < 5
    for _ in range(4):
        ticks.record(now=1200.0)
    assert ticks.demand(now=1200.0) is True  # fresh tick counts


def test_020_overrun_skips_missed_slots(tmp_path):
    """020#3: a 190 s iteration skips anchors 60/120/180 (no catch-up)."""

    class Pool:
        env = {}

    class Stop:
        def __init__(self, stop_after=4):
            self.timeouts: list = []
            self.stop_after = stop_after

        def wait(self, timeout):
            self.timeouts.append(timeout)
            return len(self.timeouts) >= self.stop_after

        def set(self):
            pass

    ex = h.HealExecutor(Pool(), h.TickStore(tmp_path / "t.json"),
                        h.HealStore(tmp_path / "heal.json"), interval=60)
    ex._monotonic = lambda: next(clock)
    clock = iter([0, 190, 190, 190, 190])
    ex.iterate_once = lambda: {"acted": False}
    stop = Stop()
    ex._stop_event = stop
    ex._loop()
    assert stop.timeouts == [50, 50, 50, 50]


def test_020_anchor_normal_cadence_waits_full_interval(tmp_path):
    """Positive control: on-time iterations wait out the interval."""

    class Pool:
        env = {}

    class Stop:
        def __init__(self, stop_after=3):
            self.timeouts: list = []
            self.stop_after = stop_after

        def wait(self, timeout):
            self.timeouts.append(timeout)
            return len(self.timeouts) >= self.stop_after

        def set(self):
            pass

    ex = h.HealExecutor(Pool(), h.TickStore(tmp_path / "t.json"),
                        h.HealStore(tmp_path / "heal.json"), interval=60)
    clock = iter([0, 1, 61, 121])
    ex._monotonic = lambda: next(clock)
    ex.iterate_once = lambda: {"acted": False}
    stop = Stop()
    ex._stop_event = stop
    ex._loop()
    assert stop.timeouts == [59, 59, 59]


# --- T5: two-process tick loss ---

def test_two_processes_lose_no_ticks(tmp_path):
    import subprocess
    import sys

    ticks_path = tmp_path / "ticks.json"
    stamp = str(int(time.time()))
    child = (
        "import sys; sys.path.insert(0, 'src');"
        "from freellmpool.heal import TickStore;"
        "t = TickStore(__import__('pathlib').Path(sys.argv[1]));"
        "[t.record(now=float(sys.argv[2])) for _ in range(20)];"
        "t.flush(now=float(sys.argv[2]))"
    )
    procs = [subprocess.Popen([sys.executable, "-c", child, str(ticks_path), stamp])
             for _ in range(2)]
    for proc in procs:
        assert proc.wait(timeout=60) == 0
    assert json.loads(ticks_path.read_text())["count"] == 40


def test_kill_minus_9_loses_only_unflushed(tmp_path):
    import signal
    import subprocess
    import sys

    ticks_path = tmp_path / "ticks.json"
    child = (
        "import sys, time; sys.path.insert(0, 'src');"
        "from freellmpool.heal import TickStore;"
        "t = TickStore(__import__('pathlib').Path(sys.argv[1]));"
        "[t.record() for _ in range(5)]; t.flush();"
        "[t.record() for _ in range(5)];"  # unflushed: must die here
        "time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, "-c", child, str(ticks_path)])
    time.sleep(3.0)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=60)
    assert json.loads(ticks_path.read_text())["count"] == 5


# --- T6: request-path purity ---

def test_record_writes_no_files(tmp_path):
    path = h.default_ticks_path(tick_env(tmp_path))
    ticks = h.TickStore(path)
    for _ in range(30):  # past the cut K: still no I/O on record
        ticks.record(now=1000.0)
    assert not path.exists()


def test_maybe_record_tick_gates(monkeypatch, tmp_path):
    path = h.default_ticks_path(tick_env(tmp_path))
    ticks = h.TickStore(path)
    assert h.maybe_record_tick(None, tick_env(tmp_path), True, exhausted_429()) is False
    assert h.maybe_record_tick(ticks, tick_env(tmp_path, autoheal=False),
                               True, exhausted_429()) is False
    assert h.maybe_record_tick(ticks, tick_env(tmp_path),
                               False, exhausted_429()) is False
    assert h.maybe_record_tick(ticks, tick_env(tmp_path), True,
                               exhausted_429()) is True
    assert not path.exists()  # memory-only even when recording


# --- T1 handler-level: tools-429 requests tick through live proxy ---

def _live_proxy(pool, ticks):
    from freellmpool.proxy import serve

    httpd = serve(pool, host="127.0.0.1", port=0, tick_store=ticks)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _exhausting_pool(providers, env, quota, status):
    from freellmpool.router import Pool

    def fail_chat(*args, **kwargs):
        raise AllProvidersExhausted(
            attempts=[("alpha/alpha-small", "down")], client_status=status,
            client_message="all down", retry_after=None, upstream_status=status)

    pool = Pool(providers[:1], quota=quota, env=env, post=lambda *a, **k: None)
    pool.chat = fail_chat
    return pool


def _post_status(base, path, payload):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 (localhost)
            return resp.status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code


def _tick_count(ticks):
    ticks.flush()
    if not ticks.path.exists():
        return 0  # no-op flush persists nothing; no ticks flushed
    return json.loads(ticks.path.read_text())["count"]


def test_tools_429_ticks_and_text_429_does_not(providers, env, quota, tmp_path):
    env = {**env, **tick_env(tmp_path)}
    pool = _exhausting_pool(providers, env, quota, 429)
    ticks = h.TickStore(h.default_ticks_path(env))
    httpd, base = _live_proxy(pool, ticks)
    try:
        tools_payload = {"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                         "tools": [{"type": "function",
                                    "function": {"name": "f", "parameters": {}}}]}
        assert _post_status(base, "/v1/chat/completions", tools_payload) == 429
        assert _post_status(base, "/v1/chat/completions",
                            {"model": "auto", "messages": [{"role": "user",
                                                             "content": "hi"}]}) == 429
        assert _tick_count(ticks) == 1  # tools request only
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_non_429_terminal_never_ticks(providers, env, quota, tmp_path):
    env = {**env, **tick_env(tmp_path)}
    pool = _exhausting_pool(providers, env, quota, 502)
    ticks = h.TickStore(h.default_ticks_path(env))
    httpd, base = _live_proxy(pool, ticks)
    try:
        payload = {"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"type": "function",
                              "function": {"name": "f", "parameters": {}}}]}
        assert _post_status(base, "/v1/chat/completions", payload) == 502
        assert _tick_count(ticks) == 0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_messages_tools_429_ticks(providers, env, quota, tmp_path):
    env = {**env, **tick_env(tmp_path)}
    pool = _exhausting_pool(providers, env, quota, 429)
    ticks = h.TickStore(h.default_ticks_path(env))
    httpd, base = _live_proxy(pool, ticks)
    try:
        payload = {"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"name": "f", "input_schema": {"type": "object"}}]}
        assert _post_status(base, "/v1/messages", payload) == 429
        assert _tick_count(ticks) == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_autoheal_off_storm_writes_no_state(providers, env, quota, tmp_path):
    env = {**env, **tick_env(tmp_path, autoheal=False)}
    pool = _exhausting_pool(providers, env, quota, 429)
    ticks = h.TickStore(h.default_ticks_path(env))
    httpd, base = _live_proxy(pool, ticks)
    try:
        payload = {"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                   "tools": [{"type": "function",
                              "function": {"name": "f", "parameters": {}}}]}
        for _ in range(6):
            assert _post_status(base, "/v1/chat/completions", payload) == 429
        assert not ticks.path.exists()  # T6: zero state without consent
        assert ticks.demand() is False
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- T4: startup ordering at both entry points ---

def _cli_proxy_harness(monkeypatch, tmp_path, autoheal=True):
    """Fake Pool + fake serve() + maybe_start spy; returns (events, captured)."""
    from types import SimpleNamespace

    from freellmpool import cli as cli_mod

    events: list = []
    captured: dict = {}
    env = tick_env(tmp_path, autoheal=autoheal)
    pool = SimpleNamespace(
        providers=[SimpleNamespace(id="fake", label="Fake",
                                   models=[SimpleNamespace()])],
        env=env,
        quota=SimpleNamespace(flush=lambda: None),
        managed_status=lambda: {"tools_ready": 9},
        probe_call=lambda *a, **k: None,
        stats_snapshot=lambda: {"requests": 0, "prompt_tokens": 0,
                                "completion_tokens": 0},
        flush=lambda: None,
    )
    monkeypatch.setattr(
        "freellmpool.cli.Pool", SimpleNamespace(from_default_config=lambda: pool))
    real_start = h.maybe_start_heal_executor

    def spy_start(pool_arg, **kwargs):
        events.append("maybe_start")
        started = real_start(pool_arg, **kwargs)
        events.append("started" if started is not None else "skipped")
        captured["executor"] = started
        return started

    monkeypatch.setattr(cli_mod, "maybe_start_heal_executor", spy_start)

    class FakeServer:
        def __init__(self, pool_arg, host="127.0.0.1", port=8080, api_key=None,
                     allowed_authorities=(), tick_store=None):
            events.append("serve")
            captured["tick_store"] = tick_store

        def serve_forever(self):
            events.append("serve_forever")
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr("freellmpool.proxy.serve", FakeServer)
    return events, captured


def test_cmd_proxy_starts_executor_before_serve(monkeypatch, tmp_path, capsys):
    from freellmpool.cli import main

    events, captured = _cli_proxy_harness(monkeypatch, tmp_path)
    try:
        assert main(["proxy"]) == 0
        assert captured["executor"]._stop_event.is_set()  # finally stopped it
        assert captured["executor"]._thread is None  # joined, not leaked
    finally:
        if captured.get("executor") is not None:
            captured["executor"].stop()
    assert events[:3] == ["maybe_start", "started", "serve"]
    assert events[3] == "serve_forever"  # ordering: start called pre-serve
    assert captured["tick_store"] is captured["executor"].ticks  # wired


def test_cmd_proxy_without_autoheal_threads_no_store(monkeypatch, tmp_path, capsys):
    from freellmpool.cli import main

    events, captured = _cli_proxy_harness(monkeypatch, tmp_path, autoheal=False)
    assert main(["proxy"]) == 0
    assert events[:2] == ["maybe_start", "skipped"]
    assert captured["tick_store"] is None
    assert not (tmp_path / "ticks.json").exists()


def test_tailnet_serve_wires_tick_store(monkeypatch, tmp_path, capsys):
    from freellmpool import tailnet
    from freellmpool.cli import main

    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(
        tailnet, "detect_tailnet",
        lambda *, binary=None, runner=tailnet._run_tailscale, timeout=4.0: (
            tailnet.TailnetStatus(state=tailnet.STATE_USABLE,
                                  ipv4="100.64.0.5", raw="100.64.0.5\n")))
    events, captured = _cli_proxy_harness(monkeypatch, tmp_path)
    try:
        assert main(["tailnet", "serve", "--port", "1234",
                     "--api-key", "user-supplied-key"]) == 0
        assert captured["executor"]._stop_event.is_set()  # finally stopped it
        assert captured["executor"]._thread is None  # joined, not leaked
    finally:
        if captured.get("executor") is not None:
            captured["executor"].stop()
    assert events[:3] == ["maybe_start", "started", "serve"]
    assert captured["tick_store"] is captured["executor"].ticks  # parity


# --- 021: adversarial-review follow-ups (SHOULD-1/2/4/5 + clamp + limit) ---

def test_record_rollover_bumps_generation_preserving_parity(tmp_path):
    """SHOULD-4 unit: rollover invalidates demand views; hot path untouched."""
    ticks = h.TickStore(tmp_path / "ticks.json")
    ticks.record(now=1000.0)
    gen = ticks._gen
    ticks.record(now=1000.0)
    assert ticks._gen == gen  # same bucket: no invalidation storm
    ticks.record(now=1200.0)
    assert ticks._gen == gen + 2  # rollover invalidates, parity preserved


def test_demand_respins_on_mid_read_rollover(tmp_path):
    """SHOULD-4 (probe B): a rollover between the mem snapshot and the
    file read must not return the expired window's count as live demand."""
    ticks = h.TickStore(tmp_path / "ticks.json")
    for _ in range(5):
        ticks.record(now=1000.0)
    original = ticks._file_window_count
    fired: list = []

    def rolling(current):
        if not fired:
            fired.append(1)
            ticks.record(now=1200.0)  # expire window 1 mid-read
        return original(current)

    ticks._file_window_count = rolling
    assert ticks.demand(now=1000.0) is False  # window-1 truth is 0 + 0
    assert (ticks._bucket, ticks._count) == (2, 1)  # fresh tick live


def test_post_run_consume_uses_fresh_window(tmp_path):
    """SHOULD-1 (probe A): a run crossing a window boundary consumes into
    the live window, never rewrites the file backward."""
    pool = _StubPool(tick_env(tmp_path), tools_ready=1)
    ex, ticks, _ = _executor(
        pool, tmp_path,
        run_fn=lambda p, s: {"ran": True, "reason": "ok", "passes": 1})
    for _ in range(5):
        ticks.record(now=1000.0)
    ticks._now = lambda now: 2500.0 if now is None else now  # world moved on
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is True
    assert ticks._bucket == 4  # live bucket, not the stale pass-start one
    raw = json.loads(ticks.path.read_text())
    assert h._parse_bucket(raw["window_start"]) == 4  # file moved forward


def test_stopped_executor_runs_but_skips_post_run_consume(tmp_path):
    """SHOULD-2 (probe C): a pass detached by stop() must not wipe the
    shutdown flush it raced; the run itself still counts."""
    pool = _StubPool(tick_env(tmp_path), tools_ready=1)
    ex, ticks, _ = _executor(
        pool, tmp_path,
        run_fn=lambda p, s: {"ran": True, "reason": "ok", "passes": 1})
    for _ in range(5):
        ticks.record(now=1000.0)
    ex._stop_event.set()  # stop() landed mid-run; shutdown flush owns disk
    out = ex.iterate_once(now=1000.0)
    assert out["acted"] is True
    assert json.loads(ticks.path.read_text())["count"] == 5  # retained


def test_executor_rejects_nonpositive_interval(tmp_path):
    """NIT: interval<=0 fails fast instead of killing the daemon thread."""
    pool = _StubPool(tick_env(tmp_path))
    ticks = h.TickStore(tmp_path / "t.json")
    store = h.HealStore(tmp_path / "h.json")
    for bad in (0, -1.0):
        with pytest.raises(ValueError):
            h.HealExecutor(pool, ticks, store, interval=bad)


def test_020_odd_generation_fallback_delays_not_loses(tmp_path):
    """021 limit port: demand while a move stays odd is conservative
    False for the spin budget, True once the move completes."""
    ticks = h.TickStore(tmp_path / "ticks.json")
    for _ in range(5):
        ticks.record(now=1000.0)
    entered, release = threading.Event(), threading.Event()
    original_write = ticks._write

    def paused_after_write(bucket, count):
        original_write(bucket, count)
        entered.set()
        assert release.wait(5)

    ticks._write = paused_after_write
    thread = threading.Thread(target=lambda: ticks.flush(now=1000.0))
    thread.start()
    assert entered.wait(5)
    delayed = ticks.demand(now=1000.0)  # ~50 ms spin budget, all odd
    release.set()
    thread.join(5)
    eventual = ticks.demand(now=1000.0)
    assert delayed is False and eventual is True
