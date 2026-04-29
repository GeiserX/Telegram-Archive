#!/usr/bin/env bash
# migrate_chat.sh — Migrate chat directories and their _shared media files
# between two mergerfs branch directories.
#
# Requires: bash 4+, rsync, GNU findutils, GNU coreutils
# License: GPL-3.0
#
# Directory layout expected:
#   BRANCH/media/<chat_id>/    -> symlinks pointing to ../_shared/<file>
#   BRANCH/media/_shared/       -> actual media files (symlink targets)

set -euo pipefail

# ─── Terminal color support (via tput, graceful fallback) ──────
if [[ -t 1 ]] && command -v tput &>/dev/null && [[ $(tput colors 2>/dev/null || echo 0) -ge 8 ]]; then
    RED=$(tput setaf 1)
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    CYAN=$(tput setaf 6)
    BOLD=$(tput bold)
    NC=$(tput sgr0)
else
    RED='' GREEN='' YELLOW='' CYAN='' BOLD='' NC=''
fi

info()    { printf '%b[INFO]%b  %s\n'   "$GREEN"  "$NC" "$*"; }
warn()    { printf '%b[WARN]%b  %s\n'   "$YELLOW" "$NC" "$*" >&2; }
error()   { printf '%b[ERROR]%b %s\n'  "$RED"    "$NC" "$*" >&2; }
header()  { printf '\n%b━━━ %s ━━━%b\n' "$CYAN" "$*" "$NC"; }
field()   { printf '  %-24s %b%s%b\n' "$1" "$2" "$3" "$NC"; }

# ─── Usage ─────────────────────────────────────────────────────
usage() {
    cat <<'EOF'
Usage: migrate_chat.sh -s SOURCE -t TARGET [OPTIONS] CHAT_ID [CHAT_ID ...]

Migrate chat directories and their referenced _shared media files
between two mergerfs branch directories.

Required:
  -s, --source DIR     Source branch (where data currently lives)
  -t, --target DIR     Target branch (where data should be moved)

Options:
  -m, --move           Move mode (delete source after verified copy)
  -c, --copy           Copy mode (default, keep source intact)
  -d, --dry-run        Show what would be done without doing anything
  --no-color           Disable colored output
  -h, --help           Show this help

Usage examples:
  # Preview what would happen
  migrate_chat.sh -s /local/branch -t /mnt/remote/branch -d <chat_id>

  # Copy: keep source files intact
  migrate_chat.sh -s /local/branch -t /mnt/remote/branch <chat_id>

  # Move: delete source after verified copy
  migrate_chat.sh -s /local/branch -t /mnt/remote/branch -m <chat_id>

  # Migrate multiple chats at once
  migrate_chat.sh -s /local/branch -t /mnt/remote/branch -m <id1> <id2> <id3>
EOF
    exit 1
}

# ─── Argument parsing ──────────────────────────────────────────
MODE="copy"
DRY_RUN=false
CHAT_IDS=()
SOURCE=""
TARGET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -s|--source) SOURCE="$2"; shift 2 ;;
        -t|--target) TARGET="$2"; shift 2 ;;
        -m|--move)   MODE="move"; shift ;;
        -c|--copy)   MODE="copy"; shift ;;
        -d|--dry-run) DRY_RUN=true; shift ;;
        --no-color)  RED='' GREEN='' YELLOW='' CYAN='' BOLD='' NC=''; shift ;;
        -h|--help)   usage ;;
        -*)          error "Unknown option: $1"; usage ;;
        *)           CHAT_IDS+=("$1"); shift ;;
    esac
done

# ─── Validation ────────────────────────────────────────────────
if [[ -z "$SOURCE" ]]; then
    error "Source branch is required (-s / --source)"
    usage
fi
if [[ -z "$TARGET" ]]; then
    error "Target branch is required (-t / --target)"
    usage
