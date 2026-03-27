#!/usr/bin/env python3
"""
JembatanAI Alert System
Send alerts to Telegram/Email for critical issues
"""

import os
import logging
import httpx
from datetime import datetime

log = logging.getLogger("alerts")

# Configuration
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_ALERT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "")
ALERT_COOLDOWN = 300  # 5 minutes between same alerts

# Alert state
_last_alert_time = {}

def should_send_alert(alert_key: str) -> bool:
    """Check if we should send this alert (respect cooldown)."""
    now = datetime.now().timestamp()
    last_time = _last_alert_time.get(alert_key, 0)
    
    if now - last_time > ALERT_COOLDOWN:
        _last_alert_time[alert_key] = now
        return True
    return False

async def send_telegram_alert(message: str, alert_key: str = "general") -> bool:
    """Send alert to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram alert not configured (missing TOKEN or CHAT_ID)")
        return False
    
    if not should_send_alert(alert_key):
        log.info(f"Alert '{alert_key}' suppressed (cooldown)")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"🚨 JembatanAI Alert\n\n{message}",
            "parse_mode": "HTML"
        }
        
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        
        log.info(f"Alert sent: {alert_key}")
        return True
    
    except Exception as e:
        log.error(f"Failed to send Telegram alert: {e}")
        return False

async def send_critical_alert(alert_type: str, details: dict):
    """Send critical alert with context."""
    message = f"<b>Type:</b> {alert_type}\n"
    
    for key, value in details.items():
        message += f"<b>{key}:</b> {value}\n"
    
    message += f"\n<b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    
    await send_telegram_alert(message, alert_key=f"critical_{alert_type}")

# Alert types
ALERT_WAF_BLOCK_HIGH = "waf_block_high"
ALERT_KILO_EXHAUSTED = "kilo_exhausted"
ALERT_LOOP_DETECTED = "loop_detected"
ALERT_ERROR_RATE_HIGH = "error_rate_high"
ALERT_PROVIDER_DOWN = "provider_down"
ALERT_CONTEXT_BLOAT = "context_bloat"
