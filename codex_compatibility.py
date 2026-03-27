#!/usr/bin/env python3
"""
JembatanAI-Codex Compatibility Layer
OpenAI Responses API + Model Catalog for Codex CLI

Based on official OpenAI Codex documentation:
- wire_api: "responses" (required for custom providers)
- model_catalog_json: Path to model catalog JSON
- model_providers: Custom provider configuration
"""

import time
import logging
from typing import Any, Optional
import uuid

log = logging.getLogger("codex")

# ─── Responses API State Management ────────────────────────────────────
# Required for previous_response_id chaining in tool calling workflows

_responses_state: dict[str, dict[str, Any]] = {}
RESPONSES_TTL = 7200  # 2 hours (matches Codex session timeout)

def store_response(response_id: str, data: dict[str, Any]):
    """Store response for Codex previous_response_id chaining."""
    _responses_state[response_id] = {
        "data": data,
        "created_at": time.time(),
        "ttl": RESPONSES_TTL
    }
    log.debug(f"Stored response {response_id[:20]}...")

def get_response(response_id: str) -> Optional[dict[str, Any]]:
    """Get stored response by ID."""
    if response_id not in _responses_state:
        return None
    
    entry = _responses_state[response_id]
    if time.time() - entry["created_at"] > entry["ttl"]:
        del _responses_state[response_id]
        return None
    
    return entry["data"]

def delete_response(response_id: str) -> bool:
    """Delete a response by ID."""
    if response_id in _responses_state:
        del _responses_state[response_id]
        return True
    return False

def cleanup_expired_responses():
    """Clean up expired responses."""
    now = time.time()
    expired = [
        rid for rid, entry in _responses_state.items()
        if now - entry["created_at"] > entry["ttl"]
    ]
    for rid in expired:
        del _responses_state[rid]
    if expired:
        log.debug(f"Cleaned up {len(expired)} expired responses")

def generate_response_id() -> str:
    """Generate unique response ID matching OpenAI format."""
    return f"resp_{uuid.uuid4().hex[:24]}"

def generate_message_id() -> str:
    """Generate unique message ID matching OpenAI format."""
    return f"msg_{uuid.uuid4().hex[:24]}"

# ─── Model Catalog for Codex /model Picker ─────────────────────────────
# Format matches OpenAI Codex config-schema.json requirements

def get_codex_model_catalog() -> dict:
    """
    Generate model catalog for Codex CLI model picker.
    
    Includes ALL Kilo free models with TOR rotation support.
    Format matches config-schema.json model_catalog_json field.
    """
    return {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": [
            # Premium Models
            {
                "slug": "kilo-mimo-v2-pro",
                "display_name": "Kilo MiMo V2 Pro",
                "description": "Xiaomi MiMo-V2-Pro (free, 1M ctx, reasoning)",
                "default_reasoning_level": "high",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": False, "reasoning": True, "function_calling": True}
            },
            {
                "slug": "kilo-mimo-omni",
                "display_name": "Kilo MiMo Omni",
                "description": "Xiaomi MiMo-V2-Omni (free, 262K ctx, multimodal)",
                "default_reasoning_level": "medium",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": True, "reasoning": True, "function_calling": True}
            },
            {
                "slug": "kilo-grok-code",
                "display_name": "Kilo Grok Code",
                "description": "xAI Grok Code Fast 1 (free, 256K ctx, coding)",
                "default_reasoning_level": "medium",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": False, "reasoning": True, "function_calling": True}
            },
            # Fast Models
            {
                "slug": "kilo-minimax",
                "display_name": "Kilo MiniMax",
                "description": "MiniMax M2.5 (free, 204K ctx, fast)",
                "default_reasoning_level": "low",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": False, "reasoning": False, "function_calling": True}
            },
            {
                "slug": "kilo-trinity",
                "display_name": "Kilo Trinity",
                "description": "Arcee Trinity Large (free, 131K ctx, reliable)",
                "default_reasoning_level": "low",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": False, "reasoning": False, "function_calling": True}
            },
            # Additional Models
            {
                "slug": "kilo-nemotron",
                "display_name": "Kilo Nemotron",
                "description": "NVIDIA Nemotron 120B (free, 262K ctx)",
                "default_reasoning_level": "medium",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": False, "reasoning": True, "function_calling": True}
            },
            {
                "slug": "kilo-stepfun",
                "display_name": "Kilo StepFun",
                "description": "StepFun Step-3.5-Flash (free, 256K ctx)",
                "default_reasoning_level": "low",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": False, "reasoning": False, "function_calling": True}
            },
            {
                "slug": "kilo-corethink",
                "display_name": "Kilo CoreThink",
                "description": "CoreThink (free, 78K ctx, reasoning)",
                "default_reasoning_level": "high",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": False, "reasoning": True, "function_calling": True}
            },
            {
                "slug": "kilo-auto-free",
                "display_name": "Kilo Auto Free",
                "description": "Kilo Auto Free (free, auto-routed best model)",
                "default_reasoning_level": "medium",
                "vendor": "kilo",
                "capabilities": {"tool_calling": True, "vision": False, "reasoning": True, "function_calling": True}
            }
        ]
    }

