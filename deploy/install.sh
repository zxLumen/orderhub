#!/usr/bin/env bash
# One-time server install. Run as root.
set -euo pipefail

REPO="${REPO:-https://github.com/zxLumen/orderhub.git}"
APP_DIR="${APP_DIR:-/opt/orderhub}"
DATA_DIR="${DATA_DIR:-/var/lib/orderhub}"
ENV_FILE="${ENV_FILE:-/etc/orderhub.env}"

if ! id orderhub >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin orderhub
fi

install -d -o orderhub -g orderhub -m 0750 "$DATA_DIR"
install -d -o root -g root -m 0755 "$APP_DIR"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO" "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
if [ -f "$APP_DIR/requirements-webpush.txt" ]; then
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-webpush.txt"
fi

if [ ! -f "$ENV_FILE" ]; then
  cp "$APP_DIR/deploy/orderhub.env.example" "$ENV_FILE"
  chown root:orderhub "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
  echo "Created $ENV_FILE — edit it before starting the service."
fi

install -m 0644 "$APP_DIR/deploy/orderhub.service" /etc/systemd/system/orderhub.service
systemctl daemon-reload
systemctl enable orderhub
systemctl restart orderhub
systemctl --no-pager status orderhub || true
