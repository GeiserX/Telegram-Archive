"""The viewer's deleted-account rule, executed under node.

A chat is a deleted account when the archive recorded the deletion
(``account_deleted_at``) or, for a row an older snapshot already erased, when
no name is left. A deleted account whose name the archive kept shows that name
with a "Deleted account" tag in the chat list, "deleted account" in the header's
status line, and a tag beside a deleted sender's name in a group.
"""

import re

from test_frontend_audit_fixes import INDEX_HTML, _extract_const_arrow_function, _run_node

HELPERS = ("getChatName", "isDeletedChat", "isKeptDeletedAccount", "isDeletedSender", "getChatTypeLabel")

PRELUDE = """
"use strict";
const assert = require('node:assert/strict');
const isGroupChatType = (type) => type === 'group' || type === 'supergroup';
"""


def _html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _script(body: str) -> str:
    html = _html()
    parts = [PRELUDE]
    parts.extend(_extract_const_arrow_function(html, name, asynchronous=False) for name in HELPERS)
    parts.append(body)
    return "\n".join(parts)


def test_recorded_deletion_marks_a_chat_that_kept_its_name():
    _run_node(
        _script(
            """
const kept = { type: 'private', first_name: 'Sample', last_name: 'Person', account_deleted_at: '2026-01-02T03:04:05' };
assert.equal(getChatName(kept), 'Sample Person');
assert.equal(isDeletedChat(kept), true);
assert.equal(isKeptDeletedAccount(kept), true);
assert.equal(getChatTypeLabel(kept), 'deleted account');

// A row an older snapshot erased still reads as deleted, with no tag: its name says it.
const erased = { type: 'private', first_name: null, last_name: null, username: null };
assert.equal(getChatName(erased), 'Deleted Account');
assert.equal(isDeletedChat(erased), true);
assert.equal(isKeptDeletedAccount(erased), false);

const live = { type: 'private', first_name: 'Sample' };
assert.equal(isDeletedChat(live), false);
assert.equal(isKeptDeletedAccount(live), false);
assert.equal(getChatTypeLabel(live), 'private chat');
assert.equal(isDeletedChat(null), false);
assert.equal(isKeptDeletedAccount(null), false);
"""
        )
    )


def test_deleted_sender_reads_the_recorded_field():
    _run_node(
        _script(
            """
assert.equal(isDeletedSender({ sender_name: 'Sample Person', sender_account_deleted_at: '2026-01-02T03:04:05' }), true);
assert.equal(isDeletedSender({ sender_name: 'Sample Person', sender_account_deleted_at: null }), false);
assert.equal(isDeletedSender({ sender_name: 'Sample Person' }), false);
assert.equal(isDeletedSender(null), false);
"""
        )
    )


def test_templates_show_the_tag_and_expose_the_helpers():
    html = _html()
    # The chat list row: the tag sits after the name, outside the truncated title.
    assert re.search(
        r'<span v-if="isKeptDeletedAccount\(chat\)" class="chat-row-sub deleted-account-tag"[^>]*>.*?Deleted account</span>',
        html,
    )
    # A group bubble: the tag follows the sender's name.
    assert re.search(
        r'<span v-if="isDeletedSender\(msg\)" class="deleted-account-tag[^"]*"[^>]*>.*?Deleted account</span>', html
    )
    # The sender details dialog says it too.
    assert 'v-if="isDeletedSender(senderInfoMessage)"' in html
    for name in ("isDeletedChat", "isKeptDeletedAccount", "isDeletedSender"):
        assert re.search(rf"^\s+{name},$", html, re.M), name
