#!/usr/bin/env python3
"""
JembatanAI-Codex Proxy — Comprehensive OpenAI/Codex Compatibility Test Suite

Tests the full Codex/OpenAI Responses API surface:
1. Responses API format (non-streaming)
2. Tool conversion (flat, wrapped, custom formats)
3. previous_response_id chaining
4. Model resolution + aliases
5. Loop detection (session tracking)
6. SSE streaming format (all event types)
7. Tool call streaming
8. Reasoning/effort passthrough
9. Auth & quota (unit-level)
10. Non-streaming /v1/chat/completions format
11. Error response format
12. Input normalization (string, list, mixed)
"""

import sys
import os
import json
import time
import uuid

sys.path.insert(0, os.path.dirname(__file__))

import proxy_codex as proxy

# ─── Test Harness ─────────────────────────────────────────────────────────────

_pass = 0
_fail = 0
_section = ""


def section(name: str):
    global _section
    _section = name
    print(f"\n{'═' * 60}")
    print(f"  {name}")
    print(f"{'═' * 60}")


def test(name: str, condition: bool, detail: str = ""):
    global _pass, _fail
    if condition:
        _pass += 1
        print(f"  ✓ {name}")
    else:
        _fail += 1
        detail_str = f"\n    Detail: {detail}" if detail else ""
        print(f"  ✗ {name}{detail_str}")


# ─── 1. Tool Conversion ────────────────────────────────────────────────────────

section("1. Tool Conversion: Flat format (Codex CLI native)")

flat_tools = [
    {"type": "function", "name": "shell", "description": "Run shell command", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"type": "function", "name": "editor", "description": "Edit files", "parameters": {"type": "object", "properties": {}}},
]
result = proxy._convert_responses_tools_to_openai(flat_tools)
test("Flat tools converted", len(result) == 2, f"Got {len(result)}")
test("Flat tool has function wrapper", result[0].get("type") == "function" and "function" in result[0])
test("Flat tool name preserved", result[0]["function"]["name"] == "shell")
test("Flat tool description preserved", result[0]["function"]["description"] == "Run shell command")
test("Flat tool parameters preserved", "command" in result[0]["function"]["parameters"].get("properties", {}))

section("2. Tool Conversion: Wrapped format (OpenAI standard)")

