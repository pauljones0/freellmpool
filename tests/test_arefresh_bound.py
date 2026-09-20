"""G27: bounded async discovery at the arefresh API (observable behavior)."""

import asyncio
import copy
import errno
import faulthandler
import fcntl
import http.server
import json
import socket
import socketserver
import ssl
import threading
import time
from pathlib import Path

import httpx
import pytest

from freellmpool import discovery as d
from freellmpool.discovery import load_registry


def provider(url="https://127.0.0.1:9/catalog"):
    spec = copy.deepcopy(load_registry()["openrouter"])
    spec["discovery"] = {**spec["discovery"], "url": url}
    return spec


def model(name="example:free", price="0"):
    return {"id": name, "pricing": {"input": price, "output": "0"}, "modalities": ["chat"]}


def install_async(monkeypatch, spec, handler):
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {spec["id"]: spec})
    monkeypatch.setattr(d, "_aclient", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False))
    return spec


class DripStream(httpx.AsyncByteStream):
    def __init__(self, chunks, delay):
        self.chunks = chunks
        self.delay = delay

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk


def drip_handler(chunks, delay, flag=None):
    def respond(request):
        if flag is not None:
            flag.append(True)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=DripStream(chunks, delay))

    return respond


def instant_handler(request):
    return httpx.Response(200, json={"data": [model()]})


def lock_path_for(path):
    return path.with_suffix(path.suffix + ".lock")


def try_lock_nonblocking(path):
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        return True


# --- default policy + min() semantics ---


def test_default_policy_defers_slow_fetch_via_wait_for(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(d, "budget_seconds", lambda env: seen.setdefault("env", env) or 6.0)
    spec = provider()
    body = json.dumps({"data": [model()]}).encode()
    install_async(monkeypatch, spec, drip_handler([body[:10]] + [b"x"] * 9, 2.0))
    path = tmp_path / "c.json"

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path)

    start = time.monotonic()
    row = asyncio.run(runner())["providers"]["openrouter"]
    elapsed = time.monotonic() - start
    assert row["status"] == "deferred"
    assert row["note"] == d._deferred_page_note(1)
    assert 5.5 < elapsed < 9.0
    assert seen["env"] == {}


def test_explicit_budget_defers_slow_fetch(monkeypatch, tmp_path):
    spec = provider()
    body = json.dumps({"data": [model()]}).encode()
    install_async(monkeypatch, spec, drip_handler([body[:10]] + [b"x"] * 9, 2.0))
    path = tmp_path / "c.json"

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=6.0)

    start = time.monotonic()
    row = asyncio.run(runner())["providers"]["openrouter"]
    elapsed = time.monotonic() - start
    assert row["status"] == "deferred"
    assert row["note"] == d._deferred_page_note(1)
    assert 5.5 < elapsed < 9.0


def test_stricter_deadline_wins_over_budget(monkeypatch, tmp_path):
    spec = provider()
    body = json.dumps({"data": [model()]}).encode()
    install_async(monkeypatch, spec, drip_handler([body[:10]] + [b"x"] * 9, 2.0))
    path = tmp_path / "c.json"

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path,
                                        deadline=time.monotonic() + 6.0,
                                        time_budget_seconds=40.0)

    start = time.monotonic()
    row = asyncio.run(runner())["providers"]["openrouter"]
    elapsed = time.monotonic() - start
    assert row["status"] == "deferred"
    assert row["note"] == d._deferred_page_note(1)
    assert 5.5 < elapsed < 9.0


def test_stricter_budget_wins_over_deadline(monkeypatch, tmp_path):
    spec = provider()
    body = json.dumps({"data": [model()]}).encode()
    install_async(monkeypatch, spec, drip_handler([body[:10]] + [b"x"] * 9, 2.0))
    path = tmp_path / "c.json"

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path,
                                        deadline=time.monotonic() + 40.0,
                                        time_budget_seconds=6.0)

    start = time.monotonic()
    row = asyncio.run(runner())["providers"]["openrouter"]
    elapsed = time.monotonic() - start
    assert row["status"] == "deferred"
    assert row["note"] == d._deferred_page_note(1)
    assert 5.5 < elapsed < 9.0


