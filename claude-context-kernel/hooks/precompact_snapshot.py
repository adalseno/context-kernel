#!/usr/bin/env python3
"""
precompact_snapshot.py — PreCompact hook: fotografa il TASK STATE TS(Q)
prima che l'auto-compact lo dissolva.

L'auto-compact e' una proiezione NON indicizzata dal task — esattamente il
"projector without a task index" che il formalismo considera o banale o
sbagliato. Non possiamo impedirla, ma possiamo difendere TS(Q): qui si
salva cio' che la definisce (carta del task T3 attiva + testa del manifest
T2 della sessione); session_brief.py lo reinietta alla SessionStart con
source=="compact", cosi' la sessione post-compact riparte col task state
invece che con un riassunto generico.

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

ENABLED = os.environ.get("CK_COMPACT", "1") != "0"
STATE = os.path.expanduser(
    os.environ.get("CK_COMPACT_STATE", "~/.context-kernel-compact.json"))
CHARTER_HEAD = int(os.environ.get("CK_COMPACT_CHARTER_LINES", "30"))


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

        rec = charter.get_for_repo(cwd) or charter.latest()
        charter_head = ""
        if rec:
            charter_head = "\n".join(rec["text"].split("\n")[:CHARTER_HEAD])

        task = _sym._task_load().get(session) or {}
        slice_head = task.get("head") or ""

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
        st[session] = {
            "ts": time.time(),
            "trigger": payload.get("trigger") or "?",
            "repo": (rec or {}).get("repo") or task.get("repo") or cwd,
            "charter_head": charter_head,
            "slice_head": slice_head,
        }
        for k in sorted(st, key=lambda k: st[k].get("ts", 0))[:-8]:
            st.pop(k, None)                    # tieni le ultime 8 sessioni
        tmp = f"{STATE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
        print("{}")
        print(f"context-kernel[compact]: TS(Q) snapshotted for {session}",
              file=sys.stderr)
        return 0
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
