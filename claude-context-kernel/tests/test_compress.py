"""Test di compress.py: contratto hook (subprocess) + unit sulle funzioni pure."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import tempfile
import unittest

import _util

FOOTER = re.compile(r"\[context-kernel: \d+ -> \d+ tokens, -\d+%\]")


def _load_module():
    spec = importlib.util.spec_from_file_location("ck_compress", _util.COMPRESS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestUnitFunctions(unittest.TestCase):
    """Unit test sulle funzioni pure (import diretto del modulo)."""

    @classmethod
    def setUpClass(cls):
        cls.ck = _load_module()

    def test_dedup_collapses_runs_of_three_or_more(self):
        out = self.ck.dedup(["riga", "riga", "riga", "altra"])
        self.assertEqual(out, ["riga  [x 3]", "altra"])

    def test_dedup_keeps_pairs_verbatim(self):
        out = self.ck.dedup(["riga", "riga", "altra"])
        self.assertEqual(out, ["riga", "riga", "altra"])

    def test_dedup_collapses_blank_runs(self):
        out = self.ck.dedup(["a", "", "", "", "b"])
        self.assertEqual(out, ["a", "", "b"])

    def test_dedup_collapses_alternating_pairs(self):
        # spinner a 2 righe alternate: A,B ripetute — il dedup semplice non
        # le vede (nessuna riga uguale alla precedente)
        out = self.ck.dedup(["stato: attivo", "attendo..."] * 5 + ["fine"])
        self.assertEqual(out, ["stato: attivo",
                               "attendo...  [x 5 coppie alternate]", "fine"])

    def test_dedup_keeps_short_alternations_verbatim(self):
        lines = ["A", "B"] * 2 + ["fine"]
        self.assertEqual(self.ck.dedup(lines), lines)

    def test_dedup_alternations_with_blank_lines_untouched(self):
        # coppie con riga vuota: le gestisce il collasso dei blank, non questo
        out = self.ck.dedup(["A", ""] * 5 + ["fine"])
        self.assertNotIn("coppie alternate", "\n".join(out))
        self.assertEqual(out.count("A"), 5)

    def test_normalize_strips_ansi(self):
        self.assertEqual(self.ck.normalize("\x1b[31mrosso\x1b[0m"), "rosso")

    def test_normalize_keeps_last_progress_state(self):
        self.assertEqual(self.ck.normalize("10%\r50%\r100%"), "100%")

    def test_truncate_preserves_signal_lines_in_middle(self):
        lines = _util.unique_lines(120)
        lines[60] = "ERROR: qualcosa di rotto a meta' output"
        out = self.ck.signal_preserving_truncate(lines)
        self.assertIn(lines[60], out)
        self.assertTrue(any("[context-kernel: elided" in l for l in out))
        self.assertLess(len(out), len(lines))

    def test_truncate_noop_under_budget(self):
        lines = _util.unique_lines(30)
        self.assertEqual(self.ck.signal_preserving_truncate(lines), lines)

    # --- degradati A/B misurati (2026-07-18): output Bash che trasporta
    #     codice/diff che la SIGNAL log-oriented eliderebbe come rumore ---

    def test_bash_signal_keeps_grep_symbol_line(self):
        line = "305:def prose_project(payload):"
        self.assertFalse(bool(self.ck.SIGNAL.search(line)))   # vecchio: rumore
        self.assertTrue(self.ck._bash_signal(line))            # nuovo: segnale

    def test_bash_signal_keeps_diff_hunk_header(self):
        line = "@@ -495,7 +498,9 @@ func squash(json string) (string, int) {"
        self.assertFalse(bool(self.ck.SIGNAL.search(line)))
        self.assertTrue(self.ck._bash_signal(line))

    def test_bash_signal_does_not_broaden_ordinary_logs(self):
        for l in ("  processing item 42 ok", "2024: server started",
                  "  INFO ready", "[  OK  ] mounted /home"):
            self.assertFalse(self.ck._bash_signal(l), l)

    def test_compress_bash_recovers_grep_symbols_in_middle(self):
        noise = [f"  processing item {i} ok" for i in range(50)]
        grep = [f"{300 + i}:def sym_{i}(x):" for i in range(8)]
        out = self.ck.compress("\n".join(noise + grep + noise[:30]))
        self.assertTrue(self.ck.has_elision(out))     # comprime comunque
        for g in grep:                                 # ma tiene i simboli
            self.assertIn(g, out)

    def test_compress_bash_recovers_diff_hunk_in_middle(self):
        head = ["diff --git a/x.go b/x.go", "--- a/x.go", "+++ b/x.go"]
        mid = [f" context line {i}" for i in range(50)]
        hunk = "@@ -495,7 +498,9 @@ func squash(s string) (string, int) {"
        out = self.ck.compress("\n".join(head + mid + [hunk] + mid[:30]))
        self.assertTrue(self.ck.has_elision(out))
        self.assertIn(hunk, out)
        self.assertIn("squash", out)

    def test_compress_diff_keeps_changed_lines_elides_context(self):
        # Un diff vero: le righe +/- (payload) sopravvivono anche nel mezzo,
        # comprese le dichiarazioni +public function; le righe di CONTESTO
        # (prefisso spazio) si comprimono. Chiude il degradato A/B 2026-07-18.
        head = ["diff --git a/x.php b/x.php", "--- a/x.php", "+++ b/x.php",
                "@@ -10,40 +10,52 @@ class Foo {"]
        context = [f"     $ctx_{i} = {i};" for i in range(60)]
        changed = ([f"+    $added_{i} = {i};" for i in range(20)]
                   + [f"-    $removed_{i} = {i};" for i in range(20)]
                   + ["+    public function bar(): void"])
        out = self.ck.compress(
            "\n".join(head + context + changed + context[:30]))
        self.assertTrue(self.ck.has_elision(out))     # il contesto si comprime
        for c in changed:                              # ma il payload resta
            self.assertIn(c, out)
        self.assertIn("+    public function bar(): void", out)

    def test_compress_bullet_log_not_treated_as_diff(self):
        # Bullet '- voce' senza struttura di diff: NON e' un diff, il
        # riconoscimento +/- non scatta e i bullet nel mezzo si elidono
        # (oltre la soglia HEAD+TAIL la compressione dei log resta piena).
        bullets = [f"- pacchetto-{i} installato" for i in range(90)]
        self.assertFalse(self.ck._diff_aware(bullets))
        out = self.ck.compress("\n".join(bullets))
        self.assertTrue(self.ck.has_elision(out))
        self.assertNotIn("- pacchetto-60 installato", out)   # riga di mezzo elisa


class TestHookContract(unittest.TestCase):
    """Contratto reale: JSON su stdin -> JSON su stdout, exit 0 sempre."""

    def setUp(self):
        fd, self.log = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        os.unlink(self.log)                      # deve poterlo creare da zero
        # stato reads ISOLATO: senza, i test scrivono nel file reale
        # dell'utente e si inquinano a vicenda (visto col page fault elisione)
        fd, self.reads = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.reads)
        self.env = {"CK_LOG": self.log, "CK_READS_STATE": self.reads}

    def tearDown(self):
        for p in (self.log, self.reads):
            if os.path.exists(p):
                os.unlink(p)

    # --- LA regressione del 2026-07-15 -------------------------------------
    def test_dict_response_gets_dict_replacement_same_shape(self):
        """Se tool_response e' un dict, updatedToolOutput DEVE essere un dict
        con le stesse chiavi: l'harness ignora silenziosamente una stringa."""
        noisy = "\n".join(["riga di rumore identica"] * 300)
        payload = _util.bash_payload(noisy, stderr_text="warn residuo")
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)

        self.assertEqual(proc.returncode, 0)
        out = _util.hook_json(proc)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        upd = hso["updatedToolOutput"]

        self.assertIsInstance(upd, dict, "regressione: era tornata una stringa")
        self.assertEqual(set(upd), set(payload["tool_response"]))
        self.assertEqual(upd["stderr"], "", "stderr va azzerato: e' gia' fuso nel compresso")
        self.assertIs(upd["interrupted"], False)
        self.assertIn("[x 300]", upd["stdout"])
        self.assertRegex(upd["stdout"], FOOTER)
        self.assertLess(len(upd["stdout"]), len(noisy) // 10)

    # --- LA regressione del 2026-07-15 sera: Read annidato ------------------
    def test_read_nested_response_gets_nested_replacement(self):
        """Per Read tool_response e' {"type","file":{"content",...}}: il testo
        compresso va rimesso in file.content preservando forma e metadati —
        prima del fix la Read non veniva MAI compressa (no-op silenzioso)."""
        noisy = "\n".join(f"{i}\triga di contenuto ripetitivo del file" for i in range(300))
        payload = _util.read_payload(noisy, file_path="/tmp/grande.txt")
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)

        self.assertEqual(proc.returncode, 0)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIsInstance(upd, dict)
        self.assertEqual(set(upd), {"type", "file"})       # stesse chiavi top-level
        self.assertEqual(upd["type"], "text")
        nf = upd["file"]
        self.assertEqual(nf["filePath"], "/tmp/grande.txt")
        self.assertEqual(nf["startLine"], 1)
        self.assertEqual(nf["totalLines"], 300)            # il file su disco non cambia
        self.assertEqual(nf["numLines"], 300)              # metadati della finestra
        self.assertRegex(nf["content"], FOOTER)            # letta, non del compresso
        self.assertLess(len(nf["content"]), len(noisy))

    def test_stderr_only_output_is_not_wiped(self):
        """Testo estratto solo da stderr: il compresso deve finire in stdout,
        non sparire (prima del fix: stdout vuoto + stderr azzerato = perso)."""
        noisy_err = "\n".join(["warning: riga ripetuta di errore"] * 300)
        payload = _util.bash_payload("", stderr_text=noisy_err)
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)

        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIsInstance(upd, dict)
        self.assertRegex(upd["stdout"], FOOTER)
        self.assertIn("[x 300]", upd["stdout"])
        self.assertEqual(upd["stderr"], "")

    def test_string_response_gets_string_replacement(self):
        payload = _util.bash_payload("")
        payload["tool_response"] = "\n".join(["riga di rumore identica"] * 300)
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIsInstance(upd, str)
        self.assertRegex(upd, FOOTER)

    # --- casi no-op: SEMPRE "{}" e exit 0, mai rompere la sessione ---------
    def test_small_output_is_noop(self):
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload("ciao"), env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(_util.hook_json(proc), {})

    def test_unmatched_tool_is_noop(self):
        noisy = "\n".join(["x"] * 500)
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload(noisy, tool="Edit"), env=self.env)
        self.assertEqual(_util.hook_json(proc), {})

    def test_unknown_shape_with_lost_text_is_logged(self):
        """Forma di tool_response sconosciuta CON testo sostanzioso ->
        una riga JSON nel shape log (solo chiavi, mai contenuto)."""
        fd, shapes = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        os.unlink(shapes)
        payload = _util.bash_payload("", tool="Grep")
        payload["tool_response"] = {"mode": "content",
                                    "matches": {"blob": "x" * 500}}
        try:
            proc = _util.run_hook(_util.COMPRESS, payload,
                                  env={**self.env, "CK_SHAPE_LOG": shapes})
            self.assertEqual(_util.hook_json(proc), {})    # comunque no-op sicuro
            with open(shapes, encoding="utf-8") as f:
                rec = json.loads(f.read().strip())
            self.assertEqual(rec["tool"], "Grep")
            self.assertEqual(rec["keys"], ["matches", "mode"])
            self.assertNotIn("x" * 50, json.dumps(rec))    # niente contenuto
        finally:
            if os.path.exists(shapes):
                os.unlink(shapes)

    def test_empty_or_metadata_only_shape_not_logged(self):
        """Bash silenzioso o dict di soli metadati corti: nessun rumore nel
        shape log."""
        fd, shapes = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        os.unlink(shapes)
        try:
            env = {**self.env, "CK_SHAPE_LOG": shapes}
            _util.run_hook(_util.COMPRESS, _util.bash_payload(""), env=env)
            payload = _util.bash_payload("", tool="Read")
            payload["tool_response"] = {"type": "text",
                                        "file": {"filePath": "/tmp/vuoto.txt",
                                                 "content": ""}}
            _util.run_hook(_util.COMPRESS, payload, env=env)
            self.assertFalse(os.path.exists(shapes))
        finally:
            if os.path.exists(shapes):
                os.unlink(shapes)

    def test_judge_agent_read_never_compressed(self):
        """La Read di un agent giudice (agent_type in CK_AGENT_SKIP) resta
        intatta: l'elisione nasconderebbe le righe sotto giudizio."""
        noisy = "\n".join(f"{i}\triga del file" for i in range(300))
        payload = _util.read_payload(noisy)
        payload["agent_type"] = "kernel-verifier"
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        self.assertEqual(_util.hook_json(proc), {})
        payload["agent_type"] = "general-purpose"      # altri agent: si comprime
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        self.assertIn("updatedToolOutput", proc.stdout)

    def test_context_state_written_from_transcript_usage(self):
        """Il hook fotografa l'occupazione della finestra dall'ultimo blocco
        "usage" del transcript -> ~/.context-kernel-context.json (per il
        --budget auto dello slicer)."""
        fd, transcript = tempfile.mkstemp(suffix=".jsonl")
        fd2, state = tempfile.mkstemp(suffix=".json")
        os.close(fd); os.close(fd2); os.unlink(state)
        try:
            with open(transcript, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "assistant", "message": {
                    "model": "claude-test-1", "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 120_000,
                        "cache_creation_input_tokens": 2_000,
                        "output_tokens": 5}}}) + "\n")
            payload = _util.bash_payload("ciao")
            payload["transcript_path"] = transcript
            _util.run_hook(_util.COMPRESS, payload,
                           env={**self.env, "CK_CONTEXT_STATE": state})
            with open(state, encoding="utf-8") as f:
                st = json.load(f)
            rec = next(iter(st.values()))
            self.assertEqual(rec["context_tokens"], 122_010)
            self.assertEqual(rec["model"], "claude-test-1")
        finally:
            for p in (transcript, state):
                if os.path.exists(p):
                    os.unlink(p)

    def test_already_compressed_output_not_recompressed(self):
        """Guardia doppia-esecuzione: se plugin e install.sh sono attivi
        insieme gli hook si sommano — un output che porta gia' il footer
        in coda NON va ricompresso."""
        already = ("\n".join(["riga di rumore identica"] * 300)
                   + "\n\n[context-kernel: 2000 -> 500 tokens, -75%]")
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload(already), env=self.env)
        self.assertEqual(_util.hook_json(proc), {})

    def test_ck_tools_env_excludes_tool(self):
        """CK_TOOLS restringe i tool trattati: un tool escluso e' no-op
        (serve p.es. agli agent verificatori per letture non alterate)."""
        noisy = "\n".join(f"{i}\triga del file" for i in range(300))
        payload = _util.read_payload(noisy)
        proc = _util.run_hook(_util.COMPRESS, payload,
                              env={**self.env, "CK_TOOLS": "Bash,Grep"})
        self.assertEqual(_util.hook_json(proc), {})
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        self.assertIn("updatedToolOutput", proc.stdout)   # default: trattato

    def test_ck_raw_marker_passes_output_untouched(self):
        """Escape per-comando: `# ck:raw` nel comando Bash -> output INTATTO,
        anche se comprimibilissimo."""
        noisy = "\n".join(_util.unique_lines(400))
        payload = _util.bash_payload(noisy)
        payload["tool_input"]["command"] = "pytest -x tests/  # ck:raw"
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        self.assertEqual(_util.hook_json(proc), {})

    def test_ck_raw_disabled_via_env(self):
        """CK_RAW_MARK vuoto disattiva l'escape: lo stesso comando torna
        a essere compresso."""
        noisy = "\n".join(_util.unique_lines(400))
        payload = _util.bash_payload(noisy)
        payload["tool_input"]["command"] = "pytest -x tests/  # ck:raw"
        proc = _util.run_hook(_util.COMPRESS, payload,
                              env={**self.env, "CK_RAW_MARK": ""})
        self.assertIn("updatedToolOutput", proc.stdout)

    def test_extraction_sed_range_passes_raw(self):
        """`sed -n 'A,Bp' file` e' una finestra esplicita come una Read con
        offset/limit: l'output E' il payload chiesto riga per riga -> intatto."""
        noisy = "\n".join(_util.unique_lines(400))
        for cmd in ("sed -n '100,499p' hooks/grande.py",
                    "awk 'NR>=100 && NR<=499' hooks/grande.py"):
            payload = _util.bash_payload(noisy)
            payload["tool_input"]["command"] = cmd
            proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
            self.assertEqual(_util.hook_json(proc), {}, cmd)

    def test_extraction_recall_passes_raw(self):
        """recall.py e' l'INVERSA della proiezione (il page fault del
        parcheggio): ricomprimerne l'output significherebbe proiettare
        l'inversa — il recupero non convergerebbe mai."""
        noisy = "\n".join(_util.unique_lines(400))
        payload = _util.bash_payload(noisy)
        payload["tool_input"]["command"] = (
            'python3 "/x/hooks/recall.py" abc123 --lines 1-400')
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        self.assertEqual(_util.hook_json(proc), {})

    def test_extraction_cap_still_compresses_huge_output(self):
        """Oltre CK_EXTRACTION_MAX l'output non e' piu' 'una finestra':
        si comprime come sempre (il cap tiene onesto il raw-pass)."""
        noisy = "\n".join(_util.unique_lines(400))
        payload = _util.bash_payload(noisy)
        payload["tool_input"]["command"] = "sed -n '1,20000p' hooks/grande.py"
        proc = _util.run_hook(_util.COMPRESS, payload,
                              env={**self.env, "CK_EXTRACTION_MAX": "50"})
        self.assertIn("updatedToolOutput", proc.stdout)

    def test_garbage_stdin_is_noop_exit_zero(self):
        proc = _util.run_hook(_util.COMPRESS, "questo non e' JSON {", env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(_util.hook_json(proc), {})

    def test_incompressible_output_is_noop(self):
        """Sopra soglia ma senza guadagno (righe uniche, sotto budget di
        troncatura): meglio non toccare nulla."""
        text = "\n".join(_util.unique_lines(60))
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload(text), env=self.env)
        self.assertEqual(_util.hook_json(proc), {})

    # --- segnale e log ------------------------------------------------------
    def test_error_lines_survive_compression(self):
        lines = _util.unique_lines(200)
        lines[100] = "ERROR [db] connection refused to 10.0.0.5:5432"
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload("\n".join(lines)), env=self.env)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn(lines[100], upd["stdout"])

    def test_savings_log_written_csv(self):
        noisy = "\n".join(["riga di rumore identica"] * 300)
        _util.run_hook(_util.COMPRESS, _util.bash_payload(noisy), env=self.env)
        with open(self.log, encoding="utf-8") as f:
            rows = f.read().strip().split("\n")
        self.assertEqual(len(rows), 1)
        ts, tool, before, after, saved, session, agent = rows[0].split(",")
        self.assertEqual(tool, "Bash")
        self.assertEqual(int(saved), int(before) - int(after))
        self.assertGreater(int(saved), 0)
        self.assertEqual(session, "-")         # nessun transcript_path nel payload
        self.assertEqual(agent, "-")           # nessun agent_id: main loop

    def test_savings_log_records_session(self):
        noisy = "\n".join(["riga di rumore identica"] * 300)
        payload = _util.bash_payload(noisy)
        payload["transcript_path"] = "/percorso/abcd1234-ef56.jsonl"
        _util.run_hook(_util.COMPRESS, payload, env=self.env)
        with open(self.log, encoding="utf-8") as f:
            row = f.read().strip()
        self.assertTrue(row.endswith(",abcd1234,-"),
                        "colonna sessione = basename corto, agent '-' nel "
                        "main loop")

    def test_savings_log_attributes_subagent(self):
        """Compressione avvenuta in un subagent (payload con agent_id): la
        sessione resta la MADRE (grouping intatto), la 7a colonna porta
        l'id corto dell'agente."""
        noisy = "\n".join(["riga di rumore identica"] * 300)
        payload = _util.bash_payload(noisy)
        payload["transcript_path"] = "/percorso/abcd1234-ef56.jsonl"
        payload["agent_id"] = "a56b8bc0f68529f1d"
        _util.run_hook(_util.COMPRESS, payload, env=self.env)
        with open(self.log, encoding="utf-8") as f:
            row = f.read().strip()
        self.assertTrue(row.endswith(",abcd1234,a56b8bc0"),
                        f"attesa sessione madre + agent corto, riga: {row}")

    def test_adaptive_start_compresses_from_first_token(self):
        """Sessione giovane (nessun tap di contesto): la scala parte a 0.75,
        non piu' a 1.0 — un output tra 0.75*MIN_TOKENS e MIN_TOKENS viene
        compresso fin dall'inizio della sessione. Con CK_ADAPTIVE_START=1.0
        (comportamento vecchio) lo stesso output passa raw."""
        noisy = "\n".join(f"riga rumore {i:03d} ....." for i in range(110))
        out = _util.hook_json(_util.run_hook(
            _util.COMPRESS, _util.bash_payload(noisy),
            env={**self.env, "CK_ADAPTIVE_START": "0.75"}))
        self.assertIn("hookSpecificOutput", out,
                      "atteso compresso con la partenza 0.75")
        out_full = _util.hook_json(_util.run_hook(
            _util.COMPRESS, _util.bash_payload(noisy),
            env={**self.env, "CK_ADAPTIVE_START": "1.0"}))
        self.assertEqual(out_full, {},
                         "con scala piena deve restare sotto soglia (raw)")

    def test_log_off_env_respected(self):
        noisy = "\n".join(["riga di rumore identica"] * 300)
        _util.run_hook(_util.COMPRESS, _util.bash_payload(noisy),
                       env={**self.env, "CK_LOG_OFF": "1"})
        self.assertFalse(os.path.exists(self.log))

    def test_stdout_is_single_json_object(self):
        """L'harness parsa lo stdout come JSON: la diagnostica deve stare su
        stderr, mai mescolata allo stdout."""
        noisy = "\n".join(["riga di rumore identica"] * 300)
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload(noisy), env=self.env)
        json.loads(proc.stdout)                  # esplode se c'e' altro
        self.assertIn("context-kernel:", proc.stderr)


