"""Test di posttool_symptom.py: T2 ambientale sui test falliti (PostToolUse)."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import _util

POSTTOOL = os.path.join(_util.HOOKS, "posttool_symptom.py")

PYTEST_FAIL = """\
============================= test session starts ==============================
collected 3 items

test_app.py ..F                                                          [100%]

=================================== FAILURES ===================================
_________________________________ test_main ____________________________________

    def test_main():
>       assert app.main()

test_app.py:4:
Traceback (most recent call last):
  File "app.py", line 4, in main
    return add(1, "x")
TypeError: unsupported operand type(s)
=========================== short test summary info ============================
FAILED test_app.py::test_main - TypeError: unsupported operand type(s)
========================= 1 failed, 2 passed in 0.12s ==========================
"""


def _payload(stdout: str, command: str = "python3 -m pytest",
             cwd: str = "/tmp", session: str = "sess-test") -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "session_id": session,
        "cwd": cwd,
        "tool_input": {"command": command, "description": "test"},
        "tool_response": {"stdout": stdout, "stderr": "",
                          "interrupted": False, "isImage": False},
    }


class TestPosttoolSymptom(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="ck-posttool-")
        os.makedirs(os.path.join(self.repo, "pkg"))
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("from pkg.calc import add\n\ndef main():\n"
                    "    return add(1, 'x')\n")
        with open(os.path.join(self.repo, "pkg", "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        with open(os.path.join(self.repo, "pkg", "__init__.py"), "w") as f:
            f.write("")
        with open(os.path.join(self.repo, "test_app.py"), "w") as f:
            f.write("import app\n\ndef test_main():\n    assert app.main()\n")
        fd, self.state = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.state)                  # deve poterlo creare da zero
        # fixture minuscola: abbassa la soglia repo-grande per il test
        self.env = {"CK_SYMPTOM_MIN_FILES": "1",
                    "CK_POST_SYMPTOM_STATE": self.state}

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        if os.path.exists(self.state):
            os.unlink(self.state)

    def _run(self, payload, env=None):
        return _util.run_hook(POSTTOOL, payload, env={**self.env, **(env or {})})

    def test_pytest_failure_injects_slice_manifest(self):
        proc = self._run(_payload(PYTEST_FAIL, cwd=self.repo))
        out = _util.hook_json(proc)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        ctx = hso["additionalContext"]
        self.assertIn("Failure detected", ctx)
        self.assertIn("## seed", ctx)
        self.assertIn("app.py", ctx)

    def test_same_failure_not_reinjected(self):
        first = self._run(_payload(PYTEST_FAIL, cwd=self.repo))
        self.assertIn("additionalContext", first.stdout)
        second = self._run(_payload(PYTEST_FAIL, cwd=self.repo))
        self.assertEqual(_util.hook_json(second), {})

    def test_different_failure_injected_again(self):
        self._run(_payload(PYTEST_FAIL, cwd=self.repo))
        other = PYTEST_FAIL.replace("TypeError", "ValueError")
        proc = self._run(_payload(other, cwd=self.repo))
        self.assertIn("additionalContext", proc.stdout)

    def test_green_output_is_noop(self):
        proc = self._run(_payload("collected 3 items\n3 passed in 0.1s",
                                  cwd=self.repo))
        self.assertEqual(_util.hook_json(proc), {})

    def test_error_word_alone_is_not_a_failure(self):
        proc = self._run(_payload("warning: 3 errors ignored, done.",
                                  cwd=self.repo))
        self.assertEqual(_util.hook_json(proc), {})

    def test_readonly_command_is_noop(self):
        """Un grep che CITA un traceback (fixture, log) non e' un fallimento."""
        proc = self._run(_payload(PYTEST_FAIL, command="grep -rn Traceback tests/",
                                  cwd=self.repo))
        self.assertEqual(_util.hook_json(proc), {})

    def test_ck_raw_command_is_noop(self):
        proc = self._run(_payload(PYTEST_FAIL,
                                  command="python3 -m pytest  # ck:raw",
                                  cwd=self.repo))
        self.assertEqual(_util.hook_json(proc), {})

    def test_subagent_is_noop(self):
        payload = _payload(PYTEST_FAIL, cwd=self.repo)
        payload["agent_id"] = "abc123"
        proc = self._run(payload)
        self.assertEqual(_util.hook_json(proc), {})

    def test_non_bash_tool_is_noop(self):
        payload = _payload(PYTEST_FAIL, cwd=self.repo)
        payload["tool_name"] = "Read"
        proc = self._run(payload)
        self.assertEqual(_util.hook_json(proc), {})

    def test_small_repo_is_noop(self):
        proc = self._run(_payload(PYTEST_FAIL, cwd=self.repo),
                         env={"CK_SYMPTOM_MIN_FILES": "50"})
        self.assertEqual(_util.hook_json(proc), {})

    def test_disabled_via_env(self):
        proc = self._run(_payload(PYTEST_FAIL, cwd=self.repo),
                         env={"CK_POST_SYMPTOM": "0"})
        self.assertEqual(_util.hook_json(proc), {})

    def test_missing_cwd_is_noop(self):
        proc = self._run(_payload(PYTEST_FAIL, cwd="/percorso/inesistente-xyz"))
        self.assertEqual(_util.hook_json(proc), {})

    def test_cd_prefix_resolves_run_dir(self):
        """`cd repo && pytest` da un cwd QUALSIASI: il repo da affettare e'
        quello del cd, non la directory di sessione (fix 2026-07-17)."""
        proc = self._run(_payload(
            PYTEST_FAIL, command=f"cd {self.repo} && python3 -m pytest",
            cwd="/tmp"))
        ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("## seed", ctx)
        self.assertIn("app.py", ctx)

    def test_cd_prefix_relative_and_quoted(self):
        parent = os.path.dirname(self.repo)
        rel = os.path.basename(self.repo)
        proc = self._run(_payload(
            PYTEST_FAIL, command=f'cd "{rel}" && python3 -m pytest',
            cwd=parent))
        self.assertIn("additionalContext", proc.stdout)

    def test_cd_prefix_does_not_bypass_readonly_guard(self):
        """Il punto cieco noto: `cd x && grep Traceback` aggirava la guardia
        read-only (matchava l'inizio). Ora la guardia vale DOPO i cd."""
        proc = self._run(_payload(
            PYTEST_FAIL, command=f"cd {self.repo} && grep -rn Traceback tests/",
            cwd="/tmp"))
        self.assertEqual(_util.hook_json(proc), {})

    def test_garbage_stdin_is_noop_exit_zero(self):
        proc = _util.run_hook(POSTTOOL, "niente json", env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(_util.hook_json(proc), {})

    def test_stdout_is_single_json_object(self):
        proc = self._run(_payload(PYTEST_FAIL, cwd=self.repo))
        json.loads(proc.stdout)                        # non deve lanciare


if __name__ == "__main__":
    unittest.main()
