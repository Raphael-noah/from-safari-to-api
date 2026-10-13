#!/usr/bin/env python3
"""Verification for the text tool-calling protocol in server.py.

Runs offline (no Safari, no DeepSeek): unit-tests the renderer/parser and an
integration loop through /v1/messages with a stateful fake _chat.

Usage: python3 test_tool_protocol.py
"""
from __future__ import annotations

import server
from server import AnthropicMessagesRequest, AnthropicMessage


def test_render_preamble_fresh():
    req = AnthropicMessagesRequest(
        model="claude-opus-5-5",
        system="你是助手",
        tools=[
            {"name": "Read", "description": "读文件", "input_schema": {"type": "object"}},
            {"name": "Bash", "description": "执行命令", "input_schema": {"type": "object"}},
        ],
        messages=[AnthropicMessage(role="user", content="读 foo.txt")],
    )
    prompt = server._build_inject_prompt(req)
    assert "<system>" in prompt and "你是助手" in prompt
    assert "Read" in prompt and "Bash" in prompt
    assert "读文件" in prompt
    assert server.TOOL_CALL_OPEN in prompt and server.TOOL_RESULT_OPEN in prompt
    assert "读 foo.txt" in prompt
    # fresh prompt should NOT already contain a rendered tool_result
    assert "文件内容" not in prompt
    print("[ok] render_preamble_fresh")


def test_render_continuation_only_latest_tool_result():
    req = AnthropicMessagesRequest(
        model="x",
        tools=[{"name": "Read", "description": "", "input_schema": {}}],
        messages=[
            AnthropicMessage(role="user", content="读 foo.txt"),
            AnthropicMessage(
                role="assistant",
                content=[
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "foo.txt"},
                    }
                ],
            ),
            AnthropicMessage(
                role="user",
                content=[
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "文件内容: hello"}
                ],
            ),
        ],
    )
    prompt = server._build_inject_prompt(req)
    # continuation injects only the latest user message (the tool_result)
    assert "<system>" not in prompt
    assert "可用工具" not in prompt
    assert 'name="Read"' in prompt
    assert "文件内容: hello" in prompt
    assert "读 foo.txt" not in prompt  # earlier history not re-injected
    print("[ok] render_continuation_only_latest_tool_result")


def test_parse_tool_calls_valid():
    text = (
        "好的，我先读文件。\n"
        '<tool_call>\n{"name":"Read","arguments":{"file_path":"foo.txt"}}\n</tool_call>\n'
    )
    calls = server._parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "Read"
    assert calls[0]["arguments"] == {"file_path": "foo.txt"}
    print("[ok] parse_tool_calls_valid")


def test_parse_tool_calls_multiple():
    text = (
        '<tool_call>{"name":"Bash","arguments":{"command":"ls"}}</tool_call>'
        '<tool_call>{"name":"Read","arguments":null}</tool_call>'
    )
    calls = server._parse_tool_calls(text)
    assert [c["name"] for c in calls] == ["Bash", "Read"]
    assert calls[1]["arguments"] == {}
    print("[ok] parse_tool_calls_multiple")


def test_parse_tool_calls_plain_text_and_malformed():
    assert server._parse_tool_calls("这是纯文本回复") == []
    assert server._parse_tool_calls('<tool_call>{"name":"Read",</tool_call>') == []
    assert server._parse_tool_calls('<tool_call>"Read"</tool_call>') == []
    # no name -> gracefully ignored
    assert server._parse_tool_calls('<tool_call>{"arguments":{}}</tool_call>') == []
    print("[ok] parse_tool_calls_plain_text_and_malformed")


def test_parse_dsml_v32_function_calls():
    text = (
        "好的，我来执行。\n"
        "<｜DSML｜function_calls>\n"
        '<｜DSML｜invoke name="Bash">\n'
        '<｜DSML｜parameter name="command" string="true">ls -la</｜DSML｜parameter>\n'
        '<｜DSML｜parameter name="timeout" string="false">5</｜DSML｜parameter>\n'
        "</｜DSML｜invoke>\n"
        "</｜DSML｜function_calls>"
    )
    calls = server._parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "Bash"
    assert calls[0]["arguments"] == {"command": "ls -la", "timeout": 5}
    print("[ok] parse_dsml_v32_function_calls")


