"""Bounded first-run discovery: budget, deferral, and absolute deadlines (G24 U1/U5)."""

import asyncio
import copy
import gzip
import json
import socket
import threading
import time
import zlib

import httpx
import pytest

from freellmpool import discovery as d
from freellmpool.provider_registry import load_registry


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
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self):
        self.closed = True


def test_discovery_constants_pin():
    assert d.DISCOVERY_BUDGET_SECONDS == 40.0
    assert d._MIN_ATTEMPT_SECONDS == 5.0
    assert d._MIN_PAGE_SECONDS == 3.0
    assert d._NOTE_NETWORK_FAILURE == "Catalog network failure; last-good evidence preserved."
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)
    assert d._IDLE_TIMEOUT == timeout


@pytest.mark.parametrize("env,expected", [
    ({}, 40.0),
    ({"FREELLMPOOL_DISCOVERY_BUDGET_SECONDS": "10"}, 10.0),
    ({"FREELLMPOOL_DISCOVERY_BUDGET_SECONDS": "1"}, 5.0),
    ({"FREELLMPOOL_DISCOVERY_BUDGET_SECONDS": "999"}, 45.0),
    ({"FREELLMPOOL_DISCOVERY_BUDGET_SECONDS": "garbage"}, 40.0),
    ({"FREELLMPOOL_DISCOVERY_BUDGET_SECONDS": "nan"}, 40.0),
])
def test_budget_seconds_parse_clamp(env, expected):
    assert d.budget_seconds(env) == expected


def test_slow_drip_page_aborts_within_budget(monkeypatch, tmp_path):
    spec = provider()
    body = json.dumps({"data": [model()]}).encode()
    chunks = [body[:10]] + [b"x" * 100] * 29
    stream = DripStream(chunks, 0.5)
    install_async(monkeypatch, spec, lambda request: httpx.Response(
        200, headers={"content-type": "application/json"}, stream=stream))
    path = tmp_path / "catalog.json"
    deadline = time.monotonic() + 8.0
    start = time.monotonic()
    snapshot = d.refresh_catalog({}, ["openrouter"], path=path, deadline=deadline)
    elapsed = time.monotonic() - start
    row = snapshot["providers"]["openrouter"]
    assert row["status"] == "deferred"
    assert row["note"] == d._deferred_page_note(1)
    assert row["complete"] is False
    assert row["models"] == [] and row["checked_at"] is None
    assert 7.5 <= elapsed < 8.0 + 8.0
    assert stream.closed
    assert json.loads(path.read_text())["schema"] == 1
    # Lock immediately re-acquirable: a second refresh with a fast fixture succeeds.
    install_async(monkeypatch, spec, lambda request: httpx.Response(200, json={"data": [model()]}))
    again = d.refresh_catalog({}, ["openrouter"], path=path,
                               deadline=time.monotonic() + 10.0)
    assert again["providers"]["openrouter"]["status"] == "ok"


def test_partial_header_stall_aborts(monkeypatch, tmp_path):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(10)
    port = server.getsockname()[1]

    def serve():
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(30)
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
                time.sleep(20)
        except OSError:
            pass
        finally:
            server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    spec = provider(f"http://127.0.0.1:{port}/catalog")
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {spec["id"]: spec})
    monkeypatch.setattr(d, "_same_origin", lambda url, origin: True)
    path = tmp_path / "catalog.json"
    deadline = time.monotonic() + 8.0
    start = time.monotonic()
    snapshot = d.refresh_catalog({}, ["openrouter"], path=path, deadline=deadline)
    elapsed = time.monotonic() - start
    row = snapshot["providers"]["openrouter"]
    assert row["status"] == "deferred"
    assert row["note"] == d._deferred_page_note(1)
    assert 7.5 <= elapsed < 8.0 + 8.0
    assert json.loads(path.read_text())["schema"] == 1