def get_openai_compatible_models() -> list[dict]:
    """
    Get models in OpenAI /v1/models format.
    
    Used for:
    - Standard OpenAI client compatibility
    - /v1/models endpoint
    - Model discovery by OpenAI-compatible tools
    """
    catalog = get_codex_model_catalog()
    return [
        {
            "id": model["slug"],
            "object": "model",
            "created": int(time.time()),
            "owned_by": model["vendor"],
            "permission": [],
            "root": model["slug"],
            "parent": None,
            "description": model["description"],
            "capabilities": model["capabilities"]
        }
        for model in catalog["models"]
    ]

# ─── OpenAI Responses API Format Conversion ────────────────────────────
# Based on official Responses API specification

def create_responses_api_response(
    response_id: str,
    model: str,
    output: list[dict],
    status: str = "completed"
) -> dict:
    """
    Create Responses API format response.
    
    Matches OpenAI Responses API specification:
    - id: Response ID
    - object: "response"
    - created_at: Unix timestamp
    - model: Model slug
    - output: Array of output items
    - status: "completed" | "failed" | "in_progress"
    """
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output": output,
        "status": status,
        "error": None
    }

def convert_tool_call_to_output(tool_call: dict) -> dict:
    """
    Convert OpenAI tool call to Responses API output format.
    
    Input (Chat Completions):
    {
        "id": "call_abc",
        "type": "function",
        "function": {
            "name": "get_weather",
            "arguments": "{\"location\": \"NYC\"}"
        }
    }
    
    Output (Responses API):
    {
        "type": "function_call",
        "id": "call_abc",
        "name": "get_weather",
        "arguments": "{\"location\": \"NYC\"}"
    }
    """
    return {
        "type": "function_call",
        "id": tool_call["id"],
        "name": tool_call["function"]["name"],
        "arguments": tool_call["function"]["arguments"]
    }

def convert_message_to_output(message: dict) -> dict:
    """
    Convert Chat Completions message to Responses API output.
    
    Supports:
    - Assistant messages with text content
    - Tool calls
    - Multi-content messages
    """
    if message.get("tool_calls"):
        # Multiple tool calls
        return [convert_tool_call_to_output(tc) for tc in message["tool_calls"]]
    
    # Text message
    return {
        "type": "message",
        "role": message.get("role", "assistant"),
        "content": message.get("content", "")
    }

# ─── SSE Event Streaming for Responses API ─────────────────────────────
# Required for Codex CLI streaming support

def create_sse_event(event_type: str, data: Any) -> str:
    """
    Create SSE event in OpenAI Responses API format.
    
    Format:
    event: <event_type>
    data: <json_data>
    
    Required events:
    - response.created
    - response.in_progress
    - response.completed
    - response.failed
    - output_item.added
    - output_item.done
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

def get_responses_api_stream_events(response_id: str, model: str) -> list[tuple]:
    """
    Get standard Responses API streaming events sequence.
    
    Sequence:
    1. response.created
    2. response.in_progress
    3. output_item.added (for each output)
    4. output_item.done (for each output)
    5. response.completed
    """
    return [
        ("response.created", {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "model": model,
                "status": "in_progress"
            }
        }),
        ("response.in_progress", {
            "type": "response.in_progress",
            "response": {
                "id": response_id,
                "status": "in_progress"
            }
        }),
        ("response.completed", {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed"
            }
        })
    ]

# Import json here to avoid circular dependency
import json
