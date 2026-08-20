"""Account isolation, proved through the adapter on real engines, both backends.

``tests/test_multiaccount_schema.py`` proves the v8.0.0 KEYS admit two accounts'
rows side by side. This file proves the ADAPTER keeps them apart: for every
method the account contract classifies REQUIRED, account 2 operating on the very
same coordinates (chat id, message id, media id, folder id) as account 1 must
coexist with — never overwrite, collide with, read, move or delete — account 1's
rows. Each test is one of the silent-loss shapes the schema change was built
against; none of them can be caught by a mocked session, so every one runs on
SQLite and PostgreSQL for real via the ``real_adapter`` fixture.

The one non-obvious member is ``detect_message_gaps``: it is raw SQL, so its
account filter can fall off without any mapper noticing, and the failure mode is
not an error but a WRONG ANSWER (the other account's ids fill the gap). The last
test here seeds the measured interleave and pins the shipped method to the
scoped result.
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, func, select

from src.db.models import (
    DEFAULT_ACCOUNT_ID,
    Account,
    Chat,
    ChatFolder,
    ChatFolderMember,
    ForumTopic,
    Media,
    Message,
    MessageVersion,
    Reaction,
    SyncStatus,
)

BASE_DATE = datetime(2026, 8, 15, 12, 0, 0)
OTHER_ACCOUNT = 2
BOTH_ACCOUNTS = (DEFAULT_ACCOUNT_ID, OTHER_ACCOUNT)
# Obviously-fake SHA-256 hex digest for content-hash dedup tests.
CONTENT_HASH = "ab" * 32


async def _seed_accounts(adapter) -> None:
    """Register both identities in the accounts table.

    No data table carries a foreign key to ``accounts`` (the surrogate has to
    exist before login), so this is hygiene rather than a constraint — but the
    tests should look like the archive they are protecting.
    """
    async with adapter.db_manager.async_session_factory() as session:
        await session.merge(Account(id=DEFAULT_ACCOUNT_ID, label="personal"))
        await session.merge(Account(id=OTHER_ACCOUNT, label="work"))
        await session.commit()


async def _seed_chat(adapter, chat_id: int, *, accounts=BOTH_ACCOUNTS, is_forum: int = 0) -> None:
    """Create each account's copy of the chat (messages/sync/topics FK to it)."""
    for account_id in accounts:
        await adapter.upsert_chat(
            {"id": chat_id, "type": "supergroup", "title": f"copy of account {account_id}", "is_forum": is_forum},
            account_id=account_id,
        )


def _message(chat_id: int, message_id: int, *, text: str | None = None, minutes: int = 0, **extra) -> dict:
    data = {
        "id": message_id,
        "chat_id": chat_id,
        "sender_id": 4242,
        "date": BASE_DATE + timedelta(minutes=minutes),
        "text": text,
        "raw_data": {},
    }
    data.update(extra)
    return data


async def _rows(adapter, stmt) -> list:
    async with adapter.db_manager.async_session_factory() as session:
        return (await session.execute(stmt)).all()


async def _scalar(adapter, stmt):
    async with adapter.db_manager.async_session_factory() as session:
        return (await session.execute(stmt)).scalar()


async def _message_row(adapter, account_id: int, chat_id: int, message_id: int) -> Message | None:
    async with adapter.db_manager.async_session_factory() as session:
        return await session.get(Message, (account_id, chat_id, message_id))


