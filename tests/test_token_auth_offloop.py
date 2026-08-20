"""Share-token verification runs its PBKDF2 scan off the event loop (9t6.4.23).

The derivations are ~50ms per stored token at 600k rounds; inline they froze
every concurrent viewer request for the whole scan on each auth attempt.
These tests pin both halves: the behavior (real engine, real hashes) and the
off-loop contract (exactly one worker-thread hop per verification).
"""

import asyncio
import hashlib
import os
import unittest.mock

import src.db.adapter as adapter_module


def _token_material(plaintext: str) -> tuple[str, str]:
    salt = os.urandom(16).hex()
    token_hash = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), bytes.fromhex(salt), 600_000).hex()
    return token_hash, salt


async def _seed_token(adapter, plaintext: str, *, revoked: int = 0) -> dict:
    token_hash, salt = _token_material(plaintext)
    created = await adapter.create_viewer_token(
        label="fixture",
        token_hash=token_hash,
        token_salt=salt,
        created_by="tests",
        allowed_chat_ids="",
    )
    if revoked:
        await adapter.update_viewer_token(created["id"], is_revoked=1)
    return created


class TestVerifyBehavior:
    async def test_roundtrip_and_use_count(self, real_adapter):
        await _seed_token(real_adapter, "token/alpha")
        await _seed_token(real_adapter, "token/beta")

        record = await real_adapter.verify_viewer_token("token/beta")
        assert record is not None and record["label"] == "fixture"
        assert record["use_count"] == 1

        again = await real_adapter.verify_viewer_token("token/beta")
        assert again["use_count"] == 2

        assert await real_adapter.verify_viewer_token("token/wrong") is None

    async def test_revoked_tokens_never_match(self, real_adapter):
        await _seed_token(real_adapter, "token/revoked", revoked=1)
        assert await real_adapter.verify_viewer_token("token/revoked") is None


class TestOffLoopContract:
    async def test_derivation_runs_in_exactly_one_worker_thread_hop(self, real_adapter):
        """Inline PBKDF2 (the regression) would never touch to_thread."""
        await _seed_token(real_adapter, "token/offloop")

        calls = []
        real_to_thread = asyncio.to_thread

        async def counting_to_thread(fn, *args, **kwargs):
            calls.append(fn.__name__)
            return await real_to_thread(fn, *args, **kwargs)

        with unittest.mock.patch.object(adapter_module.asyncio, "to_thread", counting_to_thread):
            match = await real_adapter.verify_viewer_token("token/offloop")
            miss = await real_adapter.verify_viewer_token("token/nope")

        assert match is not None
        assert miss is None
        assert calls == ["derive_match", "derive_match"]


class TestMidScanRevocation:
    async def test_a_token_revoked_during_the_scan_never_authenticates(self, real_adapter):
        """The worker-thread yield must not let a stale match through (review finding)."""
        created = await _seed_token(real_adapter, "token/race")
        real_to_thread = asyncio.to_thread

        async def revoke_then_derive(fn, *args, **kwargs):
            # A concurrent admin revokes the token while the derivation runs.
            await real_adapter.update_viewer_token(created["id"], is_revoked=1)
            return await real_to_thread(fn, *args, **kwargs)

        with unittest.mock.patch.object(adapter_module.asyncio, "to_thread", revoke_then_derive):
            assert await real_adapter.verify_viewer_token("token/race") is None
