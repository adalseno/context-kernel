"""Test del rilevatore di cambio-task (multi-Q): un secondo sintomo con
seed diversi nella stessa sessione dichiara il cambio Q1 -> Q2 col diff
dei manifest; stesso sintomo o sessione diversa non lo dichiarano."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import _util

TRACEBACK_A = """il test fallisce con questo:
Traceback (most recent call last):
  File "app.py", line 4, in main
    return add(1, "x")
TypeError: unsupported operand type(s)"""

TRACEBACK_B = """ora invece esplode il rendering:
Traceback (most recent call last):
  File "web.py", line 3, in page
    return render({})
ValueError: template mancante"""


def _payload(prompt: str, cwd: str, session: str = "s-switch") -> dict:
    return {"hook_event_name": "UserPromptSubmit", "prompt": prompt,
            "cwd": cwd, "session_id": session}


class TestTaskSwitch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-switch-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "pkg"))
        files = {
            "app.py": "from pkg.calc import add\n\ndef main():\n"
                      "    return add(1, 'x')\n",
            "web.py": "from pkg.render import render\n\ndef page():\n"
                      "    return render({})\n",
            os.path.join("pkg", "calc.py"): "def add(a, b):\n    return a + b\n",
            os.path.join("pkg", "render.py"): "def render(ctx):\n"
                                              "    return ctx['tpl']\n",
            os.path.join("pkg", "__init__.py"): "",
            "test_app.py": "import app\n\ndef test_main():\n"
                           "    assert app.main()\n",
        }
        for rel, content in files.items():
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(content)
        self.env = {
            "CK_SYMPTOM_MIN_FILES": "1",
            "CK_TASK_STATE": os.path.join(self.tmp, "task.json"),
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx(self, proc) -> str:
        out = _util.hook_json(proc)
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_first_symptom_has_no_switch_note(self):
        ctx = self._ctx(_util.run_hook(
            _util.SYMPTOM, _payload(TRACEBACK_A, self.repo), env=self.env))
        self.assertIn("working set", ctx)
        self.assertNotIn("TASK SWITCH", ctx)

    def test_second_different_symptom_declares_switch_with_diff(self):
        _util.run_hook(_util.SYMPTOM, _payload(TRACEBACK_A, self.repo),
                       env=self.env)
        ctx = self._ctx(_util.run_hook(
            _util.SYMPTOM, _payload(TRACEBACK_B, self.repo), env=self.env))
        self.assertIn("TASK SWITCH", ctx)
        self.assertIn("web.py", ctx)                   # diff: file nuovi di Q2

    def test_same_symptom_twice_is_not_a_switch(self):
        _util.run_hook(_util.SYMPTOM, _payload(TRACEBACK_A, self.repo),
                       env=self.env)
        ctx = self._ctx(_util.run_hook(
            _util.SYMPTOM, _payload(TRACEBACK_A, self.repo), env=self.env))
        self.assertNotIn("TASK SWITCH", ctx)

    def test_other_session_is_not_a_switch(self):
        _util.run_hook(_util.SYMPTOM,
                       _payload(TRACEBACK_A, self.repo, session="s-uno"),
                       env=self.env)
        ctx = self._ctx(_util.run_hook(
            _util.SYMPTOM, _payload(TRACEBACK_B, self.repo, session="s-due"),
            env=self.env))
        self.assertNotIn("TASK SWITCH", ctx)

    def test_switch_detector_disabled_via_env(self):
        _util.run_hook(_util.SYMPTOM, _payload(TRACEBACK_A, self.repo),
                       env=self.env)
        ctx = self._ctx(_util.run_hook(
            _util.SYMPTOM, _payload(TRACEBACK_B, self.repo),
            env={**self.env, "CK_TASK_SWITCH": "0"}))
        self.assertIn("working set", ctx)              # la slice arriva comunque
        self.assertNotIn("TASK SWITCH", ctx)


if __name__ == "__main__":
    unittest.main()
