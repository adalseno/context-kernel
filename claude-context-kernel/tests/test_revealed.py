"""Test di revealed.py (T5, rilevanza rivelata): dal transcript ricava
file della slice mai aperti, file aperti fuori slice, page fault
post-elisione col loro costo in token."""
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
    "sorgenti scansionati: 100  |  slice: 3 file (-97%)",
    "",
    "## seeds (from the symptom)",
    "- app.py  <- citato nel sintomo",
    "",
    "## slice files (per rilevanza)",
    "- app.py — seed",
    "- pkg/calc.py — dependency a 1 hop (via app.py)",
    "- test_app.py — related test (usa app.py)",
    "",
    "## fuori slice (modello page-fault)",
    "97 sorgenti esclusi dal grafo degli import.",
])


def _tool_use(tid: str, name: str, finput: dict) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": finput}]}}


def _tool_result(tid: str, text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": text}]}]}}


class TestRevealed(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-revealed-")
        self.transcript = os.path.join(self.tmp, "sessione.jsonl")
        lines = [
            {"type": "user", "message": {"content": [
                {"type": "text",
                 "text": "[context-kernel] Sintomo rilevato\n" + MANIFEST}]}},
            _tool_use("t1", "Read", {"file_path": "/repo/app.py"}),
            _tool_result("t1", "x" * 100 +
                         "\n[context-kernel: elided righe 46-90: 40 righe]"
                         "\n[context-kernel: 500 -> 100 tokens, -80%] "
                         "[ELIDED copy: for the full text read this "
                         "stesso file]"),
            _tool_use("t2", "Read", {"file_path": "/repo/app.py"}),
            _tool_result("t2", "y" * 400),
            _tool_use("t3", "Read", {"file_path": "/repo/config/extra.py"}),
            _tool_result("t3", "z" * 100),
        ]
        with open(self.transcript, "w") as f:
            for l in lines:
                f.write(json.dumps(l) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return _util.run_script(_util.REVEALED, "", args=list(args))

    def test_report_covers_all_signals(self):
        out = self._run(self.transcript).stdout
        self.assertIn("T2 manifest: 3 files", out)
        self.assertIn("opened from the slice: 1/3", out)
        self.assertIn("pkg/calc.py", out)              # mai aperto
        self.assertIn("test_app.py", out)
        self.assertIn("OUTSIDE the slice", out)
        self.assertIn("config/extra.py", out)          # seed perso
        self.assertIn("page faults after elision: 1", out)
        self.assertIn("-> hint:", out)

    def test_fault_cost_measured_from_reread(self):
        r = json.loads(self._run(self.transcript, "--json").stdout)[0]
        self.assertEqual(len(r["faults"]), 1)
        self.assertEqual(r["faults"][0]["file"], "/repo/app.py")
        self.assertAlmostEqual(r["faults"][0]["tokens"], 100,
                               delta=5)                # ~400 char / 4

    def test_directory_argument(self):
        out = self._run(self.tmp).stdout
        self.assertIn("revealed relevance", out)

    def test_transcript_without_slice(self):
        empty = os.path.join(self.tmp, "vuota.jsonl")
        with open(empty, "w") as f:
            f.write(json.dumps(_tool_use("a", "Read",
                                         {"file_path": "/x.py"})) + "\n")
        out = self._run(empty).stdout
        self.assertIn("no T2 manifest", out)

    def test_no_transcript_exits_nonzero(self):
        proc = self._run(os.path.join(self.tmp, "inesistente.jsonl"))
        self.assertNotEqual(proc.returncode, 0)

    def _write_second_transcript(self):
        """Stessa storia in una seconda sessione: fa scattare la ricorrenza."""
        second = os.path.join(self.tmp, "sessione2.jsonl")
        with open(self.transcript) as f:
            content = f.read()
        with open(second, "w") as f:
            f.write(content)
        return second

    def test_aggregate_proposals_on_recurrence(self):
        self._write_second_transcript()
        out = self._run(self.tmp, "--aggregate").stdout
        self.assertIn("AGGREGATE over 2 transcript", out)
        self.assertIn("suggested config", out)
        self.assertIn("# ck:raw", out)                 # fault su app.py x2
        self.assertIn("app.py", out)
        self.assertIn("propose as a slicer seed", out)        # extra.py fuori slice x2
        self.assertIn("config/extra.py", out)
        self.assertIn("wide prior", out)              # calc.py mai aperto x2
        self.assertIn("never auto-tunes", out)          # l'umano applica

    def test_aggregate_single_occurrence_no_proposal(self):
        out = self._run(self.transcript, "--aggregate").stdout
        self.assertIn("AGGREGATE over 1 transcript", out)
        self.assertIn("no recurrent pattern", out)

    def test_write_priors_on_recurrence(self):
        self._write_second_transcript()
        priors = os.path.join(self.tmp, "priors.json")
        out = _util.run_script(
            _util.REVEALED, "", args=[self.tmp, "--write-priors"],
            env={"CK_PRIORS_STATE": priors}).stdout
        self.assertIn("priors written", out)
        with open(priors) as f:
            st = json.load(f)
        rec = st[os.path.normpath("/repo")]
        self.assertEqual(rec["seeds"],                 # fuori slice x2 sessioni
                         [{"path": "config/extra.py", "sessions": 2}])
        cold = {c["path"] for c in rec["cold"]}        # mai aperti x2 manifest
        self.assertIn("pkg/calc.py", cold)

    def test_write_priors_single_occurrence_no_write(self):
        priors = os.path.join(self.tmp, "priors.json")
        out = _util.run_script(
            _util.REVEALED, "", args=[self.transcript, "--write-priors"],
            env={"CK_PRIORS_STATE": priors}).stdout
        self.assertIn("no recurrent pattern", out)
        self.assertFalse(os.path.exists(priors))

    def test_aggregate_json(self):
        self._write_second_transcript()
        a = json.loads(self._run(self.tmp, "--aggregate", "--json").stdout)
        self.assertEqual(a["transcripts"], 2)
        self.assertEqual(a["faults"], 2)
        self.assertIn("/repo/app.py", a["fault_files"])
        self.assertEqual(a["fault_files"]["/repo/app.py"]["transcripts"], 2)
        self.assertTrue(any("ck:raw" in p for p in a["proposals"]))


if __name__ == "__main__":
    unittest.main()
