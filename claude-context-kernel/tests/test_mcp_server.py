"""Test del server MCP stdio: handshake JSON-RPC, tools/list, tools/call, errori."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

import _util

FIXTURE = '''\
def helper():
    return 1

def target():
    return helper() + 1

def unrelated():
    return 99
'''


class TestMcpServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        fd, cls.fixture = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(FIXTURE)
        cls.repo = tempfile.mkdtemp(prefix="ck-mcp-repo-")
        with open(os.path.join(cls.repo, "main.py"), "w", encoding="utf-8") as fh:
            fh.write("import util\n\ndef run():\n    return util.f()\n")
        with open(os.path.join(cls.repo, "util.py"), "w", encoding="utf-8") as fh:
            fh.write("def f():\n    return 1\n")
        with open(os.path.join(cls.repo, "loner.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")

        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "kernel_slice",
                        "arguments": {"file": cls.fixture, "symbols": ["target"]}}},
            {"jsonrpc": "2.0", "id": 4, "method": "metodo/inesistente"},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "tool_sbagliato"}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "kernel_slice",
                        "arguments": {"file": "/percorso/inesistente.py",
                                      "symbols": ["x"]}}},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "kernel_repo_slice",
                        "arguments": {"repo": cls.repo,
                                      "symptom": 'File "main.py", line 4, in run'}}},
        ]
        stdin = "\n".join(json.dumps(r) for r in requests) + "\n"
        proc = subprocess.run([sys.executable, _util.MCP_SERVER],
                              input=stdin, capture_output=True, text=True,
                              timeout=60)
        cls.by_id = {}
        for line in proc.stdout.strip().split("\n"):
            resp = json.loads(line)
            cls.by_id[resp["id"]] = resp

    @classmethod
    def tearDownClass(cls):
        import shutil
        os.unlink(cls.fixture)
        shutil.rmtree(cls.repo)

    def test_initialize_handshake(self):
        res = self.by_id[1]["result"]
        self.assertEqual(res["serverInfo"]["name"], "context-kernel")
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertIn("tools", res["capabilities"])

    def test_notification_gets_no_response(self):
        """8 richieste con id + 1 notifica -> esattamente 8 risposte."""
        self.assertEqual(set(self.by_id), {1, 2, 3, 4, 5, 6, 7, 8})

    def test_tools_list_exposes_both_slicers(self):
        tools = self.by_id[2]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools],
                         ["kernel_slice", "kernel_repo_slice"])
        self.assertEqual(sorted(tools[0]["inputSchema"]["required"]),
                         ["file", "symbols"])
        self.assertEqual(sorted(tools[1]["inputSchema"]["required"]),
                         ["repo", "symptom"])

    def test_repo_slice_call_returns_manifest(self):
        res = self.by_id[8]["result"]
        self.assertIs(res["isError"], False)
        text = res["content"][0]["text"]
        self.assertIn("main.py — seed", text)
        self.assertIn("util.py — dependency", text)
        self.assertNotIn("loner.py", text)

    def test_tools_call_returns_slice_only(self):
        res = self.by_id[3]["result"]
        self.assertIs(res["isError"], False)
        text = res["content"][0]["text"]
        self.assertIn("def target", text)
        self.assertIn("def helper", text)       # dipendenza
        self.assertNotIn("def unrelated", text)  # non raggiungibile

    def test_unknown_method_is_jsonrpc_error(self):
        self.assertEqual(self.by_id[4]["error"]["code"], -32601)

    def test_unknown_tool_is_invalid_params(self):
        self.assertEqual(self.by_id[5]["error"]["code"], -32602)

    def test_missing_file_is_tool_error_not_crash(self):
        res = self.by_id[6]["result"]
        self.assertIs(res["isError"], True)
        self.assertIn("not found", res["content"][0]["text"])

    def test_ping(self):
        self.assertEqual(self.by_id[7]["result"], {})


if __name__ == "__main__":
    unittest.main()