def test_parse_dsml_v4_tool_calls_and_json_value():
    text = (
        "<｜DSML｜tool_calls>\n"
        '<｜DSML｜invoke name="Bash">\n'
        '<｜DSML｜parameter name="command" string="true">echo hi</｜DSML｜parameter>\n'
        '<｜DSML｜parameter name="tags" string="false">["a","b"]</｜DSML｜parameter>\n'
        "</｜DSML｜invoke>\n"
        "</｜DSML｜tool_calls>"
    )
    calls = server._parse_dsml_tool_calls(text)
    assert calls == [
        {"name": "Bash", "arguments": {"command": "echo hi", "tags": ["a", "b"]}}
    ]
    print("[ok] parse_dsml_v4_tool_calls_and_json_value")


def test_parse_dsml_v41_leading_space():
    text = (
        "需要工具。\n\n"
        "<｜DSML｜ calls>\n"
        '<｜DSML｜ invoke name="Read">\n'
        '<｜DSML｜ parameter name="file_path" string="true">/etc/hosts</｜DSML｜ parameter>\n'
        "</｜DSML｜ invoke>\n"
        "</｜DSML｜ calls>"
    )
    calls = server._parse_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["name"] == "Read"
    assert calls[0]["arguments"] == {"file_path": "/etc/hosts"}
    print("[ok] parse_dsml_v41_leading_space")


def test_parse_dsml_direct_json_body():
    text = (
        '<｜DSML｜invoke name="Bash">\n'
        '{"command":"ls","args":["-la"]}\n'
        "</｜DSML｜invoke>"
    )
    calls = server._parse_dsml_tool_calls(text)
    assert calls == [{"name": "Bash", "arguments": {"command": "ls", "args": ["-la"]}}]
    print("[ok] parse_dsml_direct_json_body")


def test_parse_dsml_no_calls_for_plain_text():
    assert server._parse_dsml_tool_calls("这是纯文本，没有 DSML") == []
    print("[ok] parse_dsml_no_calls_for_plain_text")


def test_parse_tool_calls_mixed_formats_merged_in_order():
    # The reported failure mode: first part <tool_call>, tail part DSML. Both
    # must be captured, in document order, so the DSML tail is never dropped.
    text = (
        '<tool_call>\n{"name":"Bash","arguments":{"command":"ls"}}\n</tool_call>\n'
        "再来一个：\n"
        '<｜DSML｜invoke name="Read">\n'
        '<｜DSML｜parameter name="file_path" string="true">foo.txt</｜DSML｜parameter>\n'
        "</｜DSML｜invoke>"
    )
    calls = server._parse_tool_calls(text)
    assert [c["name"] for c in calls] == ["Bash", "Read"]
    assert calls[1]["arguments"] == {"file_path": "foo.txt"}
    print("[ok] parse_tool_calls_mixed_formats_merged_in_order")


def test_anthropic_content_translation():
    content, stop = server._anthropic_content("hi", [{"name": "Bash", "arguments": {"command": "ls"}}])
    assert stop == "tool_use"
    assert content[0]["type"] == "tool_use"
    assert content[0]["name"] == "Bash"

    content, stop = server._anthropic_content("hi", [])
    assert stop == "end_turn"
    assert content == [{"type": "text", "text": "hi"}]
    print("[ok] anthropic_content_translation")


def test_sse_tool_use_stream():
    events = "".join(
        server._anthropic_sse("", "m", "id1", [{"name": "Read", "arguments": {"file_path": "f"}}])
    )
    assert "content_block_start" in events
    assert '"type": "tool_use"' in events
    assert "input_json_delta" in events
    assert '"stop_reason": "tool_use"' in events
    assert "message_stop" in events
    print("[ok] sse_tool_use_stream")


