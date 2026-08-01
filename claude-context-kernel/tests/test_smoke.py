"""Smoke test di release (1.17.0): il rito di verifica live, scriptato.

smoke.py generate/check verifica il CONTRATTO con l'harness reale (il
transcript contiene la versione compressa? l'ago e' eliso? il recall lo
ritrova?) — qui si testano le meccaniche del rito con transcript SINTETICI;
il rito vero si esegue in sessione viva a ogni release.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib

import _util

SMOKE = os.path.join(_util.PLUGIN_ROOT, "hooks", "smoke.py")
KEY = "abcdef0123"


def run_smoke(cmd: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, SMOKE, cmd], capture_output=True, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **env},
        encoding="utf-8", errors="replace", timeout=60)


class SmokeCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ck-smoke-")
        self.tdir = os.path.join(self.dir, "projects", "finto")
        os.makedirs(self.tdir)
        self.state = os.path.join(self.dir, "smoke.json")
        self.park = os.path.join(self.dir, "park.json")
        self.env = {
            "CK_SMOKE_STATE": self.state,
            "CK_SMOKE_TRANSCRIPTS": os.path.join(self.dir, "projects"),
            "CK_PARK_STATE": self.park,
            "CK_CANARY_STATE": os.path.join(self.dir, "canary.json"),
            "CK_CONTEXT_STATE": os.path.join(self.dir, "context.json"),
        }

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def gen(self) -> tuple[str, str, str]:
        """(run_id, needle, stdout del generate)."""
        proc = run_smoke("generate", self.env)
        with open(self.state, encoding="utf-8") as f:
            st = json.load(f)
        return st["id"], st["needle"], proc.stdout

    def write_transcript(self, text: str):
        line = json.dumps({"type": "user", "message": {
            "role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "toolu_smoke",
                "content": text}]}}) + "\n"
        with open(os.path.join(self.tdir, "sessione1.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(line)

    def compressed_of(self, run_id: str, original: str) -> str:
        """Versione 'compressa' plausibile: testa, marker, coda, footer con
        hint di parcheggio — e l'ago NON c'e' (eliso)."""
        lines = original.strip().split("\n")
        body = lines[:20] + ["[context-kernel: elided lines 21-390: ...]"] + lines[-8:]
        footer = ('[context-kernel: 5799 -> 728 tokens, -87%] '
                  f'[parked: python3 "{os.path.join(_util.HOOKS, "recall.py")}" '
                  f"{KEY} --grep PATTERN | --lines A-B]")
        return "\n".join(body) + "\n\n" + footer

    def park_the(self, original: str):
        entry = {"ts": __import__("time").time(), "tool": "Bash", "cmd": "smoke",
                 "z": base64.b64encode(zlib.compress(original.encode())).decode(),
                 "trunc": False}
        with open(self.park, "w", encoding="utf-8") as f:
            json.dump({KEY: entry}, f)


class TestGenerate(SmokeCase):

    def test_needle_is_computed_and_positioned(self):
        run_id, needle, out = self.gen()
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 400)
        self.assertIn(f"batch {run_id} — start", lines[0])
        self.assertIn(f"batch {run_id} — end", lines[-1])
        self.assertIn(needle, lines[236])                 # riga 237, 1-based
        self.assertEqual(sum(needle in l for l in lines), 1)
        self.assertRegex(needle, r"^sentinel-\d{5}$")   # niente hex/segnale


class TestCheck(SmokeCase):

    def test_full_pass_end_to_end(self):
        run_id, needle, out = self.gen()
        self.write_transcript(self.compressed_of(run_id, out))
        self.park_the(out)
        proc = run_smoke("check", self.env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("FAIL", proc.stdout)
        self.assertIn("updatedToolOutput honoured", proc.stdout)
        self.assertIn("needle elided", proc.stdout)
        self.assertIn("inverse page fault working", proc.stdout)
        self.assertIn("SKIP", proc.stdout)                # advisor: tap assente

    def test_raw_transcript_is_the_alarm_case(self):
        """IL caso per cui lo smoke esiste: harness che ignora
        updatedToolOutput -> transcript integrale -> FAIL rumoroso."""
        run_id, needle, out = self.gen()
        self.write_transcript(out)                        # integrale, con ago
        proc = run_smoke("check", self.env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ignored updatedToolOutput", proc.stdout)
        self.assertIn("needle still present", proc.stdout)

    def test_missing_generate_state(self):
        proc = run_smoke("check", self.env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("generate", proc.stdout)

    def test_canary_regression_detected(self):
        run_id, needle, out = self.gen()
        self.write_transcript(self.compressed_of(run_id, out))
        self.park_the(out)
        with open(self.env["CK_CANARY_STATE"], "w", encoding="utf-8") as f:
            json.dump({"failed": 3}, f)                   # cresciuto DOPO il generate
        proc = run_smoke("check", self.env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("canary: failed", proc.stdout)

    def test_advisor_leg_runs_on_real_context_state(self):
        run_id, needle, out = self.gen()
        self.write_transcript(self.compressed_of(run_id, out))
        self.park_the(out)
        with open(self.env["CK_CONTEXT_STATE"], "w", encoding="utf-8") as f:
            json.dump({"sessione": {"model": "m", "context_tokens": 50_000}}, f)
        proc = run_smoke("check", self.env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("warning at low threshold: PASS", proc.stdout)
        self.assertIn("one-shot per session: PASS", proc.stdout)
        self.assertIn("subagent silent: PASS", proc.stdout)
        self.assertIn("high threshold silent: PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
