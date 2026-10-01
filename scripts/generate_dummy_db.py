#!/usr/bin/env python3
"""Build a synthetic demo archive for the web viewer.

Every person, chat and message in it is invented. The archive holds two
accounts, private chats, groups, a forum with topics and channels, with
photos, an album, a sticker, voice notes with transcripts, a round video, a
document, a location, a venue, a live location and a shared contact, replies,
forwards, reactions, edits with their earlier versions,
messages deleted in Telegram that the archive kept, and pinned messages.

It also holds every state the viewer draws for what the archive alone knows:
media never downloaded (too large, filtered out, not yet) or missing from
disk, an edit made on a later day, an edit that changed only the
formatting, edits the sync found (so the history says "at least"), transcripts that failed, found no
speech or came in two versions, a closed and a pinned topic, two viewer
accounts (password DEMO_VIEWER_PASSWORD), two share links (one revoked; the
other opens with DEMO_SHARE_TOKEN and has downloads off), and a few audit
log entries. A few reactions were taken back: the archive keeps them as
tombstones, and the viewer shows them after the live ones, folded into one
quiet chip: on a photo beside live reactions, on a photo with no caption,
on the only reaction of an outgoing message, on a message deleted later, and
a day after the message. Every reaction has its history (reaction_history):
on one photo seven hearts dropped to five, and a surprised face was taken
back and given again.

Usage:
    python scripts/generate_dummy_db.py --data-dir ./demo-data

Then point the viewer at it and log in with the credentials you set:
    export BACKUP_PATH=./demo-data/backups VIEWER_USERNAME=admin VIEWER_PASSWORD=change-me
    uvicorn telegram_archive.web.main:app

Pillow draws the pictures. ffmpeg, when it is on PATH, makes the voice notes
and the round video; without it those messages keep their rows but no file.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OWNER_PERSONAL = 7000001
OWNER_WORK = 7000002

USERS = {
    OWNER_PERSONAL: ("Robin", "Ashgrove", "robin_ashgrove"),
    OWNER_WORK: ("Robin", "Ashgrove", "robin_at_work"),
    7100001: ("Juniper", "Vale", "juniper_vale"),
    7100002: ("Orson", "Quill", "orsonquill"),
    7100003: ("Kofi", "Brightwater", "kofi_bw"),
    7100004: ("Mirela", "Pinecrest", None),
    7100005: ("Tobias", "Fernwood", "tobias_f"),
    7100006: ("Priya", "Lanternfield", "priya_lf"),
    7100007: ("Esme", "Driftwood", "esme_drift"),
    7100008: ("Hugo", "Tallowmere", None),
    7100009: ("Wren", "Oakhollow", "wren_oak"),
    7100010: ("Dax", "Cinderby", "dax_c"),
    7100011: ("Lior", "Saltmarsh", None),
    7100012: ("Noor", "Emberly", "noor_em"),
}

JUNIPER, ORSON, KOFI, MIRELA, TOBIAS, PRIYA, ESME, HUGO, WREN, DAX, LIOR, NOOR = range(7100001, 7100013)

HIKERS = -1001900000001
MAKERS = -1001900000002
HARBOR = -1001900000003
BOOKS = -4012345678
PLATFORM = -1001900000010
RELEASES = -1001900000011

# Chats with earlier profile photos in the archive, and how many.
EARLIER_AVATARS = {HIKERS: 2, KOFI: 1}

# Obviously fake credentials for the demo's viewer accounts and share link.
DEMO_VIEWER_PASSWORD = "demo-viewer-not-a-secret"
DEMO_SHARE_TOKEN = "demo-share-link-not-a-secret"

PALETTES = [
    # sky top, sky bottom, sun, three mountain layers (far to near), water
    ((255, 183, 128), (255, 226, 190), (255, 244, 214), (190, 120, 130), (130, 80, 110), (70, 45, 80), (230, 150, 120)),
    ((90, 160, 230), (190, 225, 250), (255, 250, 230), (120, 160, 190), (70, 115, 140), (40, 80, 90), (80, 150, 200)),
    ((40, 50, 110), (230, 120, 110), (255, 210, 160), (110, 70, 120), (70, 45, 95), (35, 25, 60), (180, 100, 120)),
    ((120, 200, 220), (220, 245, 235), (255, 255, 240), (110, 170, 140), (60, 125, 95), (30, 80, 60), (100, 180, 190)),
    ((20, 30, 70), (70, 90, 150), (240, 240, 255), (60, 70, 120), (40, 45, 90), (20, 25, 55), (60, 80, 140)),
    ((250, 210, 140), (255, 240, 200), (255, 255, 235), (200, 160, 110), (150, 110, 80), (90, 65, 50), (230, 190, 130)),
]


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw_landscape(path: Path, seed: int, size=(1280, 853), lake=None):
    """A flat illustrated landscape: gradient sky, sun, three ridges, maybe a lake."""
    from PIL import Image, ImageDraw, ImageFilter

    rng = random.Random(seed)
    sky_top, sky_bottom, sun, far, mid, near, water = PALETTES[seed % len(PALETTES)]
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=_lerp(sky_top, sky_bottom, y / h))
    sx, sy, sr = rng.randint(w // 5, 4 * w // 5), rng.randint(h // 6, h // 3), rng.randint(h // 14, h // 8)
    halo = Image.new("L", size, 0)
    ImageDraw.Draw(halo).ellipse([sx - sr * 3, sy - sr * 3, sx + sr * 3, sy + sr * 3], fill=110)
    img.paste(Image.new("RGB", size, sun), (0, 0), halo.filter(ImageFilter.GaussianBlur(sr)))
    d = ImageDraw.Draw(img)
    d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=sun)
    lake = rng.random() < 0.5 if lake is None else lake
    horizon = int(h * (0.72 if lake else 0.95))
    for layer, (color, base, amp) in enumerate(((far, 0.45, 0.16), (mid, 0.58, 0.12), (near, 0.7, 0.1))):
        pts = [(0, h)]
        phase = rng.random() * 10
        for x in range(0, w + 16, 16):
            y = h * base - h * amp * (
                0.6 * math.sin(x / w * (3 + layer) + phase)
                + 0.3 * math.sin(x / w * (9 + layer * 2) + phase * 2)
                + 0.1 * rng.random()
            )
            pts.append((x, min(int(y), horizon)))
        pts.append((w, h))
        d.polygon(pts, fill=color)
    if lake:
        d.rectangle([0, horizon, w, h], fill=water)
        for i in range(14):
            y = rng.randint(horizon + 8, h - 8)
            x = rng.randint(0, w - 200)
            d.line([(x, y), (x + rng.randint(60, 220), y)], fill=_lerp(water, (255, 255, 255), 0.35), width=2)
    img = img.filter(ImageFilter.SMOOTH)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=86)
    return size


def draw_avatar(path: Path, seed: int, kind: str):
    """A round-friendly square picture: a landscape crop for chats, soft shapes for people."""
    from PIL import Image, ImageDraw, ImageFilter

    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "landscape":
        tmp = path.with_suffix(".tmp.jpg")
        draw_landscape(tmp, seed, size=(480, 480), lake=seed % 2 == 0)
        Image.open(tmp).resize((320, 320)).save(path, "JPEG", quality=88)
        tmp.unlink()
        return
    rng = random.Random(seed)
    a = PALETTES[seed % len(PALETTES)][0]
    b = PALETTES[(seed + 2) % len(PALETTES)][3]
    img = Image.new("RGB", (320, 320))
    d = ImageDraw.Draw(img)
    for y in range(320):
        d.line([(0, y), (320, y)], fill=_lerp(a, b, y / 320))
    for _ in range(5):
        r = rng.randint(40, 110)
        x, y = rng.randint(0, 320), rng.randint(0, 320)
        d.ellipse([x - r, y - r, x + r, y + r], fill=_lerp(b, (255, 255, 255), rng.random() * 0.6))
    img.filter(ImageFilter.GaussianBlur(18)).save(path, "JPEG", quality=88)


def draw_sticker(path: Path):
    """A smiling sun on a transparent background, the shape of a Telegram sticker."""
    from PIL import Image, ImageDraw

    s = 512
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s // 2
    for i in range(12):
        ang = i * math.pi / 6
        x1, y1 = c + math.cos(ang) * 150, c + math.sin(ang) * 150
        x2, y2 = c + math.cos(ang) * 235, c + math.sin(ang) * 235
        d.line([(x1, y1), (x2, y2)], fill=(255, 176, 32, 255), width=34)
    d.ellipse([c - 150, c - 150, c + 150, c + 150], fill=(255, 204, 51, 255), outline=(240, 150, 20, 255), width=10)
    d.ellipse([c - 70, c - 50, c - 30, c + 5], fill=(60, 40, 30, 255))
    d.ellipse([c + 30, c - 50, c + 70, c + 5], fill=(60, 40, 30, 255))
    d.arc([c - 80, c - 30, c + 80, c + 90], 20, 160, fill=(60, 40, 30, 255), width=14)
    d.ellipse([c - 115, c + 15, c - 75, c + 45], fill=(255, 140, 110, 200))
    d.ellipse([c + 75, c + 15, c + 115, c + 45], fill=(255, 140, 110, 200))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "WEBP", quality=90)


def draw_document(path: Path, title: str, lines: list[str]):
    """A one-page PDF with a title and a short table of plain lines."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([100, 100, 1140, 180], fill=(40, 90, 150))
    d.text((120, 125), title, fill="white")
    for i, line in enumerate(lines):
        d.text((120, 240 + i * 50), line, fill=(30, 30, 30))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PDF", resolution=150)


