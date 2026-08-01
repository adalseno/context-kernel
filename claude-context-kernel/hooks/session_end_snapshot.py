#!/usr/bin/env python3
"""
session_end_snapshot.py — SessionEnd hook: TS(Q) sopravvive anche al RIAVVIO.

Simmetria con precompact_snapshot.py: la compaction e' una discontinuita' del
contesto DENTRO la sessione, la chiusura ne e' una TRA sessioni — e finora il
task state moriva li' (il rituale del file di stato a mano). Qui si fotografa
cio' che definisce TS(Q) (carta T3 attiva + testa del working set T2) con
chiave il REPO, non la sessione: la prossima sessione avra' un'altra session
id, ma lo stesso repo. session_brief.py reinietta alla SessionStart con
source startup/resume se lo snapshot e' fresco.

Mai fatale: su qualsiasi imprevisto stampa "{}" ed esce 0.
"""
from __future__ import annotations

import json
import os
import sys
import time

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host, non toccarli
    pass
import charter
import symptom_slice as _sym

ENABLED = os.environ.get("CK_RESUME", "1") != "0"
STATE = os.path.expanduser(
    os.environ.get("CK_RESUME_STATE", "~/.context-kernel-resume.json"))
CHARTER_HEAD = int(os.environ.get("CK_RESUME_CHARTER_LINES", "30"))


def main() -> int:
    if not ENABLED:
        print("{}")
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0
    try:
        session = _sym.hook_session(payload)
        cwd = payload.get("cwd") or os.getcwd()

        # carta SOLO del repo corrente: il resume e' per-repo, una carta di
        # un altro progetto non deve rientrare qui (niente charter.latest())
        rec = charter.get_for_repo(cwd)
        charter_head = ""
        if rec:
            charter_head = "\n".join(rec["text"].split("\n")[:CHARTER_HEAD])

        task = _sym._task_load().get(session) or {}
        slice_head = task.get("head") or ""
        repo = (rec or {}).get("repo") or task.get("repo") or cwd

        if not charter_head and not slice_head:
            print("{}")                        # niente TS(Q) da difendere
            return 0

        try:
            with open(STATE, encoding="utf-8") as f:
                st = json.load(f)
            if not isinstance(st, dict):
                st = {}
        except Exception:                      # noqa: BLE001
            st = {}
        st[os.path.normpath(repo)] = {
            "ts": time.time(),
            "session": session,
            "charter_head": charter_head,
            "slice_head": slice_head,
        }
        for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-8]:
            st.pop(k, None)                    # tieni gli ultimi 8 repo
        tmp = f"{STATE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
        print("{}")
        print(f"context-kernel[resume]: TS(Q) snapshotted for {repo}",
              file=sys.stderr)
        return 0
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
