from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import SUPABASE_KEY, SUPABASE_URL

log = logging.getLogger(__name__)

try:
    from supabase import Client, client
    has_supabase_lib = True
except ImportError:
    has_supabase_lib = False
    Client = Any

db_client: Client | None = None

if has_supabase_lib and SUPABASE_URL and SUPABASE_KEY:
    try:
        db_client = client.create_client(SUPABASE_URL, SUPABASE_KEY)
        log.info("Supabase connected successfully.")
    except Exception as e:
        log.error("Failed to initialize Supabase client: %s", e)
else:
    log.warning(
        "Supabase is not configured. Missing URL, KEY, or 'supabase' package. "
        "User tracking and rate-limits are disabled."
    )


def check_rate_limit(ip_address: str, max_requests: int = 3) -> bool:
    """
    Returns True if the user is ALLOWED (under limit), False if they are BLOCKED.
    Fails open (returns True) if the DB is unconfigured or unreachable.
    """
    if not db_client:
        return True

    try:
        # Check requests from this IP in the last 24 hours
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        
        # supabase-py count="exact" returns (data, count) tuple or count on the response object
        response = (
            db_client.table("interactions")
            .select("id", count="exact")
            .eq("user_ip", ip_address)
            .gte("created_at", yesterday)
            .execute()
        )
        
        count = response.count if hasattr(response, "count") and response.count is not None else len(response.data)
        
        if count >= max_requests:
            log.warning(f"Rate limit exceeded for IP: {ip_address} (Count: {count})")
            return False
            
        return True
    except Exception as e:
        log.error("Failed to check rate limit in Supabase: %s", e)
        return True  # Fail open


def log_interaction(ip_address: str, text_snippet: str, wpm: int, plan: Any) -> None:
    """
    Logs the user interaction (text snippet and resulting music plan) to Supabase.
    """
    if not db_client:
        return

    try:
        db_client.table("interactions").insert({
            "user_ip": ip_address,
            "text_snippet": text_snippet[:800] + ("..." if len(text_snippet) > 800 else ""),
            "wpm": wpm,
            "composer_prompt": plan.composer_prompt,
            "bpm": plan.bpm,
            "mood_tags": ", ".join(plan.mood_tags)
        }).execute()
        
        log.info(f"Interaction logged to DB for IP: {ip_address}")
    except Exception as e:
        log.error("Failed to log interaction to Supabase: %s", e)