def test_tiny_deadline_precheck_defers_without_fetch(monkeypatch, tmp_path):
    fetched = []
    spec = provider()
    install_async(monkeypatch, spec, drip_handler([b"x"], 0.1, flag=fetched))
    path = tmp_path / "c.json"

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path,
                                        deadline=time.monotonic() + 0.3)

    start = time.monotonic()
    row = asyncio.run(runner())["providers"]["openrouter"]
    elapsed = time.monotonic() - start
    assert row["status"] == "deferred"
    assert row["note"] == d._DEFERRED_SKIP_NOTE
    assert elapsed < 2.0
    assert fetched == []


def test_large_budget_instant_ok(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, instant_handler)
    path = tmp_path / "c.json"

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=40.0)

    row = asyncio.run(runner())["providers"]["openrouter"]
    assert row["status"] == "ok" and row["complete"] is True
    assert [m["id"] for m in row["models"]] == ["example:free"]


def test_medium_fixture_within_budget_ok(monkeypatch, tmp_path):
    spec = provider()
    body = json.dumps({"data": [model()]}).encode()
    install_async(monkeypatch, spec, drip_handler([body], 0.5))
    path = tmp_path / "c.json"

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=6.0)

    row = asyncio.run(runner())["providers"]["openrouter"]
    assert row["status"] == "ok" and row["complete"] is True


# --- last-good preservation ---


def test_timeout_preserves_last_good(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, instant_handler)
    path = tmp_path / "c.json"

    async def seed():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=40.0)

    first = asyncio.run(seed())
    generation = first["generation"]
    attempted_at = first["providers"]["openrouter"]["last_attempt_at"]
    body = json.dumps({"data": [model()]}).encode()
    install_async(monkeypatch, spec, drip_handler([body[:10]] + [b"x"] * 9, 2.0))

    async def slow():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=6.0)

    second = asyncio.run(slow())
    row = second["providers"]["openrouter"]
    assert row["status"] == "deferred"
    assert [m["id"] for m in row["models"]] == ["example:free"]
    assert row["complete"] is True
    assert second["generation"] != generation
    assert row["last_attempt_at"] > attempted_at


# --- cancellation ---


def test_cancel_mid_fetch_keeps_last_good_and_frees_gate(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, instant_handler)
    path = tmp_path / "c.json"

    async def seed():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=40.0)

    asyncio.run(seed())
    before = path.read_bytes()
    entered = asyncio.Event()
    release = asyncio.Event()

    def gated(request):
        async def stream():
            entered.set()
            await release.wait()
            yield json.dumps({"data": [model()]}).encode()

        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=_StreamAdapter(stream()))

    install_async(monkeypatch, spec, gated)

    async def runner():
        task = asyncio.ensure_future(d.arefresh_catalog(
            {}, ["openrouter"], path=path, time_budget_seconds=40.0))
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        try:
            await task
        finally:
            release.set()
        return task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner())
    assert path.read_bytes() == before
    assert try_lock_nonblocking(lock_path_for(path))


class _StreamAdapter(httpx.AsyncByteStream):
    def __init__(self, agen):
        self.agen = agen

    async def __aiter__(self):
        async for chunk in self.agen:
            yield chunk


def test_cancel_during_lock_wait(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, instant_handler)
    path = tmp_path / "c.json"
    lock_path = lock_path_for(path)
    lock_path.touch()
    holder = open(lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:

        async def runner():
            task = asyncio.ensure_future(d.arefresh_catalog(
                {}, ["openrouter"], path=path, time_budget_seconds=6.0))
            await asyncio.sleep(0.5)
            task.cancel()
            await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runner())
    finally:
        holder.close()
    assert try_lock_nonblocking(lock_path)


# --- second writer + retry ---