def ffmpeg_voice(path: Path, seconds: int, seed: int) -> bool:
    """A speech-shaped tone burst in Ogg Opus: syllable-rate pulses over a low hum."""
    if not shutil.which("ffmpeg"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    f = 150 + seed % 60
    expr = f"0.4*sin(2*PI*{f}*t)*(0.5+0.5*sin(2*PI*3.1*t))*(0.6+0.4*sin(2*PI*0.37*t))"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc={expr}:s=48000:d={seconds}"]
    cmd += ["-c:a", "libopus", "-b:a", "24k", str(path)]
    return subprocess.run(cmd, check=False).returncode == 0


def ffmpeg_round_video(path: Path, seconds: int) -> bool:
    """A square H.264 clip of drifting colour, the shape of a round video message."""
    if not shutil.which("ffmpeg"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"gradients=s=384x384:d={seconds}:speed=0.03:seed=7"]
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(path)]
    return subprocess.run(cmd, check=False).returncode == 0


class ChatScript:
    """Collects one chat's messages, media, reactions and edits in order."""

    def __init__(self, account_id: int, owner: int, chat_id: int, first_id: int = 1):
        self.account_id = account_id
        self.owner = owner
        self.chat_id = chat_id
        self.next_id = first_id
        self.messages: list[dict] = []
        self.media: list[dict] = []
        self.reactions: list[tuple[int, str, int, list[int]]] = []
        # (message id, text, date, formatting entities, the path that saw it)
        self.versions: list[tuple[int, str, datetime, list | None, str | None]] = []
        self.transcripts: list[tuple[str, str, int]] = []
        # Photos an edit replaced: kept as media versions beside the text of the time.
        self.media_versions: list[dict] = []
        # (message id, emoji, when it was removed): reactions the archive keeps
        # as tombstones after they were taken back.
        self.removed_reactions: list[tuple[int, str, datetime]] = []
        # (message id, emoji) -> [(count, when)]: the states the archive saw,
        # oldest first, for a reaction whose count moved after it first came.
        self.reaction_states: dict[tuple[int, str], list[tuple[int, datetime]]] = {}
        self.by_id: dict[int, dict] = {}

    def add(
        self,
        when: datetime,
        sender: int | None,
        text: str = "",
        *,
        reply: int | None = None,
        media: dict | None = None,
        raw: dict | None = None,
        pinned: bool = False,
        topic: int | None = None,
        edited_from: str | list[str | tuple[str, list | None]] | None = None,
        edit_source: str | None = "listener",
        react: dict[str, int] | None = None,
        forward: tuple[int, int, str] | None = None,
        deleted_after: timedelta | None = None,
        edited_after: timedelta | None = None,
        reacted_after: timedelta | None = None,
    ) -> int:
        mid = self.next_id
        self.next_id += 1
        raw = dict(raw or {})
        msg = {
            "id": mid,
            "chat_id": self.chat_id,
            "sender_id": sender,
            "date": when,
            "text": text,
            "is_outgoing": 1 if sender == self.owner else 0,
            "is_pinned": 1 if pinned else 0,
            "reply_to_top_id": topic,
        }
        if sender in USERS:
            # The backup stores the sender's name as seen at capture time.
            first, last, _username = USERS[sender]
            msg["sender_name"] = f"{first} {last}"
        if reply is not None:
            msg["reply_to_msg_id"] = reply
            msg["reply_to_text"] = (self.by_id[reply]["text"] or "")[:100]
        if forward is not None:
            origin_chat, origin_msg, origin_name = forward
            msg["forward_from_id"] = origin_chat
            raw["forward_from_name"] = origin_name
            raw["forward_origin"] = {"chat_id": origin_chat, "message_id": origin_msg}
        if edited_from is not None:
            # Oldest first: each earlier text is kept as a version, a few minutes apart.
            # edited_after moves the edits later, a day on for an edit made the
            # next day.
            # An earlier version is its text, or (text, formatting entities).
            # The listener saw it unless edit_source says otherwise; a version
            # with no source is unknown and the history says "at least".
            earlier = [edited_from] if isinstance(edited_from, str) else list(edited_from)
            shift = edited_after or timedelta(0)
            for step, old in enumerate(earlier):
                old_text, old_entities = old if isinstance(old, tuple) else (old, None)
                # The original text dates from the send; each later one from its edit.
                version_date = when if step == 0 else when + shift + timedelta(minutes=2 * step)
                self.versions.append((mid, old_text, version_date, old_entities, edit_source))
            msg["edit_date"] = when + shift + timedelta(minutes=2 * len(earlier) + 1)
        if reacted_after is not None:
            # Telegram moves edit_date when only the reactions change and sets
            # edit_hide: the archive keeps both, and the viewer shows no edit.
            msg["edit_date"] = when + reacted_after
            msg["edit_hide"] = 1
        if deleted_after is not None:
            # Deleted in Telegram, kept by the archive (soft deletion).
            msg["is_deleted"] = 1
            msg["deleted_at"] = when + deleted_after
        if media is not None:
            media = dict(media)
            media["message_id"] = mid
            media["chat_id"] = self.chat_id
            media["id"] = f"{self.chat_id}_{mid}_{media['type']}"
            if media.get("grouped_id"):
                raw["grouped_id"] = media.pop("grouped_id")
            self.media.append(media)
        msg["raw_data"] = raw
        if react:
            # A channel's reactions are anonymous counts; a chat's name who reacted.
            voters = [u for u in USERS if u not in (sender, OWNER_WORK)] if sender else []
            for emoji, count in react.items():
                self.reactions.append((mid, emoji, count, voters[:count]))
        self.messages.append(msg)
        self.by_id[mid] = msg
        return mid

    def take_back(self, mid: int, emoji: str, count: int, when: datetime) -> None:
        """A reaction taken back: stored as the backup stores it, one row per
        emoji with no reactor and the count it had, tombstoned at ``when``."""
        self.reactions.append((mid, emoji, count, []))
        self.removed_reactions.append((mid, emoji, when))

    def reaction_moves(self, mid: int, emoji: str, states: list[tuple[int, datetime]]) -> None:
        """A reaction whose count moved: every state the archive saw, oldest
        first. The live row holds the last count, or the tombstone when it is 0."""
        self.reaction_states[(mid, emoji)] = states
        *_, (last, when) = states
        if last:
            self.reactions.append((mid, emoji, last, []))
        else:
            self.take_back(mid, emoji, states[-2][0], when)


