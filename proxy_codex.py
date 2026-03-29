#!/usr/bin/env python3
"""
JembatanAI-Codex Proxy — 100% OpenAI/Codex CLI Compatible Gateway

Based on research of:
- OpenAI Codex CLI (responses API, wire_api: "responses")
- Responses API specification (response.created → response.completed SSE)
- config-schema.json model catalog format
- Model provider configuration (base_url + env_key + wire_api)

Architecture:
  Codex CLI → /v1/responses → proxy_codex (port 4110) → Kilo API (OpenAI format)
                                                        ↗ TOR SOCKS5
                                                        ↗ 3-account rotation

Key differences from jembatanai (Claude Code proxy, port 4100):
  - Client speaks Responses API (not Anthropic /v1/messages)
  - No Anthropic ↔ OpenAI conversion needed (Codex already uses OpenAI format)
  - Responses API ↔ Chat Completions conversion (Responses → Kilo → Responses)
  - SSE events: response.created, response.in_progress, output_item.delta, response.completed

Port: 4110
"""

import os
import re
import json
import logging
import asyncio
import uuid
import time
import hashlib
import threading
import socket
import secrets
from typing import Any, Optional
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from circuit_breaker import (
    get_circuit_breaker,
    get_all_circuit_breakers,
    CircuitBreakerOpen,
)
from alert_system import (
    send_critical_alert,
    ALERT_WAF_BLOCK_HIGH,
    ALERT_KILO_EXHAUSTED,
    ALERT_LOOP_DETECTED,
    ALERT_ERROR_RATE_HIGH,
)
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy-codex")

# ─── Website API Auth (API Key Validation) ───────────────────────────────
# DNS issues prevent direct Supabase access from Python httpx.
# Instead, we call the website's /api/codex/* endpoints which can reach Supabase.

from datetime import datetime, timezone
import httpx

_WEBSITE_API = "http://127.0.0.1:3000"
_website_client: httpx.AsyncClient | None = None


def _get_website_client() -> httpx.AsyncClient:
    """Get or create the website API client."""
    global _website_client
    if _website_client is None:
        _website_client = httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_keepalive_connections=5, max_connections=10))
    return _website_client


# Plan daily limits (same as website plan-config.js)
PLAN_DAILY_LIMITS = {
    "free": 50,
    "starter": 500,
    "pro": 1500,
    "team": 5000,
}


async def validate_api_key(api_key: str) -> dict | None:
    """Validate API key via website API. Returns {key, user} or None."""
    try:
        client = _get_website_client()
        response = await client.post(
            f"{_WEBSITE_API}/api/codex/validate",
            headers={"x-api-key": api_key},
            timeout=10.0,
        )
        log.info(f"[AUTH] validate_api_key status={response.status_code}")
        if response.status_code == 401:
            return None
        if response.status_code == 403:
            return {"error": "expired"}
        if response.status_code != 200:
            log.warning(f"[AUTH] Unexpected status {response.status_code}: {response.text}")
            return None
        data = response.json()
        return {
            "key": {
                "id": data["key"]["id"],
                "name": data["key"].get("name", ""),
                "models": data["key"].get("models", "all"),
            },
            "user": {
                "id": data["user"]["id"],
                "email": data["user"].get("email", ""),
                "plan": data["user"].get("plan", "free"),
                "plan_expires_at": data["user"].get("plan_expires_at"),
            },
        }
    except Exception as e:
        log.warning(f"API key validation error: {e}")
        return None


async def check_daily_quota(user_id: str, plan: str) -> tuple:
    """Check daily quota via website API. Returns (allowed: bool, used: int, limit: int)."""
    limit = PLAN_DAILY_LIMITS.get(plan, 20)
    try:
        client = _get_website_client()
        response = await client.get(
            f"{_WEBSITE_API}/api/codex/quota/{user_id}",
            timeout=10.0,
        )
        if response.status_code != 200:
            log.warning(f"Quota check failed: {response.status_code}")
            return False, 0, limit
        data = response.json()
        used = data.get("used", 0)
        return data.get("allowed", False), used, data.get("limit", limit)
    except Exception as e:
        log.warning(f"Quota check error: {e}")
        return False, 0, limit


async def increment_usage(user_id: str, api_key_id: str, model: str):
    """Increment daily usage counter via website API."""
    try:
        client = _get_website_client()
        await client.post(
            f"{_WEBSITE_API}/api/codex/usage",
            json={
                "user_id": user_id,
                "api_key_id": api_key_id,
                "model": model,
            },
            timeout=10.0,
        )
    except Exception as e:
        log.warning(f"Usage increment error: {e}")


# ─── Configuration ─────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", "4110"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "300"))

# Kilo API — same as jembatanai proxy (direct Kilo, NOT through jembatanai gateway)
KILO_API_URL = "https://api.kilo.ai/api/openrouter"

# Kilo accounts directory (shared with jembatanai proxy)
KILO_ACCOUNTS_DIR = Path.home() / ".kilocode" / "accounts"
KILO_ROTATION_STATE = Path.home() / ".kilocode" / ".rotation-state"

# Kilo CLI identity constants (reverse-engineered from kilo binary)
KILO_USER_AGENT = "opencode-kilo-provider"
KILO_EDITOR_NAME = "Kilo CLI"
_kilo_machine_id_file = Path.home() / ".local" / "share" / "kilo" / "telemetry-id"
try:
    KILO_MACHINE_ID = _kilo_machine_id_file.read_text().strip()
except Exception:
    KILO_MACHINE_ID = ""
KILO_SESSION_ID = "jembatanai-codex-" + str(uuid.uuid4())

# TOR config
_TOR_SOCKS5 = "socks5://127.0.0.1:9050"
_TOR_CONTROL_PORT = 9051
_TOR_CONTROL_PASS = os.environ.get(
    "TOR_CONTROL_PASSWORD", os.environ.get("TOR_CONTROL_PASS", "")
).strip()
_tor_last_circuit_time: float = 0
_TOR_CIRCUIT_MIN_INTERVAL = 5
_tor_waf_blocked_until: float = 0
_TOR_WAF_COOLDOWN = 300

# Kilo free models: alias → (provider, model_id, description)
KILO_FREE_MODELS: dict[str, tuple[str, str, str]] = {
    "kilo-mimo-v2-pro": (
        "kilo",
        "xiaomi/mimo-v2-pro:free",
        "Xiaomi MiMo-V2-Pro (free, 1M ctx, reasoning)",
    ),
    "kilo-mimo-omni": (
        "kilo",
        "xiaomi/mimo-v2-omni:free",
        "Xiaomi MiMo-V2-Omni (free, 262K ctx, multimodal)",
    ),
    "kilo-grok-code": (
        "kilo",
        "x-ai/grok-code-fast-1:optimized:free",
        "xAI Grok Code Fast 1 (free, 256K ctx, coding)",
    ),
    "kilo-minimax": (
        "kilo",
        "minimax/minimax-m2.5:free",
        "MiniMax M2.5 (free, 204K ctx, fast)",
    ),
    "kilo-trinity": (
        "kilo",
        "arcee-ai/trinity-large-preview:free",
        "Arcee Trinity Large (free, 131K ctx, reliable)",
    ),
    "kilo-nemotron": (
        "kilo",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "NVIDIA Nemotron 120B (free, 262K ctx)",
    ),
    "kilo-stepfun": (
        "kilo",
        "stepfun/step-3.5-flash:free",
        "StepFun Step-3.5-Flash (free, 256K ctx)",
    ),
    "kilo-corethink": ("kilo", "corethink:free", "CoreThink (free, 78K ctx)"),
    "kilo-auto-free": (
        "kilo",
        "kilo-auto/free",
        "Kilo Auto Free (free, auto-routed best model)",
    ),
}
_kilo_models_lock = threading.Lock()
_kilo_models_last_refresh: float = 0

# Model alias mapping: Codex/GPT model names → Kilo aliases
MODEL_ALIAS_MAP = {
    # GPT-5 Series
    "gpt-5.4": "kilo-mimo-v2-pro",
    "gpt-5.4-mini": "kilo-minimax",
    "gpt-5.3": "kilo-mimo-omni",
    "gpt-5.3-codex": "kilo-mimo-omni",
    "gpt-5.2-codex": "kilo-grok-code",
    "gpt-5.2": "kilo-mimo-v2-pro",
    "gpt-5.1-codex-max": "kilo-grok-code",
    "gpt-5.1-codex-mini": "kilo-minimax",
    "gpt-5": "kilo-mimo-v2-pro",
    "gpt-5-codex": "kilo-mimo-v2-pro",
    # GPT-4 Series
    "gpt-4o": "kilo-mimo-v2-pro",
    "gpt-4o-mini": "kilo-minimax",
    "gpt-4-turbo": "kilo-trinity",
    "gpt-4": "kilo-mimo-v2-pro",
    # O Series (Reasoning)
    "o1": "kilo-grok-code",
    "o1-mini": "kilo-minimax",
    "o3": "kilo-grok-code",
    "o3-mini": "kilo-trinity",
    "o4-mini": "kilo-minimax",
    # Codex
    "codex": "kilo-mimo-v2-pro",
    "code-davinci": "kilo-grok-code",
    # Direct Kilo (passthrough)
    **{alias: alias for alias in KILO_FREE_MODELS},
}

# --- Provider: OpenRouter (fallback when Kilo is down/exhausted) ---
OR_API_URL = os.environ.get("OR_API_URL", "https://openrouter.ai/api/v1")
OR_API_KEY = os.environ.get("OR_API_KEY", "")

# Fallback when all Kilo accounts exhausted
KILO_FALLBACK_MODEL = ("openrouter", "stepfun/step-3.5-flash:free")
_server_start_time = time.time()

# Track consecutive 500 errors from Kilo API to detect outages
_kilo_500_error_count: int = 0
_kilo_500_error_at: float = 0

