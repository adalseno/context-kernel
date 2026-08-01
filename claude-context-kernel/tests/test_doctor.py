"""doctor.py — preflight dell'installazione.

Esercita la funzione check() (struttura) e il contratto di uscita dello script
(exit 0 senza [ko], 1 con almeno un [ko]). Gli stati canary/A-B sono isolati su
file temporanei via le env CK_CANARY_STATE / CK_AB_STATE: nessun test tocca lo
stato reale dell'utente.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import _util


def _load():
    spec = importlib.util.spec_from_file_location(
        "ck_doctor", os.path.join(_util.HOOKS, "doctor.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.canary = os.path.join(self.tmp, "canary.json")
        self.ab = os.path.join(self.tmp, "ab.json")
        os.environ["CK_CANARY_STATE"] = self.canary
        os.environ["CK_AB_STATE"] = self.ab

    def tearDown(self):
        for k in ("CK_CANARY_STATE", "CK_AB_STATE"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        return subprocess.run(
            [sys.executable, os.path.join(_util.HOOKS, "doctor.py")],
            capture_output=True, text=True,
            env={**os.environ, "CK_CANARY_STATE": self.canary,
                 "CK_AB_STATE": self.ab},
        )

    def test_clean_install_exits_zero(self):
        # Nessuno stato canary/A-B: struttura ok -> exit 0, nessun [ko].
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VERDICT", r.stdout)
        self.assertNotIn("[ko]", r.stdout)

    def test_core_scripts_and_commands_ok(self):
        mod = _load()
        rows, n_ko, _ = mod.check()
        texts = " ".join(m for _, m in rows)
        self.assertIn("core scripts present", texts)
        self.assertIn("/ck-* commands present", texts)
        self.assertEqual(n_ko, 0)

    def test_canary_failure_is_ko_and_exit_one(self):
        with open(self.canary, "w", encoding="utf-8") as fh:
            json.dump({"failed": 3, "verified": 10}, fh)
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("[ko]", r.stdout)
        self.assertIn("canary", r.stdout)

    def test_pending_ab_is_warn_not_ko(self):
        with open(self.ab, "w", encoding="utf-8") as fh:
            json.dump({"pending": [{"id": 1}, {"id": 2}], "ok": 5}, fh)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)  # warn non blocca
        self.assertIn("[warn]", r.stdout)
        self.assertIn("A/B", r.stdout)

    def test_degraded_sessions_warn(self):
        with open(self.canary, "w", encoding="utf-8") as fh:
            json.dump({"failed": 0, "verified": 4,
                       "degraded_sessions": ["s1"]}, fh)
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("auto-degrade", r.stdout)


if __name__ == "__main__":
    unittest.main()