@pytest.mark.parametrize(
    ("timeout_type", "budget", "expected_status", "timeout_field", "expected_timeout"), [
        (httpx.ConnectTimeout, 4.0, "deferred", "connect", 4.0),
        (httpx.ConnectTimeout, 20.0, "error", "connect", 5.0),
        (httpx.ReadTimeout, 8.0, "deferred", "read", 8.0),
        (httpx.ReadTimeout, 20.0, "error", "read", 10.0),
        (httpx.WriteTimeout, 4.0, "deferred", "write", 4.0),
        (httpx.WriteTimeout, 20.0, "error", "write", 5.0),
        (httpx.PoolTimeout, 8.0, "error", "pool", 2.0),
])
def test_phase_timeout_at_deadline_preserves_budget_classification(
        monkeypatch, timeout_type, budget, expected_status, timeout_field,
        expected_timeout):
    """The inner HTTPX timeout must not race the absolute-budget verdict."""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    observed = []

    async def phase_timeout(_client, _url, _headers, timeout, _result, **_kwargs):
        observed.append(getattr(timeout, timeout_field))
        raise timeout_type("synthetic deadline race")

    spec = provider("https://example.invalid/catalog")
    monkeypatch.setattr(d, "_aclient", Client)
    monkeypatch.setattr(d, "_afetch_page", phase_timeout)
    attempt = d._aattempt(spec, {}, deadline=time.monotonic() + budget)
    if expected_status == "deferred":
        with pytest.raises(d._BudgetExhausted) as exhausted:
            asyncio.run(attempt)
        row = exhausted.value.row
    else:
        row = asyncio.run(attempt)

    assert observed == [pytest.approx(expected_timeout, abs=0.1)]
    assert row["status"] == expected_status
    if expected_status == "deferred":
        assert row["note"] == d._deferred_page_note(1)
    else:
        assert row["note"] == d._NOTE_NETWORK_FAILURE


def test_preserved_deferred_keeps_serving(monkeypatch, tmp_path):
    path = tmp_path / "catalog.json"
    old = (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 3600)))
    path.write_text(json.dumps({"schema": 1, "providers": {"openrouter": {
        "status": "ok", "complete": True, "catalog_access": "public",
        "checked_at": old, "models": [model()]}}}))
    spec = provider()
    install_async(monkeypatch, spec, lambda request: pytest.fail("must not hit network"))
    snapshot = d.refresh_catalog({}, ["openrouter"], path=path,
                                   deadline=time.monotonic() + 0.1)
    row = snapshot["providers"]["openrouter"]
    assert row["status"] == "deferred"
    assert row["complete"] is True
    assert row["models"] == [model()]
    assert row["checked_at"] == old
    assert row["note"] == ("Skipped: discovery time budget exhausted (provider not "
                            "attempted); run `freellmpool update` to retry.")


def test_fresh_deferred_shows_reason(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, lambda request: pytest.fail("must not hit network"))
    snapshot = d.refresh_catalog({}, ["openrouter"], path=tmp_path / "c.json",
                                   deadline=time.monotonic() + 0.1)
    row = snapshot["providers"]["openrouter"]
    assert row["status"] == "deferred"
    assert row["complete"] is False
    assert row["models"] == [] and row["checked_at"] is None
    assert row["last_attempt_at"]
    assert row["note"] == ("Skipped: discovery time budget exhausted (provider not "
                            "attempted); run `freellmpool update` to retry.")


def test_deferred_notes_exact():
    assert d._DEFERRED_SKIP_NOTE == ("Skipped: discovery time budget exhausted (provider not "
                                     "attempted); run `freellmpool update` to retry.")
    assert d._deferred_page_note(1) == ("Skipped: discovery time budget exhausted (page 1 not "
                                        "fetched); run `freellmpool update` to retry.")


def test_sync_in_loop_error_exact(monkeypatch, tmp_path):
    async def runner():
        with pytest.raises(RuntimeError) as caught:
            d.refresh_catalog({}, ["openrouter"], path=tmp_path / "c.json")
        assert str(caught.value) == ("freellmpool: refresh_catalog cannot run inside a running "
                                     "event loop; await arefresh_catalog instead.")
    asyncio.run(runner())


def test_in_loop_prenetwork_succeeds_but_network_checks_fail(monkeypatch):
    spec = provider()
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {"openrouter": spec})

    async def runner():
        assert d.check_provider("missing", {})["status"] == "unsupported"
        with pytest.raises(RuntimeError) as caught:
            d.check_provider("openrouter", {})
        assert str(caught.value) == "freellmpool: check_provider cannot run inside a running event loop."
    asyncio.run(runner())


