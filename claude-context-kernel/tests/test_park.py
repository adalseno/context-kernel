"""Parcheggio degli output effimeri elisi + recall mirato + compact advisor.

Il page fault delle Read esiste perche' il file e' su disco; per Bash/MCP
l'inversa non c'era: l'elisione perdeva il mezzo per sempre. Il parcheggio
la rende reversibile (recall --grep/--lines, deterministico), e il footer
DICHIARA la via. L'advisor: una riga una-tantum quando la finestra supera
la soglia (il /compact manuale costa meno dell'auto-compact al pieno).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unittest

import _util

ADVISOR = os.path.join(_util.PLUGIN_ROOT, "hooks", "compact_advisor.py")
RECALL = os.path.join(_util.PLUGIN_ROOT, "hooks", "recall.py")


def _noisy_with_needle(n: int = 300) -> str:
    lines = [f"riga ordinaria numero {i} con testo ripetitivo di riempimento"
             for i in range(n)]
    lines[140] = "riga speciale AGO-NEL-PAGLIAIO 0142 senza segnale forte"
    return "\n".join(lines)


class TestPark(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ck-park-")
        self.park = os.path.join(self.dir, "park.json")
        self.env = {"CK_LOG_OFF": "1", "CK_CANARY": "0",
                    "CK_PARK_STATE": self.park,
                    "CK_READS_STATE": os.path.join(self.dir, "reads.json")}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _compress(self, text: str, env: dict | None = None):
        return _util.run_hook(_util.COMPRESS, _util.bash_payload(text),
                              env={**self.env, **(env or {})})

    def _park_key(self, stdout: str) -> str:
        # nello stdout del hook le virgolette sono JSON-escapate: aggancia
        # la chiave alla forma "KEY --grep PATTERN" del suggerimento
        m = re.search(r"([0-9a-f]{10}) --grep PATTERN", stdout)
        self.assertIsNotNone(m, f"footer senza hint di parcheggio: {stdout[-400:]}")
        return m.group(1)

    def test_elided_bash_output_is_parked_with_declared_recall(self):
        proc = self._compress(_noisy_with_needle())
        key = self._park_key(proc.stdout)
        self.assertIn("parked", proc.stdout)
        with open(self.park, encoding="utf-8") as f:
            st = json.load(f)
        self.assertIn(key, st)
        self.assertEqual(st[key]["tool"], "Bash")

    def test_recall_grep_recovers_elided_line(self):
        """La riga senza segnale forte viene ELISA dal contesto ma il grep
        sul parcheggio la recupera: il fault costa solo cio' che chiedi."""
        proc = self._compress(_noisy_with_needle())
        self.assertNotIn("AGO-NEL-PAGLIAIO", proc.stdout)   # davvero elisa
        key = self._park_key(proc.stdout)
        rec = _util.run_script(RECALL, "", args=[key, "--grep",
                                                 "AGO-NEL-PAGLIAIO"],
                               env=self.env)
        self.assertEqual(rec.returncode, 0, rec.stderr)
        self.assertIn("AGO-NEL-PAGLIAIO 0142", rec.stdout)
        self.assertIn("141\t", rec.stdout)                  # numerata (1-based)

    def test_recall_lines_range(self):
        proc = self._compress(_noisy_with_needle())
        key = self._park_key(proc.stdout)
        rec = _util.run_script(RECALL, "", args=[key, "--lines", "10-12"],
                               env=self.env)
        self.assertIn("riga ordinaria numero 9 ", rec.stdout)
        self.assertIn("riga ordinaria numero 11 ", rec.stdout)
        self.assertNotIn("numero 12 ", rec.stdout)

    def test_small_output_not_parked(self):
        self._compress("poche righe\nniente elisione")
        self.assertFalse(os.path.exists(self.park))

    def test_kill_switch(self):
        proc = self._compress(_noisy_with_needle(), env={"CK_PARK": "0"})
        self.assertNotIn("parked", proc.stdout)
        self.assertFalse(os.path.exists(self.park))

    def test_recall_unknown_key_exits_2(self):
        rec = _util.run_script(RECALL, "", args=["deadbeef00"], env=self.env)
        self.assertEqual(rec.returncode, 2)
        self.assertIn("missing or expired", rec.stderr)

    # --- recall storage di sessione: --search cross-parcheggio (1.24.0) -------

    def _seed_park(self, entries: dict):
        """entries = {key: (testo, tool, cmd)} -> scrive il park.json."""
        import base64
        import zlib
        st = {k: {"z": base64.b64encode(zlib.compress(t.encode())).decode(),
                  "ts": time.time(), "tool": tool, "cmd": cmd}
              for k, (t, tool, cmd) in entries.items()}
        with open(self.park, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def test_search_finds_across_all_parked(self):
        """--search trova cio' che cerchi in QUALSIASI output parcheggiato e ti
        da' la chiave per il recall mirato (il recall storage della sessione)."""
        self._seed_park({
            "aaa1110000": ("riga\nqui ERROR: disco pieno\ncoda", "Bash", "df -h"),
            "bbb2220000": ("ok\nWARN: retry\nfine", "Bash", "curl x"),
            "ccc3330000": ("tutto tranquillo\nnulla", "WebFetch", "fetch"),
        })
        rec = _util.run_script(RECALL, "", args=["--search", "ERROR|WARN"],
                               env=self.env)
        self.assertEqual(rec.returncode, 0, rec.stderr)
        self.assertIn("aaa1110000", rec.stdout)             # contiene ERROR
        self.assertIn("bbb2220000", rec.stdout)             # contiene WARN
        self.assertNotIn("ccc3330000", rec.stdout)          # nessun match
        self.assertIn("ERROR: disco pieno", rec.stdout)     # riga col contesto
        self.assertIn("-> recall aaa1110000 --grep", rec.stdout)  # punta al mirato

    def test_search_no_match_declares_it(self):
        self._seed_park({"aaa1110000": ("testo tranquillo", "Bash", "ls")})
        rec = _util.run_script(RECALL, "", args=["--search", "NIENTE_QUI"],
                               env=self.env)
        self.assertEqual(rec.returncode, 0)
        self.assertIn("no parked output matches", rec.stdout)

    def test_search_empty_park(self):
        rec = _util.run_script(RECALL, "", args=["--search", "x"], env=self.env)
        self.assertIn("parking lot empty", rec.stdout)

    def test_search_bad_regex_exits_2(self):
        self._seed_park({"aaa1110000": ("x", "Bash", "ls")})
        rec = _util.run_script(RECALL, "", args=["--search", "("], env=self.env)
        self.assertEqual(rec.returncode, 2)
        self.assertIn("invalid regex", rec.stderr)

    def test_search_logs_fault_when_enabled(self):
        """--search e' un page fault sul parcheggio: col ledger attivo registra
        il costo dei match mostrati (solo numeri)."""
        fault = os.path.join(self.dir, "faults.log")
        self._seed_park({"aaa1110000": ("riga\nERROR: boom\ncoda", "Bash", "x")})
        env = {**self.env, "CK_FAULT_LOG": fault}
        env.pop("CK_LOG_OFF", None)
        rec = _util.run_script(RECALL, "", args=["--search", "ERROR"], env=env)
        self.assertEqual(rec.returncode, 0, rec.stderr)
        with open(fault, encoding="utf-8") as f:
            rows = [ln for ln in f if ln.strip()]
        self.assertEqual(len(rows), 1)
        self.assertIn(",recall,recall,", rows[0])


class TestCompactAdvisor(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ck-advise-")
        self.ctx = os.path.join(self.dir, "context.json")
        self.env = {"CK_CONTEXT_STATE": self.ctx,
                    "CK_ADVISE_STATE": os.path.join(self.dir, "advised.json")}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _tap(self, used: int):
        with open(self.ctx, "w", encoding="utf-8") as f:
            json.dump({"abcd1234": {"model": "test-model",
                                    "context_tokens": used,
                                    "ts": time.time()}}, f)

    def _run(self, env: dict | None = None):
        payload = {"tool_name": "Bash",
                   "transcript_path": "/x/abcd1234-ef.jsonl",
                   "tool_input": {"command": "ls"}}
        return _util.hook_json(_util.run_script(
            ADVISOR, json.dumps(payload), env={**self.env, **(env or {})}))

    def test_advises_once_above_threshold(self):
        self._tap(190_000)                     # finestra stimata ~268k -> ~71%
        out = self._run()
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("MANUAL /compact", ctx)
        self.assertIn("One-off", ctx)
        self.assertEqual(self._run(), {})      # seconda volta: silenzio

    def test_silent_below_threshold(self):
        self._tap(100_000)
        self.assertEqual(self._run(), {})

    def test_disabled_via_env(self):
        self._tap(190_000)
        self.assertEqual(self._run(env={"CK_COMPACT_ADVISE": "0"}), {})

    def test_subagent_never_advised(self):
        self._tap(190_000)
        payload = {"tool_name": "Bash", "agent_id": "a1b2",
                   "transcript_path": "/x/abcd1234-ef.jsonl"}
        out = _util.hook_json(_util.run_script(
            ADVISOR, json.dumps(payload), env=self.env))
        self.assertEqual(out, {})


class TestEphemeralDividend(unittest.TestCase):
    """Il dividendo del parcheggio (1.16.0): l'inversa garantita autorizza
    un tasso piu' aggressivo ESATTAMENTE sui tool parcheggiati. Con
    CK_PARK=0 l'aggressivita' si spegne da sola (niente inversa)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ck-eph-")
        # l'harness pinna CK_EPHEMERAL_SCALE=1.0: qui il dividendo si
        # testa APPOSTA, quindi il valore di default va richiesto esplicito
        self.env = {"CK_LOG_OFF": "1", "CK_CANARY": "0",
                    "CK_EPHEMERAL_SCALE": "0.5",
                    "CK_PARK_STATE": os.path.join(self.dir, "park.json"),
                    "CK_READS_STATE": os.path.join(self.dir, "reads.json")}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _medium(self) -> str:
        # ~640 token, 40 righe: sotto la soglia piena (800), sopra quella
        # aggressiva (800*0.5=400) — il caso che il dividendo sblocca
        return "\n".join(_util.unique_lines(40))

    def test_medium_bash_elided_only_with_park(self):
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload(self._medium()),
                              env=self.env)
        out = _util.hook_json(proc)
        self.assertIn("updatedToolOutput", out.get("hookSpecificOutput", {}))
        self.assertIn("parked", proc.stdout)        # inversa dichiarata
        proc_off = _util.run_hook(_util.COMPRESS, _util.bash_payload(self._medium()),
                                  env={**self.env, "CK_PARK": "0"})
        self.assertEqual(_util.hook_json(proc_off), {})   # niente inversa -> raw

    def test_read_same_size_untouched(self):
        proc = _util.run_hook(_util.COMPRESS, _util.read_payload(self._medium()),
                              env=self.env)
        self.assertEqual(_util.hook_json(proc), {})       # soglia piena per i file

    def test_disabled_via_scale_one(self):
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload(self._medium()),
                              env={**self.env, "CK_EPHEMERAL_SCALE": "1.0"})
        self.assertEqual(_util.hook_json(proc), {})       # comportamento pre-1.16

    def test_aggressive_arm_elides_from_earlier_line(self):
        big = "\n".join(_util.unique_lines(300))
        def start_of_elision(env):
            proc = _util.run_hook(_util.COMPRESS, _util.bash_payload(big), env=env)
            m = re.search(r"elided lines (\d+)-", proc.stdout)
            self.assertIsNotNone(m, proc.stdout[-300:])
            return int(m.group(1))
        base = start_of_elision({**self.env, "CK_EPHEMERAL_SCALE": "1.0"})
        aggr = start_of_elision(self.env)
        self.assertLess(aggr, base)                       # head piu' corta

    def test_signal_lines_survive_aggressive_arm(self):
        lines = _util.unique_lines(300)
        lines[150] = "ERROR: sonda distintiva in mezzo al rumore eliso"
        proc = _util.run_hook(_util.COMPRESS,
                              _util.bash_payload("\n".join(lines)), env=self.env)
        self.assertIn("sonda distintiva", proc.stdout)    # il segnale non si tocca


if __name__ == "__main__":
    unittest.main()