class TestCodeAwareReads(unittest.TestCase):
    """Sui sorgenti il segnale e' la STRUTTURA: la regex log-oriented si
    inverte sul codice (teneva `except Exception: pass`, buttava la logica)."""

    def setUp(self):
        fd, self.log = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        fd, self.reads = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.reads)
        self.env = {"CK_LOG": self.log, "CK_READS_STATE": self.reads}

    def tearDown(self):
        for p in (self.log, self.reads):
            if os.path.exists(p):
                os.unlink(p)

    def _run(self, content, file_path, tool_input_extra=None):
        payload = _util.read_payload(content, file_path=file_path)
        payload["transcript_path"] = "/tmp/sess-code1111.jsonl"
        if tool_input_extra:
            payload["tool_input"].update(tool_input_extra)
        return _util.run_hook(_util.COMPRESS, payload, env=self.env)

    def test_explicit_window_read_never_compressed(self):
        """Read con offset/limit: finestra chiesta apposta, passa intatta."""
        big = "\n".join(_util.unique_lines(400))
        proc = self._run(big, "/tmp/qualunque.txt",
                         {"offset": 100, "limit": 400})
        self.assertEqual(_util.hook_json(proc), {})

    def test_source_read_keeps_structure_drops_bodies(self):
        head = [f"# intestazione {i}" for i in range(50)]
        body: list[str] = []
        for i in range(40):
            body += [f"def funzione_{i}(x):",
                     f"    valore = x * {i}",
                     "    try:",
                     "        return valore",
                     "    except Exception:",
                     "        pass"]
        tail = [f"# coda {i}" for i in range(25)]
        content = "\n".join(head + body + tail)
        proc = self._run(content, "/tmp/modulo.py")
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        got = got["file"]["content"]
        self.assertIn("def funzione_30(x):", got)      # struttura oltre HEAD
        self.assertIn("lines of body", got)           # marker code-aware
        self.assertNotIn("except Exception", got)      # non piu' "segnale"

    def test_log_read_still_keeps_error_lines(self):
        lines = _util.unique_lines(300)
        lines[150] = "ERROR: qualcosa di rotto a meta' file"
        proc = self._run("\n".join(lines), "/tmp/run.log")
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("ERROR: qualcosa di rotto", got["file"]["content"])

    def test_php_read_keeps_use_and_namespace(self):
        """Segnalazione PHP/Joomla (2026-07-17): use/namespace oltre HEAD
        venivano elisi — Claude perdeva le dipendenze del file."""
        doc = [f" * docblock inutile {i} testo variabile {i * 13}"
               for i in range(50)]
        body = [f"        $x{i} = $this->helper{i}($v); // passo {i}"
                for i in range(60)]
        content = "\n".join(
            ["<?php", "/**"] + doc + [" */",
             "namespace Acme\\Component\\Site\\Model;",
             "",
             "use Joomla\\CMS\\Factory;",
             "use Joomla\\CMS\\MVC\\Model\\BaseDatabaseModel;",
             "",
             "class ArticleModel extends BaseDatabaseModel",
             "{",
             "    public function getItem($pk = null)",
             "    {"] + body + ["    }", "}"])
        proc = self._run(content, "/tmp/ArticleModel.php")
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        got = got["file"]["content"]
        self.assertIn("lines of body", got)               # compressione avvenuta
        self.assertIn("use Joomla\\CMS\\Factory;", got)
        self.assertIn("use Joomla\\CMS\\MVC\\Model\\BaseDatabaseModel;", got)
        self.assertIn("namespace Acme\\Component\\Site\\Model;", got)
        self.assertIn("class ArticleModel", got)

    def test_elision_marker_declares_explicit_range(self):
        """Il marker cita l'intervallo eliso (page fault MIRATO: si puo'
        rileggere solo quella finestra con offset/limit)."""
        proc = self._run("\n".join(_util.unique_lines(300)), "/tmp/run.log")
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        content = got["file"]["content"]
        self.assertIn("elided lines 46-280:", content)      # HEAD 45, TAIL 20

    def test_elision_marker_declares_numeric_continuity(self):
        """Dal primo DEGRADATO A/B: log numerato eliso -> il marker dichiara
        la continuita' della numerazione, cosi' la completezza resta
        verificabile dalla proiezione."""
        lines = [f"[migrate] step {i:03d}/300 applicato {'x' * 40}"
                 for i in range(300)]
        proc = self._run("\n".join(lines), "/tmp/migrate.log")
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("unbroken numbering 45→279",
                      got["file"]["content"])

    def test_no_false_continuity_on_irregular_numbers(self):
        lines = [f"evento {i * i} registrato {'x' * 40}" for i in range(300)]
        proc = self._run("\n".join(lines), "/tmp/eventi.log")
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertNotIn("numerazione continua", got["file"]["content"])

    def test_log_read_keeps_php_deprecated_notice_strict(self):
        """Segnalazione PHP 8.1-8.4 (2026-07-17): Deprecated/Notice/Strict
        in mezzo a un output lungo sono segnale, non rumore."""
        lines = _util.unique_lines(300)
        lines[100] = "Deprecated: strlen(): Passing null to parameter #1"
        lines[150] = "PHP Notice:  Undefined index: id in /var/www/y.php"
        lines[200] = "Strict Standards: Only variables should be passed by reference"
        proc = self._run("\n".join(lines), "/tmp/phpunit.log")
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        content = got["file"]["content"]
        self.assertIn("Deprecated: strlen()", content)
        self.assertIn("PHP Notice", content)
        self.assertIn("Strict Standards", content)


