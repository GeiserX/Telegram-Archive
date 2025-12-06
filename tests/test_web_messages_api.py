import unittest
import os
import tempfile
from unittest.mock import Mock, patch, MagicMock

from src.database import Database


class TestWebMessagesAPI(unittest.TestCase):
    def setUp(self):
        # Create a temp directory for Config to use
        self.temp_dir = tempfile.mkdtemp()
        
        # Patch environment variables before importing web.main to prevent /data creation
        env_patcher = patch.dict(os.environ, {
            'CHAT_TYPES': 'private',
            'BACKUP_PATH': self.temp_dir,
            'DATABASE_DIR': self.temp_dir,
        }, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        
        # Now import after env is patched
        from src.web.main import get_messages
        self.get_messages = get_messages
        
        # Use an in-memory database for isolation
        self.db = Database(":memory:")

        # Patch the global db used by the web module so get_messages uses our in-memory DB
        patcher = patch("src.web.main.db", self.db)
        self.addCleanup(patcher.stop)
        patcher.start()

        # Insert minimal chat / user / message / media data
        cursor = self.db.conn.cursor()

        # Chat and user
        cursor.execute(
            "INSERT INTO chats (id, type) VALUES (?, ?)",
            (1, "private"),
        )
        cursor.execute(
            "INSERT INTO users (id, username, first_name, last_name) VALUES (?, ?, ?, ?)",
            (10, "user10", "User", "Ten"),
        )

        # Message with document media
        cursor.execute(
            """
            INSERT INTO messages (
                id, chat_id, sender_id, date, text,
                media_type, media_id, media_path
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
            """,
            (
                100,
                1,
                10,
                "Test document message",
                "document",
                "1_100_document",
                "data/backups/media/1/100_original.png",
            ),
        )

        # Media row linked by media_id
        cursor.execute(
            """
            INSERT INTO media (
                id, message_id, chat_id, type,
                file_name, file_path, mime_type, downloaded
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                "1_100_document",
                100,
                1,
                "document",
                "100_original.png",
                "data/backups/media/1/100_original.png",
                "image/png",
            ),
        )

        self.db.conn.commit()

    def tearDown(self):
        self.db.close()

    def test_get_messages_includes_media_metadata(self):
        """get_messages should return media_file_name and media_mime_type for messages with media."""
        messages = self.get_messages(chat_id=1, limit=10, offset=0, search=None)
        self.assertEqual(len(messages), 1)

        msg = messages[0]
        self.assertEqual(msg["id"], 100)
        self.assertEqual(msg["media_type"], "document")
        # Extra fields added by the JOIN
        self.assertEqual(msg["media_file_name"], "100_original.png")
        self.assertEqual(msg["media_mime_type"], "image/png")


if __name__ == "__main__":
    unittest.main()







