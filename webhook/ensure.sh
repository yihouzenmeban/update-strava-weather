#!/usr/bin/env bash
# (Re)install systemd units for the Strava webhook pipeline and start them.
# Idempotent: safe to run on every boot / after every VM replacement.
# /etc/systemd/system is ephemeral across deployments, so this script
# reinstalls the units from the templates in webhook/systemd/.
#
# First-time setup: copy config.sh.example to config.sh and fill in your
# ngrok reserved domain, then run: sudo bash webhook/ensure.sh
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$HOOK_DIR/systemd"
UNIT_DIR="/etc/systemd/system"

if [[ -f "$HOOK_DIR/config.sh" ]]; then
  # shellcheck disable=SC1090
  source "$HOOK_DIR/config.sh"
fi
if [[ -z "${NGROK_DOMAIN:-}" ]]; then
  echo "请先复制 webhook/config.sh.example 为 webhook/config.sh 并填入你的 ngrok 保留域名" >&2
  exit 1
fi

sed "s|__HOOK_DIR__|$HOOK_DIR|g" \
  "$SRC_DIR/strava-webhook-receiver.service" > "$UNIT_DIR/strava-webhook-receiver.service"
sed -e "s|__HOOK_DIR__|$HOOK_DIR|g" -e "s|__NGROK_DOMAIN__|$NGROK_DOMAIN|g" \
  "$SRC_DIR/strava-webhook-tunnel.service" > "$UNIT_DIR/strava-webhook-tunnel.service"
chmod 644 "$UNIT_DIR"/strava-webhook-*.service

systemctl daemon-reload
systemctl enable --now strava-webhook-receiver.service
# The tunnel needs the ngrok authtoken in /home/hatch/.config/ngrok/ngrok.yml (0600);
# if it is missing the unit will fail fast — see README.md.
systemctl enable --now strava-webhook-tunnel.service || true

sleep 2
systemctl is-active strava-webhook-receiver.service
echo "--- receiver health (local) ---"
curl -q -sS --max-time 10 http://127.0.0.1:8787/health && echo
echo "--- tunnel health (public) ---"
curl -q -sS --max-time 15 "https://$NGROK_DOMAIN/health" && echo
