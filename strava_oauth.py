#!/usr/bin/env python3
"""Capture a Strava OAuth callback associated with this login attempt."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import sys
import time
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

from project_config import load_config


def authorize(client_id: str, timeout: Optional[float] = None, opener=webbrowser.open) -> str:
    if timeout is None:
        timeout = load_config()["strava"]["oauth_timeout_seconds"]
    state = secrets.token_urlsafe(32)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != "/callback":
                status, message = 404, "无效回调路径"
            elif len(query.get("state", [])) != 1 or not secrets.compare_digest(
                    query["state"][0].encode(), state.encode()):
                status, message = 400, "授权校验失败，请回到原授权页面"
            elif query.get("error"):
                result["error"] = "用户拒绝了授权"
                status, message = 400, result["error"]
            elif len(query.get("code", [])) != 1:
                status, message = 400, "未收到唯一授权码"
            else:
                result["code"] = query["code"][0]
                status, message = 200, "✅ 授权成功，可以关闭此页面并回到终端"
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(message.encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass  # 回调 URL 包含一次性授权码，不能记录。

        def setup(self):
            super().setup()
            self.connection.settimeout(2)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        url = "https://www.strava.com/oauth/authorize?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
            "approval_prompt": "force", "scope": "activity:read_all,activity:write", "state": state,
        })
        print("→ 正在打开浏览器授权；若未打开，请访问：", file=sys.stderr)
        print(url, file=sys.stderr)
        deadline = time.monotonic() + timeout
        opener(url)
        while not result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("等待 Strava 授权超时，请重新运行 login")
            server.timeout = min(1, remaining)
            server.handle_request()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["code"]


if __name__ == "__main__":
    try:
        print(authorize(sys.argv[1]))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"❌ {error}", file=sys.stderr)
        sys.exit(1)
