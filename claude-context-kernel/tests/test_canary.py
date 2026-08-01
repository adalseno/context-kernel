"""Test del canary end-to-end: la compressione risulta APPLICATA nel transcript?

Il canary chiude il buco concettuale del log savings: compress.py logga cio'
che ha CALCOLATO, non cio' che l'harness ha APPLICATO. Il transcript della
sessione registra cio' che e' entrato davvero nel contesto: se il tool_result
di una compressione pending non contiene il footer, l'harness ha ignorato
updatedToolOutput e il canary deve dare l'allarme.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import _util

NOISY = "\n".join(["riga di rumore identica"] * 300)
TID = "toolu_canarytest_0001"


def _transcript_line(tool_use_id: str, text: str) -> str:
    """Riga JSONL come la scrive Claude Code per un tool_result."""
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": text}],
        }]},
    }) + "\n"


class CanaryCase(unittest.TestCase):
    """Base: state file + transcript temporanei, env gia' cablato."""

    def setUp(self):
        fd, self.state = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.state)
        fd, self.transcript = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        fd, self.log = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.env = {"CK_CANARY_STATE": self.state, "CK_LOG": self.log}

    def tearDown(self):
        for p in (self.state, self.transcript, self.log):
            if os.path.exists(p):
                os.unlink(p)

    def payload(self, stdout_text: str = NOISY, tid: str = TID) -> dict:
        p = _util.bash_payload(stdout_text)
        p["tool_use_id"] = tid
        p["transcript_path"] = self.transcript
        return p

    def state_dict(self) -> dict:
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def seed_pending(self, tid: str = TID, ts_offset: float = 0.0,
                     footer: str | None = None):
        import time
        entry = {"id": tid, "transcript": self.transcript,
                 "ts": time.time() + ts_offset}
        if footer is not None:
            entry["footer"] = footer
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [entry],
                       "verified": 0, "failed": 0,
                       "last_ok": None, "last_failure": None}, f)


class TestCanaryRecord(CanaryCase):

    def test_compression_records_pending(self):
        proc = _util.run_hook(_util.COMPRESS, self.payload(), env=self.env)
        self.assertIn("updatedToolOutput", proc.stdout)   # ha compresso davvero
        st = self.state_dict()
        self.assertEqual([p["id"] for p in st["pending"]], [TID])
        self.assertEqual(st["pending"][0]["transcript"], self.transcript)

    def test_pending_records_exact_footer(self):
        """Il pending deve portare il footer ESATTO (coi numeri) emesso:
        e' il marcatore che canary_check cerchera' nel transcript."""
        proc = _util.run_hook(_util.COMPRESS, self.payload(), env=self.env)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        st = self.state_dict()
        footer = st["pending"][0]["footer"]
        self.assertRegex(footer, r"^\[context-kernel: \d+ -> \d+ tokens, -\d+%\]$")
        self.assertIn(footer, upd["stdout"])              # coerente con l'emesso

    def test_subagent_compression_not_recorded(self):
        """Compressione dentro un subagent (agent_id nel payload): il result
        vive in un ALTRO transcript -> pending mai verificabile -> non
        registrare affatto."""
        payload = self.payload()
        payload["agent_id"] = "aqualcosa-123"
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        self.assertIn("updatedToolOutput", proc.stdout)   # comprime comunque
        self.assertFalse(os.path.exists(self.state))      # ma niente pending

    def test_noop_records_nothing(self):
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        self.assertFalse(os.path.exists(self.state))

    def test_disabled_via_env(self):
        _util.run_hook(_util.COMPRESS, self.payload(),
                       env={**self.env, "CK_CANARY": "0"})
        self.assertFalse(os.path.exists(self.state))


