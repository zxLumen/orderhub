#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/orderhub}"

git -C "$APP_DIR" pull --ff-only
if [ -f "$APP_DIR/requirements-webpush.txt" ]; then
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-webpush.txt"
fi
systemctl restart orderhub
systemctl --no-pager status orderhub | head -20
