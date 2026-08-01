"""Smoke test di bench/sufficiency_bench.py sull'oracolo deterministico."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _util

BENCH = os.path.join(_util.PLUGIN_ROOT, "bench", "sufficiency_bench.py")

FIXTURE = {
    "app/__init__.py": "",
    "app/db.py": (
        "def connect(addr):\n"
        "    raise ConnectionError('connection refused by upstream host')\n"
    ),
    "app/api.py": "from app import db\n\ndef handle():\n    return db.connect('x')\n",
    "app/noise.py": "x = 1\n",
}


class TestSufficiencyBench(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ck-bench-")
        for rel, content in FIXTURE.items():
            p = os.path.join(cls.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root)

    def test_raise_site_reached_from_partial_symptom(self):
        """Il sintomo parziale (frame del caller + messaggio) deve riportare
        il raise-site nel working set: sufficienza 1.0 su ogni config."""
        proc = subprocess.run([sys.executable, BENCH, self.root, "--json"],
                              capture_output=True, text=True, timeout=60,
                              encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertGreaterEqual(len(data["results"]), 1)
        for r in data["results"]:
            self.assertEqual(r["sufficiency"], 1.0, r)
            self.assertGreater(r["rate"], 0.0)

    def test_repo_without_candidates_exits_cleanly(self):
        empty = tempfile.mkdtemp(prefix="ck-bench-empty-")
        try:
            with open(os.path.join(empty, "solo.py"), "w") as f:
                f.write("x = 1\n")
            proc = subprocess.run([sys.executable, BENCH, empty],
                                  capture_output=True, text=True, timeout=60,
                                  encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("no candidate raise site", proc.stderr)
        finally:
            shutil.rmtree(empty)


if __name__ == "__main__":
    unittest.main()