class TestCanaryVerify(CanaryCase):

    def test_footer_in_transcript_means_verified(self):
        self.seed_pending()
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, "output compresso\n\n[context-kernel: 100 -> 10 tokens, -90%]"))
        proc = _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        self.assertEqual(_util.hook_json(proc), {})       # nessun allarme
        st = self.state_dict()
        self.assertEqual(st["verified"], 1)
        self.assertEqual(st["failed"], 0)
        self.assertEqual(st["pending"], [])
        self.assertIsNotNone(st["last_ok"])

    def test_missing_footer_raises_alert(self):
        """IL caso che il canary esiste per scoprire: hook girato, log
        cresciuto, ma l'output nel transcript e' quello integrale."""
        self.seed_pending()
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, NOISY))          # integrale, no footer
        proc = _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        out = _util.hook_json(proc)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CANARY", ctx)
        self.assertIn("does not appear applied", ctx)
        st = self.state_dict()
        self.assertEqual(st["failed"], 1)
        self.assertEqual(st["pending"], [])
        self.assertIsNotNone(st["last_failure"])

    def test_alert_rides_along_with_compression(self):
        """L'allarme viaggia anche insieme a una compressione nello stesso output."""
        self.seed_pending()
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, NOISY))
        proc = _util.run_hook(_util.COMPRESS, self.payload(tid="toolu_altro"), env=self.env)
        hso = _util.hook_json(proc)["hookSpecificOutput"]
        self.assertIn("updatedToolOutput", hso)
        self.assertIn("CANARY", hso["additionalContext"])

    # --- LA regressione del 2026-07-15 sera: falso "verified" ---------------
    def test_content_citing_footer_is_not_verified(self):
        """Un tool_result INTEGRALE il cui contenuto CITA un footer (doc del
        progetto, log, transcript riletti) NON deve risultare verificato:
        il match va fatto sul footer esatto della compressione pending."""
        self.seed_pending(footer="[context-kernel: 1384 -> 855 tokens, -38%]")
        citazione = ("il file spiega il formato del footer, es. "
                     "[context-kernel: 1847 -> 1014 tokens, -45%] a fine output")
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, citazione))
        proc = _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        out = _util.hook_json(proc)
        self.assertIn("CANARY", out["hookSpecificOutput"]["additionalContext"])
        st = self.state_dict()
        self.assertEqual(st["verified"], 0)
        self.assertEqual(st["failed"], 1)

    def test_exact_footer_in_transcript_means_verified(self):
        footer = "[context-kernel: 1384 -> 855 tokens, -38%]"
        self.seed_pending(footer=footer)
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, f"output compresso\n\n{footer}"))
        proc = _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        self.assertEqual(_util.hook_json(proc), {})
        st = self.state_dict()
        self.assertEqual(st["verified"], 1)
        self.assertEqual(st["failed"], 0)

    # --- LA regressione del 2026-07-18: falso allarme da hint con virgolette
    def test_park_hint_quotes_do_not_break_verification(self):
        """1.15.0: l'hint di parcheggio porta un path tra VIRGOLETTE; nel
        JSONL del transcript diventano \\" e il match esatto footer+hint
        sulla riga grezza fallirebbe -> falso allarme su una compressione
        APPLICATA. Il pending registra il footer NUDO (solo i numeri, mai
        caratteri escapabili) e la verifica passa."""
        varied = "\n".join(
            f"riga ordinaria numero {i} con testo ripetitivo di riempimento"
            for i in range(300))
        proc = _util.run_hook(_util.COMPRESS, self.payload(stdout_text=varied),
                              env=self.env)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("parked", upd["stdout"])      # hint presente
        self.assertIn('"', upd["stdout"].split("parked")[1].split("]")[0])
        st = self.state_dict()
        footer = st["pending"][0]["footer"]
        self.assertRegex(footer, r"^\[context-kernel: \d+ -> \d+ tokens, -\d+%\]$")
        self.assertNotIn("parked", footer)          # footer NUDO
        # il transcript registra l'output compresso INTEGRALE, JSON-escapato
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, upd["stdout"]))
        proc2 = _util.run_hook(_util.COMPRESS, self.payload(
            stdout_text="piccolo", tid="toolu_altro"), env=self.env)
        self.assertEqual(_util.hook_json(proc2), {})      # nessun allarme
        st = self.state_dict()
        self.assertEqual(st["verified"], 1)
        self.assertEqual(st["failed"], 0)

    def test_elision_marker_alone_is_not_verified(self):
        """Il marcatore interno di elisione '[context-kernel: elise ...]' non
        e' il footer: senza footer esatto la compressione non risulta applicata."""
        self.seed_pending(footer="[context-kernel: 1384 -> 855 tokens, -38%]")
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(
                TID, "testa\n[context-kernel: elided 100 righe di rumore "
                     "(~900 token); mantenute 2 righe con segnale]\ncoda"))
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        st = self.state_dict()
        self.assertEqual(st["verified"], 0)
        self.assertEqual(st["failed"], 1)

    def test_legacy_pending_without_footer_falls_back_to_mark(self):
        """Pending scritti prima del fix (senza campo footer): fallback al
        prefisso generico, per non condannarli tutti a failed."""
        self.seed_pending()                                # nessun footer
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, "x\n\n[context-kernel: 100 -> 10 tokens, -90%]"))
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        st = self.state_dict()
        self.assertEqual(st["verified"], 1)

    def test_not_yet_in_transcript_stays_pending(self):
        self.seed_pending()
        open(self.transcript, "w").close()                # transcript vuoto
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        st = self.state_dict()
        self.assertEqual(len(st["pending"]), 1)
        self.assertEqual(st["verified"], 0)
        self.assertEqual(st["failed"], 0)

    def test_subagent_pending_dropped_after_short_ttl(self):
        """Pending della STESSA sessione mai comparso nel transcript entro il
        TTL breve: e' una compressione dentro un subagent (il suo result vive
        in un altro transcript) -> drop silenzioso, non failed."""
        self.seed_pending(ts_offset=-4000)                # oltre 1h, sotto 24h
        open(self.transcript, "w").close()
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        st = self.state_dict()
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["failed"], 0)
        self.assertEqual(st["verified"], 0)

    def test_failure_records_session(self):
        """Ogni fallimento annota la sessione: distingue questa sessione
        dalle headless concorrenti (distiller)."""
        self.seed_pending(footer="[context-kernel: 999 -> 111 tokens, -89%]")
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, NOISY))
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        st = self.state_dict()
        self.assertEqual(st["failed"], 1)
        self.assertEqual(len(st["failures"]), 1)
        base = os.path.basename(self.transcript)[:8]
        self.assertEqual(st["failures"][0]["session"], base)

    def test_other_session_pending_not_judged_here(self):
        import time
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [{"id": "toolu_x", "transcript": "/altra/sessione.jsonl",
                                    "ts": time.time()}],
                       "verified": 0, "failed": 0,
                       "last_ok": None, "last_failure": None}, f)
        open(self.transcript, "w").close()
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        st = self.state_dict()
        self.assertEqual(len(st["pending"]), 1)           # intatta

    def test_expired_pending_is_dropped(self):
        self.seed_pending(ts_offset=-90_000)              # oltre il TTL di 24h
        open(self.transcript, "w").close()
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        st = self.state_dict()
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["failed"], 0)                 # scaduta != fallita

    def test_tool_use_line_is_not_mistaken_for_result(self):
        """La riga assistant con lo stesso id (type: tool_use) non deve
        essere scambiata per il tool_result."""
        self.seed_pending()
        assistant_line = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": TID,
                "name": "Bash", "input": {"command": "x"},
            }]},
        }) + "\n"
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(assistant_line)                       # solo la tool_use
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"), env=self.env)
        st = self.state_dict()
        self.assertEqual(len(st["pending"]), 1)           # ancora in attesa
        self.assertEqual(st["failed"], 0)