FILLER = {
    "hikers": [
        "Anyone up for a short walk after work on Wednesday?",
        "I left my water bottle in someone's car, the green one.",
        "The weather app says clear skies all weekend.",
        "Do we need a permit for the east trail or is that only in summer?",
        "Thanks for organising, that was a great day.",
        "I found a better map of the valley, will bring it next time.",
        "Knee is fine now, I'm back for the next one.",
        "The café at the trailhead opens at eight on Sundays.",
        "Who has the spare headlamp?",
        "Let's keep it under 15 km this time.",
        "New boots are finally broken in.",
        "Same meeting point as last time?",
        "I can bring snacks for everyone.",
        "Saw two deer near the bridge this morning.",
        "Rain is moving in after four, so an early start makes sense.",
        "Uploaded the route file to the shared folder.",
    ],
    "books": [
        "Finished chapter nine last night. That ending!",
        "I'm only halfway, no spoilers please.",
        "Can we push the meeting to Thursday?",
        "The library has two more copies if anyone still needs one.",
        "I liked the first half better than the second.",
        "Next pick should be something shorter.",
        "The audiobook narrator is excellent.",
        "I keep thinking about the lighthouse keeper.",
        "Tea and cake at my place this time?",
        "Adding my notes to the doc now.",
    ],
    "makers": [
        "Laser cutter is booked until three today.",
        "Who left the soldering iron on? It's off now.",
        "We are out of M3 screws again.",
        "The workshop door code changes on Monday.",
        "Great turnout at the open night.",
        "Please label your projects on the shelf.",
    ],
    "platform": [
        "Standup moved to 10:15 today.",
        "The staging deploy is green again.",
        "I'll take the on-call handover this week.",
        "Can someone review the config change before lunch?",
        "Dashboards look normal after the release.",
        "Retro notes are in the team folder.",
        "Build times dropped after the cache change.",
        "Closing the ticket, the fix is live.",
    ],
    "casual": [
        "Morning!",
        "Sounds good to me.",
        "Haha, fair enough.",
        "On my way.",
        "Talk later?",
        "Sure, no rush.",
        "Thanks again for yesterday.",
        "Let me check and get back to you.",
        "Perfect.",
        "See you then!",
    ],
}


def spread(rng: random.Random, start: datetime, end: datetime, count: int) -> list[datetime]:
    """``count`` sorted times between start and end, only during waking hours."""
    times = []
    span = (end - start).total_seconds()
    while len(times) < count:
        t = start + timedelta(seconds=rng.random() * span)
        if 8 <= t.hour <= 22:
            times.append(t.replace(second=0, microsecond=0))
    return sorted(times)


