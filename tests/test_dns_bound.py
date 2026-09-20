"""G26 U3: daemon resolver executor + no-join runner + wizard bound."""

import ast
import asyncio
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from freellmpool import discovery as d


def resolver_threads():
    return [thread for thread in threading.enumerate()
            if thread.name.startswith("freellmpool-resolver-")]


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def hung_pair():
    started = threading.Event()
    release = threading.Event()

    def hung():
        started.set()
        release.wait(30)

    return hung, started, release


# --- _DaemonExecutor ---


def test_executor_spawns_lazily_and_names_daemon_workers():
    pool = d._DaemonExecutor()
    try:
        assert resolver_threads() == []
        assert pool.submit(lambda: 7).result(timeout=10) == 7
        workers = resolver_threads()
        assert len(workers) == 1
        assert all(worker.daemon for worker in workers)
        with pytest.raises(RuntimeError):
            pool.submit(_boom).result(timeout=10)
    finally:
        pool.shutdown(wait=True)


def test_executor_skips_directly_cancelled_queued_work():
    hung, started, release = hung_pair()
    pool = d._DaemonExecutor(max_workers=1)
    try:
        pool.submit(hung)
        assert started.wait(10)
        queued = pool.submit(lambda: "never")
        assert queued.cancel()
        assert queued.cancelled()
    finally:
        release.set()
    pool.shutdown(wait=True)
    assert resolver_threads() == []


def test_executor_max_workers_mirrors_default_formula(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert d._DaemonExecutor()._max_workers == 12
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert d._DaemonExecutor()._max_workers == 5
    assert d._DaemonExecutor(max_workers=2)._max_workers == 2
    with pytest.raises(ValueError):
        d._DaemonExecutor(max_workers=0)


def test_executor_honors_initializer_and_prefix():
    seen = []
    pool = d._DaemonExecutor(max_workers=1, thread_name_prefix="init-probe-",
                             initializer=seen.append, initargs=("up",))
    try:
        assert pool.submit(lambda: "ok").result(timeout=10) == "ok"
        assert seen == ["up"]
        assert [thread.name for thread in threading.enumerate()
                if thread.name.startswith("init-probe-")] == ["init-probe-0"]
    finally:
        pool.shutdown(wait=True)


def test_executor_submit_after_shutdown_is_runtime_error():
    pool = d._DaemonExecutor()
    pool.shutdown(wait=True)
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)
    pool.shutdown(wait=True)
    pool.shutdown(wait=False)


def test_executor_shutdown_joins_idle_workers():
    pool = d._DaemonExecutor()
    assert pool.submit(lambda: 1).result(timeout=10) == 1
    assert len(resolver_threads()) == 1
    pool.shutdown(wait=True)
    assert resolver_threads() == []


def test_executor_no_join_abandons_hung_worker_until_release():
    hung, started, release = hung_pair()
    pool = d._DaemonExecutor()
    try:
        future = pool.submit(hung)
        assert started.wait(10)
        begun = time.monotonic()
        pool.shutdown(wait=False)
        assert time.monotonic() - begun < 5
        assert not future.done()
    finally:
        release.set()
    assert wait_for(lambda: resolver_threads() == [])


def test_executor_cancel_futures_drains_queued_and_drops_inflight():
    hung, started, release = hung_pair()
    pool = d._DaemonExecutor(max_workers=1)
    try:
        flying = pool.submit(hung)
        assert started.wait(10)
        queued = pool.submit(lambda: "never")
        pool.shutdown(wait=False, cancel_futures=True)
        assert queued.cancelled()
    finally:
        release.set()
    assert wait_for(lambda: resolver_threads() == [])
    assert not flying.done() and not flying.cancelled()


def test_executor_result_drop_is_atomic_and_silent():
    seen = []
    old_hook = threading.excepthook
    threading.excepthook = seen.append
    try:
        for _ in range(50):
            pool = d._DaemonExecutor(max_workers=2)
            pool.submit(lambda: "fast")
            pool.submit(_boom)
            pool.shutdown(wait=False)
    finally:
        threading.excepthook = old_hook
    assert seen == []
    assert wait_for(lambda: resolver_threads() == [])