class TestCanaryInSavingsReport(CanaryCase):

    def _seed_log(self):
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("2026-01-01T00:00:00,Bash,1000,100,900\n")

    def test_report_shows_verified(self):
        self._seed_log()
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [], "verified": 3, "failed": 0,
                       "last_ok": "2026-01-01T00:00:00", "last_failure": None}, f)
        out = _util.run_script(_util.SAVINGS, "", env=self.env).stdout
        self.assertIn("canary: ✓ 3 compressions verified", out)

    def test_report_warns_on_failures(self):
        self._seed_log()
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [], "verified": 1, "failed": 2,
                       "last_ok": None, "last_failure": "2026-01-01T00:00:00"}, f)
        out = _util.run_script(_util.SAVINGS, "", env=self.env).stdout
        self.assertIn("CANARY: ⚠ 2 compressions NOT applied", out)
        self.assertIn("overstated", out)

    def test_report_silent_without_state(self):
        self._seed_log()
        out = _util.run_script(_util.SAVINGS, "", env=self.env).stdout
        self.assertNotIn("canary", out.lower())

    def test_report_shows_auto_acked(self):
        """I failure auto-riconosciuti compaiono nello storico del report,
        distinti da quelli riconosciuti a mano — e senza ⚠."""
        self._seed_log()
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [], "verified": 5, "failed": 0,
                       "failed_auto_acked": 2, "heal_streak": 6,
                       "last_ok": "2026-01-01T00:00:00", "last_failure": None}, f)
        out = _util.run_script(_util.SAVINGS, "", env=self.env).stdout
        self.assertIn("2 auto-acknowledged", out)
        self.assertNotIn("⚠", out)

    def test_report_shows_heal_streak_on_open_failures(self):
        """Con failure aperti il report mostra l'evidenza accumulata: quante
        verificate consecutive mancano all'auto-ack."""
        self._seed_log()
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [], "verified": 3, "failed": 1,
                       "heal_streak": 2, "failures": [{"ts": "x", "session": "s1"}],
                       "last_ok": None, "last_failure": "2026-01-01T00:00:00"}, f)
        out = _util.run_script(_util.SAVINGS, "", env=self.env).stdout
        self.assertIn("auto-heal: 2 consecutive verified", out)

    def test_reset_canary_acks_failures(self):
        """--reset-canary sposta i fallimenti nello storico riconosciuto:
        l'allarme ⚠ si spegne e si riaccende solo su fallimenti NUOVI."""
        self._seed_log()
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [], "verified": 1, "failed": 2,
                       "failures": [{"ts": "x", "session": "s1"}],
                       "last_ok": None, "last_failure": "2026-01-01T00:00:00"}, f)
        out = _util.run_script(_util.SAVINGS, "", env=self.env,
                               args=["--reset-canary"]).stdout
        self.assertIn("Acknowledged 2 canary failures", out)
        report = _util.run_script(_util.SAVINGS, "", env=self.env).stdout
        self.assertNotIn("⚠", report)
        self.assertIn("2 historical, acknowledged", report)


