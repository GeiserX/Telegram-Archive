#!/usr/bin/env python3
"""
Cleanup legacy avatar files.

After v5.3.7, avatars are saved as {chat_id}_{photo_id}.jpg instead of {chat_id}.jpg.
This script removes legacy {chat_id}.jpg files when a new format file exists.

Usage:
    python scripts/cleanup_legacy_avatars.py [--dry-run] [--backup-path /path/to/backups]

Options:
    --dry-run       Show what would be deleted without actually deleting
    --backup-path   Path to backup directory (default: /data/backups)
"""

import argparse
import glob
import os
import re
import sys


def find_legacy_avatars(media_path: str) -> list:
    """Find all legacy avatar files that have a new-format replacement."""
    legacy_files_to_delete = []
    
    for avatar_type in ['users', 'chats']:
        avatar_dir = os.path.join(media_path, 'avatars', avatar_type)
        
        if not os.path.exists(avatar_dir):
            continue
        
        # Find all legacy files (just chat_id.jpg, no underscore before .jpg)
        all_files = os.listdir(avatar_dir)
        
        for filename in all_files:
            if not filename.endswith('.jpg'):
                continue
            
            # Check if this is a legacy file (no underscore suffix)
            # Legacy format: {chat_id}.jpg (e.g., "123456.jpg" or "-1001234567.jpg")
            # New format: {chat_id}_{photo_id}.jpg (e.g., "123456_789.jpg")
            
            # Match legacy pattern: optional minus, digits only, then .jpg
            if re.match(r'^-?\d+\.jpg$', filename):
                chat_id = filename[:-4]  # Remove .jpg
                
                # Check if there's a new-format file for this chat_id
                new_format_pattern = os.path.join(avatar_dir, f'{chat_id}_*.jpg')
                new_format_files = glob.glob(new_format_pattern)
                
                if new_format_files:
                    # New format exists, legacy can be deleted
                    legacy_path = os.path.join(avatar_dir, filename)
                    legacy_files_to_delete.append({
                        'legacy': legacy_path,
                        'replacement': new_format_files[0],
                        'type': avatar_type
                    })
    
    return legacy_files_to_delete


def main():
    parser = argparse.ArgumentParser(
        description='Cleanup legacy avatar files after v5.3.7 migration'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be deleted without actually deleting'
    )
    parser.add_argument(
        '--backup-path',
        default='/data/backups',
        help='Path to backup directory (default: /data/backups)'
    )
    
    args = parser.parse_args()
    
    media_path = os.path.join(args.backup_path, 'media')
    
    if not os.path.exists(media_path):
        print(f"Error: Media path not found: {media_path}")
        sys.exit(1)
    
    print(f"Scanning for legacy avatars in: {media_path}")
    print()
    
    legacy_files = find_legacy_avatars(media_path)
    
    if not legacy_files:
        print("No legacy avatar files found that can be cleaned up.")
        print("(Legacy files without a new-format replacement are kept)")
        return
    
    print(f"Found {len(legacy_files)} legacy avatar file(s) with new-format replacements:")
    print()
    
    for item in legacy_files:
        print(f"  [{item['type']}] {os.path.basename(item['legacy'])}")
        print(f"      → Replaced by: {os.path.basename(item['replacement'])}")
    
    print()
    
    if args.dry_run:
        print("DRY RUN - No files were deleted.")
        print("Run without --dry-run to actually delete these files.")
    else:
        deleted = 0
        for item in legacy_files:
            try:
                os.remove(item['legacy'])
                deleted += 1
            except Exception as e:
                print(f"  Error deleting {item['legacy']}: {e}")
        
        print(f"Deleted {deleted} legacy avatar file(s).")


if __name__ == '__main__':
    main()
