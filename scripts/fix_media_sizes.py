#!/usr/bin/env python3
"""
Script to fix zero file sizes in the database by checking actual file sizes on disk.
Usage: python scripts/fix_media_sizes.py
"""
import os
import sys
import logging

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import Database
from src.config import Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def fix_media_sizes():
    config = Config()
    db = Database(config.database_path)
    
    logger.info("Starting media size fix...")
    
    # Get all media with 0 size
    cursor = db.conn.cursor()
    cursor.execute("SELECT id, file_path FROM media WHERE file_size = 0 OR file_size IS NULL")
    media_entries = cursor.fetchall()
    
    logger.info(f"Found {len(media_entries)} media entries with invalid size")
    
    updated_count = 0
    missing_files = 0
    
    for i, entry in enumerate(media_entries):
        media_id = entry['id']
        file_path = entry['file_path']
        
        if not file_path:
            continue
            
        # Check if absolute or relative
        if not os.path.isabs(file_path):
            # Assume relative to data directory or project root?
            # The app stores absolute paths usually? Or relative to config.media_path?
            # Let's check if it exists as is
            if not os.path.exists(file_path):
                # Try combining with media_path
                full_path = os.path.join(config.media_path, file_path)
                if os.path.exists(full_path):
                    file_path = full_path
        
        if os.path.exists(file_path):
            try:
                size = os.path.getsize(file_path)
                if size > 0:
                    cursor.execute("UPDATE media SET file_size = ? WHERE id = ?", (size, media_id))
                    updated_count += 1
            except Exception as e:
                logger.warning(f"Error reading size for {file_path}: {e}")
        else:
            missing_files += 1
            # logger.debug(f"File not found: {file_path}")
            
        if i % 1000 == 0 and i > 0:
            logger.info(f"Processed {i}/{len(media_entries)}...")
            db.conn.commit()
            
    db.conn.commit()
    logger.info("=" * 40)
    logger.info(f"Fix completed!")
    logger.info(f"Updated: {updated_count}")
    logger.info(f"Missing files: {missing_files}")
    logger.info("=" * 40)
    
    # Show new stats
    stats = db.get_statistics()
    logger.info(f"New Total Storage: {stats['total_size_mb']} MB")
    
    db.close()

if __name__ == "__main__":
    fix_media_sizes()
