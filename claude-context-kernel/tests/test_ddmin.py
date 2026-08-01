"""ddmin.py — minimalita' empirica (delta debugging). Il motore trova il
sottoinsieme 1-minimale che ANCORA riproduce, via un oracolo pass/fail. Test
con oracoli sintetici deterministici (grep), senza toolchain esterne.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

import _util

DDMIN = os.path.join(_util.PLUGIN_ROOT, "hooks", "ddmin.py")


def _run(input_text: str, oracle: str, *extra: str):
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(input_text)
    try:
        return subprocess.run(
            [sys.executable, DDMIN, "--oracle", oracle, "--input", path, *extra],
            capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)


class TestDdmin(unittest.TestCase):

    def test_reduces_lines_to_the_needle(self):
        text = "\n".join([f"rumore {i}" for i in range(40)]
                         + ["AGO"] + [f"coda {i}" for i in range(20)])
        r = _run(text, "grep -q AGO {}", "--unit", "line")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "AGO")
        self.assertIn("1-minimal", r.stderr)

    def test_char_mode_reduces_to_substring(self):
        # riproduce sse il candidato contiene la sottostringa "XZ"
        text = "aaaaaXZbbbbb"
        r = _run(text, "grep -q XZ {}", "--unit", "char")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("XZ", r.stdout)
        self.assertLessEqual(len(r.stdout.rstrip("\n")), 3)   # ~1-minimale

    def test_result_is_1_minimal(self):
        """Proprieta' centrale: tolto un qualunque elemento, non riproduce piu'.
        Oracolo: servono ENTRAMBE le righe A e B presenti."""
        text = "\n".join(["x"] * 10 + ["A"] + ["y"] * 10 + ["B"] + ["z"] * 5)
        # riproduce sse contiene A E B
        oracle = r"grep -q '^A$' {} && grep -q '^B$' {}"
        r = _run(text, oracle, "--unit", "line")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = set(r.stdout.strip().split("\n"))
        self.assertEqual(got, {"A", "B"})                     # esattamente A,B

    def test_full_input_not_reproducing_errors(self):
        r = _run("nessun ago qui", "grep -q INTROVABILE {}", "--unit", "line")
        self.assertEqual(r.returncode, 2)
        self.assertIn("does not reproduce", r.stderr)

    def test_custom_fail_exit(self):
        """Oracolo che segnala 'riproduce' con exit 3."""
        text = "\n".join(["q"] * 20 + ["TARGET"])
        oracle = "grep -q TARGET {} && exit 3 || exit 0"
        r = _run(text, oracle, "--unit", "line", "--fail-exit", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "TARGET")

    def test_stdin_oracle(self):
        """Senza {} nel comando, il candidato arriva su stdin."""
        text = "\n".join(["n"] * 15 + ["NEEDLE"])
        r = _run(text, "grep -q NEEDLE", "--unit", "line")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "NEEDLE")

    def test_deterministic(self):
        text = "\n".join([f"r{i}" for i in range(30)] + ["AGO"])
        a = _run(text, "grep -q AGO {}", "--unit", "line").stdout
        b = _run(text, "grep -q AGO {}", "--unit", "line").stdout
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
