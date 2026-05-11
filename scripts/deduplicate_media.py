#!/usr/bin/env python3
"""
Deduplicate Existing Media Files Script

This script scans existing media files and deduplicates them using symlinks.
Files with the same name (based on Telegram's file_id) are consolidated into
a _shared directory, with symlinks created in chat directories.

This saves disk space when the same media is shared across multiple chats.

Usage:
    # Dry run (see what would be done)
    python -m scripts.deduplicate_media --dry-run

    # Actually deduplicate
    python -m scripts.deduplicate_media

    # Show verbose output
    python -m scripts.deduplicate_media --verbose
"""

import argparse
import hashlib
import logging
import os
import sys
from collections import defaultdict

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.message_utils import get_shared_file_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_file_hash(filepath: str, chunk_size: int = 8192) -> str:
    """Get SHA-256 hash of a file for content comparison and shard placement."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def deduplicate_media(dry_run: bool = False, verbose: bool = False):
    """
    Deduplicate media files using symlinks.

    Args:
        dry_run: If True, only report what would be done
        verbose: If True, show detailed output
    """
    config = Config()
    media_base_path = config.media_path

    if not os.path.exists(media_base_path):
        logger.error(f"Media path does not exist: {media_base_path}")
        return

    logger.info(f"Media path: {media_base_path}")
    logger.info(f"Dry run: {dry_run}")

    # Create shared directory
    shared_dir = os.path.join(media_base_path, "_shared")
    if not dry_run:
        os.makedirs(shared_dir, exist_ok=True)

    # Scan all chat directories and group files by name
    # Files with same name (based on telegram_file_id) are candidates for dedup
    files_by_name = defaultdict(list)

    logger.info("Scanning media directories...")

    for entry in os.scandir(media_base_path):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue

        chat_dir = entry.path
        for file_entry in os.scandir(chat_dir):
            if file_entry.is_file() and not file_entry.is_symlink():
                files_by_name[file_entry.name].append(file_entry.path)

    # Find duplicates (files with same name appearing in multiple directories)
    duplicates = {name: paths for name, paths in files_by_name.items() if len(paths) > 1}

    logger.info(f"Found {len(files_by_name)} unique file names")
    logger.info(f"Found {len(duplicates)} file names with duplicates")

    # Also include single files for future dedup (move to shared)
    all_files_to_process = files_by_name

    total_files = sum(len(paths) for paths in all_files_to_process.values())
    total_duplicates = sum(len(paths) - 1 for paths in duplicates.values())

    logger.info(f"Total files to process: {total_files}")
    logger.info(f"Total duplicate files: {total_duplicates}")

    # Calculate potential space savings
    space_saved = 0
    files_deduplicated = 0
    files_moved_to_shared = 0
    symlinks_created = 0
    errors = 0

    for filename, file_paths in all_files_to_process.items():
        # Check if file already exists in shared store
        shared_path = None
        source_existed = False

        # Check flat location first (fast path)
        flat_candidate = os.path.join(shared_dir, filename)
        if os.path.exists(flat_candidate):
            shared_path = flat_candidate
            source_existed = True
        else:
            # Compute hash of first available file to check sharded location
            source_path = file_paths[0]
            source_hash = get_file_hash(source_path)
            if source_hash:
                sharded_candidate = get_shared_file_path(shared_dir, filename, source_hash)
                if os.path.exists(sharded_candidate):
                    shared_path = sharded_candidate
                    source_existed = True
                else:
                    shared_path = sharded_candidate  # destination
            else:
                shared_path = flat_candidate  # fallback

        if source_existed:
            source_path = shared_path
        else:
            source_path = file_paths[0]

        try:
            source_size = os.path.getsize(source_path)
        except OSError:
            errors += 1
            continue

        for file_path in file_paths:
            chat_dir = os.path.dirname(file_path)

            # Skip if this is already a symlink
            if os.path.islink(file_path):
                continue

            # Skip if this is the source file and we haven't moved it yet
            if file_path == source_path and not source_existed:
                # Move source to shared (sharded path)
                if not dry_run:
                    try:
                        os.makedirs(os.path.dirname(shared_path), exist_ok=True)
                        os.rename(source_path, shared_path)
                        # Create symlink in original location
                        rel_path = os.path.relpath(shared_path, chat_dir)
                        os.symlink(rel_path, file_path)
                        files_moved_to_shared += 1
                        symlinks_created += 1
                    except OSError as e:
                        logger.error(f"Error moving {source_path}: {e}")
                        errors += 1
                else:
                    files_moved_to_shared += 1
                    symlinks_created += 1
                continue

            # This is a duplicate - remove and create symlink
            if verbose:
                logger.info(f"Deduplicating: {file_path} -> {shared_path}")

            if not dry_run:
                try:
                    # Verify content matches before deleting
                    if os.path.exists(shared_path):
                        source_hash = get_file_hash(shared_path)
                        dup_hash = get_file_hash(file_path)

                        if source_hash != dup_hash:
                            logger.warning(f"Hash mismatch for {filename}, skipping")
                            continue

                    # Remove duplicate
                    os.remove(file_path)

                    # Create symlink
                    rel_path = os.path.relpath(shared_path, chat_dir)
                    os.symlink(rel_path, file_path)

                    files_deduplicated += 1
                    symlinks_created += 1
                    space_saved += source_size

                except OSError as e:
                    logger.error(f"Error deduplicating {file_path}: {e}")
                    errors += 1
            else:
                files_deduplicated += 1
                symlinks_created += 1
                space_saved += source_size

    # Summary
    logger.info("=" * 60)
    logger.info("SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Files moved to _shared: {files_moved_to_shared}")
    logger.info(f"Duplicate files removed: {files_deduplicated}")
    logger.info(f"Symlinks created: {symlinks_created}")
    logger.info(f"Space saved: {space_saved / (1024 * 1024 * 1024):.2f} GB")
    logger.info(f"Errors: {errors}")

    if dry_run:
        logger.info("\n*** DRY RUN - No changes were made ***")
        logger.info("Run without --dry-run to apply changes")


def main():
    parser = argparse.ArgumentParser(description="Deduplicate media files using symlinks")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")

    args = parser.parse_args()

    deduplicate_media(dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