def test_title_request_short_circuit():
    title_text = (
        "<session>\n阅读该文件夹下面的内容，请调用工具\n</session>\n\n"
        "Write the title in the predominant language of the session — a stray word "
        "or code token in another language doesn't change it."
    )
    assert server._is_title_request(title_text)
    assert server._extract_title(title_text) == "阅读该文件夹下面的内容，请调用工具"
    assert not server._is_title_request("阅读该文件夹下面的内容，请调用工具")

    # endpoint path: title request must NOT hit _chat
    calls = []
    original_chat = server._chat
    server._chat = lambda *a, **k: calls.append(a) or {"response": "x", "elapsed_seconds": 0, "selectors": {}}
    try:
        req = AnthropicMessagesRequest(
            model="x",
            messages=[AnthropicMessage(role="user", content=title_text)],
        )
        resp = server.anthropic_messages(req)
    finally:
        server._chat = original_chat
    assert calls == []
    assert resp["stop_reason"] == "end_turn"
    assert resp["content"] == [{"type": "text", "text": "阅读该文件夹下面的内容，请调用工具"}]
    print("[ok] title_request_short_circuit")


def test_integration_loop_stateful_fake():
    """Simulate Claude Code's tool_use -> tool_result round-trip."""
    calls = []

    def fake_chat(prompt, *_):
        calls.append(prompt)
        if len(calls) == 1:
            return {"response": '<tool_call>\n{"name":"Read","arguments":{"file_path":"foo.txt"}}\n</tool_call>',
                    "elapsed_seconds": 0.1, "selectors": {}}
        return {"response": "文件内容: hello world", "elapsed_seconds": 0.1, "selectors": {}}

    original = server._chat
    original_lazy = server.LAZY_TOOLS
    server._chat = fake_chat
    server.LAZY_TOOLS = False  # test the non-lazy single-pass preamble path
    try:
        # turn 1: fresh, no tool blocks
        req1 = AnthropicMessagesRequest(
            model="x",
            system="你是助手",
            tools=[{"name": "Read", "description": "读文件", "input_schema": {"type": "object"}}],
            messages=[AnthropicMessage(role="user", content="读 foo.txt")],
        )
        resp1 = server.anthropic_messages(req1)
        assert resp1["stop_reason"] == "tool_use"
        assert resp1["content"][0]["type"] == "tool_use"
        assert resp1["content"][0]["name"] == "Read"
        tool_id = resp1["content"][0]["id"]

        # turn 2: continuation with the tool_result
        req2 = AnthropicMessagesRequest(
            model="x",
            tools=[{"name": "Read", "description": "", "input_schema": {}}],
            messages=[
                AnthropicMessage(role="user", content="读 foo.txt"),
                AnthropicMessage(
                    role="assistant",
                    content=[{"type": "tool_use", "id": tool_id, "name": "Read",
                              "input": {"file_path": "foo.txt"}}],
                ),
                AnthropicMessage(
                    role="user",
                    content=[{"type": "tool_result", "tool_use_id": tool_id,
                              "content": "文件内容: hello world"}],
                ),
            ],
        )
        resp2 = server.anthropic_messages(req2)
        assert resp2["stop_reason"] == "end_turn"
        assert resp2["content"] == [{"type": "text", "text": "文件内容: hello world"}]
    finally:
        server._chat = original
        server.LAZY_TOOLS = original_lazy

    assert len(calls) == 2
    assert "你是助手" in calls[0] and "Read" in calls[0]
    assert calls[1].strip().startswith('<tool_result name="Read">')
    print("[ok] integration_loop_stateful_fake")