class TestElisionPageFault(unittest.TestCase):
    """Una Read elisa lascia il contesto SENZA la copia piena: la rilettura
    e' un page fault e deve passare integrale (mai il marker 'copia valida')."""

    def setUp(self):
        fd, self.log = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        fd, self.reads = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.reads)
        self.env = {"CK_LOG": self.log, "CK_READS_STATE": self.reads}
        # grande abbastanza da superare MIN_TOKENS e produrre ELISIONE
        # (righe uniche senza segnale nel mezzo)
        self.content = "\n".join(_util.unique_lines(300))

    def tearDown(self):
        for p in (self.log, self.reads):
            if os.path.exists(p):
                os.unlink(p)

    def _read(self, text, session="/tmp/sess-elisa111.jsonl"):
        payload = _util.read_payload(text, file_path="/tmp/grande.py")
        payload["transcript_path"] = session
        return _util.run_hook(_util.COMPRESS, payload, env=self.env)

    def test_elided_read_marks_state_and_hints(self):
        proc = self._read(self.content)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        content = upd["file"]["content"]
        self.assertIn("elided", content)
        self.assertIn("ELIDED copy", content)              # footer azionabile
        self.assertRegex(content, FOOTER)                  # canary-compatibile
        with open(self.reads, encoding="utf-8") as f:
            st = json.load(f)
        rec = st[next(iter(st))]["/tmp/grande.py"]
        self.assertTrue(rec.get("elided"))

    def test_reread_after_elision_passes_integral(self):
        self._read(self.content)                           # elisa
        proc = self._read(self.content)                    # page fault
        self.assertEqual(_util.hook_json(proc), {})        # integrale intatto
        with open(self.reads, encoding="utf-8") as f:
            st = json.load(f)
        rec = st[next(iter(st))]["/tmp/grande.py"]
        self.assertFalse(rec.get("elided", False))         # flag consumato

    def test_reread_after_elision_ignores_file_changes(self):
        """Anche se il file e' cambiato: il contesto non ha MAI avuto la
        copia piena, un diff contro di essa sarebbe fuorviante."""
        self._read(self.content)
        changed = self.content + "\nriga nuova in coda"
        proc = self._read(changed)
        self.assertEqual(_util.hook_json(proc), {})

    def test_third_read_resumes_delta_marker(self):
        """Dopo il page fault il regime normale riprende: la terza lettura
        invariata riceve il marker delta (grande file: content non salvato
        -> marker o compressione, mai errore)."""
        self._read(self.content)                           # elisa
        self._read(self.content)                           # integrale
        proc = self._read(self.content)                    # rilettura normale
        out = _util.hook_json(proc)
        upd = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("UNCHANGED", upd["file"]["content"])