def _boom():
    raise RuntimeError("dropped")


EXIT_SCRIPT = """\
import sys
import threading

from freellmpool.discovery import _DaemonExecutor

marker = sys.argv[1]
pool = _DaemonExecutor()


def hung():
    with open(marker, "w") as handle:
        handle.write("started")
    threading.Event().wait()


pool.submit(hung)
for _ in range(1000):
    try:
        with open(marker) as handle:
            if handle.read() == "started":
                break
    except OSError:
        pass
    threading.Event().wait(0.01)
else:
    sys.exit(2)
pool.shutdown(wait=False)
print("READY", flush=True)
"""


def test_executor_hung_worker_never_blocks_process_exit(tmp_path):
    script = tmp_path / "exit_probe.py"
    script.write_text(EXIT_SCRIPT)
    marker = tmp_path / "started"
    proc = subprocess.Popen([sys.executable, str(script), str(marker)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stderr is not None
    try:
        ready, _, _ = select.select([proc.stdout], [], [], 30)
        if not ready:
            proc.kill()
            pytest.fail("exit probe never became ready")
        line = proc.stdout.readline()
        begun = time.monotonic()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("process exit joined a hung resolver worker")
        assert line.strip() == "READY"
        assert proc.returncode == 0
        assert time.monotonic() - begun <= 3
        assert proc.stderr.read() == ""
    finally:
        proc.stdout.close()
        proc.stderr.close()


# --- _run_sync ---


def test_run_sync_passes_result_through():
    async def main():
        await asyncio.sleep(0)
        return 42

    assert d._run_sync(main()) == 42


def _spy_teardown(monkeypatch, state):
    real_new = asyncio.new_event_loop
    real_set = asyncio.set_event_loop

    def spy_new():
        loop = real_new()
        state["loop"] = loop
        return loop

    def spy_set(loop):
        state.setdefault("set_calls", []).append(loop)
        return real_set(loop)

    real_default = asyncio.BaseEventLoop.set_default_executor

    def spy_default(self, executor):
        state["executor"] = executor
        return real_default(self, executor)

    monkeypatch.setattr(asyncio, "new_event_loop", spy_new)
    monkeypatch.setattr(asyncio, "set_event_loop", spy_set)
    monkeypatch.setattr(asyncio.BaseEventLoop, "set_default_executor", spy_default)


def test_run_sync_busy_propagates_and_tears_down_in_order(monkeypatch):
    state: dict = {}
    _spy_teardown(monkeypatch, state)

    async def gen():
        try:
            yield 1
        finally:
            state["gen_closed"] = True

    async def main():
        agen = gen()
        state["agen"] = agen
        await agen.__anext__()

        async def side():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["side_cancelled"] = True
                raise

        state["task"] = asyncio.ensure_future(side())
        await asyncio.sleep(0.05)
        raise d.DiscoveryBusy("another catalog refresh is running")

    with pytest.raises(d.DiscoveryBusy):
        d._run_sync(main())
    assert state["side_cancelled"] is True
    assert state["task"].cancelled()
    assert state["gen_closed"] is True
    assert state["loop"].is_closed()
    assert state["executor"]._shutdown is True
    assert state["set_calls"] and state["set_calls"][-1] is None


def test_run_sync_keyboard_interrupt_abandons_hung_worker(monkeypatch, capsys):
    state: dict = {}
    _spy_teardown(monkeypatch, state)
    hung, started, release = hung_pair()

    async def main():
        loop = asyncio.get_running_loop()
        state["future"] = loop.run_in_executor(None, hung)
        assert started.wait(10)
        raise KeyboardInterrupt

    begun = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        d._run_sync(main())
    try:
        assert time.monotonic() - begun < 5
        assert state["loop"].is_closed()
        assert state["set_calls"][-1] is None
    finally:
        release.set()
    assert wait_for(lambda: resolver_threads() == [])
    assert capsys.readouterr().err == ""


def test_run_sync_returns_without_joining_hung_worker(capsys):
    hung, started, release = hung_pair()
    seen: dict = {}

    async def main():
        loop = asyncio.get_running_loop()
        seen["future"] = loop.run_in_executor(None, hung)
        assert started.wait(10)
        return "done"

    begun = time.monotonic()
    try:
        assert d._run_sync(main()) == "done"
        assert time.monotonic() - begun < 5
    finally:
        release.set()
    assert wait_for(lambda: resolver_threads() == [])
    assert not seen["future"].done()
    assert capsys.readouterr().err == ""


def test_run_sync_signal_path_cancels_pending_task_then_propagates(monkeypatch):
    state: dict = {}
    _spy_teardown(monkeypatch, state)
    real_drive = asyncio.BaseEventLoop.run_until_complete
    calls = []

    def flaky(self, future):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real_drive(self, future)

    monkeypatch.setattr(asyncio.BaseEventLoop, "run_until_complete", flaky)

    async def main():
        await asyncio.sleep(30)

    with pytest.raises(KeyboardInterrupt):
        d._run_sync(main())
    assert state["loop"].is_closed()
    assert state["set_calls"][-1] is None
    assert len(calls) == 3  # main drive + cancel re-drive + asyncgen shutdown


def test_run_sync_keyboard_interrupt_during_teardown_still_closes(monkeypatch):
    state: dict = {}
    _spy_teardown(monkeypatch, state)

    def boom_once(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio.BaseEventLoop, "shutdown_asyncgens", boom_once)

    async def main():
        return "unreached"

    with pytest.raises(KeyboardInterrupt):
        d._run_sync(main())
    assert state["loop"].is_closed()
    assert state["set_calls"][-1] is None


def test_run_sync_double_lifecycle_and_concurrent_calls():
    async def main(value):
        await asyncio.sleep(0.01)
        return value

    assert d._run_sync(main("first")) == "first"
    assert d._run_sync(main("second")) == "second"

    results: dict = {}

    def drive(name):
        results[name] = d._run_sync(main(name))

    threads = [threading.Thread(target=drive, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert results == {"a": "a", "b": "b"}


# --- wizard bound ---


def test_wizard_check_seconds_pinned():
    assert d._WIZARD_CHECK_SECONDS == 60


def test_wizard_drip_returns_deferred_row_without_models(monkeypatch):
    seen: dict = {}

    async def fake_fetch(provider, context, result, *, deadline, progress=None):
        seen["deadline"] = deadline
        raise d._BudgetExhausted(d._deferred_row(result["last_attempt_at"], "drip note"))

    provider = {"discovery": {"url": "https://example.com/catalog",
                              "supports_public": True, "auth": "none"}}
    monkeypatch.setattr(d, "_afetch_attempt", fake_fetch)
    monkeypatch.setattr(d, "load_registry", lambda env: {"p": provider})
    result = d.check_provider("p", {})
    assert result["status"] == "deferred"
    assert "models" not in result
    assert result["model_count"] == 0
    assert 59 < seen["deadline"] - time.monotonic() <= 60


# --- grep guard ---


def test_banned_thread_and_loop_internals_absent():
    banned_everywhere = ["_threads" + "_queues", "_python" + "_exit"]
    root = Path(__file__).parents[1]
    sources = sorted((root / "src" / "freellmpool").glob("*.py"))
    tests = sorted((root / "tests").glob("*.py"))
    for path in sources + tests:
        text = path.read_text()
        for token in banned_everywhere:
            assert token not in text, f"{token} in {path}"
    loop_read = "get_event" + "_loop"
    for path in sources:
        assert loop_read not in path.read_text(), f"{loop_read} in {path}"


def test_arefresh_has_no_src_callers_and_docstring_names_deadline():
    assert "pass `deadline`" in d.arefresh_catalog.__doc__
    root = Path(__file__).parents[1] / "src" / "freellmpool"
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text())
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and ((isinstance(node.func, ast.Name) and node.func.id == "arefresh_catalog")
                      or (isinstance(node.func, ast.Attribute)
                          and node.func.attr == "arefresh_catalog"))]
        assert calls == [], path
