"""Per-account capture-filter resolution (8.1, GitHub #313).

TG_ACCOUNT_<N>_<FILTER> wins for that account, the global variable is the
fallback; an empty indexed value inherits (compose ${VAR:-} idiom), and the
literal token ``none`` is the explicit-empty override. An override-free
account must decide exactly like the global Config methods.
"""

import os
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from src.config import Config

_BASE = {
    "TELEGRAM_API_ID": "12345",
    "TELEGRAM_API_HASH": "hash/test-value",
    "TELEGRAM_PHONE": "+10000000000",
}

_TWO_ACCOUNTS = {
    "TG_ACCOUNT_1_API_ID": "11111",
    "TG_ACCOUNT_1_API_HASH": "hash/account-one",
    "TG_ACCOUNT_1_PHONE_NUMBER": "+10000000001",
    "TG_ACCOUNT_2_API_ID": "22222",
    "TG_ACCOUNT_2_API_HASH": "hash/account-two",
    "TG_ACCOUNT_2_PHONE_NUMBER": "+10000000002",
}

# suffix -> (global env var, AccountFilters/Config attribute)
_ID_LIST_SUFFIXES = {
    "CHAT_IDS": ("CHAT_IDS", "chat_ids"),
    "INCLUDE_CHAT_IDS": ("GLOBAL_INCLUDE_CHAT_IDS", "global_include_ids"),
    "EXCLUDE_CHAT_IDS": ("GLOBAL_EXCLUDE_CHAT_IDS", "global_exclude_ids"),
    "PRIVATE_INCLUDE_CHAT_IDS": ("PRIVATE_INCLUDE_CHAT_IDS", "private_include_ids"),
    "PRIVATE_EXCLUDE_CHAT_IDS": ("PRIVATE_EXCLUDE_CHAT_IDS", "private_exclude_ids"),
    "GROUPS_INCLUDE_CHAT_IDS": ("GROUPS_INCLUDE_CHAT_IDS", "groups_include_ids"),
    "GROUPS_EXCLUDE_CHAT_IDS": ("GROUPS_EXCLUDE_CHAT_IDS", "groups_exclude_ids"),
    "CHANNELS_INCLUDE_CHAT_IDS": ("CHANNELS_INCLUDE_CHAT_IDS", "channels_include_ids"),
    "CHANNELS_EXCLUDE_CHAT_IDS": ("CHANNELS_EXCLUDE_CHAT_IDS", "channels_exclude_ids"),
    "PRIORITY_CHAT_IDS": ("PRIORITY_CHAT_IDS", "priority_chat_ids"),
    "SKIP_MEDIA_CHAT_IDS": ("SKIP_MEDIA_CHAT_IDS", "skip_media_chat_ids"),
}


def _config(**extra):
    env = {**_BASE, **extra}
    # Default BACKUP_PATH is /data/backups; keep the filesystem out of it.
    with patch("os.makedirs"), patch.dict(os.environ, env, clear=True):
        return Config()


class TestResolutionMatrix(unittest.TestCase):
    """Every id-list suffix: unset inherits, set overrides, empty inherits, none empties."""

    def test_unset_inherits_global(self):
        for suffix, (global_env, attr) in _ID_LIST_SUFFIXES.items():
            with self.subTest(suffix=suffix):
                config = _config(**{global_env: "-100111,-100222"})
                self.assertEqual(getattr(config.filters_for(1), attr), frozenset({-100111, -100222}))

    def test_set_overrides_global(self):
        for suffix, (global_env, attr) in _ID_LIST_SUFFIXES.items():
            with self.subTest(suffix=suffix):
                config = _config(**{global_env: "-100111", f"TG_ACCOUNT_1_{suffix}": "-100999"})
                self.assertEqual(getattr(config.filters_for(1), attr), frozenset({-100999}))

    def test_empty_inherits_global(self):
        """The compose ${VAR:-} idiom must never silently clear a filter."""
        for suffix, (global_env, attr) in _ID_LIST_SUFFIXES.items():
            with self.subTest(suffix=suffix):
                config = _config(**{global_env: "-100111", f"TG_ACCOUNT_1_{suffix}": ""})
                self.assertEqual(getattr(config.filters_for(1), attr), frozenset({-100111}))

    def test_none_token_is_the_explicit_empty_override(self):
        for suffix, (global_env, attr) in _ID_LIST_SUFFIXES.items():
            with self.subTest(suffix=suffix):
                config = _config(**{global_env: "-100111", f"TG_ACCOUNT_1_{suffix}": "none"})
                self.assertEqual(getattr(config.filters_for(1), attr), frozenset())

    def test_indexed_accounts_resolve_independently(self):
        config = _config(
            **_TWO_ACCOUNTS,
            CHAT_IDS="-100111,-100222",
            **{"TG_ACCOUNT_2_CHAT_IDS": "none"},
        )
        self.assertEqual(config.filters_for(1).chat_ids, frozenset({-100111, -100222}))
        self.assertEqual(config.filters_for(2).chat_ids, frozenset())
        self.assertTrue(config.filters_for(1).whitelist_mode)
        self.assertFalse(config.filters_for(2).whitelist_mode)


