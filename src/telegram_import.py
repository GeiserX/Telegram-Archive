"""
Import Telegram Desktop chat exports into Telegram-Archive.

Reads result.json from Telegram Desktop exports (single-chat or full-account)
and inserts messages, users, and media into the existing database schema.
"""

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import DatabaseAdapter, close_database, get_adapter, init_database

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

CHAT_TYPE_MAP = {
    "personal_chat": "user",
    "bot_chat": "user",
    "saved_messages": "user",
    "private_group": "group",
    "private_supergroup": "supergroup",
    "public_supergroup": "supergroup",
    "private_channel": "channel",
    "public_channel": "channel",
}

MEDIA_TYPE_MAP = {
    "animation": "animation",
    "video_file": "video",
    "video_message": "video_note",
    "voice_message": "voice",
    "audio_file": "audio",
    "sticker": "sticker",
}


def parse_from_id(from_id: str | None) -> int | None:
    """Parse Telegram Desktop's from_id string into a numeric ID.

    Formats: "user123456789", "channel123456789", "group123456789"
    """
    if not from_id:
        return None
    for prefix, multiplier in (("user", 1), ("channel", -1), ("group", -1)):
        if from_id.startswith(prefix):
            try:
                raw = int(from_id[len(prefix) :])
                if prefix == "channel":
                    return -(1000000000000 + raw)
                return raw * multiplier
            except ValueError:
                return None
    return None


def derive_chat_id(export_id: int, export_type: str) -> int:
    """Derive a marked chat ID from the export's raw id and type."""
    if export_type in ("personal_chat", "bot_chat", "saved_messages"):
        return export_id
    if export_type == "private_group":
        return -export_id
    if export_type in ("private_supergroup", "public_supergroup", "private_channel", "public_channel"):
        return -(1000000000000 + export_id)
    return export_id


def flatten_text(text_field: str | list | None) -> str:
    """Flatten Telegram Desktop's text field to plain string.

    The field can be a plain string or an array of text entity objects
    like [{"type": "plain", "text": "Hello "}, {"type": "bold", "text": "world"}].
    """
    if text_field is None:
        return ""
    if isinstance(text_field, str):
        return text_field
    if isinstance(text_field, list):
        parts = []
        for item in text_field:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(text_field)


def parse_date(msg: dict) -> datetime | None:
    """Parse date from a Telegram Desktop export message."""
    if "date_unixtime" in msg:
        try:
            return datetime.fromtimestamp(int(msg["date_unixtime"]), tz=UTC).replace(tzinfo=None)
        except ValueError, TypeError, OSError:
            pass
    if "date" in msg:
        try:
            return datetime.fromisoformat(msg["date"]).replace(tzinfo=None)
        except ValueError, TypeError:
            pass
    return None


def parse_edited_date(msg: dict) -> datetime | None:
    """Parse edit date from a Telegram Desktop export message."""
    if "edited_unixtime" in msg:
        try:
            return datetime.fromtimestamp(int(msg["edited_unixtime"]), tz=UTC).replace(tzinfo=None)
        except ValueError, TypeError, OSError:
            pass
    if "edited" in msg:
        try:
            return datetime.fromisoformat(msg["edited"]).replace(tzinfo=None)
        except ValueError, TypeError:
            pass
    return None


def _detect_media(msg: dict, export_path: Path) -> tuple[str | None, str | None, str | None]:
    """Detect media type and file path from an export message.

    Returns (media_type, relative_path, original_filename).
    """
    if "photo" in msg and msg["photo"]:
        rel = msg["photo"]
        return "photo", rel, Path(rel).name

    if "file" in msg and msg["file"]:
        rel = msg["file"]
        fname = msg.get("file_name") or Path(rel).name
        media_type = MEDIA_TYPE_MAP.get(msg.get("media_type", ""), "document")
        return media_type, rel, fname

    return None, None, None