def build(now: datetime) -> tuple[list[dict], list[ChatScript], list[dict]]:
    """Every chat's rows. Returns (chats, scripts, topics)."""
    rng = random.Random(42)
    chats: list[dict] = []
    scripts: list[ChatScript] = []
    topics: list[dict] = []
    day = timedelta(days=1)
    today = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if today > now:
        today -= day

    def filler(script, pool, members, start, end, count, **kw):
        for t in spread(rng, start, end, count):
            sender = rng.choice(members)
            script.add(t, sender, rng.choice(FILLER[pool]), **kw)

    # --- Weekend Hikers: the busy group with media and reactions --------------
    chats.append(
        {
            "account": 1,
            "id": HIKERS,
            "type": "group",
            "title": "Weekend Hikers",
            "participants_count": 24,
            "description": "Day hikes, trail reports and carpools.",
            "avatar": "landscape",
        }
    )
    s = ChatScript(1, OWNER_PERSONAL, HIKERS, first_id=1200)
    members = [JUNIPER, ORSON, KOFI, ESME, HUGO, OWNER_PERSONAL]
    s.add(now - 44 * day, ORSON, "", raw={"service_type": "service", "action_type": "chat_joined_by_link"})
    filler(s, "hikers", members, now - 43 * day, now - 30 * day, 22)
    for i, cap in enumerate(
        ["Lake loop at sunrise", "", "Top of the pass", "", "The long way back", "Worth the climb"]
    ):
        sent = now - (29 - i * 3) * day + timedelta(hours=rng.randint(1, 8))
        mid = s.add(
            sent,
            rng.choice(members[:-1]),
            cap,
            media={"type": "photo", "seed": 10 + i},
            react={"❤️": rng.randint(1, 5)} if cap else None,
        )
        if i == 0:
            # Taken back the next day: the time it went shows with its date.
            s.take_back(mid, "🔥", 1, sent + day + timedelta(hours=2))
        if i == 3:
            # A photo with no caption whose only reaction was taken back: it
            # is framed like a photo with live reactions, to hold the row.
            s.take_back(mid, "👍", 1, sent + timedelta(hours=1))
    filler(s, "hikers", members, now - 27 * day, now - 3 * day, 26)
    t = now - timedelta(minutes=110)
    s.add(t, ESME, "Trail report from Saturday is up. The ridge loop was muddy but worth it.")
    album = rng.randint(10**15, 10**16)
    first = s.add(
        t + timedelta(minutes=1),
        ESME,
        "A few shots from the ridge loop",
        media={"type": "photo", "seed": 21, "grouped_id": album},
        react={"🔥": 4, "😍": 2},
    )
    s.add(t + timedelta(minutes=1), ESME, "", media={"type": "photo", "seed": 22, "grouped_id": album})
    s.add(t + timedelta(minutes=1), ESME, "", media={"type": "photo", "seed": 23, "grouped_id": album})
    s.add(t + timedelta(minutes=1), ESME, "", media={"type": "photo", "seed": 25, "grouped_id": album})
    s.add(
        t + timedelta(minutes=6),
        HUGO,
        "That first one looks like a postcard",
        reply=first,
        react={"😂": 1},
        reacted_after=timedelta(minutes=4),
    )
    q = s.add(t + timedelta(minutes=9), KOFI, "Which trailhead did you park at?")
    s.add(
        t + timedelta(minutes=12),
        ESME,
        "The north lot. It fills up by 8, so get there early.",
        reply=q,
        edited_from=["The north lot.", "The north lot. It fills up by 9."],
    )
    s.add(t + timedelta(minutes=25), OWNER_PERSONAL, "Adding this one to the list for next month.")
    s.add(
        t + timedelta(minutes=41),
        JUNIPER,
        "Trail notice: the lower canyon path is closed for bridge repairs until Friday. Use the ridge detour.",
        forward=(HARBOR, 3021, "Harbor Town Weekly"),
        react={"👍": 3},
    )
    s.add(
        t + timedelta(minutes=58),
        JUNIPER,
        "",
        media={
            "type": "voice",
            "duration": 14,
            "seed": 3,
            "transcript": "Quick update on Sunday. The forecast looks clear until mid afternoon, so let's meet at seven "
            "thirty at the north lot. Bring layers, it gets windy at the top.",
        },
        react={"👍": 3},
    )
    s.add(
        t + timedelta(minutes=66),
        ORSON,
        "",
        raw={
            "poll": {
                "question": "Where should we go next Sunday?",
                "answers": [
                    {"text": "Pine Lake loop", "option": "MA=="},
                    {"text": "Ridge trail again", "option": "MQ=="},
                    {"text": "Coastal path", "option": "Mg=="},
                ],
                "closed": False,
                "public_voters": True,
                "multiple_choice": False,
                "quiz": False,
                "results": {
                    "total_voters": 11,
                    "results": [
                        {"option": "MA==", "voters": 6},
                        {"option": "MQ==", "voters": 2},
                        {"option": "Mg==", "voters": 3},
                    ],
                },
            }
        },
    )
    s.add(t + timedelta(minutes=70), HUGO, "", media={"type": "sticker"}, raw={"sticker": {"emoji": "☀️"}})
    # Sent with another photo, which Kofi replaced three minutes later with
    # the caption unchanged: the archive keeps the first one as a media version.
    view = s.add(
        t + timedelta(minutes=83),
        KOFI,
        "Found this view on the way back",
        media={"type": "photo", "seed": 24, "replaced_seed": 27},
        react={"🔥": 1},
        edited_from="Found this view on the way back",
    )
    # Seven hearts dropped to five, and someone took their 😮 back and gave it
    # again: the history keeps both, and the list of reactions taken back reads
    # "2 of 7" and "back".
    s.reaction_moves(view, "❤️", [(7, t + timedelta(minutes=84)), (5, t + timedelta(minutes=91))])
    s.reaction_moves(
        view, "😮", [(1, t + timedelta(minutes=84)), (0, t + timedelta(minutes=88)), (1, t + timedelta(minutes=89))]
    )
    s.add(
        t + timedelta(minutes=95),
        ORSON,
        "Meeting point for Sunday: north lot, 7:30. Carpool list is in the shared sheet.",
        pinned=True,
        react={"👌": 4},
    )
    parking = s.add(
        t + timedelta(minutes=96),
        HUGO,
        "Parking at the north lot is free before nine, after that it's the paid lot by the café.",
        deleted_after=timedelta(minutes=4),
    )
    # A reaction taken back on a message deleted later, and the only reaction
    # of an outgoing message taken back.
    s.take_back(parking, "👍", 1, t + timedelta(minutes=98))
    drive = s.add(t + timedelta(minutes=97), OWNER_PERSONAL, "I can drive, room for three more.")
    s.take_back(drive, "👍", 2, t + timedelta(minutes=100))
    s.add(
        t + timedelta(minutes=98),
        KOFI,
        "Blurry one, sorry",
        media={"type": "photo", "seed": 26},
        deleted_after=timedelta(minutes=2),
    )
    s.add(t + timedelta(minutes=99), ESME, "Perfect, save me a seat 🙌", react={"❤️": 1})
    scripts.append(s)

    # --- Juniper: private chat with a voice note, a sticker and a round video ---
    chats.append(
        {
            "account": 1,
            "id": JUNIPER,
            "type": "private",
            "first_name": "Juniper",
            "last_name": "Vale",
            "username": "juniper_vale",
            "avatar": "person",
        }
    )
    s = ChatScript(1, OWNER_PERSONAL, JUNIPER, first_id=880)
    filler(s, "casual", [JUNIPER, OWNER_PERSONAL], now - 40 * day, now - 2 * day, 30)
    # A location, a venue, a live location that has ended and a shared
    # contact, drawn as cards. Demo places and a fake number only. The venue
    # and the location carry the media row the backup writes; the live
    # location and the contact have none, as the listener stores them. The
    # oldest location and the poll after it were archived by a release that
    # kept no payload and left a path to an empty placeholder file, so they
    # say "Details not archived".
    t = today - day + timedelta(hours=8)
    s.add(t - 5 * day, JUNIPER, "", media={"type": "geo", "legacy_bin": True})
    s.add(t - 5 * day + timedelta(minutes=1), JUNIPER, "", media={"type": "poll", "legacy_bin": True})
    bakery_q = s.add(t, OWNER_PERSONAL, "Where is that bakery you keep talking about?")
    bakery = s.add(
        t + timedelta(minutes=2),
        JUNIPER,
        "",
        reply=bakery_q,
        media={"type": "venue"},
        raw={
            "venue": {
                "title": "Elm Street Bakery",
                "address": "12 Elm Street, Demo Town",
                "provider": "foursquare",
                "venue_id": "demo-venue-0001",
                "venue_type": "food/bakery",
                "lat": 40.416775,
                "long": -3.70379,
            }
        },
    )
    parked = s.add(
        t + timedelta(minutes=3),
        JUNIPER,
        "",
        media={"type": "geo"},
        raw={"geo": {"lat": 40.41902, "long": -3.70091, "accuracy_radius": 25}},
    )
    s.add(t + timedelta(minutes=4), JUNIPER, "I parked there, the bakery is two streets down.", reply=parked)
    live_start = t + timedelta(hours=1)
    s.add(
        live_start,
        JUNIPER,
        "",
        raw={
            "geo_live": {
                "lat": 40.41702,
                "long": -3.70322,
                "period": 900,
                "heading": 90,
                "accuracy_radius": 10,
                "at": (live_start + timedelta(minutes=14)).isoformat(),
                "earlier": [
                    {"lat": 40.41850, "long": -3.70150, "at": (live_start + timedelta(minutes=2)).isoformat()},
                ],
            }
        },
    )
    s.add(
        live_start + timedelta(minutes=15),
        OWNER_PERSONAL,
        "On my way. Here is the number of the owner, she sells the rolls wholesale.",
        reply=bakery,
    )
    s.add(
        live_start + timedelta(minutes=16),
        OWNER_PERSONAL,
        "",
        raw={
            "contact": {
                "first_name": "Alex",
                "last_name": "Demo",
                "phone_number": "15555550100",
                "vcard": "",
                "user_id": 0,
            }
        },
    )
    t = now - timedelta(hours=3)
    s.add(t, JUNIPER, "Did you get home okay?")
    s.add(t + timedelta(minutes=2), OWNER_PERSONAL, "Yes, thanks! My legs are still complaining 😅")
    s.add(
        t + timedelta(minutes=4),
        JUNIPER,
        "",
        media={
            "type": "voice",
            "duration": 11,
            "seed": 7,
            "transcript": "So I finally tried the bakery on Elm Street. The cinnamon rolls are huge, we have to go "
            "together next weekend. Also, remind me to give your camera bag back.",
        },
    )
    pick = s.add(t + timedelta(minutes=6), OWNER_PERSONAL, "Ha, yes please. Saturday morning?")
    s.add(t + timedelta(minutes=7), JUNIPER, "Saturday works. Ten o'clock?", reply=pick, react={"👍": 1})
    s.add(t + timedelta(minutes=8), JUNIPER, "", media={"type": "sticker"}, raw={"sticker": {"emoji": "☀️"}})
    s.add(
        t + timedelta(minutes=15),
        JUNIPER,
        "",
        media={
            "type": "video_note",
            "duration": 6,
            "transcript": "Look, the first snow on the hills behind the house. Worth a trip up there soon.",
        },
    )
    s.add(t + timedelta(minutes=17), OWNER_PERSONAL, "Deal. I'll bring the bag too.", react={"❤️": 1})
    # Transcripts in the other states: two versions of one, one that found no
    # speech, and one the server refused as too large.
    s.add(
        t + timedelta(minutes=20),
        JUNIPER,
        "",
        media={
            "type": "voice",
            "duration": 9,
            "seed": 11,
            "transcript": [
                "The trail map is on the fridge, next to the bus times.",
                "The trail map is on the fridge next to the bus timetable.",
            ],
        },
    )
    s.add(t + timedelta(minutes=22), JUNIPER, "", media={"type": "voice", "duration": 4, "seed": 12, "transcript": ""})
    s.add(
        t + timedelta(minutes=24),
        JUNIPER,
        "",
        media={"type": "voice", "duration": 42, "seed": 13, "transcript_error": "too_large"},
    )
    scripts.append(s)

    # --- Orson: a document and a forward -----------------------------------------
    chats.append(
        {
            "account": 1,
            "id": ORSON,
            "type": "private",
            "first_name": "Orson",
            "last_name": "Quill",
            "username": "orsonquill",
        }
    )
    s = ChatScript(1, OWNER_PERSONAL, ORSON, first_id=410)
    filler(s, "casual", [ORSON, OWNER_PERSONAL], now - 35 * day, now - 4 * day, 16)
    t = today - 2 * day + timedelta(hours=5)
    s.add(
        t,
        ORSON,
        "Here is the budget for the cabin trip. Numbers are per person.",
        media={"type": "document", "file": "cabin-trip-budget.pdf"},
    )
    s.add(t + timedelta(minutes=10), OWNER_PERSONAL, "Looks fair. I'll send my share tonight.")
    s.add(
        t + timedelta(minutes=12),
        ORSON,
        "The ferry timetable changes on the first of the month.",
        forward=(HARBOR, 3015, "Harbor Town Weekly"),
        media={"type": "photo", "seed": 31},
    )
    q = s.add(t + timedelta(minutes=14), ORSON, "Worth checking before we book.")
    # Media the viewer cannot show, one of each reason: too large for the
    # download limit, filtered out, not downloaded yet, and a file the row
    # claims that is missing from the disk.
    later = t + timedelta(hours=3)
    s.add(later - timedelta(minutes=5), OWNER_PERSONAL, "Good call. Send the rest when you can.", reply=q)
    s.add(
        later,
        ORSON,
        "The panorama from the top, full size",
        media={"type": "photo", "skip": "oversize", "file_size": 25_165_824, "width": 6000, "height": 2400},
    )
    s.add(
        later + timedelta(minutes=1),
        ORSON,
        "Gear list for the cabin",
        media={
            "type": "document",
            "skip": "filtered",
            "file_name": "cabin-gear-list.xlsx",
            "file_size": 48_210,
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
    )
    s.add(
        later + timedelta(minutes=2),
        ORSON,
        "",
        media={"type": "photo", "skip": "pending", "width": 1600, "height": 1200},
    )
    s.add(
        later + timedelta(minutes=3),
        ORSON,
        "The clip from the ferry",
        media={
            "type": "video",
            "skip": "missing",
            "file_size": 3_407_872,
            "width": 1280,
            "height": 720,
            "duration": 21,
        },
    )
    scripts.append(s)

    # --- Kofi, Mirela (archived), Tobias -----------------------------------------
    for uid, first_id, days_ago, archived, avatar in (
        (KOFI, 150, 1, 0, "person"),
        (MIRELA, 60, 20, 1, None),
        (TOBIAS, 95, 6, 0, "person"),
    ):
        first, last, username = USERS[uid]
        chats.append(
            {
                "account": 1,
                "id": uid,
                "type": "private",
                "first_name": first,
                "last_name": last,
                "username": username,
                "is_archived": archived,
                "avatar": avatar,
            }
        )
        s = ChatScript(1, OWNER_PERSONAL, uid, first_id=first_id)
        filler(s, "casual", [uid, OWNER_PERSONAL], now - (days_ago + 30) * day, now - days_ago * day, 14)
        # The chat list preview's cases. Kofi's newest message was deleted in
        # Telegram, so the list shows the one before it. Tobias's newest is a
        # location the listener stored (a payload and no media row) and
        # Mirela's a contact the backup stored (both): the list names each with
        # a word.
        last = now - days_ago * day + timedelta(hours=1)
        if uid == KOFI:
            s.add(last, OWNER_PERSONAL, "See you at the trailhead at seven.")
            s.add(
                last + timedelta(minutes=3), KOFI, "Running ten minutes late, sorry", deleted_after=timedelta(minutes=1)
            )
        elif uid == TOBIAS:
            s.add(last, TOBIAS, "", raw={"geo": {"lat": 40.42011, "long": -3.70562, "accuracy_radius": 15}})
        elif uid == MIRELA:
            s.add(
                last,
                MIRELA,
                "",
                media={"type": "contact"},
                raw={
                    "contact": {
                        "first_name": "Sam",
                        "last_name": "Demo",
                        "phone_number": "15555550101",
                        "vcard": "",
                        "user_id": 0,
                    }
                },
            )
        scripts.append(s)

    # --- Book Club: a basic group ---------------------------------------------------
    chats.append(
        {
            "account": 1,
            "id": BOOKS,
            "type": "group",
            "title": "Book Club",
            "participants_count": 7,
            "avatar": "landscape",
        }
    )
    s = ChatScript(1, OWNER_PERSONAL, BOOKS, first_id=300)
    filler(s, "books", [NOOR, WREN, MIRELA, LIOR, OWNER_PERSONAL], now - 42 * day, now - 1 * day, 34)
    s.add(
        now - 2 * day + timedelta(hours=3),
        NOOR,
        "Next month we read The Salt Orchard. Meeting on the 14th.",
        pinned=True,
        react={"📚": 4},
        edited_from="Next month we read The Salt Orchard. Meeting on the 12th.",
        edited_after=day + timedelta(hours=2),
    )
    # An edit that changed only the formatting: the same words, "annotated" made bold.
    s.add(
        now - 3 * day + timedelta(hours=2),
        WREN,
        "Bring the annotated copy if you have one.",
        raw={"entities": [{"type": "bold", "offset": 10, "length": 9}]},
        edited_from=[("Bring the annotated copy if you have one.", None)],
        edit_source="listener",
    )
    # Edits the sync found: it reads only the text current at each run, so the
    # history can say only "at least". Each version keeps its own formatting.
    s.add(
        now - 3 * day + timedelta(hours=5),
        LIOR,
        "Chapter 4 has the best opening line in the book.",
        raw={"entities": [{"type": "bold", "offset": 23, "length": 7}]},
        edited_from=[
            ("Chapter 3 has the best line in the book.", [{"type": "italic", "offset": 18, "length": 4}]),
            ("Chapter 4 has the best line in the book.", None),
        ],
        edit_source="sync",
    )
    # A poll is the newest message: the list shows its question after the sender.
    s.add(
        now - timedelta(hours=20),
        NOOR,
        "",
        raw={
            "poll": {
                "question": "Which book should we read in spring?",
                "answers": [{"text": "The Salt Orchard", "option": "MA=="}, {"text": "Winter Lines", "option": "MQ=="}],
                "closed": False,
                "public_voters": True,
                "multiple_choice": False,
                "quiz": False,
                "results": {
                    "total_voters": 3,
                    "results": [{"option": "MA==", "voters": 2}, {"option": "MQ==", "voters": 1}],
                },
            }
        },
    )
    scripts.append(s)

    # --- Maker Space: a forum with topics --------------------------------------------
    chats.append(
        {
            "account": 1,
            "id": MAKERS,
            "type": "group",
            "title": "Maker Space",
            "participants_count": 138,
            "is_forum": 1,
            "description": "Our community workshop. One topic per craft.",
            "avatar": "landscape",
        }
    )
    s = ChatScript(1, OWNER_PERSONAL, MAKERS, first_id=1)
    makers = [DAX, WREN, TOBIAS, KOFI, LIOR, OWNER_PERSONAL]
    topic_defs = [
        (
            1,
            "General",
            None,
            0x6FB9F0,
            [
                "Welcome to everyone who joined this week!",
                "The workshop is open late on Thursday.",
            ],
        ),
        (
            None,
            "3D printing",
            "🖨️",
            0xFFD67E,
            [
                "Switched to the textured bed plate. First layers stick much better now.",
                "What temperature do you use for PETG?",
                "240 on the nozzle, 80 on the bed works for me.",
                "Printed a new handle for the band saw, fits perfectly.",
            ],
        ),
        (
            None,
            "Electronics",
            "🔌",
            0xCB86DB,
            [
                "The new oscilloscope arrived. Intro session on Saturday.",
                "Does anyone have a spare USB-C breakout board?",
                "Two in the parts drawer, second shelf.",
            ],
        ),
        (
            None,
            "Woodworking",
            "🪵",
            0x8EEE98,
            [
                "Sharpened all the chisels, please return them to the rack.",
                "The oak offcuts by the door are free to take.",
            ],
        ),
        (
            None,
            "Events",
            "📅",
            0xFF93B2,
            [
                "Open night this Thursday at 6pm. Bring a friend.",
                "Repair café is back next month.",
            ],
        ),
    ]
    for topic_id, title, emoji, color, lines in topic_defs:
        start = now - rng.randint(20, 40) * day
        if topic_id is None:
            topic_id = s.add(
                start,
                DAX,
                "",
                topic=None,
                raw={"service_type": "service", "action_type": "topic_create", "new_title": title},
            )
            s.messages[-1]["reply_to_top_id"] = topic_id
        topics.append(
            {
                "account": 1,
                "id": topic_id,
                "chat_id": MAKERS,
                "title": title,
                "icon_emoji": emoji,
                "icon_color": color,
                "date": start,
                "is_pinned": 1 if title == "Events" else 0,
                "is_closed": 1 if title == "Woodworking" else 0,
            }
        )
        times = spread(rng, start + timedelta(hours=1), now - timedelta(hours=rng.randint(2, 70)), len(lines) + 6)
        # Older chatter first, so the topic's own lines are the newest ones.
        texts = [rng.choice(FILLER["makers"]) for _ in range(6)] + lines
        for i, (when, text) in enumerate(zip(times, texts, strict=True)):
            media = {"type": "photo", "seed": 40} if text.startswith("Printed a new handle") else None
            s.add(
                when,
                rng.choice(makers),
                text,
                topic=topic_id if topic_id != 1 else None,
                media=media,
                react={"👏": rng.randint(1, 6)} if i % 4 == 0 else None,
            )
    scripts.append(s)

    # --- Harbor Town Weekly: a channel --------------------------------------------------
    chats.append(
        {
            "account": 1,
            "id": HARBOR,
            "type": "channel",
            "title": "Harbor Town Weekly",
            "username": "harbortown_weekly",
            "participants_count": 4812,
            "description": "Local news, events and notices.",
            "avatar": "landscape",
        }
    )
    s = ChatScript(1, OWNER_PERSONAL, HARBOR, first_id=3000)
    posts = [
        ("The farmers market moves to the pier this Saturday. Stalls open at 8.", 50),
        ("Library extends weekend opening hours from next month.", None),
        ("Photo of the week: fog over the old harbour wall.", 51),
        ("Road works on Mill Street start Monday. Expect short delays.", None),
        ("The summer concert series line-up is out.", None),
        ("Beach clean-up on Sunday morning. Gloves and bags provided.", 52),
        ("New bike racks are going in outside the station.", None),
        ("Photo of the week: first frost on the hill paths.", 53),
        ("Swimming pool closes for maintenance for two weeks.", None),
        ("Lost and found: a blue backpack was handed in at the town hall.", None),
        ("Harvest festival this weekend. Parade starts at noon.", 54),
        ("The night bus runs every 30 minutes on Fridays and Saturdays.", None),
        ("Volunteers wanted for the spring garden project.", None),
        ("Photo of the week: sailing boats back in the marina.", 55),
        ("Council meeting minutes are online.", None),
        ("The ferry timetable changes on the first of the month.", 31),
        ("Community choir rehearsals move to Tuesdays.", None),
        ("Street lights on the promenade are being replaced this week.", None),
        ("Market report: apples, pears and the first pumpkins.", 56),
        ("Trail closures this week are listed below.", None),
        ("Photo of the week: evening light over the lighthouse.", 57),
        ("Trail notice: the lower canyon path is closed for bridge repairs until Friday. Use the ridge detour.", None),
    ]
    times = spread(rng, now - 45 * day, now - timedelta(hours=5), len(posts))
    for (text, seed), when in zip(posts, times, strict=True):
        raw = {"post_author": "Editor"}
        if "Library" in text:
            raw["webpage"] = {
                "site_name": "Harbor Town",
                "url": "https://example.org/library-hours",
                "title": "Longer weekend hours at the town library",
                "description": "From next month the library stays open until 6pm on Saturdays and Sundays.",
            }
        s.add(
            when,
            None,
            text,
            media={"type": "photo", "seed": seed} if seed else None,
            raw=raw,
            react={"👍": rng.randint(8, 60), "❤️": rng.randint(2, 30)} if seed else {"👍": rng.randint(3, 25)},
        )
    scripts.append(s)

    # --- Work account ---------------------------------------------------------------------
    chats.append(
        {
            "account": 2,
            "id": PLATFORM,
            "type": "group",
            "title": "Platform Team",
            "participants_count": 9,
            "avatar": "landscape",
        }
    )
    s = ChatScript(2, OWNER_WORK, PLATFORM, first_id=5000)
    filler(s, "platform", [PRIYA, DAX, LIOR, OWNER_WORK], now - 30 * day, now - timedelta(hours=6), 40)
    s.add(
        now - timedelta(hours=5),
        PRIYA,
        "Release checklist for Thursday is in the team folder.",
        pinned=True,
        react={"👍": 3},
    )
    scripts.append(s)

    chats.append(
        {
            "account": 2,
            "id": PRIYA,
            "type": "private",
            "first_name": "Priya",
            "last_name": "Lanternfield",
            "username": "priya_lf",
            "avatar": "person",
        }
    )
    s = ChatScript(2, OWNER_WORK, PRIYA, first_id=700)
    filler(s, "casual", [PRIYA, OWNER_WORK], now - 20 * day, now - 5 * day, 12)
    q = s.add(now - timedelta(hours=26), PRIYA, "Can you review my pull request before lunch?")
    s.add(now - timedelta(hours=26) + timedelta(minutes=5), OWNER_WORK, "On it now.", reply=q)
    scripts.append(s)

    chats.append(
        {
            "account": 2,
            "id": RELEASES,
            "type": "channel",
            "title": "Release Notes",
            "participants_count": 312,
            "description": "What shipped this week.",
        }
    )
    s = ChatScript(2, OWNER_WORK, RELEASES, first_id=90)
    notes = [
        "Version 2.4 is out: faster search and a new export menu.",
        "Version 2.4.1 fixes the date picker.",
        "Version 2.5 adds dark mode to the admin pages.",
        "Version 2.5.1 improves import speed.",
    ]
    for text, when in zip(notes, spread(rng, now - 28 * day, now - 2 * day, len(notes)), strict=True):
        s.add(when, None, text, react={"🎉": rng.randint(4, 20)})
    scripts.append(s)

    return chats, scripts, topics


def write_media_files(media_root: Path, scripts: list[ChatScript]) -> None:
    """Draw every file a media row points at, and fill in the row's file metadata."""
    sticker = media_root / "_demo" / "sticker.webp"
    draw_sticker(sticker)
    for s in scripts:
        for m in s.media:
            folder = media_root / str(s.chat_id)
            kind = m["type"]
            file_id = int(hashlib.sha256(m["id"].encode()).hexdigest()[:8], 16)
            if kind in ("geo", "venue", "geo_live", "contact", "poll"):
                # A metadata-only row: no file. A release from 2025-12 to
                # 2026-04 left a path to an empty .bin placeholder on these,
                # which no longer exists on disk.
                m["file_size"] = 0
                m["downloaded"] = False
                if m.pop("legacy_bin", False):
                    m["file_name"] = f"{file_id}.bin"
                    m["file_path"] = f"{s.chat_id}/{file_id}.bin"
                    m["downloaded"] = True
                    m["download_date"] = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
                continue
            skip = m.pop("skip", None)
            if skip in ("oversize", "filtered", "pending"):
                # Never downloaded: the row says why, or nothing when it is only
                # waiting for the next backup.
                m["downloaded"] = False
                if skip != "pending":
                    m["skip_reason"] = skip
                continue
            if skip == "missing":
                # The row claims a file the disk does not have.
                name = f"{file_id}_{kind}.mp4" if kind == "video" else f"{file_id}_{kind}.jpg"
                m.update(file_name=name, mime_type="video/mp4" if kind == "video" else "image/jpeg")
                m["file_path"] = f"{s.chat_id}/{name}"
                m["downloaded"] = True
                m["download_date"] = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
                continue
            if kind == "photo":
                name = f"{file_id}_photo.jpg"
                w, h = draw_landscape(folder / name, m.pop("seed"))
                m.update(file_name=name, mime_type="image/jpeg", width=w, height=h)
                replaced_seed = m.pop("replaced_seed", None)
                if replaced_seed is not None:
                    earlier = f"{file_id + 1}_photo.jpg"
                    ew, eh = draw_landscape(folder / earlier, replaced_seed)
                    earlier_path = folder / earlier
                    s.media_versions.append(
                        {
                            "message_id": m["message_id"],
                            "media_id": f"{m['id']}_replaced",
                            "type": "photo",
                            "file_name": earlier,
                            "file_path": f"{s.chat_id}/{earlier}",
                            "file_size": earlier_path.stat().st_size,
                            "mime_type": "image/jpeg",
                            "width": ew,
                            "height": eh,
                            "content_hash": hashlib.sha256(earlier_path.read_bytes()).hexdigest(),
                        }
                    )
            elif kind == "sticker":
                name = f"{file_id}_sticker.webp"
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copy(sticker, folder / name)
                m.update(file_name=name, mime_type="image/webp", width=512, height=512)
            elif kind == "voice":
                name = f"{file_id}_voice.ogg"
                if not ffmpeg_voice(folder / name, m["duration"], m.pop("seed")):
                    continue
                m.update(file_name=name, mime_type="audio/ogg")
            elif kind == "video_note":
                name = f"{file_id}_round.mp4"
                if not ffmpeg_round_video(folder / name, m["duration"]):
                    continue
                m.update(file_name=name, mime_type="video/mp4", width=384, height=384)
            elif kind == "document":
                name = f"{file_id}_{m.pop('file')}"
                draw_document(
                    folder / name,
                    "Cabin trip budget",
                    [
                        "Cabin, two nights ........ 90.00",
                        "Groceries ................ 25.00",
                        "Fuel ..................... 12.50",
                        "Ferry .................... 8.00",
                        "Total per person ........ 135.50",
                    ],
                )
                m.update(file_name=name, mime_type="application/pdf")
            path = folder / name
            m["file_path"] = f"{s.chat_id}/{name}"
            m["file_size"] = path.stat().st_size
            m["content_hash"] = hashlib.sha256(path.read_bytes()).hexdigest()
            m["downloaded"] = True
            m["download_date"] = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    shutil.rmtree(media_root / "_demo")


async def seed(data_dir: Path) -> None:
    from sqlalchemy import insert, update

    from telegram_archive.db import close_adapter, create_adapter
    from telegram_archive.db.models import (
        AvatarHistory,
        MediaTranscript,
        MediaVersion,
        MessageVersion,
        Reaction,
        ReactionHistory,
    )

    backup = data_dir / "backups"
    media_root = backup / "media"
    db = await create_adapter()
    try:
        personal = await db.ensure_account(telegram_user_id=OWNER_PERSONAL, env_index=1, label="Personal")
        work = await db.ensure_account(telegram_user_id=OWNER_WORK, env_index=2, label="Work")
        accounts = {1: personal, 2: work}
        for uid, (first, last, username) in USERS.items():
            await db.upsert_user({"id": uid, "first_name": first, "last_name": last, "username": username})

        # The archive stores naive UTC, like the backup does.
        now = datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0)
        chats, scripts, topics = build(now)
        write_media_files(media_root, scripts)

        for i, chat in enumerate(chats):
            chat = dict(chat)
            account = accounts[chat.pop("account")]
            avatar = chat.pop("avatar", None)
            if avatar:
                photo_id = 5550000 + i
                chat["avatar_photo_id"] = photo_id
                folder = "users" if chat["type"] == "private" else "chats"
                draw_avatar(media_root / "avatars" / folder / f"{chat['id']}_{photo_id}.jpg", 60 + i * 7, avatar)
            await db.upsert_chat(chat, account_id=account)
            # Earlier profile photos the archive saw, so the info panel shows
            # its "Previous photos" row: an older file beside the current one
            # and a sighting of it, dated before the current photo's.
            for n in range(EARLIER_AVATARS.get(chat["id"], 0)):
                old_id = photo_id + 1000 * (n + 1)
                # Two palette steps from the current photo per earlier one, so an
                # earlier photo never reads as the current one in the panel.
                draw_avatar(
                    media_root / "avatars" / folder / f"{chat['id']}_{old_id}.jpg", 60 + i * 7 + 2 * (n + 1), avatar
                )
                async with db.db_manager.async_session_factory() as session:
                    await session.execute(
                        insert(AvatarHistory).values(
                            account_id=account,
                            chat_id=chat["id"],
                            photo_id=old_id,
                            seen_at=now - timedelta(days=150 * (n + 1)),
                        )
                    )
                    await session.commit()

        for s in scripts:
            account = accounts[s.account_id]
            await db.insert_messages_batch(s.messages, account_id=account)
            for m in s.media:
                transcript = m.pop("transcript", None)
                transcript_error = m.pop("transcript_error", None)
                await db.insert_media(m, account_id=account)
                if not m.get("downloaded"):
                    continue
                sent = s.by_id[m["message_id"]]["date"]
                # One text, several texts (later versions of the same audio, each a
                # row of its own), an empty text (no speech), or a failure.
                texts = transcript if isinstance(transcript, list) else ([transcript] if transcript is not None else [])
                for step, text in enumerate(texts):
                    row = await db.enqueue_media_transcript(
                        m["id"], account_id=account, preset="fast" if step == 0 else "best", force=step > 0
                    )
                    await db.fill_media_transcript(
                        row["id"],
                        status="done",
                        account_id=account,
                        text=text,
                        language="en",
                        source="akou",
                        engine_name="akou",
                        preset="fast" if step == 0 else "best",
                        confidence=0.94,
                        duration_s=float(m["duration"]),
                    )
                    # Finished a minute after the voice note arrived, so What
                    # changed dates it like a real run, not at seed time.
                    async with db.db_manager.async_session_factory() as session:
                        await session.execute(
                            update(MediaTranscript)
                            .where(MediaTranscript.id == row["id"])
                            .values(completed_at=sent + timedelta(minutes=1 + step))
                        )
                        await session.commit()
                if transcript_error:
                    row = await db.enqueue_media_transcript(m["id"], account_id=account)
                    await db.fill_media_transcript(
                        row["id"], status="failed", account_id=account, error=transcript_error
                    )
            async with db.db_manager.async_session_factory() as session:
                for mid, emoji, count, voters in s.reactions:
                    rows = [(uid, 1) for uid in voters] if voters else [(None, count)]
                    for uid, n in rows:
                        await session.execute(
                            insert(Reaction).values(
                                account_id=account, message_id=mid, chat_id=s.chat_id, emoji=emoji, user_id=uid, count=n
                            )
                        )
                # A reaction taken back stays as a tombstone (removed_at), the way
                # the backup keeps it.
                for mid, emoji, removed in s.removed_reactions:
                    await session.execute(
                        update(Reaction)
                        .where(Reaction.chat_id == s.chat_id, Reaction.message_id == mid, Reaction.emoji == emoji)
                        .values(removed_at=removed)
                    )
                # The history the listener would have written: a reaction seen a
                # minute after its message, and its removal when it went, unless
                # the script gave its own states.
                gone = {(mid, emoji): removed for mid, emoji, removed in s.removed_reactions}
                for mid, emoji, count, _voters in s.reactions:
                    states = s.reaction_states.get((mid, emoji))
                    if states is None:
                        states = [(count, s.by_id[mid]["date"] + timedelta(minutes=1))]
                        if (mid, emoji) in gone:
                            states.append((0, gone[(mid, emoji)]))
                    previous = None
                    for n, when in states:
                        await session.execute(
                            insert(ReactionHistory).values(
                                account_id=account,
                                chat_id=s.chat_id,
                                message_id=mid,
                                emoji=emoji,
                                count=n,
                                previous_count=previous,
                                observed_at=when,
                                source="listener",
                            )
                        )
                        previous = n
                for index, (mid, old_text, when, old_entities, source) in enumerate(s.versions):
                    digest = hashlib.sha256(f"{account}:{s.chat_id}:{mid}:{old_text}".encode()).hexdigest()
                    # Captured when the next text appeared: the next kept
                    # version of the same message, or its last edit.
                    following = s.versions[index + 1] if index + 1 < len(s.versions) else None
                    captured = following[2] if following and following[0] == mid else s.by_id[mid]["edit_date"]
                    await session.execute(
                        insert(MessageVersion).values(
                            account_id=account,
                            message_id=mid,
                            chat_id=s.chat_id,
                            text=old_text,
                            date=when,
                            captured_at=captured,
                            change_hash=digest,
                            entities=json.dumps(old_entities) if old_entities else None,
                            source=source,
                        )
                    )
                # The replaced photo, dated like the text it was sent with, as
                # the archive pairs them in the edit history.
                for earlier in s.media_versions:
                    sent_msg = s.by_id[earlier["message_id"]]
                    await session.execute(
                        insert(MediaVersion).values(
                            account_id=account,
                            chat_id=s.chat_id,
                            downloaded=1,
                            download_date=sent_msg["date"],
                            date=sent_msg["date"],
                            captured_at=sent_msg["edit_date"],
                            source="listener",
                            **earlier,
                        )
                    )
                await session.commit()
            await db.update_sync_status(
                s.chat_id, max(m["id"] for m in s.messages), len(s.messages), account_id=account
            )

        for topic in topics:
            topic = dict(topic)
            await db.upsert_forum_topic(topic, account_id=accounts[topic.pop("account")])

        await db.upsert_chat_folder(
            {"id": 2, "title": "Friends", "emoticon": "👋", "sort_order": 0}, account_id=personal
        )
        await db.sync_folder_members(2, [JUNIPER, ORSON, KOFI, TOBIAS, HIKERS, BOOKS], account_id=personal)
        await db.upsert_chat_folder(
            {"id": 3, "title": "Hobbies", "emoticon": "🎨", "sort_order": 1}, account_id=personal
        )
        await db.sync_folder_members(3, [HIKERS, MAKERS, HARBOR, BOOKS], account_id=personal)

        await seed_access(db, accounts, now)

        await db.set_metadata("last_backup_time", (now - timedelta(minutes=25)).isoformat() + "Z")
        await db.calculate_and_store_statistics(storage_path=str(backup))
        total = sum(len(s.messages) for s in scripts)
        print(f"Demo archive ready: {len(chats)} chats, {total} messages, {sum(len(s.media) for s in scripts)} media")
    finally:
        await close_adapter()