class TestMessagesAreAccountIsolated:
    async def test_same_coordinates_coexist_and_an_edit_stays_in_its_account(self, real_adapter):
        """The headline collision: PK (id, chat_id) used to drop the second copy.

        Both accounts archive message 501 of the same chat. Both rows must
        exist, and account 2 editing ITS copy (the upsert conflict branch) must
        never rewrite account 1's text.
        """
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100701)
        await real_adapter.insert_message(_message(-100701, 501, text="kept by one"), account_id=DEFAULT_ACCOUNT_ID)
        await real_adapter.insert_message(_message(-100701, 501, text="kept by two"), account_id=OTHER_ACCOUNT)

        rows = await _rows(
            real_adapter,
            select(Message.account_id, Message.text).where(Message.chat_id == -100701).order_by(Message.account_id),
        )
        assert rows == [(DEFAULT_ACCOUNT_ID, "kept by one"), (OTHER_ACCOUNT, "kept by two")]

        await real_adapter.insert_message(
            _message(-100701, 501, text="edited by two", edit_date=BASE_DATE + timedelta(hours=1)),
            account_id=OTHER_ACCOUNT,
        )
        one = await _message_row(real_adapter, DEFAULT_ACCOUNT_ID, -100701, 501)
        two = await _message_row(real_adapter, OTHER_ACCOUNT, -100701, 501)
        assert one.text == "kept by one"
        assert two.text == "edited by two"

    async def test_the_same_edit_keeps_a_version_row_in_each_account(self, real_adapter):
        """Two accounts archiving the identical edit hash to the identical value.

        ``_message_version_hash`` is a frozen contract that does NOT encode the
        account, so both version rows carry the same change_hash and only the
        (account_id, change_hash) constraint keeps the second one alive. With an
        account-blind constraint the second insert is swallowed by ON CONFLICT
        DO NOTHING and an account's edit history is silently one row short.
        """
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100702)
        for account_id in BOTH_ACCOUNTS:
            await real_adapter.insert_message(_message(-100702, 502, text="original"), account_id=account_id)
        for account_id in BOTH_ACCOUNTS:
            outcome = await real_adapter.update_message_text(
                -100702, 502, "rewritten", BASE_DATE + timedelta(hours=2), account_id=account_id
            )
            assert outcome == "applied"

        versions = await _rows(
            real_adapter,
            select(MessageVersion.account_id, MessageVersion.change_hash)
            .where(MessageVersion.chat_id == -100702)
            .order_by(MessageVersion.account_id),
        )
        assert [account_id for account_id, _ in versions] == [DEFAULT_ACCOUNT_ID, OTHER_ACCOUNT]
        assert versions[0][1] == versions[1][1]  # identical hash — the constraint carries the account

    async def test_update_message_text_cannot_reach_the_other_accounts_row(self, real_adapter):
        """Account 2 editing a message only account 1 archived is a not_found."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100703)
        await real_adapter.insert_message(_message(-100703, 503, text="only mine"), account_id=DEFAULT_ACCOUNT_ID)

        outcome = await real_adapter.update_message_text(
            -100703, 503, "hijacked", BASE_DATE + timedelta(hours=1), account_id=OTHER_ACCOUNT
        )
        assert outcome == "not_found"
        one = await _message_row(real_adapter, DEFAULT_ACCOUNT_ID, -100703, 503)
        assert one.text == "only mine"
        assert await _scalar(real_adapter, select(func.count()).select_from(MessageVersion)) == 0

    async def test_delete_message_takes_only_its_accounts_row_and_satellites(self, real_adapter):
        """delete_message removes versions, media and reactions too — all scoped.

        Both accounts hold message 504 with a version, a media row under the
        SAME media id string, and a reaction. Account 2's delete must leave
        every one of account 1's four rows standing.
        """
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100704)
        for account_id in BOTH_ACCOUNTS:
            await real_adapter.insert_message(_message(-100704, 504, text="v1"), account_id=account_id)
            await real_adapter.update_message_text(
                -100704, 504, "v2", BASE_DATE + timedelta(hours=1), account_id=account_id
            )
            await real_adapter.insert_media(
                {"id": "-100704_504_photo", "message_id": 504, "chat_id": -100704, "type": "photo"},
                account_id=account_id,
            )
            await real_adapter.reconcile_reactions(504, -100704, [{"emoji": "x", "count": 1}], account_id=account_id)

        await real_adapter.delete_message(-100704, 504, account_id=OTHER_ACCOUNT)

        for model in (Message, MessageVersion, Media, Reaction):
            survivors = await _rows(real_adapter, select(model.account_id))
            assert [a for (a,) in survivors] == [DEFAULT_ACCOUNT_ID], model.__name__

    async def test_soft_delete_marks_one_account_and_sync_data_reads_one_account(self, real_adapter):
        """mark_message_deleted under account 2 must not tombstone account 1's copy,
        and get_messages_sync_data must keep serving account 1 the still-live row."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100705)
        for account_id in BOTH_ACCOUNTS:
            await real_adapter.insert_message(_message(-100705, 505, text="alive"), account_id=account_id)

        await real_adapter.mark_message_deleted(-100705, 505, account_id=OTHER_ACCOUNT)

        one = await _message_row(real_adapter, DEFAULT_ACCOUNT_ID, -100705, 505)
        two = await _message_row(real_adapter, OTHER_ACCOUNT, -100705, 505)
        assert (one.is_deleted, two.is_deleted) == (0, 1)
        assert list(await real_adapter.get_messages_sync_data(-100705, account_id=DEFAULT_ACCOUNT_ID)) == [505]
        assert await real_adapter.get_messages_sync_data(-100705, account_id=OTHER_ACCOUNT) == {}

    async def test_message_id_lookups_never_cross_the_account(self, real_adapter):
        """get_chat_id_for_message / resolve_message_chat_id run when Telegram
        reports a deletion without naming a chat — on ONE account's session.

        Account 2 holding id 777 in two chats must not make account 1's lookup
        ambiguous, and an id only account 2 holds must stay invisible to
        account 1.
        """
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -1001000000706, accounts=(DEFAULT_ACCOUNT_ID,))
        await _seed_chat(real_adapter, -1001000000716, accounts=(OTHER_ACCOUNT,))
        await _seed_chat(real_adapter, -1001000000726, accounts=(OTHER_ACCOUNT,))
        # Common-box chats (basic groups): the peerless resolver only ever
        # searches this id space — bare deletions cannot name a -100… chat.
        await _seed_chat(real_adapter, -706, accounts=(DEFAULT_ACCOUNT_ID,))
        await _seed_chat(real_adapter, -716, accounts=(OTHER_ACCOUNT,))
        await _seed_chat(real_adapter, -726, accounts=(OTHER_ACCOUNT,))
        await real_adapter.insert_message(_message(-1001000000706, 777), account_id=DEFAULT_ACCOUNT_ID)
        await real_adapter.insert_message(_message(-1001000000716, 777), account_id=OTHER_ACCOUNT)
        await real_adapter.insert_message(_message(-1001000000726, 777), account_id=OTHER_ACCOUNT)
        await real_adapter.insert_message(_message(-1001000000716, 888), account_id=OTHER_ACCOUNT)
        await real_adapter.insert_message(_message(-706, 779), account_id=DEFAULT_ACCOUNT_ID)
        await real_adapter.insert_message(_message(-716, 779), account_id=OTHER_ACCOUNT)
        await real_adapter.insert_message(_message(-726, 779), account_id=OTHER_ACCOUNT)

        # Account isolation, in the id space the resolver actually serves:
        assert await real_adapter.resolve_message_chat_id(779, account_id=DEFAULT_ACCOUNT_ID) == -706
        assert await real_adapter.resolve_message_chat_id(779, account_id=OTHER_ACCOUNT) is None  # ambiguous for 2
        # 9t6.5.4: an id that exists ONLY in channel space resolves to nothing —
        # matching it there tombstoned a message that was never deleted.
        assert await real_adapter.resolve_message_chat_id(777, account_id=DEFAULT_ACCOUNT_ID) is None
        assert await real_adapter.get_chat_id_for_message(777, account_id=DEFAULT_ACCOUNT_ID) == -1001000000706
        assert await real_adapter.get_chat_id_for_message(888, account_id=DEFAULT_ACCOUNT_ID) is None

    async def test_sync_reads_return_only_their_accounts_ids(self, real_adapter):
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100707)
        await real_adapter.insert_messages_batch(
            [_message(-100707, 10), _message(-100707, 20)], account_id=DEFAULT_ACCOUNT_ID
        )
        await real_adapter.insert_messages_batch(
            [_message(-100707, 30), _message(-100707, 40)], account_id=OTHER_ACCOUNT
        )

        assert sorted(await real_adapter.get_messages_sync_data(-100707, account_id=DEFAULT_ACCOUNT_ID)) == [10, 20]
        cutoff = BASE_DATE - timedelta(days=1)
        assert await real_adapter.get_message_ids_since(-100707, cutoff, 10, account_id=DEFAULT_ACCOUNT_ID) == [20, 10]
        assert await real_adapter.get_message_ids_since(-100707, cutoff, 10, account_id=OTHER_ACCOUNT) == [40, 30]

    async def test_backfill_is_outgoing_flags_only_the_owning_account(self, real_adapter):
        """The same human is the owner of account 1 and a mere participant in
        account 2's copy of a shared chat — those rows are genuinely not
        outgoing there, so account 1's backfill must not touch them."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100708)
        await real_adapter.insert_message(_message(-100708, 601), account_id=DEFAULT_ACCOUNT_ID)
        await real_adapter.insert_message(_message(-100708, 601), account_id=OTHER_ACCOUNT)

        await real_adapter.backfill_is_outgoing(4242, account_id=DEFAULT_ACCOUNT_ID)

        one = await _message_row(real_adapter, DEFAULT_ACCOUNT_ID, -100708, 601)
        two = await _message_row(real_adapter, OTHER_ACCOUNT, -100708, 601)
        assert (one.is_outgoing, two.is_outgoing) == (1, 0)

    async def test_migration_markers_surface_only_their_accounts_migrations(self, real_adapter):
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100709, accounts=(DEFAULT_ACCOUNT_ID,))
        await _seed_chat(real_adapter, -100719, accounts=(OTHER_ACCOUNT,))
        await real_adapter.insert_message(
            _message(-100709, 1, raw_data={"action_type": "chat_migrate_to", "migrate_to_id": -100777}),
            account_id=DEFAULT_ACCOUNT_ID,
        )
        await real_adapter.insert_message(
            _message(-100719, 1, raw_data={"action_type": "chat_migrate_to", "migrate_to_id": -100888}),
            account_id=OTHER_ACCOUNT,
        )

        assert await real_adapter.get_migration_markers(account_id=DEFAULT_ACCOUNT_ID) == [(-100709, -100777)]
        assert await real_adapter.get_migration_markers(account_id=OTHER_ACCOUNT) == [(-100719, -100888)]


class TestChatsAreAccountIsolated:
    async def test_same_chat_id_upserts_to_two_rows_with_their_own_stable_refs(self, real_adapter):
        """Two rows for the same Telegram chat id, each with its own ref.

        The ref is minted on INSERT and must survive the other account's — and
        its own — later upserts: the viewer's share links die if it re-rolls.
        """
        await _seed_accounts(real_adapter)
        await real_adapter.upsert_chat({"id": -100711, "type": "group", "title": "mine"}, account_id=DEFAULT_ACCOUNT_ID)
        await real_adapter.upsert_chat({"id": -100711, "type": "group", "title": "theirs"}, account_id=OTHER_ACCOUNT)

        rows = await _rows(
            real_adapter,
            select(Chat.account_id, Chat.title, Chat.ref).where(Chat.id == -100711).order_by(Chat.account_id),
        )
        assert [(a, t) for a, t, _ in rows] == [(DEFAULT_ACCOUNT_ID, "mine"), (OTHER_ACCOUNT, "theirs")]
        refs = [ref for *_, ref in rows]
        assert refs[0] != refs[1] and all(ref and len(ref) == 22 for ref in refs)

        await real_adapter.upsert_chat({"id": -100711, "type": "group", "title": "renamed"}, account_id=OTHER_ACCOUNT)
        rows_after = await _rows(
            real_adapter,
            select(Chat.account_id, Chat.title, Chat.ref).where(Chat.id == -100711).order_by(Chat.account_id),
        )
        assert [(a, t) for a, t, _ in rows_after] == [(DEFAULT_ACCOUNT_ID, "mine"), (OTHER_ACCOUNT, "renamed")]
        assert [ref for *_, ref in rows_after] == refs  # update branch never touches ref

    async def test_get_chats_with_messages_lists_only_that_accounts_chats(self, real_adapter):
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100712)
        await _seed_chat(real_adapter, -100722, accounts=(OTHER_ACCOUNT,))

        assert await real_adapter.get_chats_with_messages(account_id=DEFAULT_ACCOUNT_ID) == [-100712]
        assert sorted(await real_adapter.get_chats_with_messages(account_id=OTHER_ACCOUNT)) == [-100722, -100712]

    async def test_delete_chat_and_related_data_leaves_the_other_accounts_copy_whole(self, real_adapter):
        """CLEANUP_CHATS on account 2 must not be able to empty account 1's archive.

        Every satellite the method deletes — versions, media, reactions,
        messages, sync status, forum topics, folder memberships, the chat row —
        is seeded for both accounts, then account 2's copy is deleted. Account 1
        keeps all eight. Topics and memberships must be deleted explicitly: their
        FKs cascade on paper, but SQLite runs with foreign_keys off, so relying
        on the cascade orphans them.
        """
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100713)
        for account_id in BOTH_ACCOUNTS:
            await real_adapter.insert_message(_message(-100713, 513, text="v1"), account_id=account_id)
            await real_adapter.update_message_text(
                -100713, 513, "v2", BASE_DATE + timedelta(hours=1), account_id=account_id
            )
            await real_adapter.insert_media(
                {"id": "-100713_513_photo", "message_id": 513, "chat_id": -100713, "type": "photo"},
                account_id=account_id,
            )
            await real_adapter.reconcile_reactions(513, -100713, [{"emoji": "x", "count": 2}], account_id=account_id)
            await real_adapter.update_sync_status(-100713, 513, 1, account_id=account_id)
            await real_adapter.upsert_forum_topic(
                {"id": 7, "chat_id": -100713, "title": "topic"}, account_id=account_id
            )
            await real_adapter.upsert_chat_folder({"id": 9, "title": "folder"}, account_id=account_id)
            await real_adapter.sync_folder_members(9, [-100713], account_id=account_id)

        await real_adapter.delete_chat_and_related_data(-100713, None, account_id=OTHER_ACCOUNT)

        for model in (Chat, Message, MessageVersion, Media, Reaction, SyncStatus, ForumTopic, ChatFolderMember):
            survivors = await _rows(real_adapter, select(model.account_id))
            assert [a for (a,) in survivors] == [DEFAULT_ACCOUNT_ID], model.__name__


class TestMediaAreAccountIsolated:
    async def _seed_media_pair(self, adapter, chat_id: int, message_id: int, media_id: str) -> None:
        await _seed_accounts(adapter)
        await _seed_chat(adapter, chat_id)
        for account_id in BOTH_ACCOUNTS:
            await adapter.insert_message(_message(chat_id, message_id), account_id=account_id)
            await adapter.insert_media(
                {
                    "id": media_id,
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "type": "photo",
                    "file_path": f"media/account{account_id}.jpg",
                    "content_hash": CONTENT_HASH,
                    "downloaded": True,
                },
                account_id=account_id,
            )

    async def test_same_media_id_coexists_and_path_writes_stay_scoped(self, real_adapter):
        """A media id is ``{chat}_{msg}_{type}`` — the identical string in both
        accounts. Dedup lookups and path rewrites must each stay home."""
        await self._seed_media_pair(real_adapter, -100714, 514, "-100714_514_photo")

        found_one = await real_adapter.find_media_by_content_hash(CONTENT_HASH, account_id=DEFAULT_ACCOUNT_ID)
        found_two = await real_adapter.find_media_by_content_hash(CONTENT_HASH, account_id=OTHER_ACCOUNT)
        assert found_one["file_path"] == "media/account1.jpg"
        assert found_two["file_path"] == "media/account2.jpg"

        await real_adapter.update_media_file_path("-100714_514_photo", "media/moved.jpg", account_id=OTHER_ACCOUNT)
        await real_adapter.mark_media_for_redownload("-100714_514_photo", account_id=OTHER_ACCOUNT)

        rows = await _rows(
            real_adapter, select(Media.account_id, Media.file_path, Media.downloaded).order_by(Media.account_id)
        )
        assert rows == [(DEFAULT_ACCOUNT_ID, "media/account1.jpg", 1), (OTHER_ACCOUNT, None, 0)]
        assert await real_adapter.find_media_by_content_hash(CONTENT_HASH, account_id=OTHER_ACCOUNT) is None
        assert await real_adapter.find_media_by_content_hash(CONTENT_HASH, account_id=DEFAULT_ACCOUNT_ID) is not None

    async def test_retry_bookkeeping_charges_only_the_failing_account(self, real_adapter):
        """One account's download failures must not burn the other's retry budget
        for the same media id."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100715)
        for account_id in BOTH_ACCOUNTS:
            await real_adapter.insert_message(_message(-100715, 515), account_id=account_id)
            await real_adapter.insert_media(
                {"id": "-100715_515_video", "message_id": 515, "chat_id": -100715, "type": "video"},
                account_id=account_id,
            )

        await real_adapter.increment_media_download_attempts("-100715_515_video", account_id=OTHER_ACCOUNT)
        await real_adapter.increment_media_download_attempts("-100715_515_video", account_id=OTHER_ACCOUNT)

        attempts = await _rows(
            real_adapter, select(Media.account_id, Media.download_attempts).order_by(Media.account_id)
        )
        assert attempts == [(DEFAULT_ACCOUNT_ID, 0), (OTHER_ACCOUNT, 2)]

        pending_one = await real_adapter.get_pending_media_downloads(account_id=DEFAULT_ACCOUNT_ID)
        assert [(m["id"], m["download_attempts"]) for m in pending_one] == [("-100715_515_video", 0)]
        # Account 2 hit the cap: excluded from ITS retries, counted in ITS capped
        # total — while account 1's identical id stays retryable and uncounted.
        assert await real_adapter.get_pending_media_downloads(max_attempts=2, account_id=OTHER_ACCOUNT) == []
        assert await real_adapter.count_capped_media_downloads(2, account_id=OTHER_ACCOUNT) == 1
        assert await real_adapter.count_capped_media_downloads(2, account_id=DEFAULT_ACCOUNT_ID) == 0

    async def test_chat_media_listing_and_deletion_stay_scoped(self, real_adapter):
        """get_media_for_chat feeds file deletion — surfacing the other account's
        rows would delete files its rows still point at."""
        await self._seed_media_pair(real_adapter, -100717, 517, "-100717_517_photo")

        listed = await real_adapter.get_media_for_chat(-100717, account_id=DEFAULT_ACCOUNT_ID)
        assert [m["file_path"] for m in listed] == ["media/account1.jpg"]

        assert await real_adapter.delete_media_for_chat(-100717, account_id=OTHER_ACCOUNT) == 1
        assert [m["id"] for m in await real_adapter.get_media_for_chat(-100717, account_id=DEFAULT_ACCOUNT_ID)] == [
            "-100717_517_photo"
        ]
        assert await real_adapter.get_media_for_verification(account_id=OTHER_ACCOUNT) == []
        verify_one = await real_adapter.get_media_for_verification(account_id=DEFAULT_ACCOUNT_ID)
        assert [m["file_path"] for m in verify_one] == ["media/account1.jpg"]


