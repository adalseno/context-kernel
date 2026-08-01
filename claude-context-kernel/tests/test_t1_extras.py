"""Test delle 5 ottimizzazioni T1 del 2026-07-17 (release 1.8.0):
delta sui comandi Bash, proiezione grep-aware, outline-first sui .py giganti,
rate adattivo al contesto residuo, proiezione prosa per WebFetch."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import _util


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-t1x-")
        self.env = {
            "CK_LOG_OFF": "1",
            "CK_CANARY": "0",
            "CK_AB_RATE": "0",
            "CK_CMDS_STATE": os.path.join(self.tmp, "cmds.json"),
            "CK_READS_STATE": os.path.join(self.tmp, "reads.json"),
            "CK_CONTEXT_STATE": os.path.join(self.tmp, "ctx.json"),
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bash(self, stdout: str, command: str, session: str = "sess-cmd1",
              env: dict | None = None):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "transcript_path": f"/tmp/{session}.jsonl",
            "tool_input": {"command": command},
            "tool_response": {"stdout": stdout, "stderr": ""},
        }
        return _util.run_hook(_util.COMPRESS, payload,
                              env={**self.env, **(env or {})})


class TestCmdDelta(_Base):
    """Delta sui comandi Bash ripetuti con output identico."""

    OUT = "\n".join(f"ramo attivo numero {i} {'x' * 30}" for i in range(60))

    def test_second_identical_run_gets_marker(self):
        first = self._bash(self.OUT, "git status")
        self.assertEqual(_util.hook_json(first), {})   # sotto MIN: intatto
        second = self._bash(self.OUT, "git status")
        upd = _util.hook_json(second)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("output IDENTICAL", upd["stdout"])
        self.assertIn("[context-kernel:", upd["stdout"])  # footer

    def test_rerun_after_marker_passes_integral(self):
        self._bash(self.OUT, "git status")
        self._bash(self.OUT, "git status")                 # marker
        third = self._bash(self.OUT, "git status")
        self.assertEqual(_util.hook_json(third), {})       # page fault

    def test_different_output_no_marker(self):
        self._bash(self.OUT, "git status")
        other = self.OUT.replace("attivo", "cambiato")
        proc = self._bash(other, "git status")
        self.assertEqual(_util.hook_json(proc), {})

    def test_different_command_same_output_no_marker(self):
        self._bash(self.OUT, "git status")
        proc = self._bash(self.OUT, "git log --oneline")
        self.assertEqual(_util.hook_json(proc), {})

    def test_rerun_after_elision_is_integral(self):
        noisy = "\n".join(f"riga di puro rumore {i} {'z' * 40}"
                          for i in range(150))
        first = self._bash(noisy, "make build")
        upd = _util.hook_json(first)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("elided", upd["stdout"])              # eliso davvero
        second = self._bash(noisy, "make build")
        self.assertEqual(_util.hook_json(second), {})      # integrale


class TestGrepProjection(_Base):

    def _grep(self, text: str):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Grep",
            "transcript_path": "/tmp/sess-grep1.jsonl",
            "tool_input": {"pattern": "qualcosa"},
            "tool_response": text,
        }
        return _util.run_hook(_util.COMPRESS, payload, env=self.env)

    def test_groups_by_file_and_caps_matches(self):
        lines = []
        for f in range(10):
            for i in range(20):
                lines.append(f"src/modulo_{f}.py:{i + 1}: occorrenza unica "
                             f"{f}-{i} {'y' * 20}")
        proc = self._grep("\n".join(lines))
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("matches beyond the 5th per file", upd)
        self.assertIn("[+15 more matches in src/modulo_0.py]", upd)
        self.assertIn("src/modulo_9.py:1:", upd)           # nessun file perso
        self.assertNotIn("occorrenza unica 0-7", upd)      # oltre il cap
        self.assertIn("[context-kernel:", upd)             # footer

    def test_files_mode_untouched(self):
        text = "\n".join(f"src/percorso/al/file_{i}.py" for i in range(50))
        proc = self._grep(text)
        self.assertEqual(_util.hook_json(proc), {})


class TestOutlineFirst(_Base):

    def _giant_py(self, broken: bool = False) -> str:
        parts = ["import os", "import sys", ""]
        for i in range(400):
            parts.append(f"def funzione_{i}(argomento_{i}):")
            for r in range(10):
                parts.append(f"    valore_{r} = argomento_{i} * {r}  # passo")
            parts.append(f"    return valore_9")
            parts.append("")
        if broken:
            parts.append("def rotto(:")
        return "\n".join(parts)

    def test_giant_python_read_becomes_outline(self):
        payload = _util.read_payload(self._giant_py(), "/tmp/enorme.py")
        payload["transcript_path"] = "/tmp/sess-outl1.jsonl"
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        content = got["file"]["content"]
        self.assertIn("projected to an OUTLINE", content)
        self.assertIn("def funzione_7(argomento_7):  # lines ", content)
        self.assertIn("import os", content)
        self.assertNotIn("valore_3", content)               # corpi elisi
        self.assertIn("ELIDED copy", proc.stdout)           # page fault attivo

    def test_syntax_error_falls_back_to_code_aware(self):
        payload = _util.read_payload(self._giant_py(broken=True),
                                     "/tmp/enorme_rotto.py")
        payload["transcript_path"] = "/tmp/sess-outl2.jsonl"
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        got = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        content = got["file"]["content"]
        self.assertNotIn("projected to an OUTLINE", content)
        self.assertIn("lines of body", content)            # fallback classico


class TestAdaptiveRate(_Base):

    LOG = "\n".join(f"{i:04d} riga di log qualunque {'w' * 40}"
                    for i in range(300))

    def _read_log(self, session: str):
        payload = _util.read_payload(self.LOG, "/tmp/run.log")
        payload["transcript_path"] = f"/tmp/{session}.jsonl"
        return _util.run_hook(_util.COMPRESS, payload, env=self.env)

    def test_full_headroom_keeps_default_window(self):
        proc = self._read_log("sess-adp1")
        content = _util.hook_json(proc)["hookSpecificOutput"][
            "updatedToolOutput"]["file"]["content"]
        self.assertIn("lines 46-280:", content)             # HEAD 45 / TAIL 20

    def test_high_usage_shrinks_head_tail(self):
        # finestra DICHIARATA via env (fonte 1 di window.py): 190k/200k=95%.
        # Senza dichiararla, la stima prudente assumerebbe una finestra piu'
        # grande e 190k non sarebbe "alto" — comportamento voluto da 1.18.0.
        self.env["CK_CONTEXT_WINDOW"] = "200000"
        with open(self.env["CK_CONTEXT_STATE"], "w") as f:
            json.dump({"sess-adp": {"context_tokens": 190_000}}, f)
        proc = self._read_log("sess-adp2")
        content = _util.hook_json(proc)["hookSpecificOutput"][
            "updatedToolOutput"]["file"]["content"]
        self.assertIn("lines 23-290:", content)             # scala 0.5


class TestProseProjection(_Base):

    def test_webfetch_link_runs_collapse(self):
        prose = [f"Paragrafo di testo vero numero {i} {'p' * 50}"
                 for i in range(30)]
        links = [f"- [voce di menu {i}](https://esempio.com/pagina-{i})"
                 for i in range(40)]
        text = "\n".join(prose[:15] + links + prose[15:])
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "WebFetch",
            "transcript_path": "/tmp/sess-web1.jsonl",
            "tool_input": {"url": "https://esempio.com"},
            "tool_response": {"result": text},
        }
        proc = _util.run_hook(_util.COMPRESS, payload, env=self.env)
        upd = _util.hook_json(proc)["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("lines of links/navigation", upd["result"])
        self.assertIn("voce di menu 0", upd["result"])       # le prime 2 restano
        self.assertNotIn("voce di menu 25", upd["result"])
        self.assertIn("Paragrafo di testo vero numero 29", upd["result"])


if __name__ == "__main__":
    unittest.main()
