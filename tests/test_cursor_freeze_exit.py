"""A frozen sync cursor must have a way out.

#286 stopped one unprocessable message from aborting a whole dialog: the message
is skipped, the rest of the chat is archived, and the sync cursor freezes just
behind the failure so the message stays retryable instead of being lost. That is
the right call for a TRANSIENT failure.

For a PERMANENT one it was a state with no exit. The cursor never moved, so every
later run resumed from the same ``last_message_id`` and re-fetched and re-committed
the entire tail of that chat -- a window that grows with every message the chat
receives -- while ``sync_status`` reported no progress at all. Nothing was corrupted
(the re-commits are idempotent upserts), but the cost was unbounded and permanent.

The exit is a bounded retry. The failure is counted across runs in the existing
metadata KV -- no schema change, the same place ``followed_migrations``,
``whitelist_unresolved_ids`` and ``reaction_resweep_cycle_done`` already keep
cross-run state -- and once the SAME message has failed
``MESSAGE_MAX_PROCESS_ATTEMPTS`` separate runs, the cursor is allowed past it and
its id is recorded as given up on. Gap detection cannot serve as that record:
``detect_message_gaps`` only reports holes larger than ``GAP_THRESHOLD`` (50), so a
single passed-over message is invisible to it by construction.

These tests drive real consecutive runs against a fake database that keeps its
cursor and its metadata between them, and pin both halves of the invariant: a
permanently failing message must not cause unbounded repeated work, and it must
never be passed over without a durable, operator-visible record. Plus the
properties the exit must not cost: a transient failure still gets its retry, a
clean dialog pays nothing, and a corrupt record degrades to retrying rather than
to skipping.
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.db.adapter import DatabaseAdapter
from src.telegram_backup import (
    MESSAGE_GIVE_UP_RECORD_LIMIT,
    MESSAGE_MAX_PROCESS_ATTEMPTS,
    TelegramBackup,
)

CHAT_ID = 100


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeDb:
    """Just enough database to survive between runs.

    The sync cursor and the metadata KV are real state here; everything a run
    writes is what the next run reads. That is the only way to test a bug whose
    whole shape is "run N+1 repeats run N".
    """

    def __init__(self):
        self.cursor = 0
        self.metadata: dict[str, str] = {}
        self.committed: list[int] = []
        self.sync_writes: list[int] = []
        self.metadata_reads: list[str] = []
        self.set_metadata_failures: list[Exception] = []

    async def upsert_chat(self, chat_data, *, account_id):
        return None

    async def get_last_message_id(self, chat_id, *, account_id):
        return self.cursor

    async def update_sync_status(self, chat_id, last_message_id, message_count, *, account_id):
        self.cursor = last_message_id
        self.sync_writes.append(last_message_id)

    async def get_metadata(self, key):
        self.metadata_reads.append(key)
        return self.metadata.get(key)

    async def set_metadata(self, key, value):
        if self.set_metadata_failures:
            raise self.set_metadata_failures.pop(0)
        self.metadata[key] = value

    def failure_record(self, chat_id=CHAT_ID):
        raw = self.metadata.get(TelegramBackup._message_failure_key(chat_id, 1))
        return json.loads(raw) if raw else {}


class CursorFreezeExitTestCase(unittest.TestCase):
    """Shared harness: a chat that keeps growing, and a poison message in it."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = _FakeDb()
        self.poison_ids: set[int] = set()
        self.available: list[int] = []
        self.fetched_per_run: list[int] = []
        self.batch_size = 2
        self.checkpoint_interval = 1

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_backup(self):
        """A fresh archiver, as a new run would build it, on the shared database."""
        config = MagicMock()
        config.batch_size = self.batch_size
        config.checkpoint_interval = self.checkpoint_interval
        config.skip_media_chat_ids = set()
        config.skip_media_delete_existing = False
        config.sync_deletions_edits = False
        config.reaction_resweep_days = 0
        config.should_skip_topic = MagicMock(return_value=False)
        config.media_path = os.path.join(self.temp_dir, "media")

        backup = TelegramBackup.__new__(TelegramBackup)
        backup.config = config
        backup.db = self.db
        backup.account_id = 1
        backup.client = MagicMock()
        backup._cleaned_media_chats = set()
        backup._get_marked_id = MagicMock(return_value=CHAT_ID)
        backup._extract_chat_data = MagicMock(return_value={"id": CHAT_ID})
        backup._ensure_profile_photo = AsyncMock()
        backup._sync_pinned_messages = AsyncMock()

        fetched: list[int] = []

        async def fake_iter(entity, *, min_id=0, **kwargs):
            for msg_id in sorted(self.available):
                if msg_id <= min_id:
                    continue
                fetched.append(msg_id)
                message = MagicMock()
                message.id = msg_id
                # MagicMock truthiness would make every message look like a
                # forum reply and be topic-filtered out.
                message.reply_to = None
                message.action = None
                yield message

        backup.client.iter_messages = fake_iter
        self._fetched = fetched

        async def process(message, chat_id):
            if message.id in self.poison_ids:
                raise AttributeError("'DocumentEmpty' object has no attribute 'attributes'")
            return {"id": message.id, "chat_id": chat_id}

        backup._process_message = AsyncMock(side_effect=process)

        async def commit(batch, chat_id):
            self.db.committed.extend(m["id"] for m in batch)

        backup._commit_batch = AsyncMock(side_effect=commit)
        return backup

    def _run_backup(self):
        """One archiver run over the chat as it currently stands."""
        backup = self._make_backup()
        result = _run(backup._backup_dialog(MagicMock()))
        self.fetched_per_run.append(len(self._fetched))
        return result

    def _grow_chat(self, count):
        """New messages arrive between runs, as they do in a live chat."""
        start = max(self.available, default=0) + 1
        self.available.extend(range(start, start + count))