class TestReactionsAreAccountIsolated:
    async def test_snapshots_coexist_and_the_empty_sweep_stays_home(self, real_adapter):
        """Reaction reconciliation is authoritative-by-snapshot, which makes the
        removal half dangerous: account 2 reconciling to an EMPTY snapshot must
        tombstone only its own rows, never account 1's live reaction."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100718)
        for account_id, count in ((DEFAULT_ACCOUNT_ID, 3), (OTHER_ACCOUNT, 5)):
            await real_adapter.insert_message(_message(-100718, 518), account_id=account_id)
            outcome = await real_adapter.reconcile_reactions(
                518, -100718, [{"emoji": "thumbs", "count": count}], account_id=account_id
            )
            assert outcome == "reconciled"

        rows = await _rows(real_adapter, select(Reaction.account_id, Reaction.count).order_by(Reaction.account_id))
        assert rows == [(DEFAULT_ACCOUNT_ID, 3), (OTHER_ACCOUNT, 5)]

        assert await real_adapter.reconcile_reactions(518, -100718, [], account_id=OTHER_ACCOUNT) == "reconciled"
        state = await _rows(
            real_adapter,
            select(Reaction.account_id, Reaction.count, Reaction.removed_at.is_(None)).order_by(Reaction.account_id),
        )
        assert state == [(DEFAULT_ACCOUNT_ID, 3, True), (OTHER_ACCOUNT, 5, False)]

    async def test_reconcile_needs_its_own_accounts_message(self, real_adapter):
        """A snapshot for a message only account 1 archived is a no_message for
        account 2 — never a write against account 1's rows."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100728)
        await real_adapter.insert_message(_message(-100728, 528), account_id=DEFAULT_ACCOUNT_ID)

        outcome = await real_adapter.reconcile_reactions(
            528, -100728, [{"emoji": "thumbs", "count": 9}], account_id=OTHER_ACCOUNT
        )
        assert outcome == "no_message"
        assert await _scalar(real_adapter, select(func.count()).select_from(Reaction)) == 0


