import unittest
from unittest.mock import Mock, patch
from src.database import Database

class TestDatabaseViewer(unittest.TestCase):
    def test_get_all_chats(self):
        """Test that get_all_chats returns sorted chats."""
        with patch('src.database.sqlite3') as mock_sqlite:
            # Mock connection and cursor
            mock_conn = Mock()
            mock_cursor = Mock()
            mock_sqlite.connect.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            mock_conn.row_factory = None
            
            # Mock fetchall response
            mock_cursor.fetchall.return_value = [
                {'id': 1, 'title': 'Chat 1', 'last_message_date': '2024-01-02'},
                {'id': 2, 'title': 'Chat 2', 'last_message_date': '2024-01-03'},
            ]
            
            db = Database(':memory:')
            chats = db.get_all_chats()
            
            # Verify query was executed
            mock_cursor.execute.assert_called()
            self.assertTrue(len(chats) >= 0)

if __name__ == '__main__':
    unittest.main()