def test_second_writer_busy_then_retry_ok(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, instant_handler)
    path = tmp_path / "c.json"
    lock_path = lock_path_for(path)
    lock_path.touch()
    holder = open(lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:

        async def busy():
            return await d.arefresh_catalog({}, ["openrouter"], path=path,
                                            time_budget_seconds=0.3)

        start = time.monotonic()
        with pytest.raises(d.DiscoveryBusy):
            asyncio.run(busy())
        assert time.monotonic() - start < 2.0
        assert not path.exists()
    finally:
        holder.close()

    async def retry():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=6.0)

    row = asyncio.run(retry())["providers"]["openrouter"]
    assert row["status"] == "ok"


# --- loop / executor non-interference ---


def test_loop_and_executor_untouched(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, instant_handler)
    path = tmp_path / "c.json"

    async def runner():
        loop = asyncio.get_running_loop()
        executor = loop._default_executor
        resolvers = [t for t in threading.enumerate()
                     if t.name.startswith("freellmpool-resolver-")]
        row = (await d.arefresh_catalog({}, ["openrouter"], path=path,
                                        time_budget_seconds=6.0))["providers"]["openrouter"]
        assert asyncio.get_running_loop() is loop
        assert not loop.is_closed()
        assert loop._default_executor is executor
        assert [t for t in threading.enumerate()
                if t.name.startswith("freellmpool-resolver-")] == resolvers
        assert await loop.run_in_executor(None, lambda: 7) == 7
        return row["status"]

    assert asyncio.run(runner()) == "ok"


# --- progress ---


def test_progress_order_before_deferral(monkeypatch, tmp_path):
    spec = provider()
    body = json.dumps({"data": [model()]}).encode()
    install_async(monkeypatch, spec, drip_handler([body[:10]] + [b"x"] * 9, 2.0))
    path = tmp_path / "c.json"
    events = []

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path,
                                        time_budget_seconds=6.0,
                                        progress=lambda **event: events.append(event))

    row = asyncio.run(runner())["providers"]["openrouter"]
    assert row["status"] == "deferred"
    assert events[0]["provider_id"] == "openrouter" and events[0]["index"] == 0
    pages = [e for e in events if "page" in e]
    assert pages and pages[0]["page"] == 1
    assert events.index(events[0]) < events.index(pages[0])


# --- param validation ---


@pytest.mark.parametrize("param", ["deadline", "time_budget_seconds"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "x", True])
def test_nonfinite_params_raise_before_side_effects(monkeypatch, tmp_path, param, bad):
    spec = provider()
    install_async(monkeypatch, spec, instant_handler)
    path = tmp_path / "c.json"
    kwargs = {param: bad}

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, **kwargs)

    with pytest.raises(ValueError):
        asyncio.run(runner())
    assert not path.exists()


@pytest.mark.parametrize("kwargs", [{"deadline": -1.0}, {"time_budget_seconds": 0.0},
                                    {"time_budget_seconds": -5.0}])
def test_past_or_nonpositive_bound_fast_defers_all(monkeypatch, tmp_path, kwargs):
    fetched = []
    spec = provider()
    install_async(monkeypatch, spec, drip_handler([b"x"], 0.1, flag=fetched))
    path = tmp_path / "c.json"
    if "deadline" in kwargs:
        kwargs = {"deadline": time.monotonic() + kwargs["deadline"]}

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=path, **kwargs)

    start = time.monotonic()
    row = asyncio.run(runner())["providers"]["openrouter"]
    assert row["status"] == "deferred"
    assert row["note"] == d._DEFERRED_SKIP_NOTE
    assert time.monotonic() - start < 2.0
    assert fetched == []


# --- actual transport: loopback drip + controlled resolver delay ---


