"""Unit tests for pure folder-membership resolution (src/folder_utils.py)."""

from src.folder_utils import FolderChat, FolderRules, resolve_folder_member_ids


def _chats():
    """A small archive spanning every category the resolver distinguishes."""
    return [
        FolderChat(id=1001, type="private", is_bot=False),  # a contact
        FolderChat(id=1002, type="private", is_bot=False),  # a non-contact
        FolderChat(id=1003, type="private", is_bot=True),  # a bot
        FolderChat(id=-2001, type="group"),  # basic group / megagroup
        FolderChat(id=-2002, type="supergroup"),  # tolerated group alias
        FolderChat(id=-1002003, type="channel"),  # broadcast channel
    ]


CONTACTS = frozenset({1001})


# --- explicit peers ---------------------------------------------------------


def test_include_peer_is_member():
    rules = FolderRules(include_ids=frozenset({1001}))
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {1001}


def test_pinned_peer_is_member():
    rules = FolderRules(pinned_ids=frozenset({-1002003}))
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {-1002003}


def test_pinned_and_include_overlap_dedup():
    rules = FolderRules(pinned_ids=frozenset({1001}), include_ids=frozenset({1001, 1002}))
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {1001, 1002}


def test_no_rules_yields_no_members():
    assert resolve_folder_member_ids(FolderRules(), _chats(), CONTACTS) == set()


def test_explicit_peer_passes_through_for_existence_filtering():
    # Explicit peers are returned as-is even if absent from the resolution
    # snapshot; sync_folder_members drops any that aren't archived.
    rules = FolderRules(include_ids=frozenset({999999}))
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {999999}


def test_flag_matches_are_bounded_by_provided_chats():
    # Type/flag membership can only come from the archived chats passed in.
    rules = FolderRules(groups=True)
    assert resolve_folder_member_ids(rules, [], CONTACTS) == set()


# --- exclude_peers is absolute ---------------------------------------------


def test_exclude_peer_removes_flag_match():
    rules = FolderRules(groups=True, exclude_ids=frozenset({-2001}))
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {-2002}


def test_explicit_include_beats_exclude_peer():
    # Disjoint by construction, but if a malformed filter lists a chat in both,
    # explicit include wins (matches the TDLib reference precedence).
    rules = FolderRules(include_ids=frozenset({1001}), exclude_ids=frozenset({1001}))
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {1001}


# --- category flags ---------------------------------------------------------


def test_groups_flag_matches_group_and_supergroup_only():
    rules = FolderRules(groups=True)
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {-2001, -2002}


def test_broadcasts_flag_matches_channels_only():
    rules = FolderRules(broadcasts=True)
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {-1002003}


def test_bots_flag_matches_bot_only():
    rules = FolderRules(bots=True)
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {1003}


def test_bot_never_matches_contacts_or_non_contacts():
    # 1003 is a bot; even with contacts+non_contacts it stays out (matches bots only).
    rules = FolderRules(contacts=True, non_contacts=True)
    result = resolve_folder_member_ids(rules, _chats(), CONTACTS)
    assert 1003 not in result
    assert result == {1001, 1002}


def test_contacts_flag_matches_contact_private_only():
    rules = FolderRules(contacts=True)
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {1001}


def test_non_contacts_flag_matches_non_contact_private_only():
    rules = FolderRules(non_contacts=True)
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {1002}


def test_combined_flags_union():
    rules = FolderRules(groups=True, broadcasts=True, bots=True)
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {-2001, -2002, -1002003, 1003}


# --- exclude_* state flags --------------------------------------------------


def test_exclude_archived_drops_archived_flag_match():
    chats = [
        FolderChat(id=-3001, type="group", is_archived=False),
        FolderChat(id=-3002, type="group", is_archived=True),
    ]
    rules = FolderRules(groups=True, exclude_archived=True)
    assert resolve_folder_member_ids(rules, chats, frozenset()) == {-3001}


def test_exclude_archived_does_not_drop_explicit_include():
    # Explicit includes are absolute — an archived pinned/included chat stays.
    chats = [FolderChat(id=-3002, type="group", is_archived=True)]
    rules = FolderRules(include_ids=frozenset({-3002}), groups=True, exclude_archived=True)
    assert resolve_folder_member_ids(rules, chats, frozenset()) == {-3002}


def test_exclude_muted_and_read_are_not_applied():
    # We can't reconstruct mute/unread state, so these flags don't hide anything.
    rules = FolderRules(groups=True, exclude_muted=True, exclude_read=True)
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {-2001, -2002}


# --- chatlist (shareable, include-only) -------------------------------------


def test_chatlist_style_include_only():
    # DialogFilterChatlist has no flags/excludes; only pinned+include resolve.
    rules = FolderRules(pinned_ids=frozenset({1001}), include_ids=frozenset({-1002003}))
    assert resolve_folder_member_ids(rules, _chats(), CONTACTS) == {1001, -1002003}
