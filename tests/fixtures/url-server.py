#!/usr/bin/env python3
"""A real local service with controllable URL output; standard library only."""

import http.server
import json
import os
import signal
from pathlib import Path
import sys
import threading
import time

root = Path(sys.argv[1])
if os.environ.get("TEST_EXIT_CODE"):
    sys.exit(int(os.environ["TEST_EXIT_CODE"]))
time.sleep(float(os.environ.get("TEST_LISTEN_DELAY", "0")))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        (root / "http-request").touch()
        self.send_response(int(os.environ.get("TEST_HTTP_STATUS", "200")))
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *unused):
        return


server = http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("TEST_PORT", "0"))), Handler)
if os.environ.get("TEST_GRACE_CLEANUP"):
    def cleanup(signum, frame):
        (root / "cleanup-started").touch()
        time.sleep(0.3)
        (root / "cleanup-complete").touch()
        sys.exit(0)
    signal.signal(signal.SIGTERM, cleanup)
port = server.server_address[1]
pid = os.getpid()
url = f"http://127.0.0.1:{port}/?token=token-{pid}"
(root / "service-info.json").write_text(json.dumps(dict(pid=pid, pgid=os.getpgrp(), port=port, url=url)))
with (root / "service-starts").open("a") as handle:
    handle.write(f"{pid}\n")
threading.Thread(target=server.serve_forever, daemon=True).start()
if os.environ.get("TEST_SPAM"):
    for _ in range(16000):
        print("service debug output, no URL", flush=True)
while os.environ.get("TEST_HOLD_URL") and not (root / "url-gate").exists():
    time.sleep(0.01)
if not os.environ.get("TEST_NO_URL"):
    time.sleep(float(os.environ.get("TEST_URL_DELAY", "0.03")))
    stream = sys.stderr if os.environ.get("TEST_STDERR") else sys.stdout
    line = "Open " + url
    if os.environ.get("TEST_ANSI"):
        line = "\x1b[36m" + line + "\x1b[0m"
    if os.environ.get("TEST_PARTIAL"):
        cut = line.rfind(str(pid)) + len(str(pid)) - 1
        stream.write(line[:cut]); stream.flush()
        (root / "partial-ready").touch()
        while not (root / "rest-gate").exists():
            time.sleep(0.01)
        stream.write(line[cut:] + "\n"); stream.flush()
    else:
        print(line, file=stream, flush=True)
while True:
    print("service heartbeat", flush=True)
    time.sleep(0.05)
