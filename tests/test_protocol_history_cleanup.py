"""Regressions for replaying coding-agent history through protocol shims."""

import json
import shutil
import subprocess

import pytest

from freellmpool.anthropic_shim import request_to_chat
from freellmpool.proxy import _BROWSER_SHELL_SCRIPT, _responses_input_to_messages


def _call(call_id, name):
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": "{}",
    }


def _result(call_id, output):
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def test_responses_replays_assistant_output_text_without_losing_history():
    messages = _responses_input_to_messages(
        {
            "input": [
                {"role": "user", "content": "Remember 123."},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I will remember 123."}],
                },
                {"role": "user", "content": "What did you say?"},
            ]
        }
    )
    assert messages[1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "I will remember 123."}],
    }


def test_responses_parallel_calls_stay_in_one_assistant_turn():
    messages = _responses_input_to_messages(
        {
            "input": [
                _call("call_a", "read_one"),
                _call("call_b", "read_two"),
                _result("call_a", "first file"),
                _result("call_b", "second file"),
            ]
        }
    )
    assert [message["role"] for message in messages] == ["assistant", "tool", "tool"]
    assert [call["id"] for call in messages[0]["tool_calls"]] == ["call_a", "call_b"]
    assert [message["tool_call_id"] for message in messages[1:]] == ["call_a", "call_b"]


def test_responses_separate_tool_turns_are_not_merged():
    messages = _responses_input_to_messages(
        {
            "input": [
                _call("call_a", "read_one"),
                _result("call_a", "first file"),
                _call("call_b", "read_two"),
                _result("call_b", "second file"),
            ]
        }
    )
    assert [message["role"] for message in messages] == ["assistant", "tool", "assistant", "tool"]
    assert messages[0]["tool_calls"][0]["id"] == "call_a"
    assert messages[2]["tool_calls"][0]["id"] == "call_b"


def test_anthropic_mixed_tool_results_precede_followup_user_text():
    chat = request_to_chat(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_a", "name": "read_one", "input": {}},
                        {"type": "tool_use", "id": "call_b", "name": "read_two", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_a", "content": "first file"},
                        {"type": "tool_result", "tool_use_id": "call_b", "content": "second file"},
                        {"type": "text", "text": "Continue please."},
                    ],
                },
            ]
        }
    )
    assert [message["role"] for message in chat["messages"]] == ["assistant", "tool", "tool", "user"]
    assert [message["tool_call_id"] for message in chat["messages"][1:3]] == ["call_a", "call_b"]
    assert chat["messages"][-1]["content"] == "Continue please."


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for dashboard JS test")
@pytest.mark.parametrize(
    ("models", "expected"),
    [
        ([{"used_today": 2, "daily_limit": None}], "2 / unknown"),
        ([{"used_today": 2}], "2 / unknown"),
        ([{"used_today": 2, "daily_limit": 100}, {"used_today": 3}], "5 / 100 + unknown"),
        ([{"used_today": 2, "daily_limit": 100}], "2 / 100"),
    ],
)
def test_dashboard_does_not_label_unknown_quota_as_unmetered(models, expected):
    # Run the actual rendering helper without starting polling or a DOM.
    helper = _BROWSER_SHELL_SCRIPT.split("function quotaSummary(provider) {", 1)[1].split(
        "function readinessReason", 1
    )[0]
    program = (
        "const formatNumber = value => String(value);\n"
        "function quotaSummary(provider) {"
        + helper
        + "\nconsole.log(quotaSummary("
        + json.dumps({"models": models})
        + "));"
    )
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == expected