class TestDeltaReads(unittest.TestCase):
    """Idea "Delta Context" rifocalizzata: le RILETTURE sono il costo vero
    (il prompt caching gia' sconta il prefisso invariato)."""

    def setUp(self):
        fd, self.log = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        fd, self.reads = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.reads)
        self.env = {"CK_LOG": self.log, "CK_READS_STATE": self.reads}
        # contenuto medio: sotto MIN_TOKENS (no compressione classica),
        # sopra CK_DELTA_MIN (il delta si applica)
        self.content = "\n".join(f"{i}\triga contenuto file {i}" for i in range(80))

    def tearDown(self):
        for p in (self.log, self.reads):
            if os.path.exists(p):
                os.unlink(p)

    def _read(self, text, session="/tmp/sess-aaaa1111.jsonl"):
        payload = _util.read_payload(text, file_path="/tmp/delta.txt")
        payload["transcript_path"] = session
        return _util.run_hook(_util.COMPRESS, payload, env=self.env)

    def test_first_read_passes_and_records(self):
        proc = self._read(self.content)
        self.assertEqual(_util.hook_json(proc), {})        # integrale
        with open(self.reads, encoding="utf-8") as f:
            st = json.load(f)
        rec = st[next(iter(st))]["/tmp/delta.txt"]
        self.assertIn("hash", rec)
        # contenuto salvato COMPRESSO (zlib+base64), non raw
        import base64
        import zlib
        stored = zlib.decompress(base64.b64decode(rec["z"])).decode()
        self.assertIn("riga contenuto file 0", stored)
        self.assertNotIn("content", rec)

    def test_unchanged_reread_gets_marker(self):
        self._read(self.content)
        proc = self._read(self.content)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("UNCHANGED", upd["file"]["content"])
        self.assertRegex(upd["file"]["content"], FOOTER)   # canary-compatibile

    def test_third_read_after_marker_passes_full(self):
        """Escape page-fault: rilettura subito dopo un marker -> integrale."""
        self._read(self.content)
        self._read(self.content)                           # marker
        proc = self._read(self.content)
        self.assertEqual(_util.hook_json(proc), {})        # integrale di nuovo

    def test_legacy_raw_content_record_still_diffs(self):
        """Retrocompatibilita': un record pre-0.9.2 con 'content' raw
        (non compresso) deve ancora produrre il diff."""
        self._read(self.content)
        with open(self.reads, encoding="utf-8") as f:
            st = json.load(f)
        sess = next(iter(st))
        rec = st[sess]["/tmp/delta.txt"]
        rec["content"] = self.content            # forma vecchia
        rec.pop("z", None)
        with open(self.reads, "w", encoding="utf-8") as f:
            json.dump(st, f)
        changed = self.content.replace("riga contenuto file 40",
                                       "riga MODIFICATA quaranta")
        proc = self._read(changed)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("CHANGED", upd["file"]["content"])

    def test_parallel_hooks_dont_lose_records(self):
        """Sessioni/hook concorrenti sullo stesso stato: col lock advisory
        nessun record va perso e il JSON resta integro."""
        import subprocess
        import sys as _sys
        handles = []
        for i in range(6):
            payload = _util.read_payload("contenuto parallelo\n" * 60,
                                         file_path=f"/tmp/par-{i}.txt")
            payload["transcript_path"] = "/tmp/sess-parallel.jsonl"
            p = subprocess.Popen([_sys.executable, _util.COMPRESS],
                                 stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True,
                                 env={**os.environ, **self.env})
            handles.append((p, json.dumps(payload)))
        for p, data in handles:                  # avvia tutti, poi attendi
            p.stdin.write(data)
            p.stdin.close()
        for p, _ in handles:
            p.wait(timeout=30)
        with open(self.reads, encoding="utf-8") as f:
            st = json.load(f)                    # mai corrotto
        files = st[next(iter(st))]
        self.assertEqual(len(files), 6)          # nessun update perso

    def test_changed_file_gets_unified_diff(self):
        self._read(self.content)
        changed = self.content.replace("riga contenuto file 40",
                                       "riga MODIFICATA quaranta")
        proc = self._read(changed)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        body = upd["file"]["content"]
        self.assertIn("CHANGED", body)
        self.assertIn("+40\triga MODIFICATA quaranta", body)
        self.assertIn("@@", body)
        self.assertLess(len(body), len(changed) // 2)      # il diff conviene

    def test_partial_reads_not_touched(self):
        payload = _util.read_payload(self.content, file_path="/tmp/delta.txt")
        payload["transcript_path"] = "/tmp/sess-aaaa1111.jsonl"
        payload["tool_input"]["offset"] = 10
        _util.run_hook(_util.COMPRESS, payload, env=self.env)
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        self.assertEqual(_util.hook_json(proc), {})        # mai marker

    def test_delta_disabled_via_env(self):
        self._read(self.content)
        payload = _util.read_payload(self.content, file_path="/tmp/delta.txt")
        payload["transcript_path"] = "/tmp/sess-aaaa1111.jsonl"
        proc = _util.run_hook(_util.COMPRESS, payload,
                              env={**self.env, "CK_DELTA": "0"})
        self.assertEqual(_util.hook_json(proc), {})

    def test_sessions_are_isolated(self):
        self._read(self.content, session="/tmp/sess-aaaa1111.jsonl")
        proc = self._read(self.content, session="/tmp/sess-bbbb2222.jsonl")
        self.assertEqual(_util.hook_json(proc), {})        # prima lettura per B


class TestABSampling(unittest.TestCase):
    """Campionamento A/B (T4 campionato): 1 elisione ogni CK_AB_RATE finisce
    nello stato (coppia originale+compresso, zlib) per il giudizio offline di
    ab_verify.py. Contatore deterministico, mai fatale."""

    def setUp(self):
        fd, self.log = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        fd, self.ab = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.ab)
        self.env = {"CK_LOG": self.log, "CK_AB_STATE": self.ab}
        # righe uniche senza segnale: sopra MIN_TOKENS e sopra HEAD+TAIL+5
        # -> compressione con ELISIONE garantita
        self.noisy = "\n".join(_util.unique_lines(300))

    def _state(self) -> dict:
        with open(self.ab, encoding="utf-8") as f:
            return json.load(f)

    def test_elision_sampled_with_original_and_compressed(self):
        proc = _util.run_hook(_util.COMPRESS, _util.bash_payload(self.noisy),
                              env={**self.env, "CK_AB_RATE": "1"})
        emitted = _util.hook_json(proc)["hookSpecificOutput"][
            "updatedToolOutput"]["stdout"]
        st = self._state()
        self.assertEqual(st["counter"], 1)
        self.assertEqual(len(st["pending"]), 1)
        sample = st["pending"][0]
        self.assertEqual(sample["tool"], "Bash")
        ck = _load_module()
        self.assertEqual(ck._unpack_content({"z": sample["orig_z"]}),
                         self.noisy)               # originale integro
        self.assertEqual(ck._unpack_content({"z": sample["comp_z"]}),
                         emitted)                  # compresso come emesso

    def test_rate_counts_elisions_and_samples_every_nth(self):
        env = {**self.env, "CK_AB_RATE": "3"}
        for _ in range(3):
            _util.run_hook(_util.COMPRESS, _util.bash_payload(self.noisy),
                           env=env)
        st = self._state()
        self.assertEqual(st["counter"], 3)
        self.assertEqual(len(st["pending"]), 1)    # solo la terza

    def test_rate_zero_disables_sampling(self):
        _util.run_hook(_util.COMPRESS, _util.bash_payload(self.noisy),
                       env={**self.env, "CK_AB_RATE": "0"})
        self.assertFalse(os.path.exists(self.ab))

    def test_compression_without_elision_not_sampled(self):
        # sopra MIN_TOKENS ma sotto la soglia di troncatura: niente elisione,
        # niente campione (e nemmeno il contatore si muove)
        modest = "\n".join(_util.unique_lines(69))
        _util.run_hook(_util.COMPRESS, _util.bash_payload(modest),
                       env={**self.env, "CK_AB_RATE": "1"})
        self.assertFalse(os.path.exists(self.ab))


if __name__ == "__main__":
    unittest.main()