# ─── Anti-Loop System (adapted for Codex) ─────────────────────────────

MAX_REQUESTS_PER_60S = 15  # Codex is tool-heavy, slightly higher threshold
LOOP_TOOL_REPEAT_THRESHOLD = 3
LOOP_TEXT_REPEAT_THRESHOLD = 3
MAX_SESSIONS = 1000
SESSION_TTL = 3600

_session_state: dict = {}
_state_lock = threading.Lock()

# ─── Kilo Account Rotation ────────────────────────────────────────────

_kilo_token_cache = {"access_token": "", "account_idx": 0}
_kilo_token_lock = threading.RLock()
_kilo_rate_limited: dict[int, float] = {}
_KILO_COOLDOWN = 300
_kilo_all_exhausted = False
_kilo_exhausted_at: float = 0


def _get_active_account_idx() -> int:
    try:
        return int(KILO_ROTATION_STATE.read_text().strip())
    except Exception:
        return 0


def _load_kilo_token_for_idx(idx: int) -> str:
    now_ms = time.time() * 1000
    account_files = sorted(KILO_ACCOUNTS_DIR.glob("account-*.json"))
    if idx >= len(account_files):
        idx = 0
    try:
        acc = json.loads(account_files[idx].read_text())
        kilo = acc.get("kilo", {})
        if kilo.get("type") != "oauth":
            return ""
        expires = kilo.get("expires", 0)
        if expires > now_ms + 60_000:
            token = kilo.get("access", "")
            if token:
                log.debug(f"Kilo: using account idx={idx} ({account_files[idx].name})")
                return token
    except Exception as e:
        log.warning(f"Kilo: failed to load account idx={idx}: {e}")
    return ""


def _load_kilo_token() -> str:
    global _kilo_all_exhausted
    now = time.time()
    account_files = sorted(KILO_ACCOUNTS_DIR.glob("account-*.json"))
    count = len(account_files)
    if count == 0:
        log.error("Kilo: no account files found")
        return ""
    active_idx = _get_active_account_idx()
    for offset in range(count):
        idx = (active_idx + offset) % count
        limited_at = _kilo_rate_limited.get(idx, 0)
        if now - limited_at < _KILO_COOLDOWN:
            continue
        token = _load_kilo_token_for_idx(idx)
        if token:
            with _kilo_token_lock:
                _kilo_token_cache["access_token"] = token
                _kilo_token_cache["account_idx"] = idx
            return token
    log.warning("Kilo: all accounts rate-limited, using active as last resort")
    token = _load_kilo_token_for_idx(active_idx)
    with _kilo_token_lock:
        if token:
            _kilo_token_cache["access_token"] = token
            _kilo_token_cache["account_idx"] = active_idx
        cached = _kilo_token_cache.get("access_token", "")
    return token or cached


def _get_kilo_token() -> str:
    with _kilo_token_lock:
        has_token = bool(_kilo_token_cache["access_token"])
    if not has_token:
        _load_kilo_token()
    with _kilo_token_lock:
        return _kilo_token_cache["access_token"]


def _rotate_kilo_on_rate_limit() -> str:
    global _kilo_all_exhausted, _kilo_exhausted_at
    with _kilo_token_lock:
        current_idx = _kilo_token_cache.get("account_idx", 0)
    _kilo_rate_limited[current_idx] = time.time()
    log.warning(f"Kilo: marking account idx={current_idx} rate-limited, rotating...")
    with _kilo_token_lock:
        _kilo_token_cache["access_token"] = ""
    token = _load_kilo_token()
    if not token:
        _kilo_all_exhausted = True
        _kilo_exhausted_at = time.time()
        log.warning("Kilo: ALL accounts rate-limited")
    return token


def _kilo_is_exhausted() -> bool:
    global _kilo_all_exhausted
    if not _kilo_all_exhausted:
        return False
    if time.time() - _kilo_exhausted_at > _KILO_COOLDOWN:
        _kilo_all_exhausted = False
        _kilo_rate_limited.clear()
        with _kilo_token_lock:
            _kilo_token_cache["access_token"] = ""
        log.info("Kilo: cooldown expired, accounts available again")
        return False
    return True


def _kilo_is_down() -> bool:
    """Return True if Kilo API is returning 500 errors (temporary outage)."""
    global _kilo_500_error_count, _kilo_500_error_at
    if _kilo_500_error_count >= 10:
        if time.time() - _kilo_500_error_at < 300:
            return True
        else:
            _kilo_500_error_count = 0
    return False


# ─── OpenRouter Helper ─────────────────────────────────────────────────


async def _openrouter_request(body: dict) -> dict | None:
    """Send request to OpenRouter as fallback when Kilo is down/exhausted."""
    if not OR_API_KEY:
        log.error("OpenRouter fallback requested but OR_API_KEY not set")
        return None
    try:
        or_body = dict(body)
        or_body["model"] = "stepfun/step-3.5-flash:free"
        or_body.pop("stream", None)
        or_headers = {
            "Authorization": f"Bearer {OR_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://gateway.jembatanai.com",
            "X-Title": "JembatanAI-Codex",
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{OR_API_URL}/chat/completions", json=or_body, headers=or_headers
            )
        if resp.status_code == 200:
            log.info("OpenRouter fallback succeeded")
            return resp.json()
        log.error(f"OpenRouter fallback failed: {resp.status_code} {resp.text[:200]}")
        return None
    except Exception as e:
        log.error(f"OpenRouter fallback exception: {e}")
        return None


def _build_openrouter_headers() -> dict:
    """Build OpenRouter request headers."""
    return {
        "Authorization": f"Bearer {OR_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://gateway.jembatanai.com",
        "X-Title": "JembatanAI-Codex",
    }


# ─── TOR Proxy Helpers ────────────────────────────────────────────────


def _tor_available() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", 9050), timeout=1)
        s.close()
        return True
    except Exception:
        return False


def _tor_quote_password(password: str) -> str:
    return password.replace("\\", "\\\\").replace('"', '\\"')


def _tor_request_new_circuit() -> bool:
    global _tor_last_circuit_time
    now = time.time()
    if now - _tor_last_circuit_time < _TOR_CIRCUIT_MIN_INTERVAL:
        return False
    if not _TOR_CONTROL_PASS:
        log.warning("TOR control: TOR_CONTROL_PASSWORD not set")
        return False
    try:
        escaped_pass = _tor_quote_password(_TOR_CONTROL_PASS)
        with socket.create_connection(("127.0.0.1", _TOR_CONTROL_PORT), timeout=5) as s:
            s.settimeout(5)
            s.sendall(f'AUTHENTICATE "{escaped_pass}"\r\n'.encode("utf-8"))
            auth_resp = s.recv(512).decode("utf-8", errors="ignore")
            if "250" not in auth_resp:
                log.warning(f"TOR control: auth failed ({auth_resp.strip()[:100]})")
                return False
            s.sendall(b"SIGNAL NEWNYM\r\n")
            newnym_resp = s.recv(512).decode("utf-8", errors="ignore")
            s.sendall(b"QUIT\r\n")
        if "250" in newnym_resp:
            _tor_last_circuit_time = now
            log.info("TOR: new circuit requested (new exit node)")
            return True
        log.warning(f"TOR control: NEWNYM failed ({newnym_resp.strip()[:100]})")
        return False
    except Exception as e:
        log.warning(f"TOR control: failed to request new circuit: {e}")
        return False


def _is_vercel_waf_block(status_code: int, body_text: str) -> bool:
    if status_code != 403:
        return False
    if (
        "Vercel Security" in body_text
        or "Security Checkpoint" in body_text
        or "data-astro-cid" in body_text
    ):
        return True
    if '"code":"403"' in body_text or '"code": "403"' in body_text:
        return True
    if '"Forbidden"' in body_text:
        return True
    return False


def _mark_tor_waf_blocked():
    global _tor_waf_blocked_until
    _tor_waf_blocked_until = time.time() + _TOR_WAF_COOLDOWN
    log.warning(f"TOR: WAF-blocked, skipping TOR for {_TOR_WAF_COOLDOWN}s")


def _tor_transport() -> "httpx.AsyncHTTPTransport | None":
    if time.time() < _tor_waf_blocked_until:
        return None
    if _tor_available():
        return httpx.AsyncHTTPTransport(proxy=_TOR_SOCKS5)
    return None


# ─── Kilo Headers ─────────────────────────────────────────────────────


def _build_kilo_headers(kilo_token: str = "") -> dict:
    """Build headers matching Kilo CLI exactly (reverse-engineered from kilo binary)."""
    tok = kilo_token or _get_kilo_token()
    headers = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "User-Agent": KILO_USER_AGENT,
        "X-KILOCODE-EDITORNAME": KILO_EDITOR_NAME,
        "X-KILOCODE-TASKID": KILO_SESSION_ID,
    }
    if KILO_MACHINE_ID:
        headers["X-KILOCODE-MACHINEID"] = KILO_MACHINE_ID
    return headers


# ─── Model Resolution ─────────────────────────────────────────────────


def resolve_codex_model(model: str) -> str:
    """Resolve Codex model name → Kilo model alias."""
    if model in MODEL_ALIAS_MAP:
        return MODEL_ALIAS_MAP[model]
    if model in KILO_FREE_MODELS:
        return model
    return "kilo-mimo-v2-pro"


def _model_id_to_alias(model_id: str) -> str:
    """Convert Kilo model_id to CLI alias. e.g. 'xiaomi/mimo-v2-pro:free' → 'kilo-mimo-v2-pro'"""
    s = (
        model_id.replace(":free", "")
        .replace(":optimized", "")
        .replace("/", "-")
        .lower()
    )
    for prefix in (
        "xiaomi-",
        "nvidia-",
        "arcee-ai-",
        "minimax-",
        "stepfun-",
        "x-ai-",
        "kilo-auto-",
    ):
        if s.startswith(prefix):
            rest = s[len(prefix) :]
            name = prefix.rstrip("-")
            if name == "x-ai":
                name = "kilo-xai"
            elif name == "kilo-auto":
                name = "kilo-auto"
            else:
                name = f"kilo-{rest}"
            return name
    return f"kilo-{s}"


