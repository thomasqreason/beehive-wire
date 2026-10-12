#!/usr/bin/env python3
"""
One-time: mint the YouTube refresh token that lets the video workflow upload to your channel.

    python youtube_auth.py --client-id XXX --client-secret YYY

Run it on your own computer (it opens a browser), sign in with the Google account that owns the Boring Money
channel, and when Google asks which channel, pick Boring Money. It prints a refresh token; save it as the
repo secret YOUTUBE_REFRESH_TOKEN. Nothing is stored on disk. Then check it with:

    YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=... YOUTUBE_REFRESH_TOKEN=... python video.py whoami
"""
from __future__ import annotations

import argparse
import http.server
import secrets
import threading
import urllib.parse
import webbrowser

import requests

SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    redirect = f"http://127.0.0.1:{a.port}/"
    state = secrets.token_urlsafe(16)
    got: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state") == [state] and "code" in q:
                got["code"] = q["code"][0]
            else:
                got["error"] = (q.get("error") or ["bad response"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", a.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": a.client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent select_account", "state": state})
    print("Opening your browser. Sign in, and choose the Boring Money channel if asked.\nIf it does not open, go to:\n" + url)
    webbrowser.open(url)
    done.wait(300)
    server.shutdown()
    if "code" not in got:
        raise SystemExit(f"no authorization code: {got.get('error', 'timed out')}")

    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": got["code"], "client_id": a.client_id, "client_secret": a.client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30)
    if r.status_code != 200 or "refresh_token" not in r.json():
        raise SystemExit(f"token exchange failed ({r.status_code}): {r.text[:300]}")
    print("\nYOUTUBE_REFRESH_TOKEN (save as a repo secret, and keep it private):\n" + r.json()["refresh_token"])


if __name__ == "__main__":
    main()