class TestPermanentFailureStopsCostingTheWholeTail(CursorFreezeExitTestCase):
    """The unbounded-work half of the invariant."""

    def test_repeated_runs_stop_re_fetching_the_tail(self):
        """Two runs pay for the poison message; every run after it pays nothing.

        Without the exit, run 3 and every run after it re-fetch from the poison
        message to the tip of the chat, and that window grows forever.
        """
        self.available = list(range(1, 11))  # ids 1..10, poison at 5
        self.poison_ids = {5}

        self._run_backup()  # run 1: freeze behind 5
        self.assertEqual(self.db.cursor, 4)

        self._grow_chat(2)  # ids 11, 12
        self._run_backup()  # run 2: second failure on 5 -> let the cursor past it
        self.assertEqual(self.db.cursor, 12)

        self._grow_chat(2)  # ids 13, 14
        self._run_backup()
        self._grow_chat(2)  # ids 15, 16
        self._run_backup()

        # Run 1 saw the whole chat, run 2 re-scanned from the poison message,
        # and from then on each run fetches only what actually arrived.
        self.assertEqual(self.fetched_per_run, [10, 8, 2, 2])
        self.assertEqual(self.db.cursor, 16)

    def test_every_message_except_the_poison_one_is_archived(self):
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()
        self._run_backup()

        self.assertNotIn(5, self.db.committed)
        self.assertEqual(set(self.db.committed), set(range(1, 11)) - {5})

    def test_several_poison_messages_still_converge(self):
        """Each one costs its own bounded retry, not a permanent stall."""
        self.available = list(range(1, 13))
        self.poison_ids = {3, 7, 11}

        for _ in range(len(self.poison_ids) + 1):
            self._run_backup()

        self.assertEqual(self.db.cursor, 12)
        self.assertEqual(set(self.db.committed), set(range(1, 13)) - self.poison_ids)
        # And a further run has nothing left to do at all.
        self._run_backup()
        self.assertEqual(self.fetched_per_run[-1], 0)

    def test_a_give_up_after_the_last_checkpoint_is_still_persisted(self):
        """A give-up the batch checkpoints cannot carry must force its own write.

        The final checkpoint used to fire only for un-checkpointed messages, or
        for a cursor that moved purely on topic-filtered ones. Neither covers a
        run that ends on a give-up: the batches before it were already
        checkpointed, so the cursor would be left behind a message the run had
        decided to stop retrying, and the next run would start from there again
        -- give up again, write nothing again, forever.

        The state here is the one that reaches that shape: a previous run
        recorded the give-up and then died before its checkpoint landed, which is
        precisely the ordering the record is written in.
        """
        self.db.cursor = 2
        self.db.metadata[TelegramBackup._message_failure_key(CHAT_ID, 1)] = json.dumps(
            {"frozen_id": 0, "runs": 0, "given_up_total": 1, "given_up_ids": [7]}
        )
        self.available = [3, 4, 5, 6, 7]
        self.poison_ids = {7}

        self._run_backup()  # batches [3,4] and [5,6] checkpoint; then 7 is passed over

        self.assertEqual(self.db.cursor, 7)
        self.assertEqual(self.db.failure_record()["given_up_total"], 1)  # not counted twice