# ─── Dynamic Model Refresh ────────────────────────────────────────────


async def _refresh_kilo_models():
    """Fetch live free model list from Kilo API and update KILO_FREE_MODELS + MODEL_ALIAS_MAP."""
    global _kilo_models_last_refresh
    token = _get_kilo_token()
    if not token:
        log.warning("Kilo model refresh: no token, skipping")
        return
    try:
        async with httpx.AsyncClient(timeout=15, transport=_tor_transport()) as client:
            resp = await client.get(
                f"{KILO_API_URL}/models",
                headers=_build_kilo_headers(token),
            )
        if resp.status_code != 200:
            log.warning(f"Kilo model refresh: API returned {resp.status_code}")
            return
        data = resp.json()
        api_models = data.get("data", [])
        free_models = [
            m
            for m in api_models
            if m.get("isFree") is True or ":free" in m.get("id", "")
        ]
        if not free_models:
            log.warning("Kilo model refresh: no free models returned")
            return
        with _kilo_models_lock:
            for m in free_models:
                model_id = m.get("id", "")
                alias = _model_id_to_alias(model_id)
                name = m.get("name", model_id)
                # Strip existing "(free)" from name to avoid duplicate in description
                name = re.sub(r"\s*\(free\)\s*$", "", name).strip()
                ctx = (m.get("context_length") or 0) // 1024
                ctx_label = f"{ctx}K ctx" if ctx else "?"
                desc = f"{name} (free, {ctx_label})"
                if alias not in KILO_FREE_MODELS:
                    log.info(f"Kilo model refresh: new model → {alias}: {model_id}")
                KILO_FREE_MODELS[alias] = ("kilo", model_id, desc)
                MODEL_ALIAS_MAP[alias] = alias
        _kilo_models_last_refresh = time.time()
        log.info(f"Kilo model refresh: {len(KILO_FREE_MODELS)} free models available")
    except Exception as e:
        log.warning(f"Kilo model refresh failed: {e}")


async def _model_refresh_loop():
    await asyncio.sleep(5)
    await _refresh_kilo_models()
    while True:
        await asyncio.sleep(3600)
        await _refresh_kilo_models()


# ─── Session Tracking (for anti-loop) ─────────────────────────────────


def _get_session_hash_from_input(input_data: list) -> str:
    """Generate session hash from Responses API input items."""
    texts = []
    for item in input_data:
        if item.get("type") == "message":
            content = item.get("content", "")
            if isinstance(content, str):
                texts.append(content[:200])
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", "")[:200])
        if len(texts) >= 3:
            break
    key = "|".join(texts) if texts else str(time.time())
    return hashlib.md5(key.encode()).hexdigest()[:16]


def _get_session_state(session_hash: str) -> dict:
    with _state_lock:
        if session_hash not in _session_state:
            if len(_session_state) >= MAX_SESSIONS:
                oldest = min(
                    _session_state, key=lambda k: _session_state[k].get("last_seen", 0)
                )
                del _session_state[oldest]
            _session_state[session_hash] = {
                "last_tool_sigs": [],
                "last_text": "",
                "last_text_fp": "",
                "_request_timestamps": [],
                "_total_requests": 0,
                "last_seen": time.time(),
            }
        _session_state[session_hash]["last_seen"] = time.time()
        return _session_state[session_hash]


def _extract_tool_sigs_from_messages(messages: list) -> list:
    """Extract tool call signatures from OpenAI messages format."""
    sigs = []
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            for tc in tool_calls:
                name = tc.get("function", {}).get("name", "")
                args = tc.get("function", {}).get("arguments", "")
                param_hash = hashlib.md5(args.encode()).hexdigest()[:12]
                sigs.append((name, param_hash))
            break
    return sigs


def _detect_loop_in_request(session: dict, messages: list) -> tuple:
    """Detect if Codex is stuck in a loop. Returns (is_loop: bool, reason: str)."""
    now = time.time()

    # Rapid succession check
    last_time = session.get("_last_request_time", 0)
    if last_time > 0:
        gap = now - last_time
        if gap < 1.0:
            session["_consecutive_fast"] = session.get("_consecutive_fast", 0) + 1
            if session.get("_consecutive_fast", 0) >= 5:
                return (
                    True,
                    f"rapid succession ({gap:.1f}s, {session['_consecutive_fast']}x)",
                )
        else:
            session["_consecutive_fast"] = 0
    session["_last_request_time"] = now

    # Request tracking
    if "_request_timestamps" not in session:
        session["_request_timestamps"] = []
    if "_total_requests" not in session:
        session["_total_requests"] = 0
    session["_request_timestamps"].append(now)
    session["_total_requests"] += 1

    # Clean old timestamps
    cutoff = now - 60
    session["_request_timestamps"] = [
        t for t in session["_request_timestamps"] if t > cutoff
    ]

    # Burst detection
    if len(session["_request_timestamps"]) > MAX_REQUESTS_PER_60S:
        rate = len(session["_request_timestamps"])
        return (
            True,
            f"burst detection ({rate} req/60s, threshold {MAX_REQUESTS_PER_60S})",
        )

    # Hard cap
    if session["_total_requests"] > 500:
        return True, f"session request cap ({session['_total_requests']} total)"

    # Tool repetition
    current_sigs = _extract_tool_sigs_from_messages(messages)
    if current_sigs and current_sigs == session.get("last_tool_sigs"):
        session["_tool_repeat_count"] = session.get("_tool_repeat_count", 0) + 1
        if session.get("_tool_repeat_count", 0) >= LOOP_TOOL_REPEAT_THRESHOLD:
            return (
                True,
                f"repeated tool calls ({[s[0] for s in current_sigs]} x{session['_tool_repeat_count']})",
            )
    else:
        session["_tool_repeat_count"] = 0

    return False, ""


def _update_session_state(session: dict, messages: list):
    session["last_tool_sigs"] = _extract_tool_sigs_from_messages(messages)
    last_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                last_text = content[:500]
            break
    session["last_text"] = last_text
    session["last_text_fp"] = (
        hashlib.md5(last_text.encode()).hexdigest() if last_text else ""
    )


async def _session_cleanup_loop():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        with _state_lock:
            expired = [
                k
                for k, v in _session_state.items()
                if now - v.get("last_seen", 0) > SESSION_TTL
            ]
            for k in expired:
                del _session_state[k]
        if expired:
            log.info(f"[SESSION-CLEANUP] Removed {len(expired)} stale sessions")


# ─── Responses API State Management ───────────────────────────────────
# Required for previous_response_id chaining in Codex tool calling

_responses_state: dict[str, dict[str, Any]] = {}
_responses_state_lock = threading.Lock()
RESPONSES_TTL = 7200  # 2 hours
MAX_RESPONSES = 10000  # Prevent unbounded memory growth


def store_response(response_id: str, data: dict, input_data: list | None = None):
    with _responses_state_lock:
        _responses_state[response_id] = {
            "data": data,
            "input": input_data or [],
            "created_at": time.time(),
            "ttl": RESPONSES_TTL,
        }
        # Evict oldest entries if over limit
        if len(_responses_state) > MAX_RESPONSES:
            oldest_keys = sorted(
                _responses_state.keys(),
                key=lambda k: _responses_state[k]["created_at"],
            )[: len(_responses_state) - MAX_RESPONSES]
            for k in oldest_keys:
                del _responses_state[k]


def get_stored_response(response_id: str) -> Optional[dict]:
    with _responses_state_lock:
        if response_id not in _responses_state:
            return None
        entry = _responses_state[response_id]
        if time.time() - entry["created_at"] > entry["ttl"]:
            del _responses_state[response_id]
            return None
        return entry["data"]


def get_stored_input(response_id: str) -> Optional[list]:
    """Get the stored input_data for a response (for conversation reconstruction)."""
    with _responses_state_lock:
        if response_id not in _responses_state:
            return None
        return _responses_state[response_id].get("input", [])


def delete_stored_response(response_id: str) -> bool:
    with _responses_state_lock:
        if response_id in _responses_state:
            del _responses_state[response_id]
            return True
        return False


def cleanup_expired_responses():
    now = time.time()
    with _responses_state_lock:
        expired = [
            rid
            for rid, entry in _responses_state.items()
            if now - entry["created_at"] > entry["ttl"]
        ]
        for rid in expired:
            del _responses_state[rid]


def generate_response_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def _find_response_by_call_id(call_id: str) -> Optional[str]:
    """Search stored responses for one containing the given call_id in its output."""
    with _responses_state_lock:
        for resp_id, entry in _responses_state.items():
            data = entry.get("data", {})
            for output_item in data.get("output", []):
                item_call_id = output_item.get("call_id", output_item.get("id", ""))
                if item_call_id == call_id:
                    return resp_id
    return None


# ─── Responses API ↔ Chat Completions Conversion ──────────────────────


