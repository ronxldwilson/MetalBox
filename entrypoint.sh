#!/bin/bash

HOST="host.docker.internal"
USER="${SSH_USER:-ron}"
KEY="/ssh-key"
METALBOX_DIR="${METALBOX_DIR:-/Users/ron/Desktop/tipstat/sourcer/metalbox}"
UV="/Users/ron/.local/bin/uv"
SSH="ssh -i $KEY -o StrictHostKeyChecking=no $USER@$HOST"

cleanup() {
    echo "[voice] stopping metalbox on host..."
    # stop s2st first (metalbox sends SIGTERM, waits for exit)
    $SSH "cd $METALBOX_DIR && $UV run metalbox stop s2st 2>/dev/null; true"
    # then kill the dashboard — no more restarts possible
    $SSH "pkill -f metalbox-dashboard 2>/dev/null; true"
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "[voice] starting metalbox + s2st on host"
$SSH "pkill -f metalbox-dashboard 2>/dev/null; true"
sleep 1
$SSH "cd $METALBOX_DIR && $UV run metalbox serve -d"
sleep 3
$SSH "cd $METALBOX_DIR && $UV run metalbox start s2st"

echo "[voice] waiting for s2st..."
for i in $(seq 1 60); do
    if $SSH "curl -sf http://127.0.0.1:18439/healthz" >/dev/null 2>&1; then
        echo "[voice] s2st ready on :18439"
        break
    fi
    sleep 1
done

# Keep alive — only restart via metalbox (which handles port checks)
while true; do
    sleep 30
    # Check if metalbox dashboard is alive first
    if ! $SSH "curl -sf http://127.0.0.1:9090/api/services" >/dev/null 2>&1; then
        echo "[voice] metalbox dashboard down, restarting..."
        $SSH "cd $METALBOX_DIR && $UV run metalbox serve -d 2>/dev/null; true"
        sleep 3
    fi
    # Then check s2st — metalbox start is a no-op if already running
    if ! $SSH "curl -sf http://127.0.0.1:18439/healthz" >/dev/null 2>&1; then
        echo "[voice] s2st down, restarting..."
        $SSH "cd $METALBOX_DIR && $UV run metalbox start s2st 2>/dev/null; true"
    fi
done
