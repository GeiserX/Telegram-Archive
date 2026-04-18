"""Tests for the database package __init__.py module.

Covers:
- get_adapter() (lines 101-104)
- close_adapter() (lines 126-129)
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ============================================================
# get_adapter: creates adapter when None (lines 101-104)
# ============================================================


class TestGetAdapter:
    """Test get_adapter() creates or returns global adapter."""

    @pytest.mark.asyncio
    async def test_creates_adapter_when_none(self):
        """get_adapter() creates a new adapter when global is None."""
        import src.db as db_pkg

        db_pkg._adapter = None

        mock_db_manager = MagicMock()

        with (
            patch.object(db_pkg, "get_db_manager", new_callable=AsyncMock, return_value=mock_db_manager),
            patch.object(db_pkg, "DatabaseAdapter") as MockAdapter,
        ):
            mock_adapter_instance = MagicMock()
            MockAdapter.return_value = mock_adapter_instance

            result = await db_pkg.get_adapter()

        assert result is mock_adapter_instance
        MockAdapter.assert_called_once_with(mock_db_manager)

        # Cleanup
        db_pkg._adapter = None

    @pytest.mark.asyncio
    async def test_returns_existing_adapter(self):
        """get_adapter() returns existing adapter without re-creating."""
        import src.db as db_pkg

        mock_adapter = MagicMock()
        db_pkg._adapter = mock_adapter

        result = await db_pkg.get_adapter()

        assert result is mock_adapter

        # Cleanup
        db_pkg._adapter = None


# ============================================================
# close_adapter: closes and clears (lines 126-129)
# ============================================================


class TestCloseAdapter:
    """Test close_adapter() closes and clears global adapter."""

    @pytest.mark.asyncio
    async def test_closes_adapter_and_clears_global(self):
        """close_adapter() calls adapter.close() and sets global to None."""
        import src.db as db_pkg

        mock_adapter = AsyncMock()
        mock_adapter.close = AsyncMock()
        db_pkg._adapter = mock_adapter

        with patch.object(db_pkg, "close_database", new_callable=AsyncMock) as mock_close_db:
            await db_pkg.close_adapter()

        mock_adapter.close.assert_awaited_once()
        mock_close_db.assert_awaited_once()
        assert db_pkg._adapter is None

    @pytest.mark.asyncio
    async def test_close_adapter_when_none_only_closes_database(self):
        """close_adapter() only calls close_database when adapter is None."""
        import src.db as db_pkg

        db_pkg._adapter = None

        with patch.object(db_pkg, "close_database", new_callable=AsyncMock) as mock_close_db:
            await db_pkg.close_adapter()

        mock_close_db.assert_awaited_once()
        assert db_pkg._adapter is None
