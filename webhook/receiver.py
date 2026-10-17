#!/usr/bin/env python3
"""Strava webhook receiver for the weather-annotation pipeline.

Listens on 127.0.0.1 only (ngrok is the sole ingress). Handles:
  GET  /strava-webhook  -> Strava subscription verification (hub.challenge)
  POST /strava-webhook  -> activity events; on activity/create spawns a detached
                           `update_strava_weather.sh sync --activity-id <id>`
                           after a configurable delay (60s by default; lets Strava finish processing
                           the activity's GPS data and final title)
  GET  /health          -> liveness probe for the watchdog

Strava requires a 200 response within ~2s, so event handling never blocks
the response: the sync is spawned detached and its output goes to the log.
If a sync is already running (file lock), the periodic 2h cron covers the
activity later; the receiver just logs and moves on.
"""
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "receiver_config.json"
# Absolute path anchored at this file: the service runs as root (whose $HOME
# is /root), so Path.home() must NOT be used here.
# Supports two layouts: webhook/ inside the repo checkout, or webhook/ as a
# sibling of the update-strava-weather checkout.
SYNC_SCRIPT = ROOT.parent / "update_strava_weather.sh"
if not SYNC_SCRIPT.exists():
    SYNC_SCRIPT = ROOT.parent / "update-strava-weather" / "update_strava_weather.sh"
LOG_FILE = ROOT / "logs" / "receiver.log"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8787
WEBHOOK_PATH = "/strava-webhook"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("receiver")


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    config = {"verify_token": secrets.token_urlsafe(32), "sync_delay_seconds": 60}
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)
    log.info("generated new verify_token")
    return config


CONFIG = load_config()


def sync_delay_seconds():
    if "WEBHOOK_SYNC_DELAY_SECONDS" in os.environ:
        setting, value = "WEBHOOK_SYNC_DELAY_SECONDS", os.environ["WEBHOOK_SYNC_DELAY_SECONDS"]
    else:
        setting, value = "sync_delay_seconds", CONFIG.get("sync_delay_seconds", 60)
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        try:
            return int(value)
        except ValueError:
            pass
    raise ValueError(f"{setting} 必须为非负整数（秒）")


SYNC_DELAY_SECONDS = sync_delay_seconds()


def trigger_sync(activity_id):
    """Spawn a detached per-activity sync; never blocks the HTTP response.

    Strava fires the create event as soon as the activity exists, but its GPS
    data and final title can take a little while to finish processing, so the
    sync waits the configured delay before fetching the activity details.
    """
    if not SYNC_SCRIPT.exists():
        log.error("sync script missing: %s", SYNC_SCRIPT)
        return
    log.info("triggering sync for activity %s (%ss delay)", activity_id, SYNC_DELAY_SECONDS)
    log_out = open(ROOT / "logs" / f"sync-{activity_id}.log", "a", encoding="utf-8")
    try:
        subprocess.Popen(
            ["/bin/bash", "-c", 'sleep "$1"; exec "$2" sync --activity-id "$3"',
             "delayed-sync", str(SYNC_DELAY_SECONDS), str(SYNC_SCRIPT), str(activity_id)],
            stdout=log_out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(SYNC_SCRIPT.parent),
        )
    except Exception:
        log.exception("failed to spawn sync for activity %s", activity_id)
    finally:
        log_out.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "StravaWebhook/1.0"

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self):
        return urllib.parse.urlparse(self.path).path

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            return self._send_json(200, {"ok": True})
        if parsed.path != WEBHOOK_PATH:
            return self._send_json(404, {"error": "not found"})
        # Strava subscription verification
        query = urllib.parse.parse_qs(parsed.query)
        mode = query.get("hub.mode", [""])[0]
        token = query.get("hub.verify_token", [""])[0]
        challenge = query.get("hub.challenge", [""])[0]
        if mode == "subscribe" and challenge and secrets.compare_digest(token, CONFIG["verify_token"]):
            log.info("subscription verification succeeded")
            return self._send_json(200, {"hub.challenge": challenge})
        log.warning("subscription verification failed (mode=%r)", mode)
        return self._send_json(403, {"error": "verify_token mismatch"})

    def do_POST(self):
        if self._path() != WEBHOOK_PATH:
            return self._send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            event = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            log.warning("ignoring non-JSON POST (%d bytes)", len(raw))
            return self._send_json(200, {"ok": True})
        object_type = event.get("object_type")
        aspect_type = event.get("aspect_type")
        object_id = event.get("object_id")
        log.info("event: object_type=%s aspect_type=%s object_id=%s", object_type, aspect_type, object_id)
        if object_type == "activity" and aspect_type == "create" and object_id:
            trigger_sync(object_id)
        # Always 200 fast: Strava retries otherwise; the 2h cron is the fallback.
        return self._send_json(200, {"ok": True})

    def log_message(self, fmt, *args):
        # Never log the verify_token: it arrives as a URL query parameter.
        msg = fmt % args
        msg = re.sub(r"hub\.verify_token=[^&\s]*", "hub.verify_token=<redacted>", msg)
        log.info("%s - %s", self.address_string(), msg)


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("listening on %s:%d", LISTEN_HOST, LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