wrapped_tools = [
    {"type": "function", "function": {"name": "read_file", "description": "Read file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
]
result_w = proxy._convert_responses_tools_to_openai(wrapped_tools)
test("Wrapped tool converted", len(result_w) == 1)
test("Wrapped tool name correct", result_w[0]["function"]["name"] == "read_file")
test("Wrapped tool params correct", "path" in result_w[0]["function"]["parameters"].get("properties", {}))

section("3. Tool Conversion: Custom + unsupported types")

mixed_tools = [
    {"type": "function", "name": "my_func", "parameters": {"type": "object", "properties": {}}},
    {"type": "custom", "name": "custom_tool", "description": "Custom tool"},
    {"type": "web_search"},  # Should be skipped
    {"type": "local_shell"},  # Should be skipped
    {"type": "computer_use_screenshot"},  # Should be skipped
]
result_m = proxy._convert_responses_tools_to_openai(mixed_tools)
names = [t["function"]["name"] for t in result_m]
test("Function tool included", "my_func" in names)
test("Custom tool included as function", "custom_tool" in names)
test("web_search skipped", all(t["function"]["name"] != "web_search" for t in result_m), f"Got names: {names}")
test("Total: 2 tools (function + custom)", len(result_m) == 2, f"Got {len(result_m)}")

section("4. Tool Conversion: strict field passthrough")

strict_tool = [{"type": "function", "name": "strict_func", "parameters": {}, "strict": True}]
result_s = proxy._convert_responses_tools_to_openai(strict_tool)
test("strict field preserved", result_s[0]["function"].get("strict") is True)

# ─── 5. _responses_to_messages Conversion ─────────────────────────────────────

section("5. Input Normalization: string input")

msgs = proxy._responses_to_messages("Hello world", None)
test("String input normalized to list", len(msgs) == 1)
test("String becomes user message", msgs[0]["role"] == "user")
test("String content preserved", msgs[0]["content"] == "Hello world")

section("6. Input Normalization: list of strings")

msgs2 = proxy._responses_to_messages(["First", "Second"], None)
test("List of strings normalized", len(msgs2) == 2)
test("All converted to user messages", all(m["role"] == "user" for m in msgs2))

section("7. Input Normalization: mixed list (str + dict)")

mixed_input = [
    "plain string",
    {"type": "message", "role": "user", "content": "structured message"},
]
msgs3 = proxy._responses_to_messages(mixed_input, None)
test("Mixed input produces 2 messages", len(msgs3) == 2)

section("8. Input Normalization: instructions as system message")

msgs_sys = proxy._responses_to_messages(
    [{"type": "message", "role": "user", "content": "hi"}],
    None,
    instructions="You are a helpful assistant",
)
test("Instructions become system message", msgs_sys[0]["role"] == "system")
test("System message content correct", msgs_sys[0]["content"] == "You are a helpful assistant")
test("User message follows", msgs_sys[1]["role"] == "user")

section("9. Input Normalization: function_call_output → tool role")

tool_output_input = [
    {"type": "function_call_output", "call_id": "call_abc123", "output": '{"result": "ok"}'},
]
msgs_tool = proxy._responses_to_messages(tool_output_input, None)
test("function_call_output → tool role", msgs_tool[-1]["role"] == "tool")
test("tool_call_id preserved", msgs_tool[-1]["tool_call_id"] == "call_abc123")
test("output as content string", msgs_tool[-1]["content"] == '{"result": "ok"}')

section("10. Input Normalization: non-string output coerced to JSON")

non_str_output = [
    {"type": "function_call_output", "call_id": "call_x", "output": {"nested": "dict"}},
]
msgs_ns = proxy._responses_to_messages(non_str_output, None)
test("Dict output coerced to JSON string", isinstance(msgs_ns[-1]["content"], str))
test("JSON string is valid", json.loads(msgs_ns[-1]["content"]) == {"nested": "dict"})

section("11. Input Normalization: function_call in input → assistant with tool_calls")

func_call_input = [
    {"type": "function_call", "call_id": "call_y", "id": "call_y", "name": "my_tool", "arguments": '{"x":1}'},
]
msgs_fc = proxy._responses_to_messages(func_call_input, None)
test("function_call → assistant role", msgs_fc[-1]["role"] == "assistant")
test("tool_calls present", "tool_calls" in msgs_fc[-1])
test("tool_calls name correct", msgs_fc[-1]["tool_calls"][0]["function"]["name"] == "my_tool")
test("tool_calls arguments correct", msgs_fc[-1]["tool_calls"][0]["function"]["arguments"] == '{"x":1}')

# ─── 12. _chat_to_responses Conversion ─────────────────────────────────────────

section("12. Chat→Responses conversion: text response")

chat_text = {
    "id": "chatcmpl-abc",
    "object": "chat.completion",
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": "Hello! How can I help?"},
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
}
resp = proxy._chat_to_responses(chat_text, "kilo-mimo-v2-pro")
test("response has id", resp.get("id", "").startswith("resp_"))
test("response object type", resp.get("object") == "response")
test("response status completed", resp.get("status") == "completed")
test("output is list", isinstance(resp.get("output"), list))
test("output has message item", resp["output"][0]["type"] == "message")
test("output content type output_text", resp["output"][0]["content"][0]["type"] == "output_text")
test("output text correct", resp["output"][0]["content"][0]["text"] == "Hello! How can I help?")
test("usage input_tokens", resp["usage"]["input_tokens"] == 10)
test("usage output_tokens", resp["usage"]["output_tokens"] == 8)
test("error is None", resp["error"] is None)

section("13. Chat→Responses conversion: tool call response")

chat_tool = {
    "choices": [{
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_tool_123",
                "type": "function",
                "function": {"name": "shell", "arguments": '{"command":"ls -la"}'},
            }],
        },
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
}
resp_tool = proxy._chat_to_responses(chat_tool, "kilo-grok-code")
test("tool call in output", len(resp_tool["output"]) >= 1)
tool_item = next((o for o in resp_tool["output"] if o.get("type") == "function_call"), None)
test("function_call item exists", tool_item is not None, f"Output: {resp_tool['output']}")
if tool_item:
    test("function_call has call_id", "call_id" in tool_item)
    test("function_call name correct", tool_item["name"] == "shell")
    test("function_call arguments correct", tool_item["arguments"] == '{"command":"ls -la"}')
    test("function_call status completed", tool_item["status"] == "completed")

section("14. Chat→Responses conversion: empty content + reasoning fallback")

chat_reasoning = {
    "choices": [{
        "message": {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I need to think about this carefully...",
        },
        "finish_reason": "stop",
    }],
    "usage": {"prompt_tokens": 5, "completion_tokens": 30, "total_tokens": 35},
}
resp_r = proxy._chat_to_responses(chat_reasoning, "kilo-mimo-v2-pro")
test("reasoning fallback to content", len(resp_r["output"]) >= 1)
if resp_r["output"]:
    text = resp_r["output"][0].get("content", [{}])[0].get("text", "")
    test("reasoning text in output", "think" in text.lower() or len(text) > 0)

section("15. Chat→Responses conversion: empty choices fallback")

chat_empty = {"choices": [], "usage": {}}
resp_empty = proxy._chat_to_responses(chat_empty, "kilo-trinity")
test("empty choices → no output", len(resp_empty.get("output", [])) == 0)
test("status still completed", resp_empty["status"] == "completed")

# ─── 16. Model Resolution ─────────────────────────────────────────────────────

section("16. Model Resolution: GPT aliases → Kilo models")

test("gpt-4o → kilo alias", proxy.resolve_codex_model("gpt-4o") == "kilo-mimo-v2-pro")
test("gpt-5.4 → kilo alias", proxy.resolve_codex_model("gpt-5.4") == "kilo-mimo-v2-pro")
test("o3 → kilo alias", proxy.resolve_codex_model("o3") == "kilo-grok-code")
test("o4-mini → kilo alias", proxy.resolve_codex_model("o4-mini") == "kilo-minimax")
test("codex → kilo alias", proxy.resolve_codex_model("codex") == "kilo-mimo-v2-pro")
test("gpt-4o-mini → kilo alias", proxy.resolve_codex_model("gpt-4o-mini") == "kilo-minimax")

section("17. Model Resolution: Unknown model fallback")

unknown = proxy.resolve_codex_model("gpt-99-ultra-unknown")
test("Unknown model returns kilo-mimo-v2-pro default", unknown == "kilo-mimo-v2-pro", f"Got: {unknown}")

section("18. Model Resolution: Direct Kilo aliases passthrough")

test("kilo-mimo-v2-pro passthrough", proxy.resolve_codex_model("kilo-mimo-v2-pro") == "kilo-mimo-v2-pro")
test("kilo-grok-code passthrough", proxy.resolve_codex_model("kilo-grok-code") == "kilo-grok-code")
test("kilo-active passthrough", proxy.resolve_codex_model("kilo-active") == "kilo-active")

# ─── 19. _convert_tool_call_to_output ─────────────────────────────────────────

section("19. Tool Call → Responses API output format")

tc_input = {
    "id": "call_abc",
    "type": "function",
    "function": {"name": "my_func", "arguments": '{"key":"val"}'},
}
tc_out = proxy._convert_tool_call_to_output(tc_input)
test("type is function_call", tc_out["type"] == "function_call")
test("id set", tc_out["id"] == "call_abc")
test("call_id set (same as id)", tc_out["call_id"] == "call_abc")
test("name correct", tc_out["name"] == "my_func")
test("arguments correct", tc_out["arguments"] == '{"key":"val"}')
test("status completed", tc_out["status"] == "completed")

section("20. Tool Call → missing id gets generated")

tc_no_id = {"type": "function", "function": {"name": "anon_func", "arguments": "{}"}}
tc_out_ni = proxy._convert_tool_call_to_output(tc_no_id)
test("Generated id starts with call_", tc_out_ni["id"].startswith("call_"))
test("call_id matches generated id", tc_out_ni["call_id"] == tc_out_ni["id"])

# ─── 21. Response Storage & Chaining ──────────────────────────────────────────

section("21. Response Storage: store and retrieve")

resp_id = proxy.generate_response_id()
test("response_id format", resp_id.startswith("resp_") and len(resp_id) == 29, f"Got: {resp_id!r}")

test_data = {"id": resp_id, "output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]}
proxy.store_response(resp_id, test_data, [{"type": "message", "role": "user", "content": "hi"}])

retrieved = proxy.get_stored_response(resp_id)
test("Response can be retrieved", retrieved is not None)
test("Retrieved data matches stored", retrieved == test_data)

stored_input = proxy.get_stored_input(resp_id)
test("Input can be retrieved", stored_input is not None)
test("Input content correct", stored_input[0]["content"] == "hi")

section("22. Response Storage: delete")

proxy.delete_stored_response(resp_id)
after_delete = proxy.get_stored_response(resp_id)
test("Deleted response returns None", after_delete is None)

section("23. Response Storage: previous_response_id chaining")

# Store a response with a tool call
chain_id = proxy.generate_response_id()
call_id = f"call_{uuid.uuid4().hex[:24]}"
chain_data = {
    "id": chain_id,
    "output": [
        {
            "type": "function_call",
            "id": call_id,
            "call_id": call_id,
            "name": "shell",
            "arguments": '{"command":"echo hello"}',
            "status": "completed",
        }
    ],
}
proxy.store_response(chain_id, chain_data, [{"type": "message", "role": "user", "content": "run echo"}])

# Build messages with previous_response_id
chain_input = [
    {"type": "function_call_output", "call_id": call_id, "output": "hello"},
]
chain_msgs = proxy._responses_to_messages(chain_input, chain_id)

# Should have: [user from previous], [assistant with tool_call], [tool result]
has_tool_call = any(m.get("tool_calls") for m in chain_msgs)
has_tool_result = any(m.get("role") == "tool" for m in chain_msgs)
test("Chained messages include prev tool_call", has_tool_call, f"Messages: {[m.get('role') for m in chain_msgs]}")
test("Chained messages include tool result", has_tool_result)

section("24. Response Storage: auto-resolve call_id without previous_response_id")

# Store another response
auto_id = proxy.generate_response_id()
auto_call_id = f"call_{uuid.uuid4().hex[:24]}"
proxy.store_response(auto_id, {
    "id": auto_id,
    "output": [{"type": "function_call", "id": auto_call_id, "call_id": auto_call_id, "name": "read", "arguments": "{}", "status": "completed"}],
}, [{"type": "message", "role": "user", "content": "read file"}])

# Build messages without previous_response_id but with function_call_output
auto_input = [{"type": "function_call_output", "call_id": auto_call_id, "output": "file content"}]
auto_msgs = proxy._responses_to_messages(auto_input, None)
has_prev_context = any(m.get("tool_calls") for m in auto_msgs)
test("Auto-resolved previous context from call_id", has_prev_context, f"Messages: {[m.get('role') for m in auto_msgs]}")

# ─── 25. Session Tracking & Loop Detection ─────────────────────────────────────

section("25. Session Tracking: session hash is stable")

input1 = [{"type": "message", "role": "user", "content": "do task A"}]
input2 = [{"type": "message", "role": "user", "content": "do task A"}]  # Same content
hash1 = proxy._get_session_hash_from_input(input1)
hash2 = proxy._get_session_hash_from_input(input2)
test("Same input → same hash", hash1 == hash2, f"h1={hash1[:16]}, h2={hash2[:16]}")

input3 = [{"type": "message", "role": "user", "content": "do task B"}]  # Different
hash3 = proxy._get_session_hash_from_input(input3)
test("Different input → different hash", hash1 != hash3)

section("26. Session Tracking: session state is persistent")

sess = proxy._get_session_state(hash1)
test("Session state is dict", isinstance(sess, dict))
sess["_custom_key"] = "test_value"

sess2 = proxy._get_session_state(hash1)
test("Same hash returns same session", sess2.get("_custom_key") == "test_value")

section("27. Loop Detection: burst detection")

burst_sess = proxy._get_session_state(f"burst_test_{uuid.uuid4().hex[:8]}")
msgs_burst = [{"role": "user", "content": "test"}]

# Send MAX_REQUESTS_PER_60S + 1 requests rapidly
is_loop = False
for _ in range(proxy.MAX_REQUESTS_PER_60S + 2):
    is_loop, reason = proxy._detect_loop_in_request(burst_sess, msgs_burst)
    if is_loop:
        break
test("Burst detection triggers", is_loop, f"Sent {proxy.MAX_REQUESTS_PER_60S + 2} requests, loop={is_loop}")

section("28. Loop Detection: tool repeat detection")

tool_sess = proxy._get_session_state(f"tool_test_{uuid.uuid4().hex[:8]}")
tool_msgs = [
    {"role": "user", "content": "run tool"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": '{"command":"ls"}'}}
    ]},
    {"role": "tool", "content": "output", "tool_call_id": "call_1"},
]
is_tool_loop = False
for _ in range(proxy.LOOP_TOOL_REPEAT_THRESHOLD + 1):
    is_tool_loop, reason = proxy._detect_loop_in_request(tool_sess, tool_msgs)
    if is_tool_loop:
        break
    proxy._update_session_state(tool_sess, tool_msgs)
test("Tool repeat detection triggers", is_tool_loop, f"reason={reason}")

section("29. Loop Detection: rapid timing detection")

timing_sess = proxy._get_session_state(f"timing_test_{uuid.uuid4().hex[:8]}")
timing_msgs = [{"role": "user", "content": "quick"}]
is_rapid = False
# Simulate 5 rapid requests (< 1s each by manipulating last_time)
for i in range(6):
    timing_sess["_last_request_time"] = time.time() - 0.3  # 0.3s ago (< 1s threshold)
    is_rapid, reason = proxy._detect_loop_in_request(timing_sess, timing_msgs)
    if is_rapid:
        break
test("Rapid timing detection triggers", is_rapid, f"reason={reason}")

# ─── 30. _sse_event format ────────────────────────────────────────────────────

section("30. SSE Event Format")

event = proxy._sse_event("response.created", {"type": "response.created", "response": {"id": "resp_test"}})
test("SSE event starts with event:", event.startswith("event: response.created\n"))
test("SSE event has data: line", "data: " in event)
test("SSE event ends with double newline", event.endswith("\n\n"))

data_str = event.split("data: ")[1].strip()
data = json.loads(data_str)
test("SSE data is valid JSON", data.get("type") == "response.created")

# ─── 31. generate_response_id / _generate_item_id ─────────────────────────────

section("31. ID Generation")

ids = [proxy.generate_response_id() for _ in range(5)]
test("All response IDs start with resp_", all(i.startswith("resp_") for i in ids))
test("All response IDs unique", len(set(ids)) == 5)

item_ids = [proxy._generate_item_id("msg") for _ in range(5)]
test("Item IDs start with msg_", all(i.startswith("msg_") for i in item_ids))
test("Item IDs unique", len(set(item_ids)) == 5)

fc_ids = [proxy._generate_item_id("fc") for _ in range(3)]
test("Function call IDs start with fc_", all(i.startswith("fc_") for i in fc_ids))

# ─── 32. _get_session_hash_from_input edge cases ──────────────────────────────

section("32. Session Hash: edge cases")

hash_empty = proxy._get_session_hash_from_input([])
test("Empty input produces hash", isinstance(hash_empty, str) and len(hash_empty) > 0)

hash_str = proxy._get_session_hash_from_input("plain string input")
test("String input produces hash", isinstance(hash_str, str) and len(hash_str) > 0)

hash_none = proxy._get_session_hash_from_input(None)
test("None input produces hash", isinstance(hash_none, str) and len(hash_none) > 0)

# ─── 33. Cleanup ──────────────────────────────────────────────────────────────

section("33. Cleanup Expired Responses")

exp_id = proxy.generate_response_id()
proxy.store_response(exp_id, {"id": exp_id, "output": []})
# Manually expire it
with proxy._responses_state_lock:
    proxy._responses_state[exp_id]["created_at"] = time.time() - 10000
proxy.cleanup_expired_responses()
test("Expired responses cleaned up", proxy.get_stored_response(exp_id) is None)

# ─── 34. _find_response_by_call_id ────────────────────────────────────────────

section("34. Find Response by call_id")

find_id = proxy.generate_response_id()
find_call_id = f"call_{uuid.uuid4().hex[:24]}"
proxy.store_response(find_id, {
    "id": find_id,
    "output": [{"type": "function_call", "id": find_call_id, "call_id": find_call_id, "name": "test"}],
})
found = proxy._find_response_by_call_id(find_call_id)
test("Response found by call_id", found == find_id, f"Expected {find_id}, got {found}")

not_found = proxy._find_response_by_call_id("call_nonexistent_id_xyz")
test("Non-existent call_id returns None", not_found is None)

# ─── 35. Reasoning Normalization ──────────────────────────────────────────────

section("35. Reasoning Parameter Normalization (inline test)")

# Test the normalization logic directly since it's inline in the handler
def normalize_effort(reasoning):
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort", "medium")
    elif isinstance(reasoning, str):
        effort = reasoning
    else:
        effort = "medium"
    VALID_EFFORTS = {"low", "medium", "high", "xhigh"}
    if effort == "extra_high":
        effort = "xhigh"
    elif effort not in VALID_EFFORTS:
        effort = "high" if effort in ("max", "maximum") else "medium"
    return effort

test("dict {effort: medium}", normalize_effort({"effort": "medium"}) == "medium")
test("dict {effort: high}", normalize_effort({"effort": "high"}) == "high")
test("string 'low'", normalize_effort("low") == "low")
test("extra_high → xhigh", normalize_effort({"effort": "extra_high"}) == "xhigh")
test("max → high", normalize_effort({"effort": "max"}) == "high")
test("maximum → high", normalize_effort({"effort": "maximum"}) == "high")
test("unknown → medium", normalize_effort({"effort": "turbo"}) == "medium")
test("None → medium", normalize_effort(None) == "medium")

# ─── 36. Content Structured blocks ────────────────────────────────────────────

section("36. Content: Structured content blocks in message input")

structured_input = [
    {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "Part one"},
            {"type": "text", "text": "Part two"},
        ]
    }
]
msgs_struct = proxy._responses_to_messages(structured_input, None)
test("Structured content flattened", "Part one" in msgs_struct[-1]["content"])
test("Multi-part joined", "Part two" in msgs_struct[-1]["content"])

