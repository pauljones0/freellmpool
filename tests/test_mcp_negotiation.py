"""MCP must advertise an implemented protocol and reject malformed params."""

import pytest

from freellmpool.mcp_server import _DEFAULT_PROTOCOL, handle_message


@pytest.mark.parametrize("requested", ["2025-06-18", "9999-99-99"])
def test_initialize_negotiates_implemented_version(requested):
    response = handle_message(None, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": requested}})
    assert response["result"]["protocolVersion"] == _DEFAULT_PROTOCOL


@pytest.mark.parametrize("params", [[], "bad", 1, {"protocolVersion": []}, {"protocolVersion": 7}])
def test_malformed_initialize_params_are_invalid_params(params):
    response = handle_message(None, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    assert response["error"]["code"] == -32602
