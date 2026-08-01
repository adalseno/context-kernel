"""Test di session_brief.py: brief di consapevolezza a SessionStart."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import _util

BRIEF = os.path.join(_util.HOOKS, "session_brief.py")
PAYLOAD = {"hook_event_name": "SessionStart", "source": "startup"}


class TestSessionBrief(unittest.TestCase):

    def test_brief_injected_with_mechanisms(self):
        proc = _util.run_hook(BRIEF, PAYLOAD, env={"CK_LOG": "/inesistente"})
        out = _util.hook_json(proc)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"],
                         "SessionStart")
        self.assertIn("page fault", ctx.lower())
        self.assertIn("kernel-repo-slice", ctx)

    def test_savings_totals_from_ledger(self):
        fd, log = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(log, "w") as f:
                f.write("2026-07-17T07:00:00,Read,1000,400,600,abc\n"
                        "2026-07-17T07:01:00,Bash,2000,500,1500,abc\n")
            proc = _util.run_hook(BRIEF, PAYLOAD, env={"CK_LOG": log})
            ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("2 compressions", ctx)
            self.assertIn("2,100", ctx)
        finally:
            os.unlink(log)

    def test_ab_pending_reminder_in_brief(self):
        fd, ab = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(ab, "w") as f:
                json.dump({"counter": 40, "pending": [{"ts": 1}, {"ts": 2}],
                           "ok": 1, "degraded": 0}, f)
            proc = _util.run_hook(BRIEF, PAYLOAD,
                                  env={"CK_LOG": "/inesistente",
                                       "CK_AB_STATE": ab})
            ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("2 samples awaiting judgement", ctx)
            self.assertIn("ab_verify.py", ctx)
        finally:
            os.unlink(ab)

    def test_no_ab_line_without_pending(self):
        proc = _util.run_hook(BRIEF, PAYLOAD,
                              env={"CK_LOG": "/inesistente",
                                   "CK_AB_STATE": "/inesistente-ab"})
        ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("ab_verify", ctx)

    def test_canary_open_failures_contextualized_in_brief(self):
        """Failure canary aperti: il brief li contestualizza con l'evidenza
        (verified consecutive dopo l'ultimo) invece del solo ⚠ in statusline."""
        fd, canary = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(canary, "w") as f:
                json.dump({"pending": [], "verified": 9, "failed": 2,
                           "heal_streak": 3, "degraded_sessions": ["abc12345"],
                           "last_ok": None,
                           "last_failure": "2026-07-20T10:00:00"}, f)
            proc = _util.run_hook(BRIEF, PAYLOAD,
                                  env={"CK_LOG": "/inesistente",
                                       "CK_CANARY_STATE": canary})
            ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("2 open failures", ctx)
            self.assertIn("3 compressions verified OK since", ctx)
            self.assertIn("1 sessions auto-degraded", ctx)
        finally:
            os.unlink(canary)

    def test_no_canary_line_without_open_failures(self):
        fd, canary = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(canary, "w") as f:
                json.dump({"pending": [], "verified": 9, "failed": 0,
                           "last_ok": "2026-07-20T10:00:00",
                           "last_failure": None}, f)
            proc = _util.run_hook(BRIEF, PAYLOAD,
                                  env={"CK_LOG": "/inesistente",
                                       "CK_CANARY_STATE": canary})
            ctx = _util.hook_json(proc)["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("failure aperti", ctx)
        finally:
            os.unlink(canary)

    def test_disabled_via_env(self):
        proc = _util.run_hook(BRIEF, PAYLOAD, env={"CK_BRIEF": "0"})
        self.assertEqual(_util.hook_json(proc), {})

    def test_garbage_stdin_is_noop_exit_zero(self):
        proc = _util.run_hook(BRIEF, "niente json")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(_util.hook_json(proc), {})

    def test_stdout_is_single_json_object(self):
        proc = _util.run_hook(BRIEF, PAYLOAD)
        json.loads(proc.stdout)


if __name__ == "__main__":
    unittest.main()
