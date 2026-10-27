#!/usr/bin/env python3
"""Controlled HTTP readiness and token emission for startup regressions."""

import http.server
import os
from pathlib import Path
import sys
import threading
import time

root = Path(sys.argv[1])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        (root / "request-seen").touch()
        while not (root / "response-gate").exists():
            time.sleep(0.01)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        try:
            self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionResetError):
            return  # a timed-out supervisor probe has closed the connection

    def log_message(self, *unused):
        return


server = http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
(root / "listener-ready").touch()
print(f"listening on port {server.server_address[1]}", flush=True)
while not (root / "token-gate").exists():
    if (root / "close-listener").exists() and not (root / "listener-closed").exists():
        server.shutdown()
        server.server_close()
        (root / "listener-closed").touch()
    time.sleep(0.01)
print("token: current-token", flush=True)
threading.Event().wait()