def test_arefresh_catalog_parity(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, lambda request: httpx.Response(200, json={"data": [model()]}))

    async def runner():
        return await d.arefresh_catalog({}, ["openrouter"], path=tmp_path / "c.json",
                                        deadline=time.monotonic() + 10.0)

    row = asyncio.run(runner())["providers"]["openrouter"]
    assert row["status"] == "ok" and row["complete"] is True
    assert [m["id"] for m in row["models"]] == ["example:free"]


def test_dns_stall_no_longer_delays_refresh_return(monkeypatch, tmp_path):
    """G26 closure of the v5.2 shutdown-lag residual (contract changed by goal).

    slow_resolve still stalls 10s; the fetch still fails at the pinned
    connect clamp (error + network note) but refresh now returns at ~5s
    without joining the stalled resolver thread (daemon, abandoned).
    Non-vacuous: a resolver thread must be alive at return and gone after
    its stall elapses. Pre-G26 evidence kept at
    docs/evidence/httpx-async-dns-deadline-2026-09-20.json.
    """
    real_getaddrinfo = socket.getaddrinfo

    def slow_resolve(*args, **kwargs):
        time.sleep(10)
        return real_getaddrinfo("127.0.0.1", 443, type=socket.SOCK_STREAM)

    def resolver_threads():
        return [thread for thread in threading.enumerate()
                if thread.name.startswith("freellmpool-resolver-")]

    monkeypatch.setattr(socket, "getaddrinfo", slow_resolve)
    spec = provider("https://dns-stall.invalid/catalog")
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {spec["id"]: spec})
    start = time.monotonic()
    snapshot = d.refresh_catalog({}, ["openrouter"], path=tmp_path / "c.json",
                                   deadline=time.monotonic() + 5.5)
    elapsed = time.monotonic() - start
    row = snapshot["providers"]["openrouter"]
    assert row["status"] == "error"
    assert row["note"] == d._NOTE_NETWORK_FAILURE
    assert elapsed < 6.5  # G26: return does not wait on the resolver thread
    assert elapsed >= 4.9  # the fetch itself still ran to the connect clamp
    assert resolver_threads()  # the stall was really in flight at return
    deadline = time.monotonic() + 15
    while resolver_threads():
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_refresh_lock_busy_raises_discovery_busy(monkeypatch, tmp_path):
    import fcntl

    path = tmp_path / "catalog.json"
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "a+")  # noqa: PTH123 - intentional lock holder
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert issubclass(d.DiscoveryBusy, OSError)
        with pytest.raises(d.DiscoveryBusy):
            d.refresh_catalog({}, ["openrouter"], path=path, deadline=time.monotonic() + 1.0)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    spec = provider()
    install_async(monkeypatch, spec, lambda request: httpx.Response(200, json={"data": [model()]}))
    again = d.refresh_catalog({}, ["openrouter"], path=path, deadline=time.monotonic() + 10.0)
    assert again["providers"]["openrouter"]["status"] == "ok"


def test_slow_but_working_discovery_completes(monkeypatch, tmp_path):
    """Legitimate slow providers survive the budget (scaled-down 3s/page case)."""

    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"data": [model()]})

    spec = provider()
    install_async(monkeypatch, spec, handler)
    snapshot = d.refresh_catalog({}, ["openrouter"], path=tmp_path / "c.json",
                                   deadline=time.monotonic() + 10.0)
    row = snapshot["providers"]["openrouter"]
    assert row["status"] == "ok" and row["complete"] is True


def test_progress_callback_is_exception_safe(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, lambda request: httpx.Response(200, json={"data": [model()]}))
    calls = []

    def progress(**event):
        calls.append(event)
        raise RuntimeError("caller bug must not break refresh")

    snapshot = d.refresh_catalog({}, ["openrouter"], path=tmp_path / "c.json",
                                   deadline=time.monotonic() + 10.0, progress=progress)
    assert snapshot["providers"]["openrouter"]["status"] == "ok"
    assert calls and all("provider_id" in event for event in calls)