class TestSyncStatusIsAccountIsolated:
    async def test_account_twos_sweep_cannot_move_account_ones_cursor(self, real_adapter):
        """The cursor decides where the next backup resumes. Account 2 dragging
        it forward would make account 1 skip everything in between — silently.
        The counter side matters too: the upsert ACCUMULATES message_count, so a
        shared row would not overwrite the count but sum it into a number that
        never existed."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100720)
        await real_adapter.update_sync_status(-100720, 500, 50, account_id=DEFAULT_ACCOUNT_ID)
        await real_adapter.update_sync_status(-100720, 999, 5, account_id=OTHER_ACCOUNT)
        await real_adapter.update_sync_status(-100720, 1200, 5, account_id=OTHER_ACCOUNT)

        assert await real_adapter.get_last_message_id(-100720, account_id=DEFAULT_ACCOUNT_ID) == 500
        assert await real_adapter.get_last_message_id(-100720, account_id=OTHER_ACCOUNT) == 1200

        counts = await _rows(
            real_adapter, select(SyncStatus.account_id, SyncStatus.message_count).order_by(SyncStatus.account_id)
        )
        assert counts == [(DEFAULT_ACCOUNT_ID, 50), (OTHER_ACCOUNT, 10)]  # each accumulates only its own


class TestPinsAreAccountIsolated:
    async def test_the_unpin_sweep_strips_only_its_own_accounts_pins(self, real_adapter):
        """sync_pinned_messages starts by unpinning EVERYTHING in the chat — the
        half that, unscoped, wipes the other account's pins for the same id."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100724)
        for account_id in BOTH_ACCOUNTS:
            await real_adapter.insert_messages_batch(
                [_message(-100724, message_id) for message_id in (1, 2, 3)], account_id=account_id
            )
        await real_adapter.sync_pinned_messages(-100724, [2], account_id=DEFAULT_ACCOUNT_ID)
        await real_adapter.sync_pinned_messages(-100724, [3], account_id=OTHER_ACCOUNT)

        async def pinned(account_id: int) -> list[int]:
            rows = await _rows(
                real_adapter,
                select(Message.id).where(
                    and_(Message.account_id == account_id, Message.chat_id == -100724, Message.is_pinned == 1)
                ),
            )
            return sorted(i for (i,) in rows)

        assert (await pinned(DEFAULT_ACCOUNT_ID), await pinned(OTHER_ACCOUNT)) == ([2], [3])

        await real_adapter.sync_pinned_messages(-100724, [], account_id=OTHER_ACCOUNT)
        assert (await pinned(DEFAULT_ACCOUNT_ID), await pinned(OTHER_ACCOUNT)) == ([2], [])

        await real_adapter.update_message_pinned(-100724, 1, True, account_id=OTHER_ACCOUNT)
        assert (await pinned(DEFAULT_ACCOUNT_ID), await pinned(OTHER_ACCOUNT)) == ([2], [1])


