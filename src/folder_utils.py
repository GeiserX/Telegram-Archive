"""Pure helpers for resolving Telegram chat-folder (dialog filter) membership.

Telegram folders (``dialogFilter`` / TDLib ``chatFolder``) define their contents
three ways at once: explicit peer lists (``pinned_peers``, ``include_peers``,
``exclude_peers``) and category flags (``contacts``, ``non_contacts``, ``groups``,
``broadcasts``, ``bots`` plus the ``exclude_*`` state flags). The backup only ever
persists membership for chats we actually archived, so this module answers a
bounded question: *of the chats in the archive, which belong to folder F?*

Kept dependency-free and pure (no Telethon, no DB) so the set-algebra is
unit-testable in isolation. Peer lists are expected already resolved to marked
chat ids; category evaluation uses the ``chats.type`` taxonomy that
``_extract_chat_data`` writes — only ``private`` / ``group`` / ``channel``
(megagroups are stored as ``group``; bots are ``private`` with ``is_bot`` coming
from the users table).

Membership precedence (matches the TDLib / Telegram Desktop reference
``need_dialog`` / ``ChatFilter::contains`` algorithms):

    1. chat ∈ (pinned ∪ include)  → member          (explicit peers dominate)
    2. chat ∈ exclude_peers       → not a member     (absolute over type/state)
    3. chat's category ∉ flags    → not a member
    4. exclude_* state flags      → drop type matches only

* Explicit ``pinned``/``include`` peers are **absolute** — always members
  regardless of ``exclude_peers`` or the ``exclude_*`` state flags. (pinned and
  include are disjoint by construction; deduped here via a union.)
* The ``exclude_*`` state flags refine only the flag/type matches, never the
  explicit peers. ``exclude_muted`` and ``exclude_read`` depend on live
  notification/unread state that the archive does not store, so they are **not
  applied** (a best-effort that errs toward showing a folder's chats rather than
  hiding the folder entirely). ``exclude_archived`` *is* applied — we store
  ``is_archived``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

# chats.type values that represent a "group" for the ``groups`` flag. Megagroups
# are persisted as "group"; "supergroup" is tolerated defensively in case another
# code path ever writes it.
_GROUP_TYPES = frozenset({"group", "supergroup"})


@dataclass(frozen=True)
class FolderChat:
    """The minimal archived-chat facts needed to evaluate a folder's flags."""

    id: int
    type: str  # "private" | "group" | "channel"
    is_bot: bool = False
    is_archived: bool = False


@dataclass(frozen=True)
class FolderRules:
    """A dialog filter's rules, with peer lists already resolved to marked ids."""

    pinned_ids: frozenset[int] = field(default_factory=frozenset)
    include_ids: frozenset[int] = field(default_factory=frozenset)
    exclude_ids: frozenset[int] = field(default_factory=frozenset)
    contacts: bool = False
    non_contacts: bool = False
    groups: bool = False
    broadcasts: bool = False
    bots: bool = False
    exclude_muted: bool = False  # not reconstructable from the archive; not applied
    exclude_read: bool = False  # not reconstructable from the archive; not applied
    exclude_archived: bool = False

    @property
    def has_type_flags(self) -> bool:
        return any((self.contacts, self.non_contacts, self.groups, self.broadcasts, self.bots))


def _matches_type_flags(chat: FolderChat, rules: FolderRules, contact_ids: frozenset[int]) -> bool:
    """Whether a chat matches any of the folder's category flags.

    A bot matches ``bots`` only — Telegram treats bots as their own category, so
    a bot never falls through to ``contacts``/``non_contacts``.
    """
    if chat.type in _GROUP_TYPES:
        return rules.groups
    if chat.type == "channel":
        return rules.broadcasts
    if chat.type == "private":
        if chat.is_bot:
            return rules.bots
        if chat.id in contact_ids:
            return rules.contacts
        return rules.non_contacts
    return False


def resolve_folder_member_ids(
    rules: FolderRules,
    chats: Iterable[FolderChat],
    contact_ids: Iterable[int] = (),
) -> set[int]:
    """Return the member chat ids of a folder.

    Explicit ``pinned``/``include`` peers are returned as-is (their existence in
    the archive is filtered downstream by ``sync_folder_members``, which only
    persists members that exist in the ``chats`` table). Flag/type matches are
    drawn from ``chats`` — the flags can only be evaluated against chats we've
    archived. ``contact_ids`` is the account's contact user ids (positive user
    ids, matching a private chat's id).
    """
    contacts = frozenset(contact_ids)
    explicit = rules.pinned_ids | rules.include_ids
    # Explicit peers dominate: always members, regardless of exclude_peers or the
    # exclude_* state flags (TDLib checks the explicit lists first).
    members: set[int] = set(explicit)
    if rules.has_type_flags:
        for chat in chats:
            cid = chat.id
            if cid in explicit or cid in rules.exclude_ids:
                continue
            if not _matches_type_flags(chat, rules, contacts):
                continue
            if rules.exclude_archived and chat.is_archived:
                continue
            members.add(cid)
    return members
