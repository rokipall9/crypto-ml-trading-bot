"""
pro_format_durable.py — Drop-in durable replacement for pro_format.send_embed.

Routes all Discord posts through webhook_queue:
  - Survives Discord 429 / 5xx / network blips
  - Honors X-RateLimit-Reset-After
  - Dead-letters after 5 attempts (replayable)
  - Never blocks the caller (background drain thread)

Migration:
    # before:
    import pro_format
    pro_format.send_embed(WEBHOOK, embed, image_bytes=png)

    # after — identical signature:
    import pro_format_durable as pf
    pf.send_embed(WEBHOOK, embed, image_bytes=png)

Returns the queue id (string) instead of bool, so callers can correlate
with webhook_queue logs if needed.
"""
from __future__ import annotations

import sys
from typing import Optional

sys.path.insert(0, "/home/ubuntu/common")
import webhook_queue


def send_embed(webhook_url: str,
               embed: dict,
               username: Optional[str] = None,
               image_bytes: Optional[bytes] = None,
               filename: str = "chart.png") -> str:
    """Durable counterpart to pro_format.send_embed. Returns queue id."""
    payload = {"embeds": [embed]}
    if username:
        payload["username"] = username
    files = [(filename, image_bytes)] if image_bytes else None
    return webhook_queue.enqueue(webhook_url, payload, files=files)


def send_text(webhook_url: str,
              content: str,
              username: Optional[str] = None) -> str:
    """Durable plain-text post (no embed)."""
    payload = {"content": content}
    if username:
        payload["username"] = username
    return webhook_queue.enqueue(webhook_url, payload)


def send_multi_embed(webhook_url: str,
                     embeds: list,
                     username: Optional[str] = None) -> str:
    """Durable multi-embed post (Discord allows up to 10)."""
    if len(embeds) > 10:
        raise ValueError("Discord allows max 10 embeds per webhook")
    payload = {"embeds": embeds}
    if username:
        payload["username"] = username
    return webhook_queue.enqueue(webhook_url, payload)