class TestCanaryAutoDegrade(CanaryCase):
    """Dopo N violazioni nella STESSA sessione la sessione passa a raw
    pass-through: il canary SMETTE di comprimere, non solo avvisa (1.21.0)."""

    def _sess(self) -> str:
        base = os.path.basename(self.transcript)
        if base.endswith(".jsonl"):
            base = base[:-6]
        return base[:8] or "-"

    def _seed_failures(self, n: int):
        """Stato con n fallimenti gia' registrati per QUESTA sessione + un
        pending che fallira' su questa invocazione (transcript integrale)."""
        import time
        sess = self._sess()
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({
                "pending": [{"id": TID, "transcript": self.transcript,
                             "ts": time.time()}],
                "verified": 0, "failed": n,
                "failures": [{"ts": "x", "session": sess} for _ in range(n)],
                "last_ok": None, "last_failure": None, "degraded_sessions": [],
            }, f)
        with open(self.transcript, "w", encoding="utf-8") as tf:
            tf.write(_transcript_line(TID, NOISY))         # integrale -> fallisce

    def test_nth_failure_triggers_degrade(self):
        self._seed_failures(2)                             # questo e' il 3o (soglia 3)
        proc = _util.run_hook(_util.COMPRESS,
                              self.payload(stdout_text="piccolo"), env=self.env)
        ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AUTO-DEGRADE", ctx)
        self.assertIn(self._sess(), self.state_dict()["degraded_sessions"])

    def test_below_threshold_only_warns(self):
        self._seed_failures(0)                             # questo e' il 1o
        proc = _util.run_hook(_util.COMPRESS,
                              self.payload(stdout_text="piccolo"), env=self.env)
        ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("CANARY", ctx)
        self.assertNotIn("AUTO-DEGRADE", ctx)
        self.assertEqual(self.state_dict()["degraded_sessions"], [])

    def test_degraded_session_passes_through_raw(self):
        # sanity: senza degrade lo stesso output SI comprime
        proc0 = _util.run_hook(_util.COMPRESS, self.payload(), env=self.env)
        self.assertIn("updatedToolOutput", proc0.stdout)
        # degrada la sessione a mano -> stesso output passa INTATTO
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [], "verified": 0, "failed": 3, "failures": [],
                       "last_ok": None, "last_failure": None,
                       "degraded_sessions": [self._sess()]}, f)
        proc = _util.run_hook(_util.COMPRESS, self.payload(), env=self.env)
        self.assertNotIn("updatedToolOutput", proc.stdout)

    def test_other_session_not_degraded(self):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": [], "verified": 0, "failed": 3, "failures": [],
                       "last_ok": None, "last_failure": None,
                       "degraded_sessions": ["altrasess"]}, f)
        proc = _util.run_hook(_util.COMPRESS, self.payload(), env=self.env)
        self.assertIn("updatedToolOutput", proc.stdout)   # non e' la sua sessione

    def test_degrade_disabled_via_env(self):
        self._seed_failures(5)                             # ben oltre soglia
        proc = _util.run_hook(_util.COMPRESS,
                              self.payload(stdout_text="piccolo"),
                              env={**self.env, "CK_CANARY_DEGRADE_N": "0"})
        ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("AUTO-DEGRADE", ctx)
        self.assertEqual(self.state_dict().get("degraded_sessions", []), [])