class TestTheSkipIsNeverSilent(CursorFreezeExitTestCase):
    """The durable-record half of the invariant."""

    def test_the_given_up_id_is_recorded_durably(self):
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()
        record_after_freeze = self.db.failure_record()
        self.assertEqual(record_after_freeze["frozen_id"], 5)
        self.assertEqual(record_after_freeze["runs"], 1)
        self.assertEqual(record_after_freeze["given_up_ids"], [])

        self._run_backup()
        record = self.db.failure_record()
        self.assertEqual(record["given_up_ids"], [5])
        self.assertEqual(record["given_up_total"], 1)
        # The freeze is released with the same write that records the give-up.
        self.assertEqual(record["frozen_id"], 0)
        self.assertEqual(record["runs"], 0)

    def test_the_record_survives_a_run_that_never_checkpointed(self):
        """A crash between the two writes must not restart the count.

        The give-up is written before the cursor checkpoint precisely so this
        ordering is the survivable one. If the process dies in between, the next
        run re-reads the id, recognises it, and passes over it immediately
        instead of freezing on it again -- which would be a loop with no exit.
        """
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()  # freeze behind 5
        cursor_before = self.db.cursor

        self._run_backup()  # gives up on 5 and checkpoints past it...
        self.assertEqual(self.db.failure_record()["given_up_ids"], [5])
        self.db.cursor = cursor_before  # ...but the process died before that landed

        self._run_backup()

        self.assertGreater(self.db.cursor, 5)
        self.assertEqual(self.db.failure_record()["given_up_total"], 1)  # counted once, not twice

    def test_the_operator_warning_carries_counts_and_no_identifiers(self):
        self.available = [1, 2, 4242, 4243]
        self.poison_ids = {4242}
        self.db.cursor = 0

        backup = self._make_backup()
        backup._get_marked_id = MagicMock(return_value=-1001234567890)
        backup._extract_chat_data = MagicMock(return_value={"id": -1001234567890})
        _run(backup._backup_dialog(MagicMock()))

        backup = self._make_backup()
        backup._get_marked_id = MagicMock(return_value=-1001234567890)
        backup._extract_chat_data = MagicMock(return_value={"id": -1001234567890})
        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            _run(backup._backup_dialog(MagicMock()))

        passed_over = [r.getMessage() for r in cm.records if "passed over" in r.getMessage()]
        self.assertEqual(len(passed_over), 1)
        self.assertIn("1 message(s)", passed_over[0])
        self.assertNotIn("4242", passed_over[0])
        self.assertNotIn("1001234567890", passed_over[0])

    def test_the_freeze_warning_is_not_claimed_for_a_message_passed_over(self):
        """The two outcomes are opposite, so they must not share a count.

        "the sync cursor stays behind them so they are retried next run" is
        false for a message the run just decided to stop retrying.
        """
        self.available = [1, 2, 3]
        self.poison_ids = {3}

        self._run_backup()

        backup = self._make_backup()
        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            _run(backup._backup_dialog(MagicMock()))

        messages = [r.getMessage() for r in cm.records]
        self.assertEqual([m for m in messages if "could not be processed" in m], [])
        self.assertEqual(len([m for m in messages if "passed over" in m]), 1)

    def test_the_recorded_ids_are_capped_but_the_total_stays_exact(self):
        """A chat that drifts wholesale must not grow one unbounded metadata row."""
        backup = self._make_backup()
        overflow = MESSAGE_GIVE_UP_RECORD_LIMIT + 100
        state = {
            "frozen_id": 0,
            "runs": 0,
            "given_up_total": overflow,
            "given_up_ids": set(range(1, overflow + 1)),
        }

        _run(backup._save_message_failures(CHAT_ID, state))

        record = self.db.failure_record()
        self.assertEqual(len(record["given_up_ids"]), MESSAGE_GIVE_UP_RECORD_LIMIT)
        self.assertEqual(record["given_up_ids"][-1], overflow)  # the newest are kept
        self.assertEqual(record["given_up_total"], overflow)