def test_progress_survives_epipe(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, lambda request: httpx.Response(200, json={"data": [model()]}))

    def broken(**event):
        raise BrokenPipeError("closed stderr")

    snapshot = d.refresh_catalog({}, ["openrouter"], path=tmp_path / "c.json",
                                   deadline=time.monotonic() + 10.0, progress=broken)
    assert snapshot["providers"]["openrouter"]["status"] == "ok"


def test_deferred_snapshot_schema_1_parses(monkeypatch, tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"schema": 1, "generation": "g", "updated_at": None,
                                "providers": {"openrouter": {
                                    "status": "deferred", "complete": False, "models": [],
                                    "checked_at": None,
                                    "last_attempt_at": "2026-09-20T00:00:00+00:00",
                                    "note": d._DEFERRED_SKIP_NOTE}}}))
    spec = provider()
    monkeypatch.setattr(d, "load_registry", lambda *a, **k: {spec["id"]: spec})
    loaded = d._load(path)
    assert loaded["schema"] == 1
    assert loaded["providers"]["openrouter"]["status"] == "deferred"


def test_progress_emits_provider_and_page_events(monkeypatch, tmp_path):
    spec = provider()
    install_async(monkeypatch, spec, lambda request: httpx.Response(200, json={"data": [model()]}))
    calls = []
    d.refresh_catalog({}, ["openrouter"], path=tmp_path / "c.json",
                      deadline=time.monotonic() + 10.0,
                      progress=lambda **event: calls.append(event))
    by_kind = [("page" if "page" in event else "provider", event["provider_id"]) for event in calls]
    assert ("provider", "openrouter") in by_kind
    assert ("page", "openrouter") in by_kind
    assert [event for event in calls if "page" in event][0]["page"] == 1


def _parity_cases():
    raw = b'{"data": [{"id": "example:free"}]}' * 50
    gz = gzip.compress(raw)
    cases = [("identity", raw, {}), ("gzip", gz, {"content-encoding": "gzip"}),
             ("deflate", zlib.compress(raw), {"content-encoding": "deflate"})]
    raw_deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    cases.append(("raw-deflate", raw_deflate.compress(raw) + raw_deflate.flush(),
                  {"content-encoding": "deflate"}))
    return raw, cases


def test_body_decoder_sync_async_parity():
    import asyncio as aio

    from freellmpool.http_read import abounded_response_bytes, bounded_response_bytes

    raw, cases = _parity_cases()

    class SyncStream(httpx.SyncByteStream):
        def __init__(self, chunks):
            self.chunks = chunks

        def __iter__(self):
            yield from self.chunks

    for name, payload, headers in cases:
        splits = [[payload[:1], payload[1:2], payload[2:7], payload[7:]],
                  [payload[i:i + 3] for i in range(0, len(payload), 3)]]
        for chunks in splits:
            sync = httpx.Response(200, headers=headers, stream=SyncStream(list(chunks)))
            async_resp = httpx.Response(200, headers=headers,
                                        stream=DripStream(list(chunks), 0))
            expected = bounded_response_bytes(sync, 1 << 20)
            assert aio.run(abounded_response_bytes(async_resp, 1 << 20)) == expected == raw, name


def test_body_decoder_error_parity():
    import asyncio as aio

    from freellmpool.http_read import abounded_response_bytes, bounded_response_bytes

    class SyncStream(httpx.SyncByteStream):
        def __init__(self, chunks):
            self.chunks = chunks

        def __iter__(self):
            yield from self.chunks

    gz = gzip.compress(b"x" * 1000)
    for payload, headers in [(gz[:10], {"content-encoding": "gzip"}), (b"y" * 100, {})]:
        sync = httpx.Response(200, headers=headers, stream=SyncStream([payload]))
        async_resp = httpx.Response(200, headers=headers, stream=DripStream([payload], 0))
        with pytest.raises(ValueError) as sync_caught:
            bounded_response_bytes(sync, 50)
        with pytest.raises(ValueError) as async_caught:
            aio.run(abounded_response_bytes(async_resp, 50))
        assert str(sync_caught.value) == str(async_caught.value)