class TestForumTopicsAreAccountIsolated:
    async def test_same_topic_id_keeps_two_rows_and_updates_stay_home(self, real_adapter):
        """Every forum has a topic 1 ("General"), so cross-account topic-id
        collisions are the NORM, not an edge case."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100721, is_forum=1)
        for account_id, title in ((DEFAULT_ACCOUNT_ID, "General"), (OTHER_ACCOUNT, "Generale")):
            await real_adapter.upsert_forum_topic({"id": 1, "chat_id": -100721, "title": title}, account_id=account_id)

        await real_adapter.upsert_forum_topic(
            {"id": 1, "chat_id": -100721, "title": "Renamed"}, account_id=OTHER_ACCOUNT
        )

        topics_one = await real_adapter.get_forum_topics(-100721, account_id=DEFAULT_ACCOUNT_ID)
        topics_two = await real_adapter.get_forum_topics(-100721, account_id=OTHER_ACCOUNT)
        assert [t["title"] for t in topics_one] == ["General"]
        assert [t["title"] for t in topics_two] == ["Renamed"]


class TestFoldersAreAccountIsolated:
    async def test_folder_two_of_each_account_lives_its_own_life(self, real_adapter):
        """Telegram folder ids start at 2 for EVERY account. Membership replace,
        the stale-folder sweep and the resolution read all have to stay inside
        their account or one account's Telegram state rewrites the other's."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100731)
        await _seed_chat(real_adapter, -100741, accounts=(DEFAULT_ACCOUNT_ID,))
        for account_id, title in ((DEFAULT_ACCOUNT_ID, "Work"), (OTHER_ACCOUNT, "Lavoro")):
            await real_adapter.upsert_chat_folder({"id": 2, "title": title}, account_id=account_id)
        await real_adapter.sync_folder_members(2, [-100731, -100741], account_id=DEFAULT_ACCOUNT_ID)
        await real_adapter.sync_folder_members(2, [-100731], account_id=OTHER_ACCOUNT)

        resolution_one = await real_adapter.get_chats_for_folder_resolution(account_id=DEFAULT_ACCOUNT_ID)
        resolution_two = await real_adapter.get_chats_for_folder_resolution(account_id=OTHER_ACCOUNT)
        assert sorted(c["id"] for c in resolution_one) == [-100741, -100731]
        assert [c["id"] for c in resolution_two] == [-100731]

        # Account 2's replace-all to empty: account 1's memberships survive.
        await real_adapter.sync_folder_members(2, [], account_id=OTHER_ACCOUNT)
        members = await _rows(
            real_adapter,
            select(ChatFolderMember.account_id, ChatFolderMember.chat_id).order_by(
                ChatFolderMember.account_id, ChatFolderMember.chat_id
            ),
        )
        assert members == [(DEFAULT_ACCOUNT_ID, -100741), (DEFAULT_ACCOUNT_ID, -100731)]

        # Account 2's Telegram now reports NO folders; account 1's folder and
        # title are not this sweep's to take.
        await real_adapter.cleanup_stale_folders([], account_id=OTHER_ACCOUNT)
        folders = await _rows(real_adapter, select(ChatFolder.account_id, ChatFolder.title))
        assert folders == [(DEFAULT_ACCOUNT_ID, "Work")]


