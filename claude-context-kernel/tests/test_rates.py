"""Test del loop T5 -> T1: revealed.py --apply-rates scrive i tassi
per-categoria dai fault ricorrenti, compress.py li legge e RILASSA
(mai stringe) la compressione su quella categoria."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import _util

MANIFEST = "\n".join([
    "# kernel repo slice — manifest",
    "operator: T2@test",
    "repo: /repo",
    "",
    "## seeds (from the symptom)",
    "- app.md  <- citato nel sintomo",
    "",
    "## slice files (per rilevanza)",
    "- app.md — seed",
    "",
    "## fuori slice (modello page-fault)",
    "97 sorgenti esclusi dal grafo degli import.",
])


def _tool_use(tid: str, finput: dict) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": "Read", "input": finput}]}}


def _tool_result(tid: str, text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": text}]}]}}


def _fault_transcript(path: str) -> None:
    """Una sessione con un page fault post-elisione su /repo/app.md."""
    lines = [
        {"type": "user", "message": {"content": [
            {"type": "text",
             "text": "[context-kernel] Sintomo rilevato\n" + MANIFEST}]}},
        _tool_use("t1", {"file_path": "/repo/app.md"}),
        _tool_result("t1", "x" * 100 +
                     "\n[context-kernel: elided righe 10-90: 80 righe]"
                     "\n[ELIDED copy: for the full text read this file again]"),
        _tool_use("t2", {"file_path": "/repo/app.md"}),
        _tool_result("t2", "y" * 400),
    ]
    with open(path, "w") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")


class TestApplyRates(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-rates-")
        self.rates = os.path.join(self.tmp, "rates.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, env=None):
        return _util.run_script(_util.REVEALED, "", args=list(args),
                                env={"CK_RATES_STATE": self.rates,
                                     **(env or {})})

    def test_recurrence_writes_relax_rate(self):
        for name in ("s1.jsonl", "s2.jsonl"):
            _fault_transcript(os.path.join(self.tmp, name))
        out = self._run(self.tmp, "--aggregate", "--apply-rates").stdout
        self.assertIn("explicit application", out)
        self.assertIn(".md -> relax", out)
        with open(self.rates) as f:
            st = json.load(f)
        self.assertEqual(st["categories"][".md"]["mode"], "relax")
        self.assertGreater(st["categories"][".md"]["scale"], 1.0)

    def test_heavy_faults_write_raw(self):
        for name in ("s1.jsonl", "s2.jsonl"):
            _fault_transcript(os.path.join(self.tmp, name))
        out = self._run(self.tmp, "--aggregate", "--apply-rates",
                        env={"CK_RATES_RAW_FAULTS": "2"}).stdout
        self.assertIn(".md -> raw", out)
        with open(self.rates) as f:
            st = json.load(f)
        self.assertEqual(st["categories"][".md"]["mode"], "raw")

    def test_single_occurrence_writes_nothing(self):
        _fault_transcript(os.path.join(self.tmp, "s1.jsonl"))
        out = self._run(self.tmp, "--aggregate", "--apply-rates").stdout
        self.assertIn("no recurrent fault", out)
        self.assertFalse(os.path.exists(self.rates))


class TestCompressReadsRates(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-rates-c-")
        self.rates = os.path.join(self.tmp, "rates.json")
        # 300 righe rumorose: senza tassi verrebbe compresso di sicuro
        self.noisy = "\n".join(f"riga di rumore numero {i} con testo inutile"
                               for i in range(300))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _compress(self, file_path: str):
        payload = _util.read_payload(self.noisy, file_path=file_path)
        return _util.run_script(_util.COMPRESS, json.dumps(payload),
                                env={"CK_RATES_STATE": self.rates,
                                     "CK_DELTA": "0", "CK_LOG_OFF": "1"})

    def _write_rates(self, categories: dict) -> None:
        with open(self.rates, "w") as f:
            json.dump({"ts": 0, "categories": categories}, f)

    def test_raw_category_skips_compression(self):
        self._write_rates({".log": {"mode": "raw"}})
        proc = self._compress("/tmp/grande.log")
        self.assertEqual(json.loads(proc.stdout.strip()), {})
        self.assertIn("learned rate", proc.stderr)

    def test_relax_scale_raises_threshold(self):
        # scala enorme: la soglia MIN_TOKENS*scala supera l'output -> no-op.
        # E' la direzione (solo rilassare) portata all'estremo osservabile.
        self._write_rates({".log": {"mode": "relax", "scale": 100}})
        proc = self._compress("/tmp/grande.log")
        self.assertEqual(json.loads(proc.stdout.strip()), {})

    def test_other_category_still_compressed(self):
        self._write_rates({".log": {"mode": "raw"}})
        proc = self._compress("/tmp/grande.txt")
        out = json.loads(proc.stdout.strip())
        content = out["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"]
        self.assertIn("[context-kernel:", content)

    def test_rates_disabled_by_env(self):
        self._write_rates({".log": {"mode": "raw"}})
        payload = _util.read_payload(self.noisy, file_path="/tmp/grande.log")
        proc = _util.run_script(_util.COMPRESS, json.dumps(payload),
                                env={"CK_RATES_STATE": self.rates,
                                     "CK_RATES": "0",
                                     "CK_DELTA": "0", "CK_LOG_OFF": "1"})
        out = json.loads(proc.stdout.strip())
        self.assertIn("hookSpecificOutput", out)


if __name__ == "__main__":
    unittest.main()
