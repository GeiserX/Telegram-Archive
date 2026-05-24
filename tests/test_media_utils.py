"""Tests for shared media path utilities (media_utils.py).

Verifies the Telegram marked-ID convention logic used for legacy
folder resolution across serve_media, thumbnails, and ACL checks.
"""

import unittest

from src.web.media_utils import (
    CHANNEL_ID_OFFSET,
    derive_stale_folder,
    legacy_folder_alternates,
    legacy_marked_chat_ids,
)


class TestLegacyFolderAlternatesForward(unittest.TestCase):
    """Forward resolution: positive folder -> possible negative marked IDs."""

    def test_forward_10_digit_channel_id(self):
        """Positive 10-digit folder produces basic-group and channel negatives."""
        result = legacy_folder_alternates("1234567890")
        self.assertEqual(result, ["-1234567890", "-1001234567890"])

    def test_forward_9_digit_channel_id_pads_correctly(self):
        """9-digit folder must produce -100_0_123456789, not -100_123456789."""
        result = legacy_folder_alternates("123456789")
        # CHANNEL_ID_OFFSET is 1_000_000_000_000, so 1000000000000 + 123456789 = 1000123456789
        self.assertEqual(result, ["-123456789", "-1000123456789"])

    def test_forward_small_basic_group_id(self):
        """Small group ID produces correct channel-offset alternate."""
        result = legacy_folder_alternates("54321")
        self.assertEqual(result, ["-54321", "-1000000054321"])

    def test_forward_result_types_are_strings(self):
        """All returned alternates are strings."""
        result = legacy_folder_alternates("999")
        for item in result:
            self.assertIsInstance(item, str)

    def test_forward_always_returns_two_alternates(self):
        """Forward resolution always returns exactly two alternates."""
        result = legacy_folder_alternates("42")
        self.assertEqual(len(result), 2)

    def test_non_numeric_folder_returns_empty_list(self):
        """Non-numeric folder names return empty list (no alternates possible)."""
        self.assertEqual(legacy_folder_alternates("chat1"), [])
        self.assertEqual(legacy_folder_alternates("photos"), [])
        self.assertEqual(legacy_folder_alternates("-abc"), [])


class TestLegacyFolderAlternatesReverse(unittest.TestCase):
    """Reverse resolution: negative folder -> possible old positive folder."""

    def test_reverse_channel_folder(self):
        """Channel folder -1001234567890 resolves to positive 1234567890."""
        result = legacy_folder_alternates("-1001234567890")
        self.assertEqual(result, ["1234567890"])

    def test_reverse_basic_group_folder(self):
        """Basic group folder -54321 resolves to positive 54321."""
        result = legacy_folder_alternates("-54321")
        self.assertEqual(result, ["54321"])

    def test_reverse_boundary_smallest_channel(self):
        """Smallest channel ID -1000000000001 resolves to 1."""
        result = legacy_folder_alternates("-1000000000001")
        self.assertEqual(result, ["1"])

    def test_reverse_always_returns_one_alternate(self):
        """Reverse resolution always returns exactly one alternate."""
        result = legacy_folder_alternates("-999")
        self.assertEqual(len(result), 1)

    def test_reverse_result_types_are_strings(self):
        """Reverse alternates are strings."""
        result = legacy_folder_alternates("-1001234567890")
        for item in result:
            self.assertIsInstance(item, str)


class TestLegacyMarkedChatIds(unittest.TestCase):
    """Marked chat ID generation from positive folder IDs."""

    def test_returns_basic_group_and_channel_forms(self):
        """Returns both -id and -(offset + id) for a given positive ID."""
        result = legacy_marked_chat_ids(1234567890)
        self.assertEqual(result, [-1234567890, -1001234567890])

    def test_small_id_produces_correct_pair(self):
        """Small IDs still offset correctly."""
        result = legacy_marked_chat_ids(1)
        self.assertEqual(result, [-1, -(CHANNEL_ID_OFFSET + 1)])

    def test_returns_list_of_two_integers(self):
        """Always returns exactly two integers."""
        result = legacy_marked_chat_ids(42)
        self.assertEqual(len(result), 2)
        for item in result:
            self.assertIsInstance(item, int)

    def test_both_results_are_negative(self):
        """Both marked IDs are negative."""
        result = legacy_marked_chat_ids(99999)
        self.assertTrue(all(x < 0 for x in result))


class TestDeriveStaleFolder(unittest.TestCase):
    """Derive old positive folder name from marked chat_id."""

    def test_positive_chat_id_returns_none(self):
        """User chat_id (positive) has no stale folder."""
        self.assertIsNone(derive_stale_folder(12345))

    def test_zero_returns_none(self):
        """Zero chat_id returns None."""
        self.assertIsNone(derive_stale_folder(0))

    def test_basic_group_negative(self):
        """Basic group -54321 derives folder 54321."""
        self.assertEqual(derive_stale_folder(-54321), "54321")

    def test_channel_negative(self):
        """Channel -1001234567890 derives folder 1234567890."""
        self.assertEqual(derive_stale_folder(-1001234567890), "1234567890")

    def test_boundary_smallest_channel(self):
        """Smallest channel ID -1000000000001 derives folder 1."""
        self.assertEqual(derive_stale_folder(-1000000000001), "1")

    def test_result_is_string_when_not_none(self):
        """Non-None results are strings."""
        result = derive_stale_folder(-999)
        self.assertIsInstance(result, str)


class TestMediaUtilsConsistency(unittest.TestCase):
    """Cross-function consistency: forward and reverse are inverses."""

    def test_channel_roundtrip_derive_then_reverse(self):
        """derive_stale_folder output fed to legacy_folder_alternates reverse
        produces the original chat_id's folder as an alternate."""
        chat_id = -1001234567890
        folder = derive_stale_folder(chat_id)
        # folder is "1234567890", forward alternates include str(chat_id)
        alternates = legacy_folder_alternates(folder)
        self.assertIn(str(chat_id), alternates)

    def test_basic_group_roundtrip_derive_then_reverse(self):
        """Basic group derive -> forward alternates includes original chat_id."""
        chat_id = -54321
        folder = derive_stale_folder(chat_id)
        alternates = legacy_folder_alternates(folder)
        self.assertIn(str(chat_id), alternates)

    def test_marked_ids_match_forward_alternates(self):
        """legacy_marked_chat_ids output matches legacy_folder_alternates (as ints)."""
        positive_id = 7777
        marked = legacy_marked_chat_ids(positive_id)
        alternates = legacy_folder_alternates(str(positive_id))
        # alternates are strings of the marked IDs
        self.assertEqual(sorted(str(m) for m in marked), sorted(alternates))

    def test_channel_reverse_then_forward_contains_original(self):
        """Reverse a channel folder, then forward it, original appears."""
        channel_folder = "-1001234567890"
        reversed_folders = legacy_folder_alternates(channel_folder)
        # reversed_folders = ["1234567890"]
        forward_again = legacy_folder_alternates(reversed_folders[0])
        self.assertIn(channel_folder, forward_again)

    def test_basic_group_reverse_then_forward_contains_original(self):
        """Reverse a basic group folder, then forward it, original appears."""
        group_folder = "-54321"
        reversed_folders = legacy_folder_alternates(group_folder)
        forward_again = legacy_folder_alternates(reversed_folders[0])
        self.assertIn(group_folder, forward_again)