def test_lazy_tool_flow_three_round():
    """Progressive disclosure: probe -> tool names -> full schema for chosen tools only."""
    calls = []

    def fake_chat(prompt, *_):
        calls.append(prompt)
        if len(calls) == 1:
            return {"response": "需要工具", "elapsed_seconds": 0.1, "selectors": {}}
        if len(calls) == 2:
            return {"response": "Read", "elapsed_seconds": 0.1, "selectors": {}}
        return {"response": '<tool_call>\n{"name":"Read","arguments":{"file_path":"foo.txt"}}\n</tool_call>',
                "elapsed_seconds": 0.1, "selectors": {}}

    original = server._chat
    original_lazy = server.LAZY_TOOLS
    server._chat = fake_chat
    server.LAZY_TOOLS = True
    try:
        req = AnthropicMessagesRequest(
            model="x",
            system="你是助手",
            tools=[
                {"name": "Read", "description": "读文件",
                 "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}},
                {"name": "Bash", "description": "执行命令",
                 "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}},
            ],
            messages=[AnthropicMessage(role="user", content="读 foo.txt")],
        )
        resp = server.anthropic_messages(req)
    finally:
        server._chat = original
        server.LAZY_TOOLS = original_lazy

    assert len(calls) == 3
    # round 1: user request only, no tools/system disclosed
    assert "可用工具" not in calls[0] and "Read" not in calls[0]
    assert "读 foo.txt" in calls[0]
    # round 2: tool names + descriptions, no schema types, no convention
    assert "可用工具" in calls[1] and "Read" in calls[1] and "Bash" in calls[1]
    assert server.TOOL_CALL_OPEN not in calls[1]
    # round 3: full schema for ONLY the chosen tool (Read), convention present
    assert server.TOOL_CALL_OPEN in calls[2]
    assert "file_path:string" in calls[2]      # full schema disclosed
    assert "command:string" not in calls[2]    # Bash not chosen -> not disclosed
    assert resp["stop_reason"] == "tool_use"
    assert resp["content"][0]["name"] == "Read"
    print("[ok] lazy_tool_flow_three_round")


def test_lazy_tool_flow_direct_answer():
    """When the model can answer without tools, pass 2 is skipped."""
    calls = []

    def fake_chat(prompt, *_):
        calls.append(prompt)
        return {"response": "直接回答", "elapsed_seconds": 0.1, "selectors": {}}

    original = server._chat
    original_lazy = server.LAZY_TOOLS
    server._chat = fake_chat
    server.LAZY_TOOLS = True
    try:
        req = AnthropicMessagesRequest(
            model="x",
            tools=[{"name": "Read", "description": "", "input_schema": {}}],
            messages=[AnthropicMessage(role="user", content="你好")],
        )
        resp = server.anthropic_messages(req)
    finally:
        server._chat = original
        server.LAZY_TOOLS = original_lazy

    assert len(calls) == 1  # no second pass
    assert resp["stop_reason"] == "end_turn"
    assert resp["content"] == [{"type": "text", "text": "直接回答"}]
    print("[ok] lazy_tool_flow_direct_answer")


def test_tool_convention_forbids_dsml():
    text = server._tool_convention()
    assert "DSML" in text
    assert "｜DSML｜" in text
    assert "禁止" in text
    assert server.TOOL_CALL_OPEN in text and server.TOOL_CALL_CLOSE in text
    # the forbidden tags are named explicitly (so the model can connect DSML
    # to the banned XML-ish formats instead of treating them as separate)
    assert "invoke" in text and "parameter" in text
    print("[ok] tool_convention_forbids_dsml")


def test_applescript_args_includes_poll_and_continue():
    args = server._applescript_args("hi", "in", "out", "", "")
    assert "--poll-interval" in args and "--poll-max" in args
    assert args[args.index("--poll-interval") + 1] == str(server.POLL_INTERVAL)
    assert args[args.index("--poll-max") + 1] == str(server.POLL_MAX)
    assert "-c" not in args  # empty continue selector -> no -c flag

    args2 = server._applescript_args("hi", "in", "out", "", ".btn-continue")
    assert "-c" in args2 and ".btn-continue" in args2
    print("[ok] applescript_args_includes_poll_and_continue")


if __name__ == "__main__":
    test_render_preamble_fresh()
    test_render_continuation_only_latest_tool_result()
    test_parse_tool_calls_valid()
    test_parse_tool_calls_multiple()
    test_parse_tool_calls_plain_text_and_malformed()
    test_parse_dsml_v32_function_calls()
    test_parse_dsml_v4_tool_calls_and_json_value()
    test_parse_dsml_v41_leading_space()
    test_parse_dsml_direct_json_body()
    test_parse_dsml_no_calls_for_plain_text()
    test_parse_tool_calls_mixed_formats_merged_in_order()
    test_anthropic_content_translation()
    test_sse_tool_use_stream()
    test_title_request_short_circuit()
    test_integration_loop_stateful_fake()
    test_lazy_tool_flow_three_round()
    test_lazy_tool_flow_direct_answer()
    test_tool_convention_forbids_dsml()
    test_applescript_args_includes_poll_and_continue()
    print("\nALL TESTS PASSED")
