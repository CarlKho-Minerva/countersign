"""Test site. Serves the forms on form.test and plays the collector on collector.test.
Every request is one line on stdout, so a test can prove what did and did not arrive."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).parent / "site"


class Handler(BaseHTTPRequestHandler):
    def _log(self, body=b""):
        host = (self.headers.get("Host") or "").split(":")[0]
        print(json.dumps({"host": host, "method": self.command, "path": self.path, "body": body.decode("utf-8", "replace")[:2000]}), flush=True)
        return host

    def _send(self, status, body, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        host = self._log()
        if host.startswith("collector."):
            return self._send(200, b"collected", "text/plain")
        name = self.path.split("?")[0].lstrip("/") or "plain.html"
        page = SITE / name
        if page.parent != SITE or not page.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, page.read_bytes())

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        host = self._log(body)
        if host.startswith("collector."):
            return self._send(200, b"collected", "text/plain")
        self._send(200, b"<h1>Registration received</h1>")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 80), Handler).serve_forever()
