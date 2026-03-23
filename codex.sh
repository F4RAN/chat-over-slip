#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_DIR="${CODEX_WORK_DIR:-$HOME/codex_dir}"
CODEX_STATE_DIR="$BASE_DIR/.codex_state"
LOCK_FILE="$CODEX_STATE_DIR/.codex.lock"

mkdir -p "$CODEX_STATE_DIR" "$CODEX_DIR"

# --- helpers ---

with_lock() {
    exec 8>"$LOCK_FILE"
    flock -x 8
    "$@"
}

notify_telegram() {
    local text="$1"
    local notify_user="${CODEX_TELEGRAM_NOTIFY_USER:-@Meton_exir}"
    local full_msg="$notify_user Arian take care this please.

$text"
    if [ -f "$BASE_DIR/tg_notify.py" ]; then
        (
            exec 8>&-
            cd "$BASE_DIR"
            python3 tg_notify.py -m "$full_msg" >/dev/null 2>&1 || true
        ) &
    fi
}

# --- codex login check ---
# Returns: "logged_in" or the full auth prompt text
cmd_check_login() {
    # Try running codex with a quick command to see if logged in
    # If codex returns auth prompt, capture it
    local tmp_out
    tmp_out="$(mktemp)"
    local tmp_err
    tmp_err="$(mktemp)"

    # Use timeout to avoid blocking forever; codex will print auth prompt and wait
    timeout 10 bash -c "cd '$CODEX_DIR' && codex exec 'echo __codex_auth_test__' </dev/null" \
        >"$tmp_out" 2>"$tmp_err" &
    local pid=$!

    # Wait a bit and check output for auth prompt
    sleep 3

    local combined
    combined="$(cat "$tmp_out" "$tmp_err" 2>/dev/null || true)"

    if echo "$combined" | grep -q "__codex_auth_test__"; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        rm -f "$tmp_out" "$tmp_err"
        echo "CODEX_LOGGED_IN"
        return 0
    fi

    if echo "$combined" | grep -qi "sign in\|device\|auth\|browser\|one-time code"; then
        # Extract auth message and notify via Telegram
        local auth_msg
        auth_msg="$(cat "$tmp_out" "$tmp_err" 2>/dev/null || true)"
        notify_telegram "$auth_msg"
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        rm -f "$tmp_out" "$tmp_err"
        echo "CODEX_AUTH_REQUIRED"
        echo "$auth_msg"
        return 0
    fi

    # Still running or unclear
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    rm -f "$tmp_out" "$tmp_err"
    echo "CODEX_UNKNOWN"
    echo "$combined"
}

# --- session management ---

cmd_list_sessions() {
    # List recent sessions with titles
    find ~/.codex/sessions -type f -name '*.jsonl' 2>/dev/null | tail -n 20 | while read -r f; do
        local id
        id=$(basename "$f" .jsonl | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || echo "")
        [ -z "$id" ] && continue
        local title
        title=$(jq -r '
            select(.payload.type=="message" and .payload.role=="user")
            | .payload.content[]
            | select(.type=="input_text")
            | .text
        ' "$f" 2>/dev/null | grep -v '^<environment_context>$' | grep -v '^</environment_context>$' | grep -v '^  <' | head -n 1 || echo "")
        printf '%s|%s\n' "$id" "$title"
    done
}

# --- prompt execution ---

cmd_send_prompt() {
    local prompt="$1"
    local session_id="${2:-}"

    local state_file="$CODEX_STATE_DIR/current_prompt.state"
    local output_file="$CODEX_STATE_DIR/current_output.txt"

    # Write state
    echo "ANSWERING" > "$state_file"
    : > "$output_file"

    (
        exec 8>&-
        cd "$CODEX_DIR"

        if [ -n "$session_id" ]; then
            # Resume existing session
            codex resume "$session_id" "$prompt" > "$output_file" 2>&1 || true
        else
            codex exec "$prompt" > "$output_file" 2>&1 || true
        fi

        # Extract session_id from output if new session
        if [ -z "$session_id" ]; then
            # Try to detect session_id from codex sessions
            local latest_session
            latest_session=$(find ~/.codex/sessions -type f -name '*.jsonl' -newer "$state_file" 2>/dev/null | tail -1 || echo "")
            if [ -n "$latest_session" ]; then
                local new_id
                new_id=$(basename "$latest_session" .jsonl | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || echo "")
                if [ -n "$new_id" ]; then
                    echo "__SESSION_ID__:$new_id" >> "$output_file"
                fi
            fi
        fi

        echo "DONE" > "$state_file"
    ) &

    echo "PROMPT_SUBMITTED"
}

# --- check prompt status ---

cmd_check_status() {
    local state_file="$CODEX_STATE_DIR/current_prompt.state"
    local output_file="$CODEX_STATE_DIR/current_output.txt"

    if [ ! -f "$state_file" ]; then
        echo "IDLE"
        return 0
    fi

    local state
    state="$(cat "$state_file" 2>/dev/null || echo "UNKNOWN")"
    echo "$state"

    if [ -f "$output_file" ]; then
        cat "$output_file"
    fi
}

# --- clear session ---

cmd_clear_session() {
    local session_id="${1:-}"
    if [ -n "$session_id" ]; then
        # Find and remove specific session
        local session_file
        session_file=$(find ~/.codex/sessions -type f -name "*${session_id}.jsonl" 2>/dev/null | head -1 || echo "")
        if [ -n "$session_file" ]; then
            rm -f "$session_file"
            echo "CLEARED"
        else
            echo "NOT_FOUND"
        fi
    else
        # Clear current state
        rm -f "$CODEX_STATE_DIR/current_prompt.state" "$CODEX_STATE_DIR/current_output.txt"
        echo "STATE_CLEARED"
    fi
}

# --- usage ---

usage() {
    cat <<'EOF'
Usage:
  codex.sh -l                        Check login status
  codex.sh -s                        List sessions
  codex.sh -p PROMPT [SESSION_ID]    Send prompt
  codex.sh -c [SESSION_ID]           Check status / get response
  codex.sh -x [SESSION_ID]           Clear session
EOF
}

case "${1:-}" in
    -l)
        cmd_check_login
        ;;
    -s)
        with_lock cmd_list_sessions
        ;;
    -p)
        shift
        prompt="${1:-}"
        session_id="${2:-}"
        if [ -z "$prompt" ]; then
            echo "ERROR: prompt required"
            exit 1
        fi
        with_lock cmd_send_prompt "$prompt" "$session_id"
        ;;
    -c)
        cmd_check_status
        ;;
    -x)
        shift
        with_lock cmd_clear_session "${1:-}"
        ;;
    *)
        usage
        exit 1
        ;;
esac