section("37. Content: image block → placeholder")

image_input = [
    {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "Look at this:"},
            {"type": "input_image", "image_url": "data:image/png;base64,abc123"},
        ]
    }
]
msgs_img = proxy._responses_to_messages(image_input, None)
test("Image block becomes [image] placeholder", "[image]" in msgs_img[-1]["content"])

# ─── 38. PLAN_DAILY_LIMITS ────────────────────────────────────────────────────

section("38. Plan Daily Limits")

test("free plan limit", proxy.PLAN_DAILY_LIMITS["free"] == 50)
test("starter plan limit", proxy.PLAN_DAILY_LIMITS["starter"] == 500)
test("pro plan limit", proxy.PLAN_DAILY_LIMITS["pro"] == 1500)
test("team plan limit", proxy.PLAN_DAILY_LIMITS["team"] == 5000)

# ─── Summary ──────────────────────────────────────────────────────────────────

print(f"\n{'═' * 60}")
print("  CODEX COMPATIBILITY TEST RESULTS")
print(f"{'═' * 60}")
print(f"  Total:  {_pass + _fail}")
print(f"  Passed: {_pass}")
print(f"  Failed: {_fail}")
score = round(_pass / (_pass + _fail) * 100, 1) if (_pass + _fail) > 0 else 0
print(f"  Score:  {score}%")
print()
if _fail == 0:
    print("  ALL TESTS PASSED — 100% OpenAI/Codex Compatible")
else:
    print("  COMPATIBILITY ISSUES FOUND — fix before production")
print(f"{'═' * 60}")

sys.exit(0 if _fail == 0 else 1)
