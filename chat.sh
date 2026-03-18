#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILE="$BASE_DIR/messages.txt"
IDS_FILE="$BASE_DIR/.message_ids"
ONLINE_FILE="$BASE_DIR/.online_users"
LOCK_FILE="$BASE_DIR/.chat.lock"

mkdir -p "$BASE_DIR"
touch "$FILE" "$IDS_FILE" "$ONLINE_FILE"

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
    [[ "$name" == news/* ]] && return 0

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

cmd_import_news_file() {
    local news_file="$1"
    local msg_id=""
    local name=""
    local text=""

    while IFS=$'\t' read -r msg_id name text; do
        [ -n "$msg_id" ] || continue
        [ -n "$name" ] || continue
        [ -n "$text" ] || continue
        cmd_add "$name" "$msg_id" "$text"
    done < "$news_file"
}

cmd_news() {
    local channel="$1"
    local range_spec="${2:-10}"
    local tmp_file=""

    if [ -z "$channel" ]; then
        return 1
    fi

    tmp_file="$(mktemp)"
    if ! python3 "$BASE_DIR/tg_news.py" "$channel" "$range_spec" >"$tmp_file"; then
        rm -f "$tmp_file"
        return 1
    fi

    with_lock cmd_import_news_file "$tmp_file"
    rm -f "$tmp_file"
}

cmd_touch_presence() {
    local name="$1"
    local ts
    local safe_name
    safe_name="$(sanitize_message "$name")"
    [ -n "$safe_name" ] || return 1
    ts="$(date +%s)"
    awk -F'|' -v user="$safe_name" '$1 != user {print}' "$ONLINE_FILE" > "${ONLINE_FILE}.tmp" || true
    printf '%s|%s\n' "$safe_name" "$ts" >> "${ONLINE_FILE}.tmp"
    mv "${ONLINE_FILE}.tmp" "$ONLINE_FILE"
}

cmd_list_online() {
    local window="${1:-90}"
    local now
    now="$(date +%s)"
    awk -F'|' -v now="$now" -v win="$window" '
        NF >= 2 {
            age = now - $2
            if (age >= 0 && age <= win) {
                print $1
            }
        }
    ' "$ONLINE_FILE" | awk '!seen[$0]++'
}

usage() {
    cat <<'EOF'
Usage:
  chat.sh -n NAME MSG_ID MSG
  chat.sh -r [LINES]
  chat.sh -c
  chat.sh -x MSG_ID
  chat.sh -g CHANNEL [COUNT|START-END]
  chat.sh -u NAME
  chat.sh -w [WINDOW_SECONDS]
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
    -g)
        shift
        cmd_news "${1:-}" "${2:-10}"
        ;;
    -u)
        shift
        with_lock cmd_touch_presence "${1:-}"
        ;;
    -w)
        shift
        with_lock cmd_list_online "${1:-90}"
        ;;
    *)
        usage
        exit 1
        ;;
esac
