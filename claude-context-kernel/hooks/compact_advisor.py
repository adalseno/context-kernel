#!/usr/bin/env python3
"""
compact_advisor.py — PostToolUse (Bash): UNA riga quando la finestra e' piena.

Un /compact MANUALE al 70% costa meno dell'auto-compact vicino al pieno
(riassunto piu' corto, momento scelto, e lo snapshot TS(Q) del PreCompact
e' comunque pronto). Il tap dell'occupazione c'e' gia' (compress.py):
qui solo la soglia e un avviso UNA-TANTUM per sessione. Mai fatale.

Finestra: da window.resolve_window — la fonte UNICA (env -> pattern noti
-> stima prudente che satura a ~0.87: a finestra ignota l'avviso scatta
solo su occupazioni grandi in assoluto). CK_COMPACT_ADVISE=0 disattiva.
"""
from __future__ import annotations

import json
import os
import sys
import time

try:
    import _utf8  # noqa: F401 — import con effetto: stream UTF-8 (Windows)
except ImportError:                        # embed per-path: stream dell'host
    pass

try:
    from window import resolve_window      # fonte UNICA della finestra
except ImportError:                        # installazione parziale: PARITA'
    def resolve_window(model, used):       # type: ignore[misc]
        try:
            win = int(os.environ.get("CK_CONTEXT_WINDOW", "0") or 0)
        except ValueError:
            win = 0
        if win > 0:
            return win, "env"
        if "[1m]" in (model or "").lower():
            return 1_000_000, "pattern [1m]"
        return max(200_000, max(0, used) * 115 // 100 + 50_000), "stima"

THRESHOLD = float(os.environ.get("CK_COMPACT_ADVISE", "0.70") or 0)
CONTEXT_STATE = os.path.expanduser(
    os.environ.get("CK_CONTEXT_STATE", "~/.context-kernel-context.json"))
ADVISE_STATE = os.path.expanduser(
    os.environ.get("CK_ADVISE_STATE", "~/.context-kernel-advised.json"))
def session_id(transcript_path: str | None) -> str:
    if not transcript_path:
        return "-"
    base = os.path.basename(transcript_path)
    return (base[:-6] if base.endswith(".jsonl") else base)[:8] or "-"


def main() -> int:
    if THRESHOLD <= 0:
        print("{}")
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0
    try:
        if payload.get("agent_id"):            # i subagent non compattano
            print("{}")
            return 0
        sid = session_id(payload.get("transcript_path"))
        with open(CONTEXT_STATE, encoding="utf-8") as f:
            rec = (json.load(f) or {}).get(sid) or {}
        used = int(rec.get("context_tokens") or 0)
        if used <= 0:
            print("{}")
            return 0
        win, _src = resolve_window(rec.get("model"), used)
        thr = THRESHOLD                        # soglia base (70% fisso storico)
        if os.environ.get("CK_COMPACT_ADAPT", "1") != "0":
            # Scheduler: la soglia si MODULA sul costo MISURATO di buttare
            # contesto (page fault recenti). Drop che rientrano -> tieni, avvisa
            # tardi; drop che non tornano -> avvisa prima. Banda limitata; se
            # lifetime.py manca o il log e' vuoto -> resta THRESHOLD (parita').
            try:
                from lifetime import adaptive_threshold, recall_pressure
                thr = adaptive_threshold(THRESHOLD, recall_pressure())
            except Exception:                  # noqa: BLE001 — mai fatale
                thr = THRESHOLD
        if used / win < thr:
            print("{}")
            return 0
        try:                                   # una-tantum per sessione
            with open(ADVISE_STATE, encoding="utf-8") as f:
                st = json.load(f)
            if not isinstance(st, dict):
                st = {}
        except Exception:                      # noqa: BLE001
            st = {}
        if sid in st:
            print("{}")
            return 0
        st[sid] = time.time()
        for k in sorted(st, key=lambda k: st[k])[:-16]:
            st.pop(k, None)
        tmp = f"{ADVISE_STATE}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, ADVISE_STATE)
        pct = int(used / win * 100)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"[context-kernel] window at {pct}% (~{used // 1000}k of "
                f"~{win // 1000}k). A MANUAL /compact now costs less than an "
                "auto-compact near the limit — and the TS(Q) snapshot is "
                "already protected by PreCompact. One-off warning per "
                "session."),
        }}))
        return 0
    except Exception:                          # noqa: BLE001
        print("{}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