def _generate_item_id(prefix: str = "msg") -> str:
    """Generate unique item ID matching OpenAI format."""
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _responses_to_messages(
    input_data: list,
    previous_response_id: Optional[str],
    instructions: Optional[str] = None,
) -> list:
    """Convert Responses API input items to OpenAI Chat Completions messages."""
    messages = []

    # Handle instructions field (Codex CLI system prompt)
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # Normalize input: handle plain string or list of strings from Codex CLI
    if isinstance(input_data, str):
        input_data = [{"type": "message", "role": "user", "content": input_data}]
    elif isinstance(input_data, list):
        normalized_input = []
        for item in input_data:
            if isinstance(item, str):
                normalized_input.append(
                    {"type": "message", "role": "user", "content": item}
                )
            elif isinstance(item, dict):
                normalized_input.append(item)
        input_data = normalized_input

    # If no previous_response_id but input has function_call_output,
    # try to find the matching stored response by call_id
    if not previous_response_id:
        has_tool_output = any(
            item.get("type") == "function_call_output" for item in input_data
        )
        if has_tool_output:
            for item in input_data:
                if item.get("type") == "function_call_output":
                    call_id = item.get("call_id", "")
                    if call_id:
                        found_id = _find_response_by_call_id(call_id)
                        if found_id:
                            previous_response_id = found_id
                            log.debug(
                                f"[CHAIN] Auto-resolved previous_response_id={found_id[:20]} from call_id={call_id[:20]}"
                            )
                            break

    # Add previous response context if chaining
    if previous_response_id:
        prev = get_stored_response(previous_response_id)
        if prev and prev.get("output"):
            # Check if current input only has tool outputs (no user message)
            has_only_tool_outputs = (
                all(item.get("type") == "function_call_output" for item in input_data)
                and input_data
            )

            # If current input only has tool outputs (no user message),
            # include stored input from previous response for conversation context.
            # This prevents "user content must not be empty" errors from some providers.
            if has_only_tool_outputs:
                prev_input = get_stored_input(previous_response_id)
                if prev_input:
                    for prev_item in prev_input:
                        ptype = prev_item.get("type")
                        if ptype == "message" or (
                            "role" in prev_item and "type" not in prev_item
                        ):
                            pcontent = prev_item.get("content", "")
                            prole = prev_item.get("role", "user")
                            if isinstance(pcontent, list):
                                text_parts = []
                                for block in pcontent:
                                    if isinstance(block, dict):
                                        btype = block.get("type", "")
                                        if btype in (
                                            "text",
                                            "input_text",
                                            "output_text",
                                        ):
                                            text_parts.append(block.get("text", ""))
                                    elif isinstance(block, str):
                                        text_parts.append(block)
                                pcontent = "\n".join(text_parts)
                            messages.append({"role": prole, "content": pcontent})

            # Add previous assistant output for context
            for output_item in prev["output"]:
                otype = output_item.get("type")
                if otype == "message":
                    # Extract text from structured content array
                    content_text = ""
                    raw_content = output_item.get("content", "")
                    if isinstance(raw_content, str):
                        content_text = raw_content
                    elif isinstance(raw_content, list):
                        parts = []
                        for block in raw_content:
                            if isinstance(block, dict):
                                if block.get("type") in (
                                    "output_text",
                                    "input_text",
                                    "text",
                                ):
                                    parts.append(block.get("text", ""))
                                elif block.get("type") == "refusal":
                                    parts.append(block.get("refusal", ""))
                            elif isinstance(block, str):
                                parts.append(block)
                        content_text = "\n".join(parts)
                    messages.append(
                        {
                            "role": output_item.get("role", "assistant"),
                            "content": content_text,
                        }
                    )
                elif otype == "function_call":
                    messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": output_item.get(
                                        "call_id",
                                        output_item.get(
                                            "id", f"call_{uuid.uuid4().hex[:24]}"
                                        ),
                                    ),
                                    "type": "function",
                                    "function": {
                                        "name": output_item.get("name", ""),
                                        "arguments": output_item.get("arguments", "{}"),
                                    },
                                }
                            ],
                        }
                    )
                # Skip reasoning items — they're internal

    # Convert current input items
    for item in input_data:
        item_type = item.get("type")

        if item_type == "message" or ("role" in item and "type" not in item):
            content = item.get("content", "")
            role = item.get("role", "user")
            # Handle structured content (array of text/image blocks)
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype in ("text", "input_text", "output_text"):
                            text_parts.append(block.get("text", ""))
                        elif btype == "input_image":
                            text_parts.append("[image]")
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)
            messages.append({"role": role, "content": content})

        elif item_type == "function_call_output":
            output_val = item.get("output", "")
            # Ensure output is string (Codex/Azure requirement)
            if not isinstance(output_val, str):
                output_val = json.dumps(output_val)
            messages.append(
                {
                    "role": "tool",
                    "content": output_val,
                    "tool_call_id": item.get("call_id", ""),
                }
            )

        elif item_type == "function_call":
            call_id = item.get(
                "call_id", item.get("id", f"call_{uuid.uuid4().hex[:24]}")
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }
                    ],
                }
            )

        # Skip reasoning, web_search_call, etc. — not supported in Chat Completions

    return messages


def _convert_tool_call_to_output(tc: dict) -> dict:
    """Convert OpenAI tool_calls item to Responses API function_call output.

    Codex CLI requires:
    - type: "function_call"
    - id: unique item ID
    - call_id: correlation ID (must match function_call_output.call_id)
    - name: function name
    - arguments: JSON string of arguments
    - status: "completed"
    """
    call_id = tc.get("id", f"call_{uuid.uuid4().hex[:24]}")
    return {
        "type": "function_call",
        "id": call_id,
        "call_id": call_id,
        "name": tc.get("function", {}).get("name", ""),
        "arguments": tc.get("function", {}).get("arguments", "{}"),
        "status": "completed",
    }


def _chat_to_responses(chat_response: dict, model: str) -> dict:
    """Convert OpenAI Chat Completions response to Responses API format.

    Codex CLI expects:
    - output items have "id" and "status" fields
    - message content is array of {"type": "output_text", "text": "..."}
    - function_call items have "call_id" field
    """
    response_id = generate_response_id()
    choices = chat_response.get("choices", [{}])
    message = choices[0].get("message", {})

    output = []
    content_text = message.get("content") or ""
    # Handle reasoning content: when model uses reasoning tokens, content may be null
    # but reasoning/reasoning_content has the model's internal thinking.
    # Include reasoning as output so Codex CLI gets something useful.
    reasoning_text = message.get("reasoning") or message.get("reasoning_content") or ""
    if not content_text.strip() and reasoning_text:
        content_text = reasoning_text
    if content_text.strip():
        output.append(
            {
                "type": "message",
                "id": _generate_item_id("msg"),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": content_text}],
            }
        )
    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            output.append(_convert_tool_call_to_output(tc))

    usage = chat_response.get("usage", {})
    finish_reason = choices[0].get("finish_reason", "stop")

    # Custom models: ALWAYS "completed" — "incomplete" triggers continuation
    # which only works with native OpenAI models that support it.
    # All our Kilo/OpenRouter models are custom and don't support continuation.
    status = "completed"

    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output": output,
        "status": status,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "error": None,
    }


# ─── SSE Event Helpers ────────────────────────────────────────────────


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ─── Kilo Request with Retry ──────────────────────────────────────────


async def _request_with_retry(
    url: str, body: dict, headers: dict, max_retries: int = 4
):
    """Request Kilo API with exponential backoff + account rotation + TOR circuit rotation.
    Matches jembatanai proxy behavior: 500 tracking, alert integration, circuit breaker."""
    global _kilo_500_error_count, _kilo_500_error_at

    # Check circuit breaker before attempting
    cb = get_circuit_breaker("kilo-codex")
    if cb.is_open:
        log.warning("Kilo circuit breaker OPEN — skipping request")
        return None

    resp: httpx.Response | None = None
    for attempt in range(max_retries + 1):
        try:
            transport = _tor_transport()
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, transport=transport
            ) as client:
                resp = await client.post(url, json=body, headers=headers)

            if resp.status_code == 200:
                cb._on_success()
                # Reset 500 error counter on success
                if _kilo_500_error_count > 0:
                    _kilo_500_error_count = 0
                    log.info("Kilo API recovered, reset 500 error counter")
                return resp

            body_text = resp.text[:500]

            # Vercel WAF block → rotate TOR circuit + retry
            if (
                _is_vercel_waf_block(resp.status_code, body_text)
                and attempt < max_retries
            ):
                if _tor_request_new_circuit():
                    log.warning(
                        f"WAF block (attempt {attempt + 1}), rotated TOR circuit, retrying..."
                    )
                    await asyncio.sleep(2)
                else:
                    _mark_tor_waf_blocked()
                    log.warning(
                        f"WAF block (attempt {attempt + 1}), TOR rotation failed, retrying direct..."
                    )
                # Alert on high WAF block rate
                if attempt >= 2:
                    try:
                        await send_critical_alert(
                            ALERT_WAF_BLOCK_HIGH,
                            {"proxy": "codex", "attempt": attempt + 1, "url": url[:80]},
                        )
                    except Exception:
                        pass
                continue

            # Server error (500/502/503) → track + retry with exponential backoff
            is_server_error = resp.status_code in (500, 502, 503)
            if is_server_error:
                _kilo_500_error_count += 1
                if _kilo_500_error_count == 1:
                    _kilo_500_error_at = time.time()
                log.error(
                    f"Kilo 500 error count: {_kilo_500_error_count} (fallback at 10)"
                )
                cb._on_failure()

            if is_server_error and attempt < max_retries:
                wait = min(2 ** (attempt + 2), 30)  # 4s, 8s, 16s, 30s max
                log.error(
                    f"Server error {resp.status_code} (attempt {attempt + 1}/{max_retries + 1}), "
                    f"retry in {wait}s... Response body: {body_text[:200]}"
                )
                await asyncio.sleep(wait)
                continue

            # Rate limit / auth error → rotate account
            is_rate_limit = (
                resp.status_code == 429
                or "Rate limit" in body_text
                or "FreeUsageLimitError" in body_text
            )
            is_auth_error = resp.status_code == 401
            if (is_rate_limit or is_auth_error) and attempt < max_retries:
                if is_rate_limit and attempt < 2:
                    wait = min(5 * (2**attempt), 40)  # 5s, 10s, then rotate
                    log.warning(
                        f"429 Rate limit (attempt {attempt + 1}/{max_retries + 1}), "
                        f"waiting {wait}s before account rotation..."
                    )
                    await asyncio.sleep(wait)
                    if attempt >= 1:
                        _tor_request_new_circuit()
                        new_token = _rotate_kilo_on_rate_limit()
                        if new_token:
                            headers = _build_kilo_headers(new_token)
                            log.warning("Rotated to next Kilo account after rate limit")
                    continue
                # Rotate TOR circuit first (new IP), then rotate account
                _tor_request_new_circuit()
                new_token = _rotate_kilo_on_rate_limit()
                if new_token:
                    headers = _build_kilo_headers(new_token)
                    log.warning(
                        f"Kilo rate limit/auth error (attempt {attempt + 1}), rotated to next account"
                    )
                    # Alert when all accounts exhausted
                    if _kilo_all_exhausted:
                        try:
                            await send_critical_alert(
                                ALERT_KILO_EXHAUSTED,
                                {"proxy": "codex", "fallback": "openrouter"},
                            )
                        except Exception:
                            pass
                    continue
                wait = (attempt + 1) * 3
                log.warning(f"Rate limit (attempt {attempt + 1}), retry in {wait}s...")
                await asyncio.sleep(wait)
                continue

            log.error(f"Provider error {resp.status_code}: {body_text[:100]}")
            return resp

        except Exception as e:
            cb._on_failure()
            if attempt < max_retries:
                wait = (attempt + 1) * 2
                log.warning(
                    f"Exception (attempt {attempt + 1}): {e}, retry in {wait}s..."
                )
                await asyncio.sleep(wait)
                continue
            raise

    return resp