class _DripHandler(http.server.BaseHTTPRequestHandler):
    hits: list = []
    stop = threading.Event()
    deadline = float("inf")

    def do_GET(self):
        _DripHandler.hits.append(self.path)
        if self.path == "/fast":
            body = json.dumps({"data": [{"id": "example:free",
                                         "pricing": {"input": "0", "output": "0"},
                                         "modalities": ["chat"]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "1000000")
        self.end_headers()
        # Bounded handler lifetime: stop wakes the cadence sleep at teardown
        # and deadline is the backstop, so a lost FIN cannot outlive the test.
        while not _DripHandler.stop.is_set() and time.monotonic() < _DripHandler.deadline:
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if _DripHandler.stop.wait(2.0):
                return

    def log_message(self, *args):
        pass


class _TLSHandshakeServer(socketserver.TCPServer):
    """TCPServer that wraps ACCEPTED sockets in TLS (never the listener).

    A timeout on the listening socket does not bound the accepted client's
    handshake (proven: accept wedged 5x past the listener timeout on a
    half-open peer). Each accepted socket therefore gets a finite timeout
    BEFORE the server-side handshake runs, so a half-open peer can wedge
    get_request() for at most _HANDSHAKE_TIMEOUT seconds; socketserver drops
    the resulting OSError and the serve loop continues.
    """

    _HANDSHAKE_TIMEOUT = 5.0

    def __init__(self, *args, ssl_context, **kwargs):
        self._ssl_context = ssl_context
        super().__init__(*args, **kwargs)

    def get_request(self):
        newsock, addr = self.socket.accept()
        tls = self._ssl_context.wrap_socket(newsock, server_side=True,
                                            do_handshake_on_connect=False)
        try:
            tls.settimeout(self._HANDSHAKE_TIMEOUT)
            tls.do_handshake()
        except OSError:
            tls.close()
            raise
        return tls, addr


def _start_tls_drip_server(fixtures):
    """Start the loopback TLS drip server; reset per-test handler state."""
    _DripHandler.hits = []
    _DripHandler.stop.clear()
    _DripHandler.deadline = time.monotonic() + 120.0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(fixtures / "localhost.pem"), str(fixtures / "localhost_key.pem"))
    server = _TLSHandshakeServer(("127.0.0.1", 0), _DripHandler, ssl_context=context)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              daemon=True)
    thread.start()
    return server, thread


def _stop_tls_server(server, thread):
    """Bounded teardown: stop handlers, shutdown, join with a finite timeout.

    Never hangs: the serve loop selects on a 0.1s cadence and every accepted
    handshake is bounded by _HANDSHAKE_TIMEOUT, so shutdown() always lands.
    A missed join fails loudly with faulthandler stacks instead of wedging.
    """
    _DripHandler.stop.set()
    server.shutdown()
    thread.join(timeout=15.0)
    server.server_close()
    if thread.is_alive():
        faulthandler.dump_traceback()
        pytest.fail("TLS drip serve thread did not stop within 15s")


def _run_bounded(label, func, timeout):
    """Run func() in a worker thread under a bounded wait (not a hard timeout).

    thread.join(timeout) only bounds how long THIS CALL waits: on timeout the
    worker is neither killed nor joined -- it keeps running as a daemon until
    func() returns or the interpreter exits -- while this call fails loudly
    with faulthandler stacks instead of wedging the suite. Remaining work is
    therefore limited to func()'s own bounds plus daemon abandonment at exit;
    a process hard bound needs a real outer subprocess timeout (timeout(1)).
    """
    outcome = {}

    def worker():
        try:
            outcome["result"] = func()
        except BaseException as error:  # propagate, never swallow
            outcome["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        faulthandler.dump_traceback()
        pytest.fail(f"{label} exceeded outer {timeout}s timeout")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def test_actual_transport_repeat_cancel_retry(monkeypatch, tmp_path):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.setenv("SSL_CERT_FILE", str(fixtures / "localhost.pem"))
    real_getaddrinfo = socket.getaddrinfo
    resolved = []

    def slow_resolve(*args, **kwargs):
        resolved.append(True)
        time.sleep(1.0)
        return real_getaddrinfo(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow_resolve)
    server, thread = _start_tls_drip_server(fixtures)
    port = server.server_address[1]
    # https (scheme-checked) + "localhost" (not the literal) forces the real
    # getaddrinfo path: anyio parses IP literals without resolving.
    monkeypatch.setattr(d, "load_registry",
                        lambda *a, **k: {"openrouter": provider(f"https://localhost:{port}/slow")})
    path = tmp_path / "c.json"

    async def runner():
        results = {}
        monkeypatch.setattr(d, "load_registry",
                            lambda *a, **k: {"openrouter": provider(f"https://localhost:{port}/fast")})
        fast = await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=6.0)
        results["fast"] = fast["providers"]["openrouter"]["status"]
        monkeypatch.setattr(d, "load_registry",
                            lambda *a, **k: {"openrouter": provider(f"https://localhost:{port}/slow")})
        start = time.monotonic()
        slow = await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=6.0)
        results["slow"] = (slow["providers"]["openrouter"]["status"], time.monotonic() - start)
        before = path.read_bytes()
        task = asyncio.ensure_future(d.arefresh_catalog(
            {}, ["openrouter"], path=path, time_budget_seconds=40.0))
        await asyncio.sleep(3.0)
        task.cancel()
        try:
            await task
            results["cancelled"] = False
        except asyncio.CancelledError:
            results["cancelled"] = True
        results["identical"] = path.read_bytes() == before
        results["gate_free"] = try_lock_nonblocking(lock_path_for(path))
        retry = await d.arefresh_catalog({}, ["openrouter"], path=path, time_budget_seconds=6.0)
        results["retry"] = retry["providers"]["openrouter"]["status"]
        return results

    try:
        results = _run_bounded("repeat-cancel-retry",
                               lambda: asyncio.run(runner()), timeout=120.0)
    finally:
        _stop_tls_server(server, thread)
    assert resolved, "expected real getaddrinfo path"
    assert "/fast" in _DripHandler.hits and "/slow" in _DripHandler.hits
    assert results["fast"] == "ok"
    assert results["slow"][0] == "deferred" and 5.5 < results["slow"][1] < 12.0
    assert results["cancelled"] is True
    assert results["identical"] is True
    assert results["gate_free"] is True
    assert results["retry"] == "deferred"


