#!/usr/bin/env python3
"""
Simple test for SQLAlchemy adapters without full Config initialization
"""

import os
import sys
import tempfile
from datetime import datetime

# Add src to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Mock minimal config
class MockConfig:
    def __init__(self):
        self.db_type = "sqlite-alchemy"
        self.database_path = "/tmp/test_sqlalchemy.db"
        self.database_timeout = 60.0
        self.postgres_host = "localhost"
        self.postgres_port = 5432
        self.postgres_db = "telegram_backup"
        self.postgres_user = "postgres"
        self.postgres_password = ""
        self.postgres_pool_size = 5


def test_sqlite_adapter():
    print("\n=== Testing SQLite SQLAlchemy Adapter ===")

    config = MockConfig()
    config.db_type = "sqlite-alchemy"

    # Create temporary database file
    temp_db = tempfile.mktemp(suffix='.db')
    config.database_path = temp_db
    print(f"Testing with temporary database: {temp_db}")

    try:
        from db_adapters.factory import create_database_adapter

        # Create adapter
        adapter = create_database_adapter(config)
        print("✓ SQLite adapter created successfully")

        # Initialize schema
        adapter.initialize_schema()
        print("✓ Database schema initialized")

        # Test basic operations
        chat_data = {
            "id": 123456789,
            "type": "private",
            "first_name": "Test",
            "last_name": "User",
            "username": "testuser",
            "phone": "+1234567890"
        }
        adapter.upsert_chat(chat_data)
        print("✓ Chat upserted")

        retrieved_chat = adapter.get_chat(123456789)
        assert retrieved_chat is not None
        assert retrieved_chat["first_name"] == "Test"
        print("✓ Chat retrieved successfully")

        # Test messages
        messages = [
            {
                "id": 1,
                "chat_id": 123456789,
                "sender_id": 123456789,
                "date": datetime.utcnow(),
                "text": "Hello, world!"
            }
        ]
        adapter.insert_messages(messages)
        print("✓ Messages inserted")

        retrieved_messages = adapter.get_messages(123456789)
        assert len(retrieved_messages) == 1
        print("✓ Messages retrieved successfully")

        # Test stats
        stats = adapter.get_stats()
        assert stats["chats"] == 1
        assert stats["messages"] == 1
        print("✓ Statistics retrieved")

        print("\n✅ SQLite adapter test passed!")
        return True

    except Exception as e:
        print(f"\n❌ SQLite adapter test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Clean up
        if os.path.exists(temp_db):
            os.remove(temp_db)
        if 'adapter' in locals():
            adapter.close()


def test_factory():
    print("\n=== Testing Adapter Factory ===")

    from src.db_adapters.factory import create_database_adapter, is_sqlalchemy_adapter

    assert is_sqlalchemy_adapter("sqlite-alchemy") == True
    assert is_sqlalchemy_adapter("postgres-alchemy") == True
    assert is_sqlalchemy_adapter("sqlite") == False
    print("✓ is_sqlalchemy_adapter function working")

    # Test invalid db_type
    config = MockConfig()
    config.db_type = "invalid"

    try:
        adapter = create_database_adapter(config)
        print("❌ Should have raised ValueError for invalid db_type")
        return False
    except ValueError:
        print("✓ Factory correctly raises ValueError for invalid db_type")

    # Test direct sqlite (should raise error)
    config.db_type = "sqlite"
    try:
        adapter = create_database_adapter(config)
        print("❌ Should have raised error for direct sqlite")
        return False
    except ValueError as e:
        assert "use the original src.database.Database class" in str(e)
        print("✓ Factory correctly handles direct sqlite type")

    print("\n✅ Factory test passed!")
    return True


def main():
    print("Starting simple SQLAlchemy adapter tests...")

    results = []
    results.append(test_factory())
    results.append(test_sqlite_adapter())

    print("\n" + "="*50)
    print("TEST SUMMARY")
    print("="*50)

    test_names = ["Factory", "SQLite Adapter"]
    for name, result in zip(test_names, results):
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{name:.<30} {status}")

    passed = sum(results)
    total = len(results)
    print("\nOverall: {}/{} tests passed".format(passed, total))

    if passed == total:
        print("\n🎉 All tests passed! SQLAlchemy adapters are working correctly.")
        return 0
    else:
        print("\n💥 Some tests failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())