class TestGapDetectionIsAccountScoped:
    async def test_the_other_accounts_interleaved_ids_cannot_hide_a_real_gap(self, real_adapter):
        """The measured case, against the SHIPPED method. Account 1 archived ids
        100 and 400 — a 300-wide hole it still has to fill. Account 2's ids
        120..360 sit inside that hole, every neighbouring pair closer than the
        threshold, so an account-blind LAG() window reads one dense sequence
        and reports NO gap: not unscoped, WRONG. The scoped query must report
        (100, 400, 300) for account 1 and nothing for account 2."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -100723)
        await real_adapter.insert_messages_batch(
            [_message(-100723, 100), _message(-100723, 400)], account_id=DEFAULT_ACCOUNT_ID
        )
        await real_adapter.insert_messages_batch(
            [_message(-100723, message_id) for message_id in range(120, 361, 40)], account_id=OTHER_ACCOUNT
        )
        per_account = await _rows(real_adapter, select(Message.account_id, func.count()).group_by(Message.account_id))
        assert dict(per_account) == {DEFAULT_ACCOUNT_ID: 2, OTHER_ACCOUNT: 7}

        assert await real_adapter.detect_message_gaps(-100723, account_id=DEFAULT_ACCOUNT_ID) == [(100, 400, 300)]
        assert await real_adapter.detect_message_gaps(-100723, account_id=OTHER_ACCOUNT) == []


class TestPeerlessDeletionScope:
    """9t6.5.4: bare deletions live in the common message box, never in -100… space."""

    async def test_collision_resolves_to_the_common_box_row(self, real_adapter):
        """A supergroup sharing the bare id must not shadow (or tombstone for) a private chat."""
        await _seed_accounts(real_adapter)
        await _seed_chat(real_adapter, -1001000000800, accounts=(DEFAULT_ACCOUNT_ID,))
        await _seed_chat(real_adapter, 800555, accounts=(DEFAULT_ACCOUNT_ID,))
        await real_adapter.insert_message(_message(-1001000000800, 4242), account_id=DEFAULT_ACCOUNT_ID)

        # Channel-space only: excluded outright.
        assert await real_adapter.resolve_message_chat_id(4242, account_id=DEFAULT_ACCOUNT_ID) is None

        # The same bare id also exists in a private chat: unambiguous there.
        await real_adapter.insert_message(_message(800555, 4242), account_id=DEFAULT_ACCOUNT_ID)
        assert await real_adapter.resolve_message_chat_id(4242, account_id=DEFAULT_ACCOUNT_ID) == 800555