def test_tls_fixture_survives_held_open_handshake(monkeypatch, tmp_path):
    """Held-open TLS peer: retry queues behind the live handshake, then lands.

    Discriminating regression proof for the accepted-handshake wedge
    (listener timeouts do not bound it): the first peer stays
    connected-but-silent for the WHOLE retry -- closing it first would
    release even the old broken fixture's unlimited handshake, proving
    nothing. The single-threaded server must therefore serve this fetch only
    after its 5s accepted-handshake wait on the held peer expires, all with
    real transport under a bounded wait.
    """
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.setenv("SSL_CERT_FILE", str(fixtures / "localhost.pem"))
    server, thread = _start_tls_drip_server(fixtures)
    port = server.server_address[1]
    monkeypatch.setattr(d, "load_registry",
                        lambda *a, **k: {"openrouter": provider(f"https://localhost:{port}/fast")})
    path = tmp_path / "c.json"

    def body():
        peer = socket.create_connection(("127.0.0.1", port))
        try:
            time.sleep(1.0)  # let the server enter the handshake wait

            async def runner():
                return await d.arefresh_catalog({}, ["openrouter"], path=path,
                                                time_budget_seconds=10.0)

            start = time.monotonic()
            status = asyncio.run(runner())["providers"]["openrouter"]["status"]
            elapsed = time.monotonic() - start
            assert peer.fileno() != -1  # held open across the whole retry
        finally:
            peer.close()
        return status, elapsed

    try:
        status, elapsed = _run_bounded("held-open-handshake", body, timeout=60.0)
    finally:
        teardown_start = time.monotonic()
        _stop_tls_server(server, thread)
        teardown_secs = time.monotonic() - teardown_start
    assert status == "ok"
    # Queued behind the live 5s handshake wait (the no-queue fast path would
    # be <1.5s), yet bounded well inside the 10s fetch budget.
    assert 3.0 < elapsed < 10.0
    assert "/fast" in _DripHandler.hits
    assert teardown_secs < 15.0
