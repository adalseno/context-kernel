"""Test della sopravvivenza al RIAVVIO: session_end_snapshot.py fotografa
TS(Q) per-repo alla SessionEnd, session_brief.py lo reinietta alla
SessionStart successiva (source startup/resume) sullo stesso repo."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

import _util

CARTA = """# carta del task
Q: il retry non rispetta il limite

## vincoli
1. [comportamento] retry esattamente 3 volte  (test_db.py:8)
"""

MANIFEST_HEAD = """# kernel repo slice — manifest
operator: T2@test
repo: /repo
## seeds (from the symptom)
- db.py  <- citato nel sintomo"""


class TestResume(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-resume-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.env = {
            "CK_CHARTER_STATE": os.path.join(self.tmp, "charter.json"),
            "CK_TASK_STATE": os.path.join(self.tmp, "task.json"),
            "CK_RESUME_STATE": os.path.join(self.tmp, "resume.json"),
        }
        _util.run_script(_util.CHARTER, CARTA, env=self.env,
                         args=["save", "--repo", self.repo])
        with open(self.env["CK_TASK_STATE"], "w") as f:
            json.dump({"sessA": {"repo": self.repo, "seeds": ["db.py"],
                                 "files": ["db.py"], "head": MANIFEST_HEAD,
                                 "ts": time.time()}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _end(self, session="sessA", env=None):
        payload = {"hook_event_name": "SessionEnd", "session_id": session,
                   "cwd": self.repo, "reason": "exit"}
        return _util.run_hook(_util.SESSION_END, payload,
                              env={**self.env, **(env or {})})

    def _start(self, source="startup", cwd=None):
        payload = {"hook_event_name": "SessionStart", "source": source,
                   "session_id": "sessNUOVA", "cwd": cwd or self.repo}
        return _util.hook_json(_util.run_hook(_util.BRIEF, payload,
                                              env=self.env))

    def test_end_saves_by_repo(self):
        proc = self._end()
        self.assertEqual(_util.hook_json(proc), {})    # mai invadente
        st = json.load(open(self.env["CK_RESUME_STATE"]))
        rec = st[os.path.normpath(self.repo)]          # chiave: REPO
        self.assertIn("retry esattamente 3 volte", rec["charter_head"])
        self.assertIn("## seed", rec["slice_head"])
        self.assertEqual(rec["session"], "sessA")

    def test_new_session_same_repo_restores(self):
        self._end()
        ctx = self._start()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TS(Q) from the previous session", ctx)
        self.assertIn("retry esattamente 3 volte", ctx)
        self.assertIn("## seed", ctx)

    def test_other_repo_not_restored(self):
        self._end()
        altro = os.path.join(self.tmp, "altro-repo")
        os.makedirs(altro)
        ctx = self._start(cwd=altro)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("TS(Q)", ctx)

    def test_stale_snapshot_not_restored(self):
        self._end()
        st = json.load(open(self.env["CK_RESUME_STATE"]))
        st[os.path.normpath(self.repo)]["ts"] = time.time() - 999999
        with open(self.env["CK_RESUME_STATE"], "w") as f:
            json.dump(st, f)
        ctx = self._start()["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("TS(Q)", ctx)

    def test_cleared_charter_drops_charter_keeps_slice(self):
        self._end()
        _util.run_script(_util.CHARTER, "", env=self.env,
                         args=["clear", "--repo", self.repo])
        ctx = self._start()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TS(Q) from the previous session", ctx)
        self.assertNotIn("retry esattamente 3 volte", ctx)  # carta pulita
        self.assertIn("## seed", ctx)                       # working set resta

    def test_compact_source_uses_compact_path_not_resume(self):
        self._end()
        payload = {"hook_event_name": "SessionStart", "source": "compact",
                   "session_id": "sessNUOVA", "cwd": self.repo}
        out = _util.hook_json(_util.run_hook(_util.BRIEF, payload,
                                             env=self.env))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("sessione precedente", ctx)   # via compact, non resume

    def test_nothing_to_defend_is_noop_on_state(self):
        env = {"CK_CHARTER_STATE": os.path.join(self.tmp, "no-ch.json"),
               "CK_TASK_STATE": os.path.join(self.tmp, "no-task.json"),
               "CK_RESUME_STATE": os.path.join(self.tmp, "no-res.json")}
        payload = {"hook_event_name": "SessionEnd", "session_id": "sessB",
                   "cwd": self.tmp, "reason": "exit"}
        proc = _util.run_hook(_util.SESSION_END, payload, env=env)
        self.assertEqual(_util.hook_json(proc), {})
        self.assertFalse(os.path.exists(env["CK_RESUME_STATE"]))

    def test_disabled_via_env(self):
        proc = self._end(env={"CK_RESUME": "0"})
        self.assertEqual(_util.hook_json(proc), {})
        self.assertFalse(os.path.exists(self.env["CK_RESUME_STATE"]))

    def test_garbage_stdin_is_noop_exit_zero(self):
        proc = _util.run_script(_util.SESSION_END, "niente json",
                                env=self.env)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
