"""Test di ab_verify.py: il giudizio A/B di answer-invariance con un giudice
FINTO (uno script locale al posto di `claude -p`) — il contratto testato e'
quello reale: campioni zlib nello stato, verdetto sull'ultima riga, ledger."""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
import zlib

import _util


def _pack(text: str) -> str:
    return base64.b64encode(zlib.compress(text.encode(), 6)).decode("ascii")


def _sample(orig: str = "riga a\nERRORE: rotto\nriga b",
            comp: str = "riga a\n[context-kernel: elided 1 righe]\nERRORE: rotto",
            attempts: int = 0) -> dict:
    return {"ts": 1752700000.0, "tool": "Bash", "file": None,
            "session": "abcd1234", "attempts": attempts,
            "orig_z": _pack(orig), "comp_z": _pack(comp)}


class TestABVerify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ab = os.path.join(self.dir, "ab.json")

    def _write_state(self, samples: list[dict], **extra) -> None:
        st = {"counter": 20, "pending": samples, "ok": 0, "degraded": 0,
              "last_run": None, **extra}
        with open(self.ab, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def _state(self) -> dict:
        with open(self.ab, encoding="utf-8") as f:
            return json.load(f)

    def _fake_judge(self, response: str, rc: int = 0) -> str:
        """Giudice finto: legge il prompt da stdin, risponde `response`.
        Un giudice `.py` e' la via portabile (ab_verify lo lancia con
        l'interprete corrente: su Windows lo shebang non esiste)."""
        path = os.path.join(self.dir, "fake_claude.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"import sys\nsys.stdin.read()\n"
                    f"print({response!r})\nsys.exit({rc})\n")
        return path

    def _run(self, judge: str, args: list[str] | None = None):
        return _util.run_script(
            _util.AB_VERIFY, "", args=args or [],
            env={"CK_AB_STATE": self.ab, "CK_AB_CLAUDE": judge})

    def test_invariant_verdict_updates_ledger(self):
        self._write_state([_sample()])
        judge = self._fake_judge("analisi breve\nVERDICT: INVARIANT")
        proc = self._run(judge)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = self._state()
        self.assertEqual(st["ok"], 1)
        self.assertEqual(st["degraded"], 0)
        self.assertEqual(st["pending"], [])
        self.assertIn("INVARIANT", proc.stdout)

    def test_degraded_verdict_records_what_was_lost(self):
        self._write_state([_sample()])
        judge = self._fake_judge(
            "manca l'esito dei test\n"
            "VERDICT: DEGRADED — conteggio dei test falliti perso")
        proc = self._run(judge)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = self._state()
        self.assertEqual(st["degraded"], 1)
        self.assertEqual(st["pending"], [])
        self.assertIn("conteggio dei test falliti perso",
                      st["degradations"][0]["missing"])
        self.assertIn("DEGRADED", proc.stdout)

    def test_unparsable_answer_retries_then_drops(self):
        self._write_state([_sample()])
        judge = self._fake_judge("boh, non saprei")
        for expected_attempts in (1, 2):
            self._run(judge)
            st = self._state()
            self.assertEqual(len(st["pending"]), 1)
            self.assertEqual(st["pending"][0]["attempts"], expected_attempts)
        self._run(judge)                       # terzo tentativo: scarto
        st = self._state()
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["ok"], 0)
        self.assertEqual(st["degraded"], 0)

    def test_missing_judge_keeps_sample_pending(self):
        self._write_state([_sample()])
        proc = self._run(os.path.join(self.dir, "non-esiste"))
        self.assertEqual(proc.returncode, 0)   # mai fatale
        st = self._state()
        self.assertEqual(len(st["pending"]), 1)
        self.assertIn("not found", proc.stderr)

    def test_judge_prompt_contains_both_versions(self):
        self._write_state([_sample(orig="ORIGINALE-UNICO-XYZ",
                                   comp="COMPRESSO-UNICO-XYZ")])
        proc = self._run("irrilevante", args=["--dry-run"])
        self.assertIn("ORIGINALE-UNICO-XYZ", proc.stdout)
        self.assertIn("COMPRESSO-UNICO-XYZ", proc.stdout)
        st = self._state()                     # dry-run: nessun consumo
        self.assertEqual(len(st["pending"]), 1)

    def test_status_flag_reports_without_calling(self):
        self._write_state([_sample()], ok=4, degraded=1)
        proc = self._run("irrilevante", args=["--status"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("4 invariant", proc.stdout)
        self.assertIn("1 degraded", proc.stdout)
        self.assertIn("1 samples pending", proc.stdout)

    def test_limit_judges_only_first_n(self):
        self._write_state([_sample(), _sample(orig="secondo campione qui")])
        judge = self._fake_judge("VERDICT: INVARIANT")
        self._run(judge, args=["--limit", "1"])
        st = self._state()
        self.assertEqual(st["ok"], 1)
        self.assertEqual(len(st["pending"]), 1)

    def test_cron_flag_prints_ready_line_without_touching_state(self):
        """--cron stampa la riga crontab pronta (path assoluti, --limit) e
        NON installa nulla: lo stato resta intatto e il giudice non parte."""
        self._write_state([_sample()])
        proc = self._run("claude-inesistente-xyz", args=["--cron"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("crontab -e", proc.stdout)
        self.assertIn("ab_verify.py", proc.stdout)
        self.assertIn("--limit 5", proc.stdout)
        self.assertIn("§11", proc.stdout)
        self.assertEqual(len(self._state()["pending"]), 1)

    def test_savings_report_shows_ab_line(self):
        self._write_state([_sample()], ok=7, degraded=2)
        fd, log = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as f:
            f.write("2026-07-17T00:00:00,Bash,1000,200,800,abcd1234\n")
        proc = _util.run_script(
            _util.SAVINGS, "",
            env={"CK_AB_STATE": self.ab, "CK_LOG": log,
                 "CK_CANARY_STATE": os.path.join(self.dir, "no-canary.json")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("A/B invariance", proc.stdout)
        self.assertIn("2 degraded", proc.stdout)
        self.assertIn("ab_verify.py", proc.stdout)


if __name__ == "__main__":
    unittest.main()