# ─── Streaming Functions ──────────────────────────────────────────────


async def _stream_chat_completions(body: dict, headers: dict):
    """Stream Kilo response in OpenAI SSE format (passthrough)."""
    try:
        transport = _tor_transport()
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, transport=transport
        ) as client:
            async with client.stream(
                "POST", f"{KILO_API_URL}/chat/completions", json=body, headers=headers
            ) as resp:
                # B1 FIX: Content-type validation for streaming path
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("text/event-stream"):
                    err_body = await resp.aread()
                    yield f"data: {json.dumps({'error': {'message': f'Expected SSE stream, got: {content_type}', 'type': 'api_error'}})}\n\n"
                    return
                if resp.status_code != 200:
                    err_body = await resp.aread()
                    yield f"data: {json.dumps({'error': {'message': err_body.decode()[:200], 'type': 'api_error'}})}\n\n"
                    return
                async for line in resp.aiter_lines():
                    # B4 FIX: Filter out SSE comments (lines starting with ':') like "KILO PROCESSING"
                    if line and not line.startswith(":"):
                        yield f"{line}\n"
                        if line.startswith("data:") and not line.endswith("\n"):
                            yield "\n"
    except Exception as e:
        log.error(f"[STREAM-CHAT] Error: {e}")
        yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'api_error'}})}\n\n"


async def _stream_responses_api(
    body: dict, headers: dict, model: str, input_data: list | None = None
):
    """Stream Kilo Chat Completions as Responses API SSE events.

    Codex CLI expects the EXACT SSE event sequence from OpenAI Responses API:
    1. response.created
    2. response.in_progress
    3. response.output_item.added       (per output item)
    4. response.content_part.added      (per content part)
    5. response.output_text.delta       (multiple, for text chunks)
    OR response.function_call_arguments.delta (multiple, for tool args)
    6. response.output_text.done        (full accumulated text)
    OR response.function_call_arguments.done (complete tool args)
    7. response.content_part.done       (per content part)
    8. response.output_item.done        (per output item)
    9. response.completed
    """
    response_id = generate_response_id()
    created_at = int(time.time())

    # Send initial lifecycle events
    yield _sse_event(
        "response.created",
        {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": created_at,
                "model": model,
                "status": "in_progress",
                "output": [],
            },
        },
    )
    yield _sse_event(
        "response.in_progress",
        {
            "type": "response.in_progress",
            "response": {"id": response_id, "status": "in_progress", "output": []},
        },
    )

    try:
        transport = _tor_transport()
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, transport=transport
        ) as tc:
            async with tc.stream(
                "POST", f"{KILO_API_URL}/chat/completions", json=body, headers=headers
            ) as resp:
                # B1 FIX: Validate content-type to catch WAF/HTML error pages
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith(
                    "application/json"
                ) and not content_type.startswith("text/event-stream"):
                    err_body = await resp.aread()
                    err_text = err_body.decode()[:500]
                    # Check if this is a WAF block (HTML page)
                    if _is_vercel_waf_block(resp.status_code, err_text):
                        _mark_tor_waf_blocked()
                    yield _sse_event(
                        "response.failed",
                        {
                            "type": "response.failed",
                            "response": {
                                "id": response_id,
                                "status": "failed",
                                "error": {
                                    "message": f"Upstream returned {content_type}: {err_text[:200]}",
                                    "type": "api_error",
                                },
                            },
                        },
                    )
                    return

                if resp.status_code != 200:
                    err_body = await resp.aread()
                    yield _sse_event(
                        "response.failed",
                        {
                            "type": "response.failed",
                            "response": {
                                "id": response_id,
                                "status": "failed",
                                "error": {
                                    "message": err_body.decode()[:200],
                                    "type": "api_error",
                                },
                            },
                        },
                    )
                    return

                accumulated_content: list[str] = []
                tool_calls: list[dict] = []
                current_tool_args: dict[int, str] = {}
                output_index = 0
                text_item_id = None
                tool_item_ids: dict[int, str] = {}
                text_started = False
                tools_started: set[int] = set()

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    delta = chunk.get("choices", [{}])[0].get("delta", {})

                    # B2 FIX: Handle reasoning content from extended thinking models
                    reasoning_content = delta.get("reasoning") or delta.get(
                        "reasoning_content"
                    )
                    if reasoning_content:
                        accumulated_content.append(reasoning_content)
                        if not text_started:
                            text_started = True
                            text_item_id = _generate_item_id("msg")
                            yield _sse_event(
                                "response.output_item.added",
                                {
                                    "type": "response.output_item.added",
                                    "output_index": output_index,
                                    "item": {
                                        "id": text_item_id,
                                        "type": "message",
                                        "status": "in_progress",
                                        "role": "assistant",
                                        "content": [],
                                    },
                                },
                            )
                            yield _sse_event(
                                "response.content_part.added",
                                {
                                    "type": "response.content_part.added",
                                    "item_id": text_item_id,
                                    "output_index": output_index,
                                    "content_index": 0,
                                    "part": {"type": "output_text", "text": ""},
                                },
                            )
                        yield _sse_event(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "item_id": text_item_id,
                                "output_index": output_index,
                                "content_index": 0,
                                "delta": reasoning_content,
                            },
                        )

                    # ── Text content streaming ──
                    if delta.get("content"):
                        text_chunk = delta["content"]
                        accumulated_content.append(text_chunk)

                        # Emit output_item.added for text message (first time)
                        if not text_started:
                            text_started = True
                            text_item_id = _generate_item_id("msg")
                            yield _sse_event(
                                "response.output_item.added",
                                {
                                    "type": "response.output_item.added",
                                    "output_index": output_index,
                                    "item": {
                                        "id": text_item_id,
                                        "type": "message",
                                        "status": "in_progress",
                                        "role": "assistant",
                                        "content": [],
                                    },
                                },
                            )
                            yield _sse_event(
                                "response.content_part.added",
                                {
                                    "type": "response.content_part.added",
                                    "item_id": text_item_id,
                                    "output_index": output_index,
                                    "content_index": 0,
                                    "part": {"type": "output_text", "text": ""},
                                },
                            )

                        yield _sse_event(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "item_id": text_item_id,
                                "output_index": output_index,
                                "content_index": 0,
                                "delta": text_chunk,
                            },
                        )

                    # ── Tool call streaming ──
                    for tc_delta in delta.get("tool_calls", []):
                        tc_idx = tc_delta.get("index", 0)
                        if tc_idx not in tool_item_ids:
                            tool_item_ids[tc_idx] = _generate_item_id("fc")
                            tc_id = tc_delta.get("id", f"call_{uuid.uuid4().hex[:24]}")
                            tool_calls.append(
                                {
                                    "id": tc_id,
                                    "call_id": tc_id,
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            )
                            current_tool_args[tc_idx] = ""

                        func = tc_delta.get("function", {})
                        if func.get("name"):
                            tool_calls[tc_idx]["function"]["name"] = func["name"]
                            # Emit output_item.added for tool call (first time)
                            if tc_idx not in tools_started:
                                tools_started.add(tc_idx)
                                tc_output_idx = (
                                    output_index + (1 if text_started else 0) + tc_idx
                                )
                                yield _sse_event(
                                    "response.output_item.added",
                                    {
                                        "type": "response.output_item.added",
                                        "output_index": tc_output_idx,
                                        "item": {
                                            "id": tool_item_ids[tc_idx],
                                            "type": "function_call",
                                            "call_id": tool_calls[tc_idx]["call_id"],
                                            "name": func["name"],
                                            "arguments": "",
                                            "status": "in_progress",
                                        },
                                    },
                                )

                        if func.get("arguments"):
                            current_tool_args[tc_idx] += func["arguments"]
                            tool_calls[tc_idx]["function"]["arguments"] = (
                                current_tool_args[tc_idx]
                            )
                            tc_delta_output_idx = (
                                output_index + (1 if text_started else 0) + tc_idx
                            )
                            yield _sse_event(
                                "response.function_call_arguments.delta",
                                {
                                    "type": "response.function_call_arguments.delta",
                                    "item_id": tool_item_ids[tc_idx],
                                    "output_index": tc_delta_output_idx,
                                    "delta": func["arguments"],
                                },
                            )

                # ── Close all open items ──

                # Close text item
                if text_started and text_item_id:
                    full_text = "".join(accumulated_content)
                    yield _sse_event(
                        "response.output_text.done",
                        {
                            "type": "response.output_text.done",
                            "item_id": text_item_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "text": full_text,
                            "content": [{"type": "output_text", "text": full_text}],
                        },
                    )
                    yield _sse_event(
                        "response.content_part.done",
                        {
                            "type": "response.content_part.done",
                            "item_id": text_item_id,
                            "output_index": output_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": full_text},
                        },
                    )
                    yield _sse_event(
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "output_index": output_index,
                            "item": {
                                "id": text_item_id,
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": full_text}],
                            },
                        },
                    )

                # Close tool call items
                for tc_idx in sorted(tool_item_ids.keys()):
                    tc_output_idx = output_index + (1 if text_started else 0) + tc_idx
                    item_id = tool_item_ids[tc_idx]
                    tc = tool_calls[tc_idx]
                    yield _sse_event(
                        "response.function_call_arguments.done",
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": item_id,
                            "output_index": tc_output_idx,
                            "arguments": tc["function"]["arguments"],
                        },
                    )
                    yield _sse_event(
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "output_index": tc_output_idx,
                            "item": {
                                "id": item_id,
                                "type": "function_call",
                                "call_id": tc["call_id"],
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                                "status": "completed",
                            },
                        },
                    )

                # ── Build final output for storage ──
                final_output = []
                if accumulated_content:
                    final_output.append(
                        {
                            "type": "message",
                            "id": text_item_id or _generate_item_id("msg"),
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "".join(accumulated_content),
                                }
                            ],
                        }
                    )
                for tc in tool_calls:
                    final_output.append(_convert_tool_call_to_output(tc))

                response_data = {
                    "id": response_id,
                    "object": "response",
                    "created_at": created_at,
                    "model": model,
                    "output": final_output,
                    "status": "completed",
                    "error": None,
                }
                store_response(response_id, response_data, input_data)

                # Send completion event
                yield _sse_event(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": response_data,
                    },
                )

    except Exception as e:
        log.error(f"[RESPONSE-STREAM] Error: {e}")
        yield _sse_event(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "id": response_id,
                    "status": "failed",
                    "error": {"message": str(e), "type": "api_error"},
                },
            },
        )


