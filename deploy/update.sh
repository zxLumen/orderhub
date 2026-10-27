#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/orderhub}"

git -C "$APP_DIR" pull --ff-only
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
if [ -f "$APP_DIR/requirements-webpush.txt" ]; then
  "$APP_DIR/.venv/bin/pip" install -i "$PIP_INDEX_URL" -r "$APP_DIR/requirements-webpush.txt"
fi
systemctl restart orderhub
systemctl --no-pager status orderhub | head -20
