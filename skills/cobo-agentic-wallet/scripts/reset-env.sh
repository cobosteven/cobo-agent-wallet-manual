#!/usr/bin/env bash
# Reset the cobo-agentic-wallet environment for re-provisioning.
# Usage: bash scripts/reset-env.sh
set -euo pipefail

STATE_DIR="${HOME}/.cobo-agentic-wallet"

if [ ! -d "$STATE_DIR" ]; then
  echo "Nothing to reset: $STATE_DIR does not exist."
  exit 0
fi

# 1. Stop the TSS Node process
PID_FILE="${STATE_DIR}/.tss-node.pid"
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping TSS Node (pid=$PID)..."
    kill "$PID"
    echo "TSS Node stopped."
  else
    echo "TSS Node not running (stale pid file)."
  fi
else
  echo "No TSS Node pid file found, skipping."
fi

# 2. Back up existing state
BACKUP_DIR="${STATE_DIR}.bak.$(date +%s)"
echo "Backing up $STATE_DIR → $BACKUP_DIR"
cp -r "$STATE_DIR" "$BACKUP_DIR"
echo "Backup created."

# 3. Remove state files to allow re-provisioning
FILES_TO_REMOVE=(
  "${STATE_DIR}/db/secrets.db"
  "${STATE_DIR}/.tss-node.pid"
  "${STATE_DIR}/.tss-env"
  "${STATE_DIR}/config"
  "${STATE_DIR}/.password"
)

echo "Removing state files..."
for f in "${FILES_TO_REMOVE[@]}"; do
  if [ -f "$f" ]; then
    rm -f "$f"
    echo "  Removed: $f"
  fi
done

echo "Reset complete. You can now re-run provisioning."