# ─── Model Catalog ────────────────────────────────────────────────────


def _get_codex_model_catalog() -> dict:
    with _kilo_models_lock:
        models = list(KILO_FREE_MODELS.items())
    return {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": [
            {
                "slug": alias,
                "display_name": desc.split(" (")[0] if "(" in desc else desc,
                "description": desc,
                "default_reasoning_level": "high"
                if "reasoning" in desc.lower()
                or "grok" in alias
                or "corethink" in alias
                or "mimo-v2-pro" in alias
                else "medium"
                if "omni" in alias or "nemotron" in alias
                else "low",
                "vendor": provider,
                "capabilities": {
                    "tool_calling": True,
                    "vision": "omni" in alias,
                    "reasoning": "reasoning" in desc.lower()
                    or "grok" in alias
                    or "corethink" in alias
                    or "mimo-v2-pro" in alias
                    or "nemotron" in alias,
                    "reasoning_levels": ["low", "medium", "high", "xhigh"],
                    "function_calling": True,
                },
            }
            for alias, (provider, model_id, desc) in models
        ],
    }


def _get_openai_compatible_models() -> list:
    catalog = _get_codex_model_catalog()
    return [
        {
            "id": m["slug"],
            "object": "model",
            "created": int(time.time()),
            "owned_by": m["vendor"],
            "permission": [],
            "root": m["slug"],
            "parent": None,
            "description": m["description"],
            "capabilities": m["capabilities"],
        }
        for m in catalog["models"]
    ]


# ─── FastAPI App ───────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("JembatanAI-Codex Proxy starting on port %d...", PORT)
    log.info("Kilo API: %s", KILO_API_URL)
    log.info("TOR available: %s", _tor_available())
    log.info("Loading %d Kilo models...", len(KILO_FREE_MODELS))

    asyncio.create_task(_model_refresh_loop())
    asyncio.create_task(_session_cleanup_loop())

    # Periodic response cleanup
    async def _resp_cleanup():
        while True:
            await asyncio.sleep(300)
            cleanup_expired_responses()

    asyncio.create_task(_resp_cleanup())

    yield
    log.info("JembatanAI-Codex Proxy shutting down...")


app = FastAPI(title="JembatanAI-Codex Proxy", lifespan=lifespan)

# CORS middleware for browser-based Codex clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # Must be False when allow_origins=["*"] (CORS spec)
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health & Discovery Endpoints ──────────────────────────────────────


@app.get("/health")
async def health():
    kilo_ok = bool(_get_kilo_token())
    with _kilo_models_lock:
        model_count = len(KILO_FREE_MODELS)
    return {
        "status": "ok",
        "type": "codex",
        "port": PORT,
        "kilo_api": KILO_API_URL,
        "kilo_token_valid": kilo_ok,
        "kilo_accounts": len(list(KILO_ACCOUNTS_DIR.glob("account-*.json")))
        if KILO_ACCOUNTS_DIR.exists()
        else 0,
        "kilo_exhausted": _kilo_all_exhausted,
        "kilo_500_errors": _kilo_500_error_count,
        "kilo_is_down": _kilo_is_down(),
        "tor_available": _tor_available(),
        "tor_waf_blocked": time.time() < _tor_waf_blocked_until,
        "openrouter_configured": bool(OR_API_KEY),
        "models_count": model_count,
        "active_sessions": len(_session_state),
        "stored_responses": len(_responses_state),
        "uptime_seconds": int(time.time() - _server_start_time),
        "circuit_breakers": get_all_circuit_breakers(),
        "codex_compatible": True,
        "wire_api": "responses",
        "features": {
            "responses_api": True,
            "chat_completions": True,
            "websocket": True,
            "streaming": True,
            "tool_calling": True,
            "previous_response_id": True,
            "reasoning_parameter": True,
            "reasoning_levels": ["low", "medium", "high", "xhigh"],
            "cors": True,
            "model_catalog": True,
            "tor_ip_rotation": _tor_available(),
            "account_rotation": True,
            "openrouter_fallback": bool(OR_API_KEY),
            "500_error_tracking": True,
            "circuit_breaker": True,
            "alerts": True,
        },
    }


@app.get("/v1/models")
async def list_models():
    """OpenAI-compatible model listing for model discovery."""
    return {"object": "list", "data": _get_openai_compatible_models()}


@app.get("/codex/models")
async def codex_model_catalog():
    """Codex-specific model catalog for /model picker."""
    return _get_codex_model_catalog()