def _build_service_text(msg: dict) -> str:
    """Build display text for service messages from action fields."""
    action = msg.get("action", "")
    actor = msg.get("actor", "") or msg.get("from", "")
    text_parts = []

    if actor:
        text_parts.append(actor)

    action_map = {
        "pin_message": "pinned a message",
        "phone_call": "made a phone call",
        "create_group": "created the group",
        "invite_members": "invited members",
        "remove_members": "removed members",
        "join_group_by_link": "joined the group via invite link",
        "join_group_by_request": "joined the group via request",
        "migrate_to_supergroup": "upgraded to supergroup",
        "migrate_from_group": "migrated from group",
        "edit_group_title": "changed the group title",
        "edit_group_photo": "changed the group photo",
        "delete_group_photo": "removed the group photo",
        "score_in_game": "scored in a game",
        "custom_action": msg.get("text", "performed an action"),
    }

    text_parts.append(action_map.get(action, action.replace("_", " ") if action else "performed an action"))

    if msg.get("title"):
        text_parts.append(f'"{msg["title"]}"')
    if msg.get("members"):
        names = [m if isinstance(m, str) else str(m) for m in msg["members"]]
        text_parts.append(", ".join(names))

    return " ".join(text_parts)


class TelegramImporter:
    """Import Telegram Desktop exports into Telegram-Archive database."""

    def __init__(self, db: DatabaseAdapter, media_path: str):
        self.db = db
        self.media_path = media_path

    @classmethod
    async def create(cls, media_path: str) -> TelegramImporter:
        await init_database()
        db = await get_adapter()
        return cls(db, media_path)

    async def close(self) -> None:
        await close_database()

    async def run(
        self,
        export_path: str,
        chat_id_override: int | None = None,
        dry_run: bool = False,
        skip_media: bool = False,
        merge: bool = False,
    ) -> dict[str, Any]:
        """Run the import process.

        Returns a summary dict with counts per chat.
        """
        path = Path(export_path)
        result_file = path / "result.json"
        if not result_file.exists():
            raise FileNotFoundError(f"result.json not found in {path}")

        logger.info(f"Reading {result_file}...")
        with open(result_file, encoding="utf-8") as f:
            data = json.load(f)

        chats = self._extract_chats(data)
        if not chats:
            raise ValueError("No chats found in export file")

        summary: dict[str, Any] = {"chats_imported": 0, "total_messages": 0, "total_media": 0, "details": []}

        for chat_data in chats:
            chat_id = (
                chat_id_override
                if chat_id_override
                else derive_chat_id(chat_data.get("id", 0), chat_data.get("type", "personal_chat"))
            )

            if chat_id == 0:
                logger.warning(f"Skipping chat with no ID: {chat_data.get('name', 'unknown')}")
                continue

            result = await self._import_chat(
                chat_data=chat_data,
                chat_id=chat_id,
                export_path=path,
                dry_run=dry_run,
                skip_media=skip_media,
                merge=merge,
            )

            summary["chats_imported"] += 1
            summary["total_messages"] += result["messages"]
            summary["total_media"] += result["media"]
            summary["details"].append(result)

            if chat_id_override and len(chats) > 1:
                logger.info("--chat-id provided with multi-chat export; only importing first chat")
                break

        return summary

    def _extract_chats(self, data: dict) -> list[dict]:
        """Extract chat list from either single-chat or full-account export."""
        if "messages" in data:
            return [data]
        if "chats" in data and isinstance(data["chats"], dict):
            chat_list = data["chats"].get("list", [])
            if isinstance(chat_list, list):
                return chat_list
        return []

    async def _import_chat(
        self,
        chat_data: dict,
        chat_id: int,
        export_path: Path,
        dry_run: bool,
        skip_media: bool,
        merge: bool,
    ) -> dict[str, Any]:
        """Import a single chat from export data."""
        chat_name = chat_data.get("name", "Unknown")
        export_type = chat_data.get("type", "personal_chat")
        messages = chat_data.get("messages", [])

        logger.info(f"Importing chat '{chat_name}' (ID: {chat_id}, type: {export_type}) - {len(messages)} messages")

        if not merge and not dry_run:
            existing = await self.db.get_chat_stats(chat_id)
            if existing and existing.get("messages", 0) > 0:
                raise ValueError(
                    f"Chat {chat_id} ('{chat_name}') already has {existing['messages']} messages. "
                    "Use --merge to import into an existing chat."
                )

        if not dry_run:
            await self.db.upsert_chat(
                {
                    "id": chat_id,
                    "type": CHAT_TYPE_MAP.get(export_type, "unknown"),
                    "title": chat_name if export_type not in ("personal_chat", "bot_chat") else None,
                    "first_name": chat_name if export_type in ("personal_chat", "bot_chat") else None,
                }
            )

        seen_users: set[int] = set()
        msg_count = 0
        media_count = 0
        max_msg_id = 0
        batch: list[dict[str, Any]] = []
        media_batch: list[dict[str, Any]] = []

        for msg in messages:
            msg_id = msg.get("id")
            if msg_id is None:
                continue

            max_msg_id = max(max_msg_id, msg_id)
            msg_type = msg.get("type", "message")

            sender_id = parse_from_id(msg.get("from_id"))
            if sender_id and sender_id > 0 and sender_id not in seen_users and not dry_run:
                seen_users.add(sender_id)
                await self.db.upsert_user(
                    {
                        "id": sender_id,
                        "first_name": msg.get("from", ""),
                    }
                )

            if msg_type == "service":
                text = _build_service_text(msg)
            else:
                text = flatten_text(msg.get("text"))

            date = parse_date(msg)
            if date is None:
                logger.warning(f"Skipping message {msg_id}: no valid date")
                continue

            raw_data: dict[str, Any] = {}
            if msg.get("forwarded_from"):
                raw_data["forward_from_name"] = msg["forwarded_from"]

            message_data = {
                "id": msg_id,
                "chat_id": chat_id,
                "sender_id": sender_id,
                "date": date,
                "text": text,
                "reply_to_msg_id": msg.get("reply_to_message_id"),
                "forward_from_id": None,
                "edit_date": parse_edited_date(msg),
                "raw_data": raw_data,
                "is_outgoing": 0,
                "is_pinned": 0,
            }

            batch.append(message_data)
            msg_count += 1

            if not skip_media:
                media_type, rel_path, orig_name = _detect_media(msg, export_path)
                if media_type and rel_path:
                    source = export_path / rel_path
                    if source.exists():
                        media_id = f"import_{chat_id}_{msg_id}"
                        dest_dir = Path(self.media_path) / str(chat_id)
                        dest_name = f"{media_id}_{orig_name}" if orig_name else f"{media_id}"
                        dest_file = dest_dir / dest_name
                        stored_path = f"{chat_id}/{dest_name}"

                        media_data = {
                            "id": media_id,
                            "message_id": msg_id,
                            "chat_id": chat_id,
                            "type": media_type,
                            "file_name": orig_name,
                            "file_path": stored_path,
                            "file_size": source.stat().st_size,
                            "mime_type": msg.get("mime_type"),
                            "width": msg.get("width"),
                            "height": msg.get("height"),
                            "duration": msg.get("duration_seconds"),
                            "downloaded": True,
                            "download_date": datetime.now(UTC).replace(tzinfo=None),
                            "_source": str(source),
                            "_dest": str(dest_file),
                        }
                        media_batch.append(media_data)
                        media_count += 1
                    else:
                        logger.warning(f"Media file not found: {source}")

            if len(batch) >= BATCH_SIZE:
                if not dry_run:
                    await self._flush_batch(batch, media_batch)
                batch.clear()
                media_batch.clear()
                logger.info(f"  Progress: {msg_count}/{len(messages)} messages")

        if batch and not dry_run:
            await self._flush_batch(batch, media_batch)

        if not dry_run and msg_count > 0:
            await self.db.update_sync_status(chat_id, max_msg_id, msg_count)

        action = "Would import" if dry_run else "Imported"
        logger.info(f"{action} {msg_count} messages and {media_count} media files for '{chat_name}'")

        return {
            "chat_id": chat_id,
            "chat_name": chat_name,
            "messages": msg_count,
            "media": media_count,
            "max_message_id": max_msg_id,
        }

    async def _flush_batch(
        self,
        messages: list[dict[str, Any]],
        media: list[dict[str, Any]],
    ) -> None:
        """Flush a batch of messages and media to the database."""
        await self.db.insert_messages_batch(messages)

        for m in media:
            source = m.pop("_source")
            dest = m.pop("_dest")

            dest_path = Path(dest)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if not dest_path.exists():
                shutil.copy2(source, dest)

            await self.db.insert_media(m)
