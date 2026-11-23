"""Best-effort recovery of Matrix reply targets through the configured client.

Opt in via ``platforms.matrix.extra.reply_context_rooms`` (list of room IDs).
Only name private rooms whose fixed two participants are trusted to read the
history being retrieved: current membership does not grant historical access.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


def _field(event, key: str):
    return event.get(key) if isinstance(event, dict) else getattr(event, key, None)


async def fetch_reply_context(client, room_id: str, reply_to: str, *, sender: str, bot: str) -> tuple[str | None, str | None]:
    from plugins.platforms.matrix.adapter import EventID, RoomID, _strip_reply_fallback

    # Validate the actual participants rather than relying on cached DM classification.
    # One budget covers both requests and decryption.
    async with asyncio.timeout(3):
        members = await client.get_joined_members(RoomID(room_id))
        if set(members) != {sender, bot}:
            return None, None
        related_event = await client.get_event(RoomID(room_id), EventID(reply_to))
        if _field(related_event, "room_id") != room_id or _field(related_event, "event_id") != reply_to:
            return None, None
        if str(_field(related_event, "type")) == "m.room.encrypted":
            if not client.crypto:
                return None, None
            if isinstance(related_event, dict):
                from mautrix.types import EncryptedEvent
                related_event = EncryptedEvent.deserialize(related_event)
            related_event = await client.crypto.decrypt_megolm_event(related_event)
    if str(_field(related_event, "type")) != "m.room.message":
        return None, None
    related_content = _field(related_event, "content")
    if not isinstance(related_content, dict) and hasattr(related_content, "serialize"):
        related_content = related_content.serialize()
    if not isinstance(related_content, dict):
        return None, None
    body = related_content.get("body")
    if not isinstance(body, str) or not body.strip():
        return None, None
    relates_to = related_content.get("m.relates_to")
    if isinstance(relates_to, dict) and relates_to.get("m.in_reply_to"):
        body = _strip_reply_fallback(body)
    author = _field(related_event, "sender")
    return body[:2000], author if isinstance(author, str) else None


async def extract_reply_context(adapter, room_id: str, body: str, relates_to: dict, *, sender: str, is_dm: bool):
    """Strip transport quotes and recover genuine reply metadata best-effort."""
    from plugins.platforms.matrix.adapter import (
        _csv_set, _extract_reply_fallback, _strip_reply_fallback,
    )

    reply_to = (relates_to.get("m.in_reply_to") or {}).get("event_id")
    if relates_to.get("rel_type") == "m.thread" and relates_to.get("is_falling_back") is True:
        # The fallback is transport context, not user text or a genuine reply.
        return _strip_reply_fallback(body) if reply_to else body, None, None, None, None
    reply_to_text = reply_to_author_id = reply_to_author_name = None
    if reply_to and body.startswith("> "):
        reply_to_text, reply_to_author_id = _extract_reply_fallback(body)
        body = _strip_reply_fallback(body)
    elif (
        reply_to and adapter._client and is_dm and sender in adapter._allowed_user_ids
        and room_id in _csv_set(adapter.config.extra.get("reply_context_rooms"))
    ):
        try:
            reply_to_text, reply_to_author_id = await fetch_reply_context(
                adapter._client, room_id, reply_to, sender=sender, bot=adapter._user_id,
            )
        except Exception:
            logger.debug("Matrix: failed to fetch reply target %s in %s", reply_to, room_id, exc_info=True)
    # Resolve the replied-to author's display name (falls back to localpart).
    if reply_to_author_id:
        reply_to_author_name = await adapter._get_display_name(room_id, reply_to_author_id)
    return body, reply_to, reply_to_text, reply_to_author_id, reply_to_author_name