class TestTheRetryTheExitMustNotCost(CursorFreezeExitTestCase):
    """#286's guarantee has to survive the exit being added."""

    def test_the_first_failure_still_freezes_the_cursor(self):
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()

        self.assertEqual(self.db.cursor, 4)
        self.assertNotIn(5, self.db.committed)
        for written in self.db.sync_writes:
            self.assertLess(written, 5)

    def test_a_transient_failure_is_archived_on_its_retry(self):
        """The whole reason the freeze exists: one bad run must not lose a message."""
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()
        self.poison_ids = set()  # whatever it was, it cleared
        self._run_backup()

        self.assertIn(5, self.db.committed)
        self.assertEqual(self.db.cursor, 10)
        self.assertEqual(self.db.failure_record()["given_up_total"], 0)

    def test_a_failure_on_a_different_message_does_not_inherit_the_count(self):
        """The count is per message, not per chat: only a repeat earns the exit."""
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()  # freeze behind 5, runs=1
        self.poison_ids = {8}  # 5 recovers, a different message fails now

        self._run_backup()

        self.assertIn(5, self.db.committed)
        self.assertEqual(self.db.cursor, 7)  # frozen behind 8, not past it
        record = self.db.failure_record()
        self.assertEqual(record["frozen_id"], 8)
        self.assertEqual(record["runs"], 1)
        self.assertEqual(record["given_up_total"], 0)

    def test_a_clean_dialog_never_touches_the_failure_record(self):
        """Positive control on the lazy read: no failures, no cost."""
        self.available = list(range(1, 11))

        self._run_backup()

        self.assertEqual(self.db.metadata_reads, [])
        self.assertEqual(self.db.metadata, {})
        self.assertEqual(self.db.cursor, 10)

    def test_a_corrupt_record_degrades_to_retrying_not_to_skipping(self):
        """Unreadable state must never be read as 'this already failed once'."""
        self.available = list(range(1, 11))
        self.poison_ids = {5}
        self.db.metadata[TelegramBackup._message_failure_key(CHAT_ID, 1)] = "{not json"

        self._run_backup()

        self.assertEqual(self.db.cursor, 4)  # frozen, not skipped
        record = self.db.failure_record()
        self.assertEqual(record["frozen_id"], 5)
        self.assertEqual(record["runs"], 1)

    def test_a_database_that_cannot_store_the_record_still_backs_up_the_chat(self):
        """The record is a safety net, not a dependency of the capture path."""
        self.available = list(range(1, 11))
        self.poison_ids = {5}
        self.db.set_metadata = AsyncMock(side_effect=RuntimeError("metadata write failed"))
        self.db.get_metadata = AsyncMock(side_effect=RuntimeError("metadata read failed"))

        result = self._run_backup()

        self.assertEqual(result, 9)
        self.assertEqual(self.db.cursor, 4)  # falls back to the freeze, exactly as before


class TestTheExitIsBounded(CursorFreezeExitTestCase):
    """The number of attempts is a stated constant, not an accident."""

    def test_a_message_is_retried_on_more_than_one_run_before_it_is_passed_over(self):
        self.assertGreaterEqual(MESSAGE_MAX_PROCESS_ATTEMPTS, 2)

    def test_the_cursor_advances_after_exactly_that_many_failed_runs(self):
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        for attempt in range(1, MESSAGE_MAX_PROCESS_ATTEMPTS):
            self._run_backup()
            self.assertEqual(self.db.cursor, 4, f"still frozen after {attempt} failed run(s)")

        self._run_backup()
        self.assertEqual(self.db.cursor, 10)