# ─── OpenAI Chat Completions API ───────────────────────────────────────


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI Chat Completions endpoint. Routes to Kilo API directly."""
    try:
        # ── API Key Auth (always required for usage tracking) ──
        auth_result = None  # Initialize for all paths
        api_key = (
            request.headers.get("authorization", "").replace("Bearer ", "").strip()
        )
        if not api_key:
            api_key = request.headers.get("x-api-key", "").strip()
        if not api_key:
            return JSONResponse(
                {
                    "error": {
                        "message": "Missing or invalid API key.",
                        "type": "authentication_error",
                    }
                },
                status_code=401,
            )
        auth_result = await validate_api_key(api_key)
        log.info(f"[CHAT] DEBUG: validate_api_key result={auth_result is not None}, error={auth_result.get('error') if isinstance(auth_result, dict) else None}")
        if auth_result is None:
            return JSONResponse(
                {
                    "error": {
                        "message": "Invalid API key.",
                        "type": "authentication_error",
                    }
                },
                status_code=401,
            )
        if auth_result.get("error") == "expired":
            return JSONResponse(
                {
                    "error": {
                        "message": "Subscription expired.",
                        "type": "permission_error",
                    }
                },
                status_code=403,
            )
        user = auth_result["user"]
        allowed, used, limit = await check_daily_quota(
            user["id"], user.get("plan", "free")
        )
        if not allowed:
            return JSONResponse(
                {
                    "error": {
                        "message": f"Daily quota exceeded ({used}/{limit}).",
                        "type": "rate_limit_error",
                    }
                },
                status_code=429,
            )

        body = await request.json()
        model = body.get("model", "kilo-mimo-v2-pro")
        stream = body.get("stream", False)

        kilo_alias = resolve_codex_model(model)
        _, kilo_model_id, _ = KILO_FREE_MODELS.get(
            kilo_alias, ("kilo", "xiaomi/mimo-v2-pro:free", "")
        )

        log.info(f"[CHAT] {model} → {kilo_alias} ({kilo_model_id}) stream={stream}")

        # Build Kilo request
        kilo_body = dict(body)
        kilo_body["model"] = kilo_model_id

        # Ensure stream_options.include_usage is passed through
        stream_options = body.get("stream_options")
        if stream and stream_options:
            kilo_body["stream_options"] = stream_options

        # Cap max_tokens for free models
        if "max_tokens" in kilo_body:
            kilo_body["max_tokens"] = min(kilo_body["max_tokens"], 16384)

        headers = _build_kilo_headers()

        if stream:
            # Wrap streaming to track usage after completion
            async def _stream_with_usage():
                async for chunk in _stream_chat_completions(kilo_body, headers):
                    yield chunk
                if auth_result:
                    try:
                        await increment_usage(
                            auth_result["user"]["id"],
                            auth_result["key"]["id"],
                            kilo_alias,
                        )
                    except Exception:
                        pass
            return StreamingResponse(
                _stream_with_usage(),
                media_type="text/event-stream",
            )
        else:
            log.info(f"[CHAT] DEBUG: Before _request_with_retry, auth_result={auth_result is not None}")
            resp = await _request_with_retry(
                f"{KILO_API_URL}/chat/completions", kilo_body, headers
            )
            log.info(f"[CHAT] DEBUG: After request, resp={resp is not None}, status={resp.status_code if resp else None}")
            if resp is None or resp.status_code != 200:
                # Fallback to OpenRouter when Kilo is exhausted or down
                if _kilo_is_exhausted() or _kilo_is_down():
                    log.warning(
                        f"[CHAT] Kilo failed ({'exhausted' if _kilo_is_exhausted() else 'down'}), "
                        f"trying OpenRouter fallback..."
                    )
                    or_result = await _openrouter_request(kilo_body)
                    if or_result:
                        return JSONResponse(or_result)
                status = resp.status_code if resp else 502
                text = resp.text[:200] if resp else "No response"
                return JSONResponse(
                    {"error": {"message": text, "type": "api_error"}},
                    status_code=status,
                )
            data = resp.json()
            # Handle reasoning content: when model spends tokens on reasoning
            # and content is null, forward reasoning as content so client gets output
            for choice in data.get("choices", []):
                msg = choice.get("message", {})
                if not (msg.get("content") or "").strip():
                    reasoning = (
                        msg.get("reasoning") or msg.get("reasoning_content") or ""
                    )
                    if reasoning.strip():
                        msg["content"] = reasoning
            # Track usage for authenticated customers
            if auth_result:
                try:
                    log.info(f"[CHAT] Incrementing usage for user {auth_result['user']['id']}")
                    await increment_usage(
                        auth_result["user"]["id"],
                        auth_result["key"]["id"],
                        kilo_alias,
                    )
                except Exception as e:
                    log.error(f"[CHAT] Increment usage failed: {e}")

            return JSONResponse(data)

    except Exception as e:
        log.error(f"[CHAT] Error: {e}")
        return JSONResponse(
            {"error": {"message": str(e), "type": "api_error"}}, status_code=500
        )


# ─── OpenAI Responses API (PRIMARY Codex CLI Endpoint) ────────────────


@app.post("/v1/responses")
async def create_response(request: Request):
    """
    OpenAI Responses API endpoint — PRIMARY endpoint for Codex CLI.

    Codex CLI sends:
    {
        "model": "gpt-5.4",
        "input": [
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "function_call_output", "call_id": "call_123", "output": "..."}
        ],
        "previous_response_id": "resp_abc...",
        "stream": true,
        "tools": [...]
    }

    We convert to Kilo Chat Completions format, send to Kilo API,
    and convert the response back to Responses API format.
    """
    try:
        # ── API Key Auth (skip for localhost) ──
        client_ip = request.client.host if request.client else "unknown"
        auth_info = None
        if client_ip not in ("127.0.0.1", "::1", "localhost"):
            api_key = (
                request.headers.get("authorization", "").replace("Bearer ", "").strip()
            )
            if not api_key:
                api_key = request.headers.get("x-api-key", "").strip()
            if not api_key:
                return JSONResponse(
                    {
                        "error": {
                            "message": "Missing API key. Set JEMBATANAI_API_KEY env var.",
                            "type": "authentication_error",
                        }
                    },
                    status_code=401,
                )
            auth_info = await validate_api_key(api_key)
            if auth_info is None:
                return JSONResponse(
                    {
                        "error": {
                            "message": "Invalid API key. Check your key at https://gateway.jembatanai.com/dashboard.html",
                            "type": "authentication_error",
                        }
                    },
                    status_code=401,
                )
            if auth_info.get("error") == "expired":
                return JSONResponse(
                    {
                        "error": {
                            "message": "Subscription expired. Renew at https://gateway.jembatanai.com/dashboard.html",
                            "type": "permission_error",
                        }
                    },
                    status_code=403,
                )
            # Check daily quota
            user = auth_info["user"]
            plan = user.get("plan", "free")
            allowed, used, limit = await check_daily_quota(user["id"], plan)
            if not allowed:
                return JSONResponse(
                    {
                        "error": {
                            "message": f"Daily quota exceeded ({used}/{limit}). Upgrade at https://gateway.jembatanai.com/dashboard.html",
                            "type": "rate_limit_error",
                        }
                    },
                    status_code=429,
                )
        body = await request.json()
        model = body.get("model", "kilo-mimo-v2-pro")
        raw_input = body.get("input", [])
        # Normalize input: handle plain string or list with string items
        if isinstance(raw_input, str):
            input_data = [{"type": "message", "role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            input_data = []
            for item in raw_input:
                if isinstance(item, str):
                    input_data.append(
                        {"type": "message", "role": "user", "content": item}
                    )
                elif isinstance(item, dict):
                    input_data.append(item)
        else:
            input_data = []
        previous_response_id = body.get("previous_response_id")
        stream = body.get("stream", False)
        max_output_tokens = body.get("max_output_tokens", body.get("max_tokens", 4096))
        tools = body.get("tools", [])
        instructions = body.get("instructions")  # Codex CLI system prompt
        reasoning = body.get(
            "reasoning"
        )  # Codex sends {"effort": "medium"|"high"|"low"}
        temperature = body.get("temperature")
        top_p = body.get("top_p")
        truncation = body.get("truncation")  # Codex sends "auto"|"disabled"
        store = body.get("store", True)  # Whether to store response for chaining

        # Resolve model
        kilo_alias = resolve_codex_model(model)
        _, kilo_model_id, _ = KILO_FREE_MODELS.get(
            kilo_alias, ("kilo", "xiaomi/mimo-v2-pro:free", "")
        )
        log.info(
            f"[RESPONSE] {model} → {kilo_alias} ({kilo_model_id}) prev={previous_response_id[:20] if previous_response_id else 'None'} stream={stream}"
        )

        # Convert Responses API input to messages
        messages = _responses_to_messages(
            input_data, previous_response_id, instructions
        )

        # Direct routing: no loop detection, no session tracking
        session: dict = {}

        # Build Kilo request
        kilo_body = {
            "model": kilo_model_id,
            "messages": messages,
            "max_tokens": min(max_output_tokens, 16384),
            "stream": stream,
        }

        # Pass through optional parameters from Codex CLI
        if temperature is not None:
            kilo_body["temperature"] = temperature
        if top_p is not None:
            kilo_body["top_p"] = top_p

        # Convert reasoning effort to Kilo-compatible format
        # Codex sends: {"effort": "low"|"medium"|"high"|"xhigh"}
        # Kilo supports: low, medium, high, xhigh (extra high for deep reasoning)
        if reasoning:
            if isinstance(reasoning, dict):
                effort = reasoning.get("effort", "medium")
            elif isinstance(reasoning, str):
                effort = reasoning
            else:
                effort = "medium"
            # Normalize non-standard values
            VALID_EFFORTS = {"low", "medium", "high", "xhigh"}
            if effort == "extra_high":
                effort = "xhigh"
            elif effort not in VALID_EFFORTS:
                effort = "high" if effort in ("max", "maximum") else "medium"
            kilo_body["reasoning"] = {"effort": effort}
            kilo_body["reasoning_effort"] = effort

        # Convert Responses API tools → OpenAI Chat Completions tools format
        # Codex sends flat: {"type":"function","name":"shell","description":"...","parameters":{...}}
        # Kilo Chat Completions needs: {"type":"function","function":{"name":"shell","description":"...","parameters":{...}}}
        if tools:
            openai_tools = []
            for t in tools:
                t_type = t.get("type", "")
                if (
                    t_type == "function"
                    or ("name" in t and "parameters" in t)
                    or ("function" in t)
                ):
                    # Handle both flat and wrapped formats:
                    # Flat: {"type":"function","name":"Bash","description":"...","parameters":{...}}
                    # Wrapped: {"type":"function","function":{"name":"Bash","description":"...","parameters":{...}}}
                    inner = t.get("function", t)
                    func_def = {
                        "name": inner.get("name", t.get("name", "")),
                        "description": inner.get(
                            "description", t.get("description", "")
                        ),
                        "parameters": inner.get("parameters", t.get("parameters", {})),
                    }
                    if t.get("strict") is not None:
                        func_def["strict"] = t["strict"]
                    openai_tools.append(
                        {
                            "type": "function",
                            "function": func_def,
                        }
                    )
                elif t_type == "custom":
                    # Freeform tool → convert to function type
                    openai_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": t.get("name", ""),
                                "description": t.get("description", ""),
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    )
                # Skip other tool types (web_search, local_shell, etc.) — not supported
            if openai_tools:
                kilo_body["tools"] = openai_tools
                # Pass through tool_choice if provided
                tc = body.get("tool_choice")
                if tc is not None:
                    kilo_body["tool_choice"] = tc
                # Pass through parallel_tool_calls if provided
                ptc = body.get("parallel_tool_calls")
                if ptc is not None:
                    kilo_body["parallel_tool_calls"] = ptc

        headers = _build_kilo_headers()

        if stream:
            # Wrap streaming generator to track usage after completion
            async def _stream_with_usage():
                async for chunk in _stream_responses_api(
                    kilo_body, headers, model, input_data
                ):
                    yield chunk
                # Track usage after stream completes
                if auth_info:
                    try:
                        await increment_usage(
                            auth_info["user"]["id"],
                            auth_info["key"]["id"],
                            kilo_alias,
                        )
                    except Exception:
                        pass

            return StreamingResponse(
                _stream_with_usage(),
                media_type="text/event-stream",
            )
        else:
            resp = await _request_with_retry(
                f"{KILO_API_URL}/chat/completions", kilo_body, headers
            )
            if resp is None or resp.status_code != 200:
                # Fallback to OpenRouter when Kilo is exhausted or down
                if _kilo_is_exhausted() or _kilo_is_down():
                    log.warning(
                        f"[RESPONSE] Kilo failed ({'exhausted' if _kilo_is_exhausted() else 'down'}), "
                        f"trying OpenRouter fallback..."
                    )
                    or_result = await _openrouter_request(kilo_body)
                    if or_result:
                        response_data = _chat_to_responses(or_result, model)
                        store_response(response_data["id"], response_data, input_data)
                        if auth_info:
                            try:
                                await increment_usage(
                                    auth_info["user"]["id"],
                                    auth_info["key"]["id"],
                                    kilo_alias,
                                )
                            except Exception:
                                pass
                        return JSONResponse(response_data)
                status = resp.status_code if resp else 502
                text = resp.text[:200] if resp else "No response"
                return JSONResponse(
                    {"error": {"message": text, "type": "api_error"}},
                    status_code=status,
                )

            # Convert to Responses API format
            response_data = _chat_to_responses(resp.json(), model)

            # Store for chaining
            store_response(response_data["id"], response_data, input_data)

            # Track usage for authenticated customers
            if auth_info:
                try:
                    await increment_usage(
                        auth_info["user"]["id"],
                        auth_info["key"]["id"],
                        kilo_alias,
                    )
                except Exception:
                    pass

            return JSONResponse(response_data)

    except Exception as e:
        log.error(f"[RESPONSE] Error: {e}")
        return JSONResponse(
            {"error": {"message": str(e), "type": "api_error"}}, status_code=500
        )


@app.get("/v1/responses/{response_id}")
async def get_response_endpoint(response_id: str):
    """Retrieve a previous response by ID (for previous_response_id chaining)."""
    data = get_stored_response(response_id)
    if data:
        return JSONResponse(data)
    return JSONResponse(
        {"error": {"message": "Response not found", "type": "not_found"}},
        status_code=404,
    )


@app.delete("/v1/responses/{response_id}")
async def delete_response_endpoint(response_id: str):
    """Delete a response by ID."""
    if delete_stored_response(response_id):
        return JSONResponse({"status": "deleted"})
    return JSONResponse(
        {"error": {"message": "Response not found", "type": "not_found"}},
        status_code=404,
    )


@app.put("/v1/responses/{response_id}")
async def update_response_endpoint(response_id: str, request: Request):
    """Update a stored response (e.g. for output modifications)."""
    data = get_stored_response(response_id)
    if not data:
        return JSONResponse(
            {"error": {"message": "Response not found", "type": "not_found"}},
            status_code=404,
        )
    try:
        body = await request.json()
        # Allow updating specific fields
        for key in ("metadata",):
            if key in body:
                data[key] = body[key]
        store_response(response_id, data)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse(
            {"error": {"message": str(e), "type": "api_error"}}, status_code=400
        )


# ─── WebSocket Endpoint for /v1/responses ──────────────────────────────
# Codex CLI tries WebSocket first, falls back to HTTPS.
# This endpoint handles WS connections and streams SSE events as text frames.


@app.websocket("/v1/responses")
async def websocket_responses(websocket: WebSocket):
    """WebSocket endpoint for Responses API streaming.

    Codex CLI connects to ws://host:port/v1/responses and sends:
    {"model":"...", "input":[...], "tools":[...], "stream":true, ...}

    Server streams back SSE-formatted text frames:
    event: response.created\ndata: {...}\n\n
    """
    # ── API Key Auth (skip for localhost) ──
    client_ip = websocket.client.host if websocket.client else "unknown"
    auth_info = None
    auth_rejected = False
    reject_reason = ""
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        # Extract API key from query param or Authorization header
        api_key = websocket.query_params.get("api_key", "")
        if not api_key:
            auth_header = websocket.headers.get("authorization", "")
            api_key = auth_header.replace("Bearer ", "").strip()
        if not api_key:
            api_key = websocket.headers.get("x-api-key", "").strip()
        if not api_key:
            auth_rejected = True
            reject_reason = "Missing API key"
            reject_type = "authentication_error"
        elif not api_key.startswith("gw-"):
            auth_rejected = True
            reject_reason = "Invalid API key format"
            reject_type = "authentication_error"
        else:
            auth_info = await validate_api_key(api_key)
            if auth_info is None:
                auth_rejected = True
                reject_reason = "Invalid API key"
                reject_type = "authentication_error"
            elif auth_info.get("error") == "expired":
                auth_rejected = True
                reject_reason = "Subscription expired"
                reject_type = "permission_error"
            else:
                # Check daily quota
                user = auth_info["user"]
                plan = user.get("plan", "free")
                allowed, used, limit = await check_daily_quota(user["id"], plan)
                if not allowed:
                    auth_rejected = True
                    reject_reason = f"Daily quota exceeded ({used}/{limit})"
                    reject_type = "rate_limit_error"

    # Accept WebSocket (FastAPI auto-completes handshake before handler runs)
    await websocket.accept()
    if auth_rejected:
        log.warning(f"[WS] Auth rejected for {client_ip}: {reject_reason}")
        try:
            error_event = _sse_event(
                "response.failed",
                {
                    "type": "response.failed",
                    "response": {
                        "id": generate_response_id(),
                        "status": "failed",
                        "error": {
                            "message": reject_reason,
                            "type": reject_type,
                        },
                    },
                },
            )
            await websocket.send_text(error_event)
            await websocket.close(code=4001, reason=reject_reason)
        except Exception:
            pass
        return

    log.info("[WS] WebSocket connection accepted for /v1/responses")

    try:
        # Receive the initial request as JSON
        raw = await websocket.receive_text()
        body = json.loads(raw)
    except (WebSocketDisconnect, json.JSONDecodeError) as e:
        log.warning(f"[WS] Failed to receive/parse request: {e}")
        try:
            await websocket.close(code=1003, reason="Invalid request format")
        except Exception:
            pass
        return

    try:
        model = body.get("model", "kilo-mimo-v2-pro")
        raw_input = body.get("input", [])
        # Normalize input: handle plain string or list with string items
        if isinstance(raw_input, str):
            input_data = [{"type": "message", "role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            input_data = []
            for item in raw_input:
                if isinstance(item, str):
                    input_data.append(
                        {"type": "message", "role": "user", "content": item}
                    )
                elif isinstance(item, dict):
                    input_data.append(item)
        else:
            input_data = []
        previous_response_id = body.get("previous_response_id")
        max_output_tokens = body.get("max_output_tokens", body.get("max_tokens", 4096))
        tools = body.get("tools", [])
        instructions = body.get("instructions")
        reasoning = body.get("reasoning")
        temperature = body.get("temperature")
        top_p = body.get("top_p")

        # Resolve model
        kilo_alias = resolve_codex_model(model)
        _, kilo_model_id, _ = KILO_FREE_MODELS.get(
            kilo_alias, ("kilo", "xiaomi/mimo-v2-pro:free", "")
        )
        log.info(f"[WS] {model} → {kilo_alias} ({kilo_model_id})")

        # Convert input to messages
        messages = _responses_to_messages(
            input_data, previous_response_id, instructions
        )

        # Direct routing: no loop detection, no session tracking
        session: dict = {}

        # Build Kilo request
        kilo_body = {
            "model": kilo_model_id,
            "messages": messages,
            "max_tokens": min(max_output_tokens, 16384),
            "stream": True,
        }
        if temperature is not None:
            kilo_body["temperature"] = temperature
        if top_p is not None:
            kilo_body["top_p"] = top_p
        if reasoning:
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
            kilo_body["reasoning"] = {"effort": effort}
            kilo_body["reasoning_effort"] = effort

        # Convert tools
        if tools:
            openai_tools = []
            for t in tools:
                t_type = t.get("type", "")
                if (
                    t_type == "function"
                    or ("name" in t and "parameters" in t)
                    or ("function" in t)
                ):
                    # Handle both flat and wrapped formats:
                    # Flat: {"type":"function","name":"Bash","parameters":{...}}
                    # Wrapped: {"type":"function","function":{"name":"Bash","parameters":{...}}}
                    inner = t.get("function", t)
                    func_def = {
                        "name": inner.get("name", t.get("name", "")),
                        "description": inner.get("description", t.get("description", "")),
                        "parameters": inner.get("parameters", t.get("parameters", {})),
                    }
                    if t.get("strict") is not None:
                        func_def["strict"] = t["strict"]
                    openai_tools.append({"type": "function", "function": func_def})
                elif t_type == "custom":
                    openai_tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": t.get("name", ""),
                                "description": t.get("description", ""),
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    )
            if openai_tools:
                kilo_body["tools"] = openai_tools
                # Pass through tool_choice if provided
                tc = body.get("tool_choice")
                if tc is not None:
                    kilo_body["tool_choice"] = tc
                ptc = body.get("parallel_tool_calls")
                if ptc is not None:
                    kilo_body["parallel_tool_calls"] = ptc

        headers = _build_kilo_headers()

        # Stream via WebSocket
        async for sse_chunk in _stream_responses_api(
            kilo_body, headers, model, input_data
        ):
            try:
                await websocket.send_text(sse_chunk)
            except (WebSocketDisconnect, Exception) as e:
                log.info(f"[WS] Client disconnected during stream: {e}")
                return

        # Track usage for authenticated customers
        if auth_info:
            try:
                await increment_usage(
                    auth_info["user"]["id"],
                    auth_info["key"]["id"],
                    kilo_alias,
                )
            except Exception:
                pass

        # Close WebSocket cleanly
        try:
            await websocket.close(code=1000, reason="Stream complete")
        except Exception:
            pass

    except WebSocketDisconnect:
        log.info("[WS] Client disconnected")
    except Exception as e:
        log.error(f"[WS] Error: {e}")
        try:
            error_event = _sse_event(
                "response.failed",
                {
                    "type": "response.failed",
                    "response": {
                        "id": generate_response_id(),
                        "status": "failed",
                        "error": {"message": str(e), "type": "api_error"},
                    },
                },
            )
            await websocket.send_text(error_event)
            await websocket.close(code=1011, reason=str(e)[:100])
        except Exception:
            pass


# ─── Main ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