class TestChatTypesResolution(unittest.TestCase):
    def test_unset_inherits_global_default(self):
        config = _config()
        self.assertEqual(config.filters_for(1).chat_types, ("private", "groups", "channels"))

    def test_override_and_none(self):
        config = _config(**_TWO_ACCOUNTS, CHAT_TYPES="private", TG_ACCOUNT_2_CHAT_TYPES="groups,channels")
        self.assertEqual(config.filters_for(1).chat_types, ("private",))
        self.assertEqual(config.filters_for(2).chat_types, ("groups", "channels"))

        config = _config(TG_ACCOUNT_1_CHAT_TYPES="none")
        self.assertEqual(config.filters_for(1).chat_types, ())
        self.assertFalse(config.filters_for(1).should_backup_chat_type(True, True, True, True))

    def test_invalid_type_names_the_indexed_variable(self):
        with self.assertRaises(ValueError) as ctx:
            _config(TG_ACCOUNT_1_CHAT_TYPES="private,gropus")
        self.assertIn("TG_ACCOUNT_1_CHAT_TYPES", str(ctx.exception))
        self.assertIn("gropus", str(ctx.exception))


class TestDeclarationRules(unittest.TestCase):
    def test_typo_suffix_is_still_a_loud_startup_error(self):
        with self.assertRaises(ValueError) as ctx:
            _config(TG_ACCOUNT_1_CHATIDS="-100111")
        self.assertIn("TG_ACCOUNT_1_CHATIDS", str(ctx.exception))

    def test_filter_override_does_not_force_indexed_mode(self):
        """A legacy zero-config install may override account 1's filters alone."""
        config = _config(CHAT_IDS="-100111", TG_ACCOUNT_1_CHAT_IDS="none")
        self.assertFalse(config._indexed_accounts)
        self.assertEqual(len(config.accounts), 1)
        self.assertEqual(config.accounts[0].api_id, 12345)
        self.assertFalse(config.filters_for(1).whitelist_mode)
        # The global view is untouched — only the account's effective set changed.
        self.assertTrue(config.whitelist_mode)

    def test_override_for_a_nonexistent_account_is_an_error(self):
        with self.assertRaises(ValueError) as ctx:
            _config(TG_ACCOUNT_2_CHAT_IDS="-100111")
        self.assertIn("TG_ACCOUNT_2_", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            _config(**_TWO_ACCOUNTS, TG_ACCOUNT_3_PRIORITY_CHAT_IDS="-100111")
        self.assertIn("TG_ACCOUNT_3_", str(ctx.exception))


class TestEquivalenceWithGlobalMethods(unittest.TestCase):
    """An override-free account's decisions must be exactly the global ones."""

    _TYPE_COMBOS = [
        (is_user, is_group, is_channel, is_bot)
        for is_user in (False, True)
        for is_group in (False, True)
        for is_channel in (False, True)
        for is_bot in (False, True)
    ]

    def _assert_equivalent(self, config):
        filters = config.filters_for(1)
        candidates = [-100111, -100222, -100333, -100444, -100555, -100999, 42]
        for combo in self._TYPE_COMBOS:
            for chat_id in candidates:
                with self.subTest(combo=combo, chat_id=chat_id):
                    self.assertEqual(
                        filters.should_backup_chat(chat_id, *combo),
                        config.should_backup_chat(chat_id, *combo),
                    )

    def test_type_mode_with_every_list_populated(self):
        self._assert_equivalent(
            _config(
                CHAT_TYPES="private,groups",
                GLOBAL_INCLUDE_CHAT_IDS="-100111",
                GLOBAL_EXCLUDE_CHAT_IDS="-100222",
                PRIVATE_INCLUDE_CHAT_IDS="-100333",
                PRIVATE_EXCLUDE_CHAT_IDS="-100444",
                GROUPS_INCLUDE_CHAT_IDS="-100555",
                CHANNELS_EXCLUDE_CHAT_IDS="-100999",
            )
        )

    def test_type_mode_without_includes(self):
        self._assert_equivalent(_config(CHAT_TYPES="channels", GLOBAL_EXCLUDE_CHAT_IDS="-100222"))

    def test_whitelist_mode(self):
        self._assert_equivalent(_config(CHAT_IDS="-100111,-100333"))


class TestImmutability(unittest.TestCase):
    def test_filters_are_frozen(self):
        config = _config()
        with self.assertRaises(FrozenInstanceError):
            config.filters_for(1).chat_ids = frozenset()


class TestAccountScopedConfig(unittest.TestCase):
    """The config view capture workers hold: filters overlaid, everything else delegated."""

    def test_overlays_every_filter_attribute(self):
        config = _config(**_TWO_ACCOUNTS, CHAT_IDS="-100111", TG_ACCOUNT_2_CHAT_IDS="none")
        view1, view2 = config.for_account(1), config.for_account(2)
        self.assertEqual(view1.chat_ids, {-100111})
        self.assertTrue(view1.whitelist_mode)
        self.assertEqual(view2.chat_ids, set())
        self.assertFalse(view2.whitelist_mode)
        self.assertEqual(view1.account_index, 1)
        self.assertEqual(view2.account_index, 2)
        # Concrete types match the global attributes' types.
        self.assertIsInstance(view1.chat_ids, set)
        self.assertIsInstance(view1.chat_types, list)

    def test_delegates_everything_else_to_the_base_config(self):
        config = _config(SCHEDULE="0 */4 * * *")
        view = config.for_account(1)
        self.assertEqual(view.schedule, "0 */4 * * *")
        self.assertEqual(view.backup_path, config.backup_path)
        self.assertIs(view.accounts, config.accounts)
        # Methods that are not filter decisions resolve on the base too.
        self.assertEqual(view.get_max_media_size_bytes(), config.get_max_media_size_bytes())

    def test_decision_methods_match_the_base_for_an_override_free_account(self):
        config = _config(
            CHAT_TYPES="private,groups",
            GLOBAL_EXCLUDE_CHAT_IDS="-100222",
            GROUPS_INCLUDE_CHAT_IDS="-100555",
        )
        view = config.for_account(1)
        for chat_id in (-100222, -100555, -100999):
            for combo in TestEquivalenceWithGlobalMethods._TYPE_COMBOS:
                with self.subTest(chat_id=chat_id, combo=combo):
                    self.assertEqual(
                        view.should_backup_chat(chat_id, *combo),
                        config.should_backup_chat(chat_id, *combo),
                    )

    def test_media_gate_uses_the_accounts_skip_list(self):
        config = _config(**_TWO_ACCOUNTS, SKIP_MEDIA_CHAT_IDS="-100111", TG_ACCOUNT_2_SKIP_MEDIA_CHAT_IDS="none")
        self.assertFalse(config.for_account(1).should_download_media_for_chat(-100111))
        self.assertTrue(config.for_account(2).should_download_media_for_chat(-100111))
        # The global DOWNLOAD_MEDIA gate still applies to every account.
        config = _config(**_TWO_ACCOUNTS, DOWNLOAD_MEDIA="false", TG_ACCOUNT_2_SKIP_MEDIA_CHAT_IDS="none")
        self.assertFalse(config.for_account(2).should_download_media_for_chat(-100111))

    def test_decision_methods_use_the_accounts_own_filters(self):
        config = _config(
            **_TWO_ACCOUNTS, CHAT_IDS="-100111", TG_ACCOUNT_2_CHAT_IDS="none", TG_ACCOUNT_2_CHAT_TYPES="groups"
        )
        view2 = config.for_account(2)
        # Account 2 is type-based: a group outside any list is captured, a private chat is not.
        self.assertTrue(view2.should_backup_chat(-100999, False, True, False))
        self.assertFalse(view2.should_backup_chat(-100999, True, False, False))
        # Account 1 still whitelists.
        self.assertTrue(config.for_account(1).should_backup_chat(-100111, True, False, False))
        self.assertFalse(config.for_account(1).should_backup_chat(-100999, False, True, False))


if __name__ == "__main__":
    unittest.main()
