#!/usr/bin/env python3
"""stub-server -- test double for a managed web app's local service.

Reads PORT from the environment (0 or unset -> the kernel picks a free
ephemeral port), prints one line a port_pattern can match and one line a
token_pattern can match, then serves HTTP 200 forever on 127.0.0.1.

Optional environment knobs:

    STUB_SERVER_TOKEN         token to print (default stub-token-<pid>)
    STUB_SERVER_INFO          file that receives one line
                              "actual_port=<p> PORT=<env> WEBAPP_PORT=<env>
                               WEBAPP_NAME=<env>" for env-injection checks
    STUB_SERVER_EXIT_AFTER_S  uptime before exiting (default: never exit)
    STUB_SERVER_EXIT_CODE     exit code for the timed exit (default 0)

Standard library only; python >= 3.9.
"""

import http.server
import os
import sys
import time

PORT_LINE = "stub-server listening on port {port}"
TOKEN_LINE = "stub-server auth token: {token}"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"stub-server ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *unused):
        pass  # stdout stays reserved for the two extractor lines


def main():
    try:
        wanted = int(os.environ.get("PORT", "0"))
    except ValueError:
        wanted = 0
    token = os.environ.get("STUB_SERVER_TOKEN") or f"stub-token-{os.getpid()}"

    server = http.server.ThreadingHTTPServer(("127.0.0.1", wanted), Handler)
    port = server.server_address[1]

    # Both lines flushed: the supervisor reads them from a pipe.
    print(PORT_LINE.format(port=port), flush=True)
    print(TOKEN_LINE.format(token=token), flush=True)

    info = os.environ.get("STUB_SERVER_INFO")
    if info:
        with open(info, "a") as fh:
            fh.write(
                f"actual_port={port} PORT={os.environ.get('PORT', '')} "
                f"WEBAPP_PORT={os.environ.get('WEBAPP_PORT', '')} "
                f"WEBAPP_NAME={os.environ.get('WEBAPP_NAME', '')}\n")

    try:
        exit_after = float(os.environ.get("STUB_SERVER_EXIT_AFTER_S", "0") or "0")
    except ValueError:
        exit_after = 0.0
    try:
        exit_code = int(os.environ.get("STUB_SERVER_EXIT_CODE", "0") or "0")
    except ValueError:
        exit_code = 0

    if exit_after > 0:
        deadline = time.monotonic() + exit_after
        server.timeout = 0.5
        while time.monotonic() < deadline:
            server.handle_request()  # returns after at most server.timeout
        server.server_close()
        sys.exit(exit_code)

    server.serve_forever()


if __name__ == "__main__":
    main()
