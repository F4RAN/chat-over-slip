#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILE="$BASE_DIR/messages.txt"
IDS_FILE="$BASE_DIR/.message_ids"
LOCK_FILE="$BASE_DIR/.chat.lock"

mkdir -p "$BASE_DIR"
touch "$FILE" "$IDS_FILE"

sanitize_message() {
    printf '%s' "$1" | tr '\r\n' ' ' | sed 's/[[:space:]]\+/ /g'
}

notify_telegram() {
    local name="$1"
    local text="$2"
    local file_name=""
    local rel_path=""
    local file_path=""

    [[ "$name" == tg/* ]] && return 0

    if [ -f "$BASE_DIR/tg_notify.py" ]; then
        (
            exec 9>&-
            cd "$BASE_DIR"
            if [[ "$text" =~ ^\[file\]\ (.+)::(.+)$ ]]; then
                file_name="${BASH_REMATCH[1]}"
                rel_path="${BASH_REMATCH[2]}"
                file_path="$BASE_DIR/$rel_path"
                if [ -f "$file_path" ]; then
                    python3 tg_notify.py -m "$name: [file] $file_name" -f "$file_path" >/dev/null 2>&1 || true
                else
                    python3 tg_notify.py -m "$name: [file] $file_name" >/dev/null 2>&1 || true
                fi
            else
                python3 tg_notify.py -m "$name: $text" >/dev/null 2>&1 || true
            fi
        ) &
    fi
}

with_lock() {
    exec 9>"$LOCK_FILE"
    flock -x 9
    "$@"
}

cmd_add() {
    local name="$1"
    local msg_id="$2"
    local msg="$3"
    local safe_msg
    local ts

    safe_msg="$(sanitize_message "$msg")"
    if [ -z "$name" ] || [ -z "$msg_id" ] || [ -z "$safe_msg" ]; then
        return 1
    fi

    if ! awk -v id="$msg_id" '$0 == id {found=1} END {exit found ? 0 : 1}' "$IDS_FILE"; then
        ts="$(date '+%Y-%m-%d %H:%M:%S')"
        printf '%s|%s|%s|%s\n' "$ts" "$msg_id" "$name" "$safe_msg" >> "$FILE"
        printf '%s\n' "$msg_id" >> "$IDS_FILE"
            notify_telegram "$name" "$safe_msg"
    fi
}

cmd_read() {
    local lines="${1:-200}"
    tail -n "$lines" "$FILE"
}

cmd_clear() {
    : > "$FILE"
    : > "$IDS_FILE"
}

cmd_delete() {
    local msg_id="$1"
    awk -F'|' -v id="$msg_id" '$2 != id {print}' "$FILE" > "${FILE}.tmp" && mv "${FILE}.tmp" "$FILE"
    awk -v id="$msg_id" '$0 != id {print}' "$IDS_FILE" > "${IDS_FILE}.tmp" && mv "${IDS_FILE}.tmp" "$IDS_FILE"
}

usage() {
    cat <<'EOF'
Usage:
  chat.sh -n NAME MSG_ID MSG
  chat.sh -r [LINES]
  chat.sh -c
  chat.sh -x MSG_ID
EOF
}

case "${1:-}" in
    -n)
        shift
        with_lock cmd_add "${1:-}" "${2:-}" "${3:-}"
        ;;
    -r)
        shift
        with_lock cmd_read "${1:-200}"
        ;;
    -c)
        with_lock cmd_clear
        ;;
    -x)
        shift
        with_lock cmd_delete "${1:-}"
        ;;
    *)
        usage
        exit 1
        ;;
esac
