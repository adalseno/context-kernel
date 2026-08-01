"""Test della compaction kernel-aware: precompact_snapshot.py fotografa
TS(Q) (carta T3 + working set T2), session_brief.py lo reinietta alla
SessionStart con source=="compact"."""
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


class TestPrecompact(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ck-compact-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.env = {
            "CK_CHARTER_STATE": os.path.join(self.tmp, "charter.json"),
            "CK_TASK_STATE": os.path.join(self.tmp, "task.json"),
            "CK_COMPACT_STATE": os.path.join(self.tmp, "compact.json"),
        }
        _util.run_script(_util.CHARTER, CARTA, env=self.env,
                         args=["save", "--repo", self.repo])
        with open(self.env["CK_TASK_STATE"], "w") as f:
            json.dump({"sessA": {"repo": self.repo, "seeds": ["db.py"],
                                 "files": ["db.py"], "head": MANIFEST_HEAD,
                                 "ts": time.time()}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _snapshot(self, session="sessA", env=None):
        payload = {"hook_event_name": "PreCompact", "session_id": session,
                   "cwd": self.repo, "trigger": "auto"}
        return _util.run_hook(_util.PRECOMPACT, payload,
                              env={**self.env, **(env or {})})

    def test_snapshot_saves_charter_and_slice(self):
        proc = self._snapshot()
        self.assertEqual(_util.hook_json(proc), {})    # mai invadente
        st = json.load(open(self.env["CK_COMPACT_STATE"]))
        rec = st["sessA"]
        self.assertIn("retry esattamente 3 volte", rec["charter_head"])
        self.assertIn("## seed", rec["slice_head"])
        self.assertEqual(rec["trigger"], "auto")

    def test_brief_restores_ts_q_after_compact(self):
        self._snapshot()
        payload = {"hook_event_name": "SessionStart", "source": "compact",
                   "session_id": "sessA"}
        out = _util.hook_json(_util.run_hook(_util.BRIEF, payload,
                                             env=self.env))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TS(Q) survived the compaction", ctx)
        self.assertIn("retry esattamente 3 volte", ctx)
        self.assertIn("## seed", ctx)

    def test_brief_startup_has_no_restore_block(self):
        self._snapshot()
        payload = {"hook_event_name": "SessionStart", "source": "startup",
                   "session_id": "sessA"}
        out = _util.hook_json(_util.run_hook(_util.BRIEF, payload,
                                             env=self.env))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("TS(Q)", ctx)

    def test_stale_snapshot_not_restored(self):
        self._snapshot()
        st = json.load(open(self.env["CK_COMPACT_STATE"]))
        st["sessA"]["ts"] = time.time() - 99999
        with open(self.env["CK_COMPACT_STATE"], "w") as f:
            json.dump(st, f)
        payload = {"hook_event_name": "SessionStart", "source": "compact",
                   "session_id": "sessA"}
        out = _util.hook_json(_util.run_hook(_util.BRIEF, payload,
                                             env=self.env))
        self.assertNotIn("TS(Q)",
                         out["hookSpecificOutput"]["additionalContext"])

    def test_nothing_to_defend_is_noop_on_state(self):
        env = {"CK_CHARTER_STATE": os.path.join(self.tmp, "no-ch.json"),
               "CK_TASK_STATE": os.path.join(self.tmp, "no-task.json"),
               "CK_COMPACT_STATE": os.path.join(self.tmp, "no-comp.json")}
        payload = {"hook_event_name": "PreCompact", "session_id": "sessB",
                   "cwd": self.tmp, "trigger": "manual"}
        proc = _util.run_hook(_util.PRECOMPACT, payload, env=env)
        self.assertEqual(_util.hook_json(proc), {})
        self.assertFalse(os.path.exists(env["CK_COMPACT_STATE"]))

    def test_disabled_via_env(self):
        proc = self._snapshot(env={"CK_COMPACT": "0"})
        self.assertEqual(_util.hook_json(proc), {})

    def test_garbage_stdin_is_noop_exit_zero(self):
        proc = _util.run_hook(_util.PRECOMPACT, "niente json", env=self.env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(_util.hook_json(proc), {})


if __name__ == "__main__":
    unittest.main()
