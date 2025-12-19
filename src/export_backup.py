"""
Export backup data for recovery purposes.
Allows exporting messages from database to JSON format with date filtering.

v3.0: Async database operations with SQLAlchemy.
"""

import asyncio
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Config, setup_logging
from .db import DatabaseAdapter, init_database, close_database

logger = logging.getLogger(__name__)


class BackupExporter:
    """Export backup data for recovery."""
    
    def __init__(self, db: DatabaseAdapter):
        """
        Initialize exporter.
        
        Args:
            db: Async database adapter
        """
        self.db = db
    
    @classmethod
    async def create(cls, config: Config) -> "BackupExporter":
        """
        Factory method to create BackupExporter with initialized database.
        
        Args:
            config: Configuration object
            
        Returns:
            Initialized BackupExporter instance
        """
        await init_database()
        from .db import get_adapter
        db = await get_adapter()
        return cls(db)
    
    async def export_to_json(
        self,
        output_file: str,
        chat_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ):
        """
        Export messages to JSON file.
        
        Args:
            output_file: Path to output JSON file
            chat_id: Optional chat ID to filter by
            start_date: Optional start date (YYYY-MM-DD)
            end_date: Optional end date (YYYY-MM-DD)
        """
        logger.info("Starting export...")
        
        # Parse dates
        start_dt = datetime.strptime(start_date, '%Y-%m-%d') if start_date else None
        end_dt = datetime.strptime(end_date, '%Y-%m-%d') if end_date else None
        
        # Get messages
        messages = await self.db.get_messages_by_date_range(chat_id, start_dt, end_dt)
        
        # Get chats
        chats = await self.db.get_all_chats()
        chats_dict = {chat['id']: chat for chat in chats}
        
        # Build export data
        export_data = {
            'export_date': datetime.now().isoformat(),
            'filters': {
                'chat_id': chat_id,
                'start_date': start_date,
                'end_date': end_date
            },
            'statistics': {
                'total_messages': len(messages),
                'total_chats': len(chats_dict)
            },
            'chats': chats,
            'messages': messages
        }
        
        # Write to file
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"Exported {len(messages)} messages to {output_file}")
        logger.info(f"File size: {output_path.stat().st_size / 1024:.2f} KB")
    
    async def list_chats(self):
        """List all backed up chats."""
        chats = await self.db.get_all_chats()
        
        print("\n" + "=" * 80)
        print("Backed Up Chats")
        print("=" * 80)
        print(f"{'ID':<15} {'Type':<10} {'Name':<40} {'Last Updated':<20}")
        print("-" * 80)
        
        for chat in chats:
            chat_id = chat['id']
            chat_type = chat['type']
            name = chat.get('title') or f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
            updated_at = chat.get('updated_at')
            if updated_at:
                if hasattr(updated_at, 'isoformat'):
                    updated = updated_at.isoformat()[:19]
                else:
                    updated = str(updated_at)[:19]
            else:
                updated = 'N/A'
            
            print(f"{chat_id:<15} {chat_type:<10} {name:<40} {updated:<20}")
        
        print("=" * 80)
        print(f"Total: {len(chats)} chats\n")
    
    async def show_statistics(self):
        """Show backup statistics."""
        stats = await self.db.get_statistics()
        
        print("\n" + "=" * 60)
        print("Backup Statistics")
        print("=" * 60)
        print(f"Total chats:        {stats['chats']}")
        print(f"Total messages:     {stats['messages']}")
        print(f"Media files:        {stats['media_files']}")
        print(f"Total storage:      {stats['total_size_mb']} MB")
        print("=" * 60 + "\n")
    
    async def close(self):
        """Close database connection."""
        await close_database()


async def async_main():
    """Async main entry point."""
    parser = argparse.ArgumentParser(
        description='Export Telegram backup data for recovery'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Export command
    export_parser = subparsers.add_parser('export', help='Export messages to JSON')
    export_parser.add_argument(
        '-o', '--output',
        required=True,
        help='Output JSON file path'
    )
    export_parser.add_argument(
        '-c', '--chat-id',
        type=int,
        help='Filter by chat ID'
    )
    export_parser.add_argument(
        '-s', '--start-date',
        help='Start date (YYYY-MM-DD)'
    )
    export_parser.add_argument(
        '-e', '--end-date',
        help='End date (YYYY-MM-DD)'
    )
    
    # List chats command
    subparsers.add_parser('list-chats', help='List all backed up chats')
    
    # Statistics command
    subparsers.add_parser('stats', help='Show backup statistics')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 0
    
    try:
        # Load configuration
        config = Config()
        setup_logging(config)
        
        exporter = await BackupExporter.create(config)
        
        try:
            if args.command == 'export':
                await exporter.export_to_json(
                    args.output,
                    args.chat_id,
                    args.start_date,
                    args.end_date
                )
            elif args.command == 'list-chats':
                await exporter.list_chats()
            elif args.command == 'stats':
                await exporter.show_statistics()
        finally:
            await exporter.close()
        
    except Exception as e:
        logger.error(f"Export failed: {e}", exc_info=True)
        return 1
    
    return 0


def main():
    """Main entry point."""
    return asyncio.run(async_main())


if __name__ == '__main__':
    exit(main())
