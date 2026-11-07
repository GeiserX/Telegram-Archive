#!/usr/bin/env python3
"""
HTML Media Injector for Telegram Archive
========================================

This script is an add-on for `tg-export` (or similar HTML exports).
It solves the problem of missing media (audio/video) in generated HTML files.

By default, `tg-export` saves media files to disk but leaves text placeholders
in the HTML (e.g., "Voice message" or "Video message").
This script reads the `tg-export-state.db` SQLite database, which maps
`message_id` to the actual `file_path`, and injects `<audio>` and `<video>`
tags directly into the HTML.

Usage:
    python inject_html_media.py [path_to_export_directory]

If no path is provided, the script will try to find the directory containing
`messages_*.html` files and `.tg-export-state.db` automatically.
"""

import os
import re
import sqlite3
import sys


def find_export_paths(start_dir="."):
    """Searches for HTML files and the state database."""
    target_folder = None
    db_path = None

    for root, dirs, files in os.walk(start_dir):
        # Look for the folder containing HTML files
        if any(f.startswith("messages_") and f.endswith(".html") for f in files):
            target_folder = root
        # Look for the tg-export state database
        if ".tg-export-state.db" in files:
            db_path = os.path.join(root, ".tg-export-state.db")

    return target_folder, db_path


def build_media_map(db_path):
    """Reads the SQLite DB and builds a dictionary: {message_id: file_path}."""
    print(f"Reading database: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check the structure of the 'files' table
    cursor.execute("PRAGMA table_info(files);")
    columns = [col[1] for col in cursor.fetchall()]

    # In tg-export, the column is usually 'msg_id' and the path is 'local_path'
    # But let's check for safety
    if 'msg_id' not in columns or 'local_path' not in columns:
        print(f"Warning: Expected columns not found. Available: {columns}")
        conn.close()
        return {}

    msg_to_file = {}
    cursor.execute("SELECT msg_id, local_path FROM files")
    for row in cursor.fetchall():
        msg_id, file_path = row
        if file_path and ('voice_messages' in file_path or 'video_messages' in file_path):
            msg_to_file[str(msg_id)] = file_path

    conn.close()
    print(f"Mapped {len(msg_to_file)} media files to message IDs.")
    return msg_to_file


def process_html_files(folder_path, msg_to_file):
    """Iterates through HTML files and replaces placeholders with media tags."""
    count = 0
    v_inserted = 0
    vid_inserted = 0

    html_files = sorted([f for f in os.listdir(folder_path) if f.startswith("messages_") and f.endswith(".html")])

    for filename in html_files:
        file_path = os.path.join(folder_path, filename)

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        original_content = content

        # --- 1. Process voice messages ---
        def voice_replacer(match):
            nonlocal v_inserted
            # Look for the message ID within 2000 characters before the placeholder
            start_idx = max(0, match.start() - 2000)
            text_before = content[start_idx:match.start()]
            id_matches = list(re.finditer(r'id="message(\d+)"', text_before))

            if id_matches:
                msg_id = id_matches[-1].group(1)
                if msg_id in msg_to_file:
                    file_path = msg_to_file[msg_id]
                    if 'voice_messages' in file_path:
                        file_name = os.path.basename(file_path)
                        v_inserted += 1
                        return f'<div class="media-block"><audio controls src="voice_messages/{file_name}" style="margin-left: 15px; margin-bottom: 5px;"></audio></div>'
            return match.group(0)

        content = re.sub(r'<div class="voice">.*?</div>', voice_replacer, content, flags=re.DOTALL)

        # --- 2. Process video messages (round video notes) ---
        def video_replacer(match):
            nonlocal vid_inserted
            start_idx = max(0, match.start() - 2000)
            text_before = content[start_idx:match.start()]
            id_matches = list(re.finditer(r'id="message(\d+)"', text_before))

            if id_matches:
                msg_id = id_matches[-1].group(1)
                if msg_id in msg_to_file:
                    file_path = msg_to_file[msg_id]
                    if 'video_messages' in file_path:
                        file_name = os.path.basename(file_path)
                        vid_inserted += 1
                        return f'<div class="media-block"><video controls src="video_messages/{file_name}" style="margin-left: 15px; margin-bottom: 5px; max-width: 250px; border-radius: 50%;"></video></div>'
            return match.group(0)

        content = re.sub(r'<div class="video-note">.*?</div>', video_replacer, content, flags=re.DOTALL)

        # --- Save changes ---
        if content != original_content:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            count += 1
            print(f"Updated: {filename}")

    print(f"\nDone! Updated files: {count}")
    print(f"Inserted players: audio - {v_inserted}, video - {vid_inserted}")


def main():
    # If a path is passed as an argument, use it. Otherwise, search automatically.
    if len(sys.argv) > 1:
        start_dir = sys.argv[1]
    else:
        start_dir = "."
        print("No path provided. Searching for export directory...")

    target_folder, db_path = find_export_paths(start_dir)

    if not target_folder:
        print("ERROR: Could not find directory with messages_*.html files.")
        sys.exit(1)
    if not db_path:
        print("ERROR: Could not find .tg-export-state.db.")
        sys.exit(1)

    print(f"Working directory: {target_folder}")
    msg_to_file = build_media_map(db_path)

    if not msg_to_file:
        print("No media found in database. Exiting.")
        sys.exit(0)

    process_html_files(target_folder, msg_to_file)


if __name__ == "__main__":
    main()
