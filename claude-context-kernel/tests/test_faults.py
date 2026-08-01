"""Ledger dei PAGE FAULT (1.19.0) — il lato DISTORSIONE della curva.

savings.log misura il rate (token tolti); faults.log misura la distorsione
(token rientrati quando l'elisione "sbaglia" e il resto serve davvero). Qui:
i tre siti di fault (rilettura integrale, riesecuzione, recall mirato) loggano
davvero, col costo ATTRIBUITO all'elisione che l'ha causato, e savings.py
riporta il rapporto. Contratto reale via subprocess, come il resto della suite.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest

import _util


def _rows(path: str) -> list[list[str]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [ln.strip().split(",") for ln in f if ln.strip()]


class TestRereadFault(unittest.TestCase):
    """Una Read elisa, poi riletta, e' un page fault: il token risparmiato su
    quel file RIENTRA nel contesto -> riga 'reread' col costo ESATTO (il saved
    memorizzato all'elisione, non una stima)."""

    def setUp(self):
        fd, self.log = tempfile.mkstemp(suffix=".csv"); os.close(fd)
        fd, self.faults = tempfile.mkstemp(suffix=".log"); os.close(fd)
        fd, self.reads = tempfile.mkstemp(suffix=".json"); os.close(fd)
        os.unlink(self.reads)
        self.env = {"CK_LOG": self.log, "CK_FAULT_LOG": self.faults,
                    "CK_READS_STATE": self.reads}
        self.content = "\n".join(_util.unique_lines(300))

    def tearDown(self):
        for p in (self.log, self.faults, self.reads):
            if os.path.exists(p):
                os.unlink(p)

    def _read(self, text):
        payload = _util.read_payload(text, file_path="/tmp/grande.py")
        payload["transcript_path"] = "/tmp/sess-fault11.jsonl"
        return _util.run_hook(_util.COMPRESS, payload, env=self.env)

    def test_reread_logs_fault_with_attributed_cost(self):
        self._read(self.content)                 # elisa: 1 riga in savings.log
        save_rows = _rows(self.log)
        self.assertEqual(len(save_rows), 1)
        saved = int(save_rows[0][4])             # before-after della PRIMA read

        proc = self._read(self.content)          # page fault -> integrale
        self.assertEqual(_util.hook_json(proc), {})   # passa intatto

        frows = _rows(self.faults)
        self.assertEqual(len(frows), 1, frows)
        _ts, kind, bucket, tok, sess = frows[0]
        self.assertEqual(kind, "reread")
        self.assertEqual(bucket, ".py")          # categoria = estensione
        self.assertEqual(int(tok), saved)        # costo ATTRIBUITO all'elisione
        self.assertEqual(sess, "sess-fau")       # id corto della sessione

    def test_no_fault_without_rereading(self):
        self._read(self.content)                 # solo elisione, nessun fault
        self.assertEqual(_rows(self.faults), [])


class TestRecmdFault(unittest.TestCase):
    """Un comando Bash con output eliso, rieseguito identico, e' un page fault
    -> riga 'recmd' col nome del tool come categoria."""

    def setUp(self):
        fd, self.log = tempfile.mkstemp(suffix=".csv"); os.close(fd)
        fd, self.faults = tempfile.mkstemp(suffix=".log"); os.close(fd)
        fd, self.cmds = tempfile.mkstemp(suffix=".json"); os.close(fd)
        os.unlink(self.cmds)
        self.env = {"CK_LOG": self.log, "CK_FAULT_LOG": self.faults,
                    "CK_CMDS_STATE": self.cmds}
        self.out = "\n".join(_util.unique_lines(300))

    def tearDown(self):
        for p in (self.log, self.faults, self.cmds):
            if os.path.exists(p):
                os.unlink(p)

    def _run(self):
        payload = _util.bash_payload(self.out)
        payload["transcript_path"] = "/tmp/sess-recmd1.jsonl"
        return _util.run_hook(_util.COMPRESS, payload, env=self.env)

    def test_rerun_after_elision_logs_recmd_fault(self):
        self._run()                              # eliso + marcato
        proc = self._run()                       # riesecuzione = page fault
        self.assertEqual(_util.hook_json(proc), {})
        frows = _rows(self.faults)
        self.assertEqual(len(frows), 1, frows)
        _ts, kind, bucket, tok, _sess = frows[0]
        self.assertEqual(kind, "recmd")
        self.assertEqual(bucket, "Bash")
        self.assertGreater(int(tok), 0)


class TestRecallFault(unittest.TestCase):
    """Un recall E' il pagamento di un page fault sul parcheggio: registra i
    token EFFETTIVAMENTE restituiti (recupero mirato), non l'output intero."""

    def setUp(self):
        fd, self.faults = tempfile.mkstemp(suffix=".log"); os.close(fd)
        fd, self.park = tempfile.mkstemp(suffix=".json"); os.close(fd)
        os.unlink(self.park)
        # park e fault CONDIVISI tra la compressione (che parcheggia) e il recall
        self.env = {"CK_LOG_OFF": "1", "CK_FAULT_LOG": self.faults,
                    "CK_PARK_STATE": self.park}
        self.out = "\n".join(_util.unique_lines(300))

    def tearDown(self):
        for p in (self.faults, self.park):
            if os.path.exists(p):
                os.unlink(p)

    def test_recall_logs_targeted_cost(self):
        payload = _util.bash_payload(self.out)
        payload["transcript_path"] = "/tmp/sess-recall.jsonl"
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        blob = upd["stdout"]
        m = re.search(r'parked: python3 "[^"]+" (\w+) ', blob)
        self.assertIsNotNone(m, blob)
        key = m.group(1)

        recall = os.path.join(_util.HOOKS, "recall.py")
        env = {**os.environ, "CK_PARK_STATE": self.park,
               "CK_FAULT_LOG": self.faults}
        r = subprocess.run([sys.executable, recall, key, "--lines", "1-5"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0001", r.stdout)          # ha restituito la fetta

        frows = _rows(self.faults)
        self.assertEqual(len(frows), 1, frows)
        _ts, kind, bucket, tok, _sess = frows[0]
        self.assertEqual(kind, "recall")
        self.assertEqual(bucket, "recall")
        self.assertGreater(int(tok), 0)          # costo mirato, non l'intero

    def test_recall_respects_log_off(self):
        payload = _util.bash_payload(self.out)
        payload["transcript_path"] = "/tmp/sess-recall.jsonl"
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        blob = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        key = re.search(r'parked: python3 "[^"]+" (\w+) ', blob).group(1)
        recall = os.path.join(_util.HOOKS, "recall.py")
        env = {**os.environ, "CK_PARK_STATE": self.park,
               "CK_FAULT_LOG": self.faults, "CK_LOG_OFF": "1"}
        subprocess.run([sys.executable, recall, key, "--all"],
                       capture_output=True, text=True, env=env)
        self.assertEqual(_rows(self.faults), [])  # kill-switch onorato


class TestFaultReport(unittest.TestCase):
    """savings.py riporta la distorsione accanto al risparmio: conteggio,
    token rientrati e la frazione del risparmio recuperata."""

    def setUp(self):
        fd, self.log = tempfile.mkstemp(suffix=".csv"); os.close(fd)
        fd, self.faults = tempfile.mkstemp(suffix=".log"); os.close(fd)
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("2026-07-18T10:00:00,Read,1000,200,800,sessAAAA,-\n")
            f.write("2026-07-18T10:01:00,Bash,600,100,500,sessAAAA,-\n")
        with open(self.faults, "w", encoding="utf-8") as f:
            f.write("2026-07-18T10:05:00,reread,.py,800,sessAAAA\n")
            f.write("2026-07-18T10:06:00,recall,recall,40,-\n")

    def tearDown(self):
        for p in (self.log, self.faults):
            if os.path.exists(p):
                os.unlink(p)

    def test_text_report_shows_distortion(self):
        env = {**os.environ, "CK_LOG": self.log, "CK_FAULT_LOG": self.faults,
               "CK_CANARY_STATE": "/nonexistent-ck-canary.json",
               "CK_AB_STATE": "/nonexistent-ck-ab.json"}
        r = subprocess.run([sys.executable, _util.SAVINGS],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        self.assertIn("page faults (distortion)", out)
        self.assertIn("2 recoveries", out)
        self.assertIn("840", out)                # 800 + 40 rientrati
        # 840 rientrati su 1300 risparmiati = 64.6%
        self.assertIn("64.6%", out)
        self.assertIn("full re-reads", out)
        self.assertIn("targeted recalls", out)

    def test_read_faults_helper(self):
        sys.path.insert(0, _util.HOOKS)
        try:
            os.environ["CK_FAULT_LOG"] = self.faults
            import importlib
            import savings
            importlib.reload(savings)
            n, tok, per_kind, per_bucket = savings.read_faults()
        finally:
            os.environ.pop("CK_FAULT_LOG", None)
            sys.path.remove(_util.HOOKS)
        self.assertEqual(n, 2)
        self.assertEqual(tok, 840)
        self.assertEqual(per_kind["reread"], [800, 1])
        self.assertEqual(per_bucket["recall"], [40, 1])


if __name__ == "__main__":
    unittest.main()
