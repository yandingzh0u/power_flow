"""Minimal HTTP API for GMSS using the Python standard library."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import traceback
from typing import Any, Dict

from .contracts import describe_public_contract
from .service import make_api_request, run_api_request


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


class GMSSRequestHandler(BaseHTTPRequestHandler):
    server_version = "GMSSHTTP/1.0"

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # The static analysis page is normally served on a separate local port.
        # Keep the API usable from that page without requiring a proxy.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_payload(self) -> Dict[str, Any]:
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        if not data:
            return {}
        return json.loads(data.decode("utf-8"))

    def do_GET(self) -> None:
        if self.path in {"/health", "/api/v1/health"}:
            self._send_json(200, {"status": "ok"})
            return
        if self.path in {"/contract", "/api/v1/contract"}:
            self._send_json(200, describe_public_contract())
            return
        self._send_json(404, {"status": "error", "message": f"unknown path: {self.path}"})

    def do_POST(self) -> None:
        try:
            payload = self._read_payload()
            if self.path in {"/normalize", "/api/v1/gmss/normalize"}:
                self._send_json(200, {"status": "ok", "request": make_api_request(payload)})
                return
            if self.path in {"/run", "/api/v1/gmss/run"}:
                self._send_json(200, run_api_request(payload))
                return
            self._send_json(404, {"status": "error", "message": f"unknown path: {self.path}"})
        except Exception as exc:
            traceback.print_exc()
            self._send_json(500, {"status": "error", "message": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the GMSS HTTP API service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), GMSSRequestHandler)
    print(f"GMSS API listening on http://{args.host}:{args.port}", flush=True)
    print("Health check: GET /api/v1/health", flush=True)
    print("Run endpoint: POST /api/v1/gmss/run", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