FOOTER_OK = "[context-kernel: 100 -> 10 tokens, -90%]"


class TestCanaryAutoheal(CanaryCase):
    """Auto-ack con evidenza (1.34.0): ogni verified e' una sonda naturale del
    contratto — dopo HEAL_M verified CONSECUTIVE i failed aperti diventano
    transitori riconosciuti da soli (contatore separato, failure archiviati in
    auto_acks: nulla sparisce, si spegne solo l'allarme in statusline)."""

    HEAL_ENV = {"CK_CANARY_HEAL_M": "3"}

    def _seed(self, failed: int, streak: int):
        """Stato con failed aperti, striscia gia' a quota `streak` e un
        pending che VERIFICHERA' (o fallira', a seconda del transcript)."""
        import time
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({
                "pending": [{"id": TID, "transcript": self.transcript,
                             "ts": time.time(), "footer": FOOTER_OK,
                             "tool": "Bash"}],
                "verified": 7, "failed": failed,
                "failures": [{"ts": "x", "session": "vecchia", "tool": "Bash"}
                             for _ in range(failed)],
                "last_ok": None, "last_failure": "2026-07-20T10:00:00",
                "degraded_sessions": [], "heal_streak": streak,
            }, f)

    def test_streak_at_m_auto_acks_open_failures(self):
        self._seed(failed=2, streak=2)                     # questo verified e' il 3o
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, f"ok\n\n{FOOTER_OK}"))
        proc = _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"),
                              env={**self.env, **self.HEAL_ENV})
        self.assertEqual(_util.hook_json(proc), {})        # nessun allarme
        self.assertIn("auto-ack", proc.stderr)
        st = self.state_dict()
        self.assertEqual(st["failed"], 0)
        self.assertEqual(st["failed_auto_acked"], 2)
        self.assertEqual(st["auto_acks"][-1]["n"], 2)
        self.assertEqual(len(st["auto_acks"][-1]["failures"]), 2)  # archiviati
        self.assertEqual(st["failures"], [])

    def test_streak_below_m_keeps_failures_open(self):
        self._seed(failed=2, streak=0)                     # 1o verified: non basta
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, f"ok\n\n{FOOTER_OK}"))
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"),
                       env={**self.env, **self.HEAL_ENV})
        st = self.state_dict()
        self.assertEqual(st["failed"], 2)                  # ancora aperti
        self.assertEqual(st.get("failed_auto_acked", 0), 0)
        self.assertEqual(st["heal_streak"], 1)             # ma la striscia cresce

    def test_new_failure_resets_streak_and_fingerprints_tool(self):
        self._seed(failed=0, streak=4)                     # striscia quasi a quota
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, NOISY))          # integrale -> fallisce
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"),
                       env={**self.env, **self.HEAL_ENV})
        st = self.state_dict()
        self.assertEqual(st["heal_streak"], 0)             # evidenza azzerata
        self.assertEqual(st["failed"], 1)
        self.assertEqual(st["failures"][-1]["tool"], "Bash")  # fingerprint

    def test_autoheal_disabled_via_env(self):
        self._seed(failed=2, streak=9)                     # ben oltre quota
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, f"ok\n\n{FOOTER_OK}"))
        _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"),
                       env={**self.env, **self.HEAL_ENV,
                            "CK_CANARY_AUTOHEAL": "0"})
        st = self.state_dict()
        self.assertEqual(st["failed"], 2)                  # resta tutto manuale
        self.assertEqual(st.get("failed_auto_acked", 0), 0)


