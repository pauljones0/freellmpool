"""Browser requests cannot turn a local gateway into a quota-spending deputy."""

from __future__ import annotations

import http.client
import json
import threading
from email.message import Message

import pytest
from helpers import make_post

from freellmpool.proxy import serve
from freellmpool.request_security import RequestBoundaryPolicy, validate_boundary
from freellmpool.router import Pool


def _headers(**values):
    headers = Message()
    for name, value in values.items():
        headers[name.replace("_", "-")] = value
    return headers


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8080", "http://localhost:8080"])
def test_cli_and_approved_browser_origins(origin):
    headers = _headers(Host="localhost:8080", Content_Type="application/json; charset=utf-8")
    if origin is not None:
        headers["Origin"] = origin
    assert validate_boundary(headers, RequestBoundaryPolicy.for_address("127.0.0.1", 8080), json_body=True) is None


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("Host", "attacker.test:8080", 403),
        ("Host", "127.0.0.1:9999", 403),
        ("Host", "user@127.0.0.1:8080", 400),
        ("Origin", "https://attacker.test", 403),
        ("Origin", "null", 403),
        ("Origin", "http://localhost:8080/path", 403),
        ("Content-Type", "text/plain", 415),
        ("Content-Type", "application/x-www-form-urlencoded", 415),
        ("Transfer-Encoding", "chunked", 400),
    ],
)
def test_bad_request_boundaries(field, value, status):
    headers = _headers(Host="localhost:8080", Content_Type="application/json")
    if field in headers:
        del headers[field]
    headers[field] = value
    rejection = validate_boundary(headers, RequestBoundaryPolicy.for_address("127.0.0.1", 8080), json_body=True)
    assert rejection.status == status
    assert value not in rejection.message


@pytest.mark.parametrize("field", ["Host", "Origin", "Content-Length", "Content-Type"])
def test_ambiguous_duplicate_headers_fail_closed(field):
    headers = _headers(Host="localhost:8080", Content_Type="application/json")
    if field not in headers:
        headers[field] = "1"
    headers[field] = headers[field]
    assert validate_boundary(headers, RequestBoundaryPolicy.for_address("127.0.0.1", 8080), json_body=True).status == 400


def test_multipart_and_ipv6_authorities():
    headers = _headers(Host="[::1]:8080", Content_Type="multipart/form-data; boundary=abc")
    policy = RequestBoundaryPolicy.for_address("::1", 8080)
    assert validate_boundary(headers, policy, multipart_body=True) is None
    del headers["Content-Type"]
    headers["Content-Type"] = "application/json"
    assert validate_boundary(headers, policy, multipart_body=True).status == 415


@pytest.mark.parametrize("key", [None, "synthetic-proxy-key"])
def test_socket_requests_enforce_host_origin_media_and_auth(providers, env, quota, key):
    post = make_post({})
    pool = Pool(providers, env=env, quota=quota, post=post)
    server = serve(pool, port=0, api_key=key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    payload = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}]})

    def request(headers):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("POST", "/v1/chat/completions", payload, headers)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        assert request({**headers, "Host": f"attacker.test:{port}", "Origin": f"http://attacker.test:{port}"}) == 403
        assert request({**headers, "Origin": "https://attacker.test"}) == 403
        assert request({**headers, "Content-Type": "text/plain"}) == 415
        if key:
            assert request({"Content-Type": "application/json"}) == 401
            assert request({"Content-Type": "application/json", "Authorization": "Bearer caf\u00e9"}) == 401
            assert request({"Content-Type": "application/json", "x-api-key": "caf\u00e9"}) == 401
        assert not post.calls
        assert request({**headers, "Origin": f"http://127.0.0.1:{port}"}) == 200
        assert len(post.calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_explicit_port_zero_is_not_normalized_to_http_default():
    policy = RequestBoundaryPolicy.for_address("127.0.0.1", 80)
    rejection = validate_boundary(_headers(Host="localhost:0"), policy)
    assert rejection is not None and rejection.status == 400