async def seed_access(db, accounts: dict[int, int], now: datetime) -> None:
    """Two viewer accounts, two share links (one revoked) and a few audit rows."""
    import json
    import secrets

    from sqlalchemy import select, update

    from telegram_archive.db.models import Chat, ViewerAuditLog, ViewerToken

    async with db.db_manager.async_session_factory() as session:
        rows = (await session.execute(select(Chat.account_id, Chat.id, Chat.ref))).all()
    ref = {(account_id, chat_id): chat_ref for account_id, chat_id, chat_ref in rows}
    personal, work = accounts[1], accounts[2]

    def password(salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", DEMO_VIEWER_PASSWORD.encode(), salt.encode(), 600_000).hex()

    salt = secrets.token_hex(16)
    await db.create_viewer_account(
        username="family",
        password_hash=password(salt),
        salt=salt,
        created_by="admin",
        allowed_chat_refs=json.dumps([ref[(personal, HIKERS)], ref[(personal, JUNIPER)], ref[(personal, BOOKS)]]),
    )
    salt = secrets.token_hex(16)
    await db.create_viewer_account(
        username="work-readonly",
        password_hash=password(salt),
        salt=salt,
        created_by="admin",
        no_download=1,
        is_active=0,
        allowed_accounts=json.dumps([work]),
    )

    def token_hash(plaintext: str, token_salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", plaintext.encode(), bytes.fromhex(token_salt), 600_000).hex()

    token_salt = secrets.token_hex(16)
    await db.create_viewer_token(
        label="Hike photos",
        token_hash=token_hash(DEMO_SHARE_TOKEN, token_salt),
        token_salt=token_salt,
        created_by="admin",
        allowed_chat_ids="[]",
        no_download=1,
        expires_at=now + timedelta(days=14),
        allowed_chat_refs=json.dumps([ref[(personal, HIKERS)]]),
    )
    token_salt = secrets.token_hex(16)
    old = await db.create_viewer_token(
        label="Book club link",
        token_hash=token_hash(secrets.token_urlsafe(24), token_salt),
        token_salt=token_salt,
        created_by="admin",
        allowed_chat_ids="[]",
        allowed_chat_refs=json.dumps([ref[(personal, BOOKS)]]),
    )
    async with db.db_manager.async_session_factory() as session:
        await session.execute(
            update(ViewerToken)
            .where(ViewerToken.id == old["id"])
            .values(is_revoked=1, use_count=5, last_used_at=now - timedelta(days=3))
        )
        await session.commit()

    events = [
        ("admin", "master", "login_success", 190),
        ("admin", "master", "viewer_created", 185),
        ("admin", "master", "token_created", 180),
        ("family", "viewer", "login_failed", 95),
        ("family", "viewer", "login_success", 94),
        ("Hike photos", "token", "token_auth_success", 40),
        ("admin", "master", "token_updated", 12),
    ]
    async with db.db_manager.async_session_factory() as session:
        for username, role, action, minutes_ago in events:
            await session.execute(
                ViewerAuditLog.__table__.insert().values(
                    username=username,
                    role=role,
                    action=action,
                    ip_address="192.0.2.10",
                    created_at=now - timedelta(minutes=minutes_ago),
                )
            )
        await session.commit()


# Written into the demo backups folder so --force can tell a demo archive from a
# real one. --force never deletes a folder without it.
DEMO_MARKER = ".telegram-archive-demo"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", default="demo-data", help="Base data directory (default: demo-data)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete an existing archive first, only if this script created it",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    backup = data_dir / "backups"
    if (backup / "telegram_backup.db").exists():
        if not args.force:
            sys.exit(f"{backup / 'telegram_backup.db'} already exists; pass --force to replace it")
        if not (backup / DEMO_MARKER).exists():
            sys.exit(f"{backup} holds an archive this script did not create; refusing to delete it")
        shutil.rmtree(backup)
    backup.mkdir(parents=True, exist_ok=True)
    (backup / DEMO_MARKER).write_text("demo archive written by scripts/generate_dummy_db.py\n")

    os.environ.pop("DATABASE_URL", None)
    os.environ["DB_TYPE"] = "sqlite"
    os.environ["BACKUP_PATH"] = str(backup)
    os.environ["DB_PATH"] = str(backup / "telegram_backup.db")

    # Alembic runs its own event loop, so the schema is built before ours starts.
    from telegram_archive.db.migrations import upgrade_to_head

    upgrade_to_head()
    asyncio.run(seed(data_dir))


if __name__ == "__main__":
    main()