class TestCanaryProbe(CanaryCase):
    """Un-degrade a sonda (1.34.0): la sessione degradata resta raw, ma ogni
    PROBE_K output comprimibili UNO ripassa dal flusso normale come sonda;
    PROBE_M sonde verificate consecutive tolgono il degrado. Evidence-based:
    senza prove nel transcript la sessione resta raw com'era prima."""

    PROBE_ENV = {"CK_CANARY_PROBE_K": "3", "CK_CANARY_PROBE_M": "2"}

    def _sess(self) -> str:
        base = os.path.basename(self.transcript)
        if base.endswith(".jsonl"):
            base = base[:-6]
        return base[:8] or "-"

    def _seed_degraded(self, probe: dict | None = None,
                       pending: list | None = None):
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"pending": pending or [], "verified": 0, "failed": 3,
                       "failures": [], "last_ok": None, "last_failure": None,
                       "degraded_sessions": [self._sess()],
                       "probe": probe or {}}, f)

    def test_probe_fires_every_k_compressible_outputs(self):
        self._seed_degraded()
        env = {**self.env, **self.PROBE_ENV}
        p1 = _util.run_hook(_util.COMPRESS, self.payload(tid="toolu_p1"), env=env)
        p2 = _util.run_hook(_util.COMPRESS, self.payload(tid="toolu_p2"), env=env)
        self.assertNotIn("updatedToolOutput", p1.stdout)   # raw: slot 1
        self.assertNotIn("updatedToolOutput", p2.stdout)   # raw: slot 2
        p3 = _util.run_hook(_util.COMPRESS, self.payload(tid="toolu_p3"), env=env)
        self.assertIn("updatedToolOutput", p3.stdout)      # slot 3: la sonda comprime
        st = self.state_dict()
        self.assertTrue(st["pending"][-1].get("probe"))    # pending marcato sonda
        self.assertEqual(st["probe"][self._sess()]["count"], 3)

    def test_small_outputs_do_not_consume_probe_slots(self):
        """La sonda deve cadere su una compressione REALE: gli output sotto
        soglia non fanno avanzare il contatore (niente slot sprecati)."""
        self._seed_degraded()
        env = {**self.env, **self.PROBE_ENV}
        for i in range(4):
            proc = _util.run_hook(
                _util.COMPRESS,
                self.payload(stdout_text="piccolo", tid=f"toolu_s{i}"), env=env)
            self.assertNotIn("updatedToolOutput", proc.stdout)
        st = self.state_dict()
        self.assertEqual(st.get("probe", {}).get(self._sess(),
                                                 {}).get("count", 0), 0)

    def test_mth_verified_probe_lifts_degrade(self):
        import time
        self._seed_degraded(
            probe={self._sess(): {"count": 3, "ok": 1}},   # M=2: questa e' la 2a
            pending=[{"id": TID, "transcript": self.transcript,
                      "ts": time.time(), "footer": FOOTER_OK,
                      "tool": "Bash", "probe": True}])
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, f"ok\n\n{FOOTER_OK}"))
        proc = _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"),
                              env={**self.env, **self.PROBE_ENV})
        ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("RECOVERY", ctx)
        st = self.state_dict()
        self.assertEqual(st["degraded_sessions"], [])      # degrado rimosso
        self.assertNotIn(self._sess(), st.get("probe", {}))

    def test_failed_probe_stays_degraded_and_quiet(self):
        """Sonda fallita in sessione GIA' degradata: esito atteso — si conta
        (verita' dello stato) ma NIENTE allarme, e la striscia si azzera."""
        import time
        self._seed_degraded(
            probe={self._sess(): {"count": 3, "ok": 1}},
            pending=[{"id": TID, "transcript": self.transcript,
                      "ts": time.time(), "footer": FOOTER_OK,
                      "tool": "Bash", "probe": True}])
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(_transcript_line(TID, NOISY))          # integrale: non applicata
        proc = _util.run_hook(_util.COMPRESS, self.payload(stdout_text="piccolo"),
                              env={**self.env, **self.PROBE_ENV})
        self.assertEqual(_util.hook_json(proc), {})        # muto
        st = self.state_dict()
        self.assertIn(self._sess(), st["degraded_sessions"])
        self.assertEqual(st["probe"][self._sess()]["ok"], 0)
        self.assertEqual(st["failed"], 4)                  # ma contato davvero

    def test_probe_disabled_when_autoheal_off(self):
        self._seed_degraded()
        env = {**self.env, **self.PROBE_ENV, "CK_CANARY_AUTOHEAL": "0"}
        for i in range(4):
            proc = _util.run_hook(_util.COMPRESS,
                                  self.payload(tid=f"toolu_d{i}"), env=env)
            self.assertNotIn("updatedToolOutput", proc.stdout)


if __name__ == "__main__":
    unittest.main()