if __name__ == "__main__":
    unittest.main()


class TestTheRecordSurvivesACrashMidDialog(CursorFreezeExitTestCase):
    """The give-up must be on disk before the cursor is allowed to move.

    A batch checkpoint later in the same run can commit a cursor past the
    message that was passed over. If the record were only written when the
    dialog finishes, a process that dies in between would leave the cursor
    beyond a message that nothing remembers skipping -- it would never be
    retried and never be reported, which is the silent skip this whole
    mechanism exists to prevent.
    """

    def _run_until_crash(self):
        """A run that dies after the poison message, before the dialog ends."""
        backup = self._make_backup()

        async def commit_then_die(batch, chat_id):
            self.db.committed.extend(m["id"] for m in batch)
            raise RuntimeError("process died mid-dialog")

        backup._commit_batch = AsyncMock(side_effect=commit_then_die)
        with self.assertRaises(RuntimeError):
            _run(backup._backup_dialog(MagicMock()))

    def test_given_up_id_is_on_disk_even_if_the_run_dies_after_it(self):
        self.available = list(range(1, 9))
        self.poison_ids = {3}

        # First run freezes on the poison message and records the attempt.
        self._run_backup()
        self.assertEqual(self.db.failure_record().get("frozen_id"), 3)

        # Second run gives up on it, then the process dies before the dialog ends.
        self._run_until_crash()

        record = self.db.failure_record()
        self.assertIn(3, record.get("given_up_ids", []), "the skip was not recorded before the crash")
        self.assertEqual(record.get("given_up_total"), 1)

    def test_a_crashed_run_does_not_leave_the_cursor_past_an_unrecorded_skip(self):
        self.available = list(range(1, 9))
        self.poison_ids = {3}

        self._run_backup()
        self._run_until_crash()

        record = self.db.failure_record()
        skipped = set(record.get("given_up_ids", []))
        # Whatever the cursor reached, every id it moved past that failed must
        # be accounted for on disk.
        self.assertTrue(
            self.db.cursor < 3 or 3 in skipped,
            f"cursor {self.db.cursor} moved past message 3 with skipped={skipped}",
        )


class TestAGiveUpIsOnlyAsGoodAsItsRecord(CursorFreezeExitTestCase):
    """The cursor must never pass a message whose give-up record is not on disk.

    The record write is the whole difference between "given up, durably
    recorded" and the silent skip this mechanism exists to prevent. So a failed
    write means the give-up did not happen: the freeze holds for one more run,
    and the next run whose write lands completes the give-up. That converges --
    at the cost of one extra run, never of an unrecorded skip.
    """

    def test_a_failed_record_write_keeps_the_freeze_instead_of_skipping(self):
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()  # run 1: freeze behind 5
        record_after_freeze = self.db.failure_record()

        self.db.set_metadata_failures = [RuntimeError("metadata write failed")]
        self._run_backup()  # run 2: gives up on 5, but the record write fails

        self.assertEqual(self.db.cursor, 4, "cursor passed a message with no record of the skip")
        self.assertNotIn(5, self.db.committed)
        self.assertEqual(self.db.failure_record(), record_after_freeze)

        self._run_backup()  # run 3: the write works again, the give-up lands

        self.assertEqual(self.db.cursor, 10)
        record = self.db.failure_record()
        self.assertEqual(record["given_up_ids"], [5])
        self.assertEqual(record["given_up_total"], 1)
        self.assertNotIn(5, self.db.committed)

    def test_a_failed_record_write_is_not_reported_as_a_recorded_skip(self):
        """The warning must describe what actually happened: a held freeze.

        "their ids are kept in the backup metadata" would be false for a
        message whose record write just failed, and "passed over" would be
        false for a cursor that stayed put.
        """
        self.available = [1, 2, 3]
        self.poison_ids = {3}

        self._run_backup()  # freeze behind 3
        self.db.set_metadata_failures = [RuntimeError("metadata write failed")]

        backup = self._make_backup()
        with self.assertLogs("src.telegram_backup", level="WARNING") as cm:
            _run(backup._backup_dialog(MagicMock()))

        messages = [r.getMessage() for r in cm.records]
        self.assertEqual([m for m in messages if "passed over" in m], [])
        self.assertEqual(len([m for m in messages if "could not be processed" in m]), 1)