fi
if [[ ${#CHAT_IDS[@]} -eq 0 ]]; then
    error "At least one CHAT_ID is required"
    echo ""
    echo "Available chats on source branch:"
    ls "$SOURCE/media/" 2>/dev/null | grep -v "^_\|^avatars" | sort | sed 's/^/  /'
    exit 1
fi

for path in "$SOURCE" "$TARGET"; do
    if [[ ! -d "$path" ]]; then
        error "Directory does not exist: $path"
        exit 1
    fi
done

SOURCE_MEDIA="$SOURCE/media"
TARGET_MEDIA="$TARGET/media"
SOURCE_SHARED="$SOURCE_MEDIA/_shared"
TARGET_SHARED="$TARGET_MEDIA/_shared"

for d in "$SOURCE_MEDIA" "$SOURCE_SHARED"; do
    if [[ ! -d "$d" ]]; then
        error "Required directory missing: $d"
        exit 1
    fi
done

# Ensure target media directory exists
if [[ ! -d "$TARGET_MEDIA" ]] && [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$TARGET_MEDIA"
fi

# Ensure target _shared exists
if [[ ! -d "$TARGET_SHARED" ]] && [[ "$DRY_RUN" != true ]]; then
    mkdir -p "$TARGET_SHARED"
fi

# ─── Rsync flags ───────────────────────────────────────────────
# For _shared transfer — never use --remove-source-files;
# we verify and delete explicitly in move mode.
RSYNC_FLAGS="-ah --progress --info=progress2 --partial"
# For chat symlink transfer — move mode can use --remove-source-files safely.
RSYNC_CHAT_FLAGS="-ah --links"
if [[ "$MODE" == "move" ]]; then
    RSYNC_CHAT_FLAGS="$RSYNC_CHAT_FLAGS --remove-source-files"
fi
if [[ "$DRY_RUN" == true ]]; then
    RSYNC_FLAGS="$RSYNC_FLAGS --dry-run"
    RSYNC_CHAT_FLAGS="$RSYNC_CHAT_FLAGS --dry-run"
fi

# ─── Helper functions ──────────────────────────────────────────

# Collect all _shared filenames referenced by symlinks in a chat dir.
# Output: null-delimited sorted unique filenames.
collect_shared_targets() {
    local chat_dir="$1"
    # Pipeline reads null-delimited symlink targets from find(1),
    # strips the directory prefix (../_shared/) via parameter expansion,
    # and outputs null-delimited unique filenames via sort -zu.
    find "$chat_dir" -maxdepth 1 -type l -printf '%l\0' 2>/dev/null \
        | while IFS= read -r -d '' link_target; do
            printf '%s\0' "${link_target##*/}"
          done \
        | sort -zu
}

# Count symlinks in a directory.
count_symlinks() {
    local dir="$1"
    if [[ -d "$dir" ]]; then
        find "$dir" -maxdepth 1 -type l 2>/dev/null | wc -l
    else
        echo "0"
    fi
}

# ─── Main ──────────────────────────────────────────────────────

header "Migration config"
field "Mode:"      "$BOLD"   "$MODE"
field "Source:"    "$YELLOW" "$SOURCE"
field "Target:"    "$YELLOW" "$TARGET"
field "Chat IDs:"  "$GREEN"  "${CHAT_IDS[*]}"
field "Preview:"   "$YELLOW" "$( [[ "$DRY_RUN" == true ]] && echo "yes" || echo "no" )"
echo ""

# ─── Phase 1: Analyze ──────────────────────────────────────────
header "Phase 1: Analyzing symlinks"

declare -A ALL_SHARED_FILES
TOTAL_LINKS=0

for CHAT_ID in "${CHAT_IDS[@]}"; do
    SRC_CHAT_DIR="$SOURCE_MEDIA/$CHAT_ID"

    if [[ ! -d "$SRC_CHAT_DIR" ]]; then
        warn "Source chat directory not found, skipping: $SRC_CHAT_DIR"
        continue
    fi

    link_count=$(count_symlinks "$SRC_CHAT_DIR")
    TOTAL_LINKS=$((TOTAL_LINKS + link_count))
    info "Chat $CHAT_ID: $link_count symlinks"

    while IFS= read -r -d '' fname; do
        ALL_SHARED_FILES["$fname"]=1
    done < <(collect_shared_targets "$SRC_CHAT_DIR")
done

TOTAL_SHARED=${#ALL_SHARED_FILES[@]}
echo ""
info "Summary: $TOTAL_LINKS symlinks across ${#CHAT_IDS[@]} chats, $TOTAL_SHARED unique _shared files"

# Check which _shared files already exist on the target
TODO_COUNT=$TOTAL_SHARED
if [[ "$DRY_RUN" != true ]] && [[ $TOTAL_SHARED -gt 0 ]]; then
    already_exist=0
    for fname in "${!ALL_SHARED_FILES[@]}"; do
        if [[ -f "$TARGET_SHARED/$fname" ]]; then
            ALL_SHARED_FILES["$fname"]=2
            already_exist=$((already_exist + 1))
        fi
    done
    TODO_COUNT=$((TOTAL_SHARED - already_exist))
    if [[ $already_exist -gt 0 ]]; then
        info "$already_exist already on target, $TODO_COUNT need transfer"
    fi
fi

# ─── Phase 2: Transfer _shared files ───────────────────────────
if [[ ${#ALL_SHARED_FILES[@]} -gt 0 ]]; then
    header "Phase 2: Transferring _shared files ($TODO_COUNT to copy)"

    SHARED_FILE_LIST=$(mktemp)
    trap 'rm -f "$SHARED_FILE_LIST"' EXIT

    for fname in "${!ALL_SHARED_FILES[@]}"; do
        if [[ "${ALL_SHARED_FILES[$fname]}" -eq 1 ]]; then
            printf '%s\n' "$fname" >> "$SHARED_FILE_LIST"
        fi
    done

    files_to_sync=$(wc -l < "$SHARED_FILE_LIST")

    if [[ $files_to_sync -gt 0 ]]; then
        if [[ "$MODE" == "move" ]]; then
            info "Move mode: files will be deleted from source after successful transfer"
        fi
        echo ""

        rsync $RSYNC_FLAGS \
            --files-from="$SHARED_FILE_LIST" \
            "$SOURCE_SHARED/" \
            "$TARGET_SHARED/"

        rc=$?
        if [[ $rc -ne 0 ]] && [[ "$DRY_RUN" != true ]]; then
            error "rsync exited with code $rc — some files may not have transferred"
        fi

        # Move mode: verify and delete source files
        if [[ "$MODE" == "move" ]] && [[ "$DRY_RUN" != true ]]; then
            header "Verifying and cleaning up source _shared files"
            deleted=0
            failed=0
            while IFS= read -r fname; do
                src_file="$SOURCE_SHARED/$fname"
                tgt_file="$TARGET_SHARED/$fname"
                if [[ -f "$tgt_file" ]] && [[ -f "$src_file" ]]; then
                    src_size=$(stat -c%s "$src_file" 2>/dev/null || echo "0")
                    tgt_size=$(stat -c%s "$tgt_file" 2>/dev/null || echo "0")
                    if [[ "$src_size" -eq "$tgt_size" ]]; then
                        rm -f "$src_file"
                        deleted=$((deleted + 1))
                    else
                        warn "Size mismatch, keeping source: $fname (src=$src_size tgt=$tgt_size)"
                        failed=$((failed + 1))
                    fi
                fi
            done < "$SHARED_FILE_LIST"
            info "Deleted $deleted source files, $failed kept (verification failed)"
        fi

        echo ""
        info "_shared transfer complete"
    else
        info "All _shared files already exist on target, nothing to transfer"
    fi
fi

# ─── Phase 3: Transfer chat directories (symlinks) ─────────────
header "Phase 3: Transferring chat symlinks"

for CHAT_ID in "${CHAT_IDS[@]}"; do
    SRC_CHAT_DIR="$SOURCE_MEDIA/$CHAT_ID"
    TGT_CHAT_DIR="$TARGET_MEDIA/$CHAT_ID"

    if [[ ! -d "$SRC_CHAT_DIR" ]]; then
        warn "Source directory not found, skipping: $SRC_CHAT_DIR"
        continue
    fi

    link_count=$(count_symlinks "$SRC_CHAT_DIR")
    info "Chat $CHAT_ID: transferring $link_count symlinks..."

    if [[ "$DRY_RUN" != true ]]; then
        mkdir -p "$TGT_CHAT_DIR"
    fi

    rsync $RSYNC_CHAT_FLAGS \
        "$SRC_CHAT_DIR/" \
        "$TGT_CHAT_DIR/"

    rc=$?
    if [[ $rc -eq 0 ]] && [[ "$MODE" == "move" ]] && [[ "$DRY_RUN" != true ]]; then
        # Chat dir should be empty now (rsync --remove-source-files did its job)
        # but remove the directory itself if it still exists.
        if [[ -d "$SRC_CHAT_DIR" ]]; then
            rm -rf "$SRC_CHAT_DIR"
        fi
        info "Chat $CHAT_ID migrated (source removed)"
    elif [[ $rc -ne 0 ]] && [[ "$DRY_RUN" != true ]]; then
        warn "rsync exited with code $rc — chat $CHAT_ID may be incomplete"
    else
        info "Chat $CHAT_ID transfer complete"
    fi
done

# ─── Summary ───────────────────────────────────────────────────
header "Migration summary"
echo ""

src_size=$(du -sh "$SOURCE_MEDIA" 2>/dev/null | cut -f1 || echo "N/A")
field "Source media left:" "$YELLOW" "$src_size"

tgt_chat_count=$(ls "$TARGET_MEDIA/" 2>/dev/null | grep -v "^_\|^avatars" | wc -l)
tgt_shared_count=$(ls "$TARGET_SHARED/" 2>/dev/null | wc -l)
tgt_size=$(du -sh "$TARGET_MEDIA" 2>/dev/null | cut -f1 || echo "N/A")
field "Target chat count:"  "$GREEN" "$tgt_chat_count"
field "Target _shared files:" "$GREEN" "$tgt_shared_count"
field "Target media size:"   "$GREEN" "$tgt_size"

echo ""
df -h "$SOURCE" "$TARGET" 2>/dev/null | awk 'NR==1 {print "  "$0} NR>1 {printf "  %s  %s  %s  %s  %s  %s\n", $1, $2, $3, $4, $5, $6}'

echo ""
if [[ "$DRY_RUN" == true ]]; then
    info "This was a dry-run — no changes were made."
else
    info "Migration complete."
fi
