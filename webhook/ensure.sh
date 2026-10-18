#!/usr/bin/env bash
# (Re)install systemd units for the Strava webhook pipeline and start them.
# Idempotent: safe to run on every boot / after every VM replacement.
# Reinstall units from webhook/systemd/ when a deployment loses them.
#
# First-time setup: set ngrok.domain in config.local.json, then run with sudo.
set +x
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$HOOK_DIR/.." && pwd)"
source "$SCRIPT_DIR/strava_common.sh"

if [[ -z "${NGROK_DOMAIN:-}" ]]; then
  echo "请在 config.local.json 的 ngrok.domain 中填写 ngrok 保留域名" >&2
  exit 1
fi

python3 "$SCRIPT_DIR/project_config.py" render-units "$UNIT_DIR"

systemctl daemon-reload
systemctl enable --now strava-webhook-receiver.service
# ngrok uses the private file selected by ngrok.config_file (see README.md).
systemctl enable --now strava-webhook-tunnel.service || true

sleep 2
systemctl is-active strava-webhook-receiver.service
echo "--- receiver health (local) ---"
curl -q -sS --max-time "$HTTP_CONNECT_TIMEOUT_SECONDS" "http://$WEBHOOK_LISTEN_HOST:$WEBHOOK_LISTEN_PORT/health" && echo
echo "--- tunnel health (public) ---"
curl -q -sS --max-time 15 "https://$NGROK_DOMAIN/health" && echo