class TestALockedDatabaseIsRetriedNotGivenUpOn(CursorFreezeExitTestCase):
    """'database is locked' is transient contention, not a broken database.

    The listener writes the same SQLite file concurrently, so the realistic
    failure of the record write is a lock the other process is about to
    release. The adapter's metadata methods retry it exactly as
    ``update_sync_status`` does, and the give-up completes durably.
    """

    def test_the_record_write_retries_through_transient_lock_contention(self):
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()  # run 1: freeze behind 5

        # The REAL adapter's set_metadata, over a session whose first two
        # writes find the database locked; later ones land in the shared
        # fake store, so the whole give-up flow sees what actually persisted.
        db_manager = MagicMock()
        db_manager._is_sqlite = True
        session = AsyncMock()
        lock_errors = [Exception("database is locked"), Exception("database is locked")]

        async def execute(stmt, *args, **kwargs):
            if lock_errors:
                raise lock_errors.pop(0)
            params = stmt.compile().params
            self.db.metadata[params["key"]] = params["value"]
            return MagicMock()

        session.execute = AsyncMock(side_effect=execute)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db_manager.async_session_factory.return_value = ctx
        self.db.set_metadata = DatabaseAdapter(db_manager).set_metadata

        self._run_backup()  # run 2: the give-up completes despite the lock

        self.assertEqual(lock_errors, [], "both transient lock errors should have been absorbed")
        self.assertEqual(self.db.cursor, 10)
        record = self.db.failure_record()
        self.assertEqual(record["given_up_ids"], [5])
        self.assertEqual(record["given_up_total"], 1)


class TestTheFrozenHalfDoesNotOutliveTheFreeze(CursorFreezeExitTestCase):
    """frozen_id/runs describe the message the cursor is parked behind.

    Once the cursor has moved past that id -- the message finally processed,
    or no longer exists -- a record still naming it claims a freeze that is
    over. The frozen half must be dropped; the give-up half is a permanent log
    and stays.
    """

    def test_the_frozen_half_is_cleared_after_the_message_finally_succeeds(self):
        self.available = list(range(1, 11))
        self.poison_ids = {5}

        self._run_backup()  # run 1: freeze behind 5
        record = self.db.failure_record()
        self.assertEqual((record["frozen_id"], record["runs"]), (5, 1))

        self.poison_ids = set()  # whatever it was, it cleared
        self._run_backup()  # run 2: 5 archives and the freeze is over

        self.assertIn(5, self.db.committed)
        self.assertEqual(self.db.cursor, 10)
        record = self.db.failure_record()
        self.assertEqual(record["frozen_id"], 0)
        self.assertEqual(record["runs"], 0)
        self.assertEqual(record["given_up_total"], 0)  # a recovery is not a give-up

    def test_the_give_up_half_survives_the_clearing_of_the_frozen_half(self):
        """The two halves are independent: dropping one must not touch the other."""
        self.available = list(range(1, 11))
        self.poison_ids = {3, 5}

        self._run_backup()  # freeze behind 3
        self._run_backup()  # give up on 3, freeze behind 5
        self._run_backup()  # give up on 5 too
        self.poison_ids = set()
        self._grow_chat(2)  # ids 11, 12
        self._run_backup()  # a clean run over new messages only

        record = self.db.failure_record()
        self.assertEqual(record["given_up_ids"], [3, 5])
        self.assertEqual(record["given_up_total"], 2)
        self.assertEqual(record["frozen_id"], 0)
        self.assertEqual(record["runs"], 0)
