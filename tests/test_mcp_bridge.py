"""Regression test: the labapi MCP bridge must serve tools under Python 3.9.

`the-lab-agent` and `the-lab init` launch the bridge with a bare ``python3``
from PATH, which on macOS is /usr/bin/python3 (3.9). If the bridge stops
importing there, the agent starts with no labapi tools and nothing errors
loudly. This test runs the bridge the way a client does -- as a subprocess
speaking JSON-RPC over stdio -- against a local HTTP server that serves the
real app's OpenAPI spec. No network, no account.

The interpreter that runs the bridge is ``LAB_MCP_PYTHON`` (default: the
interpreter running this test), so one test covers any bridge Python:

    LAB_MCP_PYTHON=/usr/bin/python3 python -m unittest tests.test_mcp_bridge
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "the_lab" / "agent_skills" / "skills" / "lab_api_mcp.py"
BRIDGE_PYTHON = os.environ.get("LAB_MCP_PYTHON") or sys.executable
SPEC_SCRIPT = "import json; from the_lab.app import app; print(json.dumps(app.openapi()))"


def _real_openapi_spec():
    # Importing the app initialises a project in the cwd, so do it in a
    # throwaway directory and a separate process.
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            [sys.executable, "-c", SPEC_SCRIPT],
            cwd=tmp, capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONPATH=str(ROOT)),
        )
    if out.returncode != 0:
        raise RuntimeError("could not build OpenAPI spec:\n" + out.stderr)
    return out.stdout.strip().splitlines()[-1].encode()


class BridgeServesTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = _real_openapi_spec()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                found = self.path == "/openapi.json"
                body = spec if found else b"{}"
                self.send_response(200 if found else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_initialize_and_tools_list(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        port = self.server.server_address[1]
        proc = subprocess.run(
            [BRIDGE_PYTHON, str(BRIDGE)],
            input="".join(json.dumps(m) + os.linesep for m in msgs),
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, THE_LAB_API_URL=f"http://127.0.0.1:{port}/api/v1"),
        )
        where = f"bridge under {BRIDGE_PYTHON}; stderr:{os.linesep}{proc.stderr}"
        self.assertEqual(proc.returncode, 0, where)

        replies = {r.get("id"): r for r in map(json.loads, proc.stdout.splitlines())}
        self.assertIn("result", replies.get(1, {}), where)
        tools = {t["name"] for t in replies.get(2, {}).get("result", {}).get("tools", [])}
        # watch_events/watch_keyword are static; the rest come from the spec.
        # Checking spec-derived names catches both a dead bridge and a bridge
        # whose INCLUDE table no longer matches the API.
        for name in ("orient", "create_idea", "list_ideas", "get_instructions"):
            self.assertIn(name, tools, where)


if __name__ == "__main__":
    unittest.main()